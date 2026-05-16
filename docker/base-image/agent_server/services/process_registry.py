"""
Process Registry for tracking running subprocess handles.

Enables termination of executions by execution_id.
Used by both Claude Code and Gemini runtimes.

Also provides log streaming infrastructure for live execution monitoring.
"""

import signal
import subprocess
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional, List, AsyncIterator
from threading import Lock

from ..utils.subprocess_pgroup import (
    signal_process_tree as _signal_process_tree,
)
from ..utils.orphan_sweep import kill_cgroup_orphans

logger = logging.getLogger(__name__)


class ProcessRegistry:
    """
    Registry for tracking running subprocess handles.
    Enables termination of executions by execution_id.

    Thread-safe via mutex lock for all operations.

    Also provides log streaming infrastructure:
    - Each execution can have multiple log subscribers (asyncio.Queue)
    - Log entries are published to all subscribers as they arrive
    - Subscribers receive entries until execution completes
    """

    def __init__(self):
        self._processes: Dict[str, dict] = {}
        self._lock = Lock()
        # Log streaming: execution_id -> list of subscriber queues
        self._log_subscribers: Dict[str, List[asyncio.Queue]] = {}
        # Buffered logs: execution_id -> list of log entries (for late joiners)
        self._log_buffers: Dict[str, List[dict]] = {}
        # Maximum buffer size per execution (prevents memory bloat)
        self._max_buffer_size = 1000

    def register(self, execution_id: str, process: subprocess.Popen, metadata: dict = None):
        """
        Register a running process.

        Args:
            execution_id: Unique identifier for this execution
            process: The subprocess.Popen handle
            metadata: Optional metadata (type, message preview, etc.)
        """
        with self._lock:
            self._processes[execution_id] = {
                "process": process,
                "started_at": datetime.utcnow(),
                "metadata": metadata or {}
            }
            # Initialize log streaming structures
            self._log_subscribers[execution_id] = []
            self._log_buffers[execution_id] = []
            logger.info(f"[ProcessRegistry] Registered execution {execution_id}")

    def unregister(self, execution_id: str):
        """Unregister a completed process and signal stream end to subscribers."""
        with self._lock:
            if execution_id in self._processes:
                del self._processes[execution_id]
                logger.info(f"[ProcessRegistry] Unregistered execution {execution_id}")

            # Signal end of stream to all subscribers
            if execution_id in self._log_subscribers:
                for queue in self._log_subscribers[execution_id]:
                    try:
                        queue.put_nowait({"type": "stream_end"})
                    except asyncio.QueueFull:
                        pass
                del self._log_subscribers[execution_id]

            # Clean up buffer (keep for a bit for late requests, but this is fine)
            if execution_id in self._log_buffers:
                del self._log_buffers[execution_id]

    def terminate(self, execution_id: str, graceful_timeout: int = 5) -> dict:
        """
        Terminate a running process.

        Uses graceful termination (SIGINT) first, then force kills (SIGKILL)
        if the process doesn't respond within the timeout.

        Args:
            execution_id: The execution to terminate
            graceful_timeout: Seconds to wait after SIGINT before SIGKILL

        Returns:
            dict with termination status:
            - {"success": True, "returncode": int} on success
            - {"success": False, "reason": "not_found"} if not registered
            - {"success": False, "reason": "already_finished", "returncode": int}
            - {"success": False, "reason": "error", "error": str}
        """
        with self._lock:
            entry = self._processes.get(execution_id)
            if not entry:
                return {"success": False, "reason": "not_found"}

            process = entry["process"]
            if process.poll() is not None:
                # Already finished
                returncode = process.returncode
                del self._processes[execution_id]
                return {"success": False, "reason": "already_finished", "returncode": returncode}

        # Read pgid from the entry metadata (captured at register time)
        # so we can signal the full process group even if the parent has
        # already been reaped (Issue #407).
        pgid = (entry.get("metadata") or {}).get("pgid")

        # Terminate outside lock to avoid blocking other operations
        try:
            # Graceful termination first (SIGINT = Ctrl+C)
            # Claude Code handles SIGINT gracefully, finishing current tool.
            # Issue #407: signal the whole process group so hook
            # grandchildren don't linger holding our pipe FDs.
            logger.info(f"[ProcessRegistry] Sending SIGINT to execution {execution_id} (process group)")
            _signal_process_tree(process, signal.SIGINT, pgid=pgid)

            try:
                process.wait(timeout=graceful_timeout)
                logger.info(f"[ProcessRegistry] Execution {execution_id} terminated gracefully")
            except subprocess.TimeoutExpired:
                # Force kill if graceful didn't work
                logger.warning(f"[ProcessRegistry] Force killing execution {execution_id} (process group)")
                _signal_process_tree(process, signal.SIGKILL, pgid=pgid)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    logger.error(
                        f"[ProcessRegistry] Execution {execution_id} did not exit after SIGKILL"
                    )

            returncode = process.returncode

            # Issue #817 follow-up: cgroup-walk sweep for descendants
            # that escaped the pgid kill via setsid, FD detachment, or
            # env stripping. Best-effort — never fail termination on
            # this. Preserve every OTHER active execution by passing
            # their PIDs/pgids as extra_pids so we don't kill them
            # while terminating this one.
            try:
                preserve: list[int] = []
                with self._lock:
                    for other_id, other_entry in self._processes.items():
                        if other_id == execution_id:
                            continue
                        other_proc = other_entry["process"]
                        if other_proc.poll() is not None:
                            continue
                        preserve.append(other_proc.pid)
                        other_pgid = (other_entry.get("metadata") or {}).get("pgid")
                        if isinstance(other_pgid, int) and other_pgid > 0:
                            preserve.append(other_pgid)
                killed = kill_cgroup_orphans(extra_pids=preserve)
                if killed:
                    logger.info(
                        f"[ProcessRegistry] Cgroup sweep killed {killed} "
                        f"orphan(s) after terminating {execution_id} "
                        f"(preserved {len(preserve)} pid(s) for "
                        f"{len(self._processes) - 1} other execution(s))"
                    )
            except Exception:
                logger.exception(
                    f"[ProcessRegistry] cgroup sweep raised after "
                    f"terminating {execution_id} — continuing"
                )

            with self._lock:
                if execution_id in self._processes:
                    del self._processes[execution_id]

            return {"success": True, "returncode": returncode}

        except Exception as e:
            logger.error(f"[ProcessRegistry] Error terminating {execution_id}: {e}")
            return {"success": False, "reason": "error", "error": str(e)}

    def get_status(self, execution_id: str) -> Optional[dict]:
        """
        Get status of a registered process.

        Returns None if execution not found.
        """
        with self._lock:
            entry = self._processes.get(execution_id)
            if not entry:
                return None

            process = entry["process"]
            poll_result = process.poll()

            return {
                "execution_id": execution_id,
                "running": poll_result is None,
                "returncode": poll_result,
                "started_at": entry["started_at"].isoformat(),
                "metadata": entry["metadata"]
            }

    def list_running(self) -> list:
        """List all currently running executions.

        ``pid`` was added to the returned shape (#817 follow-up) so the
        periodic orphan sweeper can preserve every active claude
        process and its descendants. Existing callers that only read
        ``execution_id`` / ``started_at`` / ``metadata`` are
        unaffected.
        """
        with self._lock:
            result = []
            for exec_id, entry in self._processes.items():
                process = entry["process"]
                if process.poll() is None:
                    result.append({
                        "execution_id": exec_id,
                        "pid": process.pid,
                        "started_at": entry["started_at"].isoformat(),
                        "metadata": entry["metadata"]
                    })
            return result

    def cleanup_finished(self) -> int:
        """
        Remove entries for finished processes.

        Returns the count of cleaned up entries.
        """
        with self._lock:
            finished = [
                exec_id for exec_id, entry in self._processes.items()
                if entry["process"].poll() is not None
            ]
            for exec_id in finished:
                del self._processes[exec_id]
            if finished:
                logger.info(f"[ProcessRegistry] Cleaned up {len(finished)} finished processes")
            return len(finished)

    # ========================================================================
    # Log Streaming Methods
    # ========================================================================

    def publish_log_entry(self, execution_id: str, entry: dict):
        """
        Publish a log entry to all subscribers for an execution.

        Called from claude_code.py (chat path) and headless_executor.py
        (task path) as each stdout line is processed.
        Non-blocking: if a subscriber's queue is full, the entry is dropped for that subscriber.

        Args:
            execution_id: The execution ID
            entry: The raw JSON log entry from Claude Code
        """
        with self._lock:
            # Add to buffer for late joiners
            if execution_id in self._log_buffers:
                buffer = self._log_buffers[execution_id]
                buffer.append(entry)
                # Trim buffer if too large
                if len(buffer) > self._max_buffer_size:
                    self._log_buffers[execution_id] = buffer[-self._max_buffer_size:]

            # Publish to all subscribers
            if execution_id in self._log_subscribers:
                for queue in self._log_subscribers[execution_id]:
                    try:
                        queue.put_nowait(entry)
                    except asyncio.QueueFull:
                        # Drop entry for this slow subscriber
                        logger.warning(f"[ProcessRegistry] Log queue full for execution {execution_id}, dropping entry")

    def subscribe_logs(self, execution_id: str) -> asyncio.Queue:
        """
        Subscribe to log entries for an execution.

        Returns a queue that will receive log entries as they arrive.
        First sends all buffered entries, then streams new ones.
        Returns None if execution not found.

        Args:
            execution_id: The execution ID to subscribe to

        Returns:
            asyncio.Queue to receive log entries, or None if not found
        """
        queue = asyncio.Queue(maxsize=500)

        with self._lock:
            # Check if execution exists (or recently existed with buffer)
            if execution_id not in self._log_subscribers and execution_id not in self._log_buffers:
                return None

            # Send buffered entries first
            if execution_id in self._log_buffers:
                for entry in self._log_buffers[execution_id]:
                    try:
                        queue.put_nowait(entry)
                    except asyncio.QueueFull:
                        break

            # Register as subscriber if execution is still running
            if execution_id in self._log_subscribers:
                self._log_subscribers[execution_id].append(queue)
            else:
                # Execution finished, just send stream_end
                try:
                    queue.put_nowait({"type": "stream_end"})
                except asyncio.QueueFull:
                    pass

        return queue

    def unsubscribe_logs(self, execution_id: str, queue: asyncio.Queue):
        """
        Unsubscribe from log entries.

        Args:
            execution_id: The execution ID
            queue: The queue to unsubscribe
        """
        with self._lock:
            if execution_id in self._log_subscribers:
                try:
                    self._log_subscribers[execution_id].remove(queue)
                except ValueError:
                    pass

    def get_buffered_logs(self, execution_id: str) -> Optional[List[dict]]:
        """
        Get all buffered log entries for an execution.

        Used for non-streaming requests (e.g., page refresh on completed execution).

        Args:
            execution_id: The execution ID

        Returns:
            List of log entries, or None if execution not found
        """
        with self._lock:
            if execution_id in self._log_buffers:
                return list(self._log_buffers[execution_id])
            return None

    def is_execution_running(self, execution_id: str) -> bool:
        """Check if an execution is currently running."""
        with self._lock:
            return execution_id in self._processes

    def get_last_error(self, execution_id: str) -> Optional[dict]:
        """
        Extract the last error from an execution's log buffer.

        Scans the log buffer for error indicators:
        - Messages with is_error=True
        - Messages with error_type set
        - Result messages indicating failure

        Args:
            execution_id: The execution ID

        Returns:
            Dict with error_type and error_message, or None if no error found
        """
        with self._lock:
            if execution_id not in self._log_buffers:
                return None

            buffer = self._log_buffers[execution_id]
            if not buffer:
                return None

            # Scan buffer in reverse (most recent first) for error indicators
            last_error_type = None
            last_error_message = None

            for entry in reversed(buffer):
                if not isinstance(entry, dict):
                    continue

                # Check for is_error flag on result messages
                if entry.get("is_error"):
                    last_error_type = "execution_error"
                    last_error_message = entry.get("result", "")
                    break

                # Check for error field on assistant messages
                if entry.get("error"):
                    last_error_type = entry.get("error")
                    # Try to extract error text from message content
                    message = entry.get("message", {})
                    content = message.get("content", [])
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            last_error_message = block.get("text", "")
                            break
                    if last_error_type:
                        break

                # Check for tool_result with is_error
                if entry.get("type") == "assistant" or entry.get("type") == "user":
                    message = entry.get("message", {})
                    content = message.get("content", [])
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            if block.get("is_error"):
                                last_error_type = "tool_error"
                                result_content = block.get("content", [])
                                for item in result_content:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        last_error_message = item.get("text", "")
                                        break
                                break

            if last_error_type or last_error_message:
                return {
                    "error_type": last_error_type,
                    "error_message": last_error_message[:2000] if last_error_message else None
                }

            return None


# Global instance
_process_registry: Optional[ProcessRegistry] = None


def get_process_registry() -> ProcessRegistry:
    """Get the global process registry instance."""
    global _process_registry
    if _process_registry is None:
        _process_registry = ProcessRegistry()
    return _process_registry
