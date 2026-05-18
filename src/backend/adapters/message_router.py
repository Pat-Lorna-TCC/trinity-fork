"""
Channel-agnostic message router.

Receives NormalizedMessage from any adapter, resolves the agent,
builds context, dispatches to the agent via TaskExecutionService,
persists messages, and returns the response through the adapter.

Uses the same execution path as web public chat (EXEC-024) for:
- Execution records and audit trail
- Activity tracking (Dashboard timeline)
- Slot management (capacity limits)
- Credential sanitization
"""

import asyncio
import logging
import os
import re
import time
import unicodedata
from typing import List, Optional, Tuple
from collections import defaultdict

from database import db
from services.docker_service import get_agent_container
from services.platform_prompt_service import (
    format_user_memory_block,
    summarize_user_memory_background,
)
from services.settings_service import settings_service
from services.task_execution_service import get_task_execution_service
from services.docker_utils import container_exec_run
from services.telegram_media import process_voice
from services.upload_service import process_file_uploads, format_file_size, sanitize_filename
from adapters.base import ChannelAdapter, ChannelResponse, FileAttachment, NormalizedMessage, OutboundFile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configurable defaults (overridable via settings_service)
# ---------------------------------------------------------------------------

_DEFAULT_RATE_LIMIT_MAX = 30        # messages per window
_DEFAULT_RATE_LIMIT_WINDOW = 60     # seconds
_DEFAULT_CHANNEL_TIMEOUT = 120      # seconds
_DEFAULT_CHANNEL_ALLOWED_TOOLS = "WebSearch,WebFetch"
_FILE_UPLOAD_RATE_LIMIT_MAX = 5     # file uploads per window
_FILE_UPLOAD_RATE_LIMIT_WINDOW = 60 # seconds

# No-reply marker for observation mode (Issue #349)
_NO_REPLY_MARKER = "[NO_REPLY]"

# Outbound file extraction from agent responses
_OUTBOUND_MIN_BLOCK_CHARS = 100
_OUTBOUND_MAX_FILES = 5
_OUTBOUND_MAX_FILE_BYTES = 500 * 1024       # 500 KB per block
_OUTBOUND_MAX_TOTAL_BYTES = 2 * 1024 * 1024 # 2 MB total

_OUTBOUND_LANG_MAP = {
    "csv": "csv", "json": "json", "html": "html", "xml": "xml",
    "yaml": "yaml", "yml": "yaml", "sql": "sql",
    "python": "py", "py": "py",
    "javascript": "js", "js": "js", "typescript": "ts", "ts": "ts",
    "txt": "txt", "text": "txt",
}

# Regex: fenced code block at start of line with language hint
_CODE_BLOCK_RE = re.compile(r'^```(\w+)\s*\n(.*?)^```', re.DOTALL | re.MULTILINE)


def _get_rate_limit_max() -> int:
    return int(settings_service.get_setting("channel_rate_limit_max", str(_DEFAULT_RATE_LIMIT_MAX)))


def _get_rate_limit_window() -> int:
    return int(settings_service.get_setting("channel_rate_limit_window", str(_DEFAULT_RATE_LIMIT_WINDOW)))


def _get_channel_timeout() -> int:
    return int(settings_service.get_setting("channel_timeout_seconds", str(_DEFAULT_CHANNEL_TIMEOUT)))


def _get_channel_allowed_tools() -> List[str]:
    raw = settings_service.get_setting("channel_allowed_tools", _DEFAULT_CHANNEL_ALLOWED_TOOLS)
    return [t.strip() for t in raw.split(",") if t.strip()]


# ---------------------------------------------------------------------------
# Group chat sender formatting (Issue #349)
# ---------------------------------------------------------------------------

def _format_group_sender(message: NormalizedMessage) -> str:
    """
    Format sender identity for group chat context.

    Extracts username and first_name from message metadata to provide
    clear sender attribution in group conversations.

    Returns a formatted string like:
        [Group: AI Builders Chat]
        [From: @johndoe (John)]
    """
    parts = []

    # Add group title if available
    chat_title = message.metadata.get("chat_title")
    if chat_title:
        parts.append(f"[Group: {chat_title}]")

    # Extract sender info from metadata
    username = message.metadata.get("username")
    raw_message = message.metadata.get("raw_message", {})
    from_user = raw_message.get("from", {}) if isinstance(raw_message, dict) else {}
    first_name = from_user.get("first_name")

    # Format sender identity with available info
    if username and first_name:
        parts.append(f"[From: @{username} ({first_name})]")
    elif username:
        parts.append(f"[From: @{username}]")
    elif first_name:
        parts.append(f"[From: {first_name}]")
    else:
        # Fallback to sender_id if no identity info available
        parts.append(f"[From: User #{message.sender_id}]")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Simple in-memory rate limiter with periodic pruning
# ---------------------------------------------------------------------------

_rate_limit_buckets: dict = defaultdict(list)  # key → list of timestamps
_PRUNE_INTERVAL = 300  # prune stale buckets every 5 minutes
_last_prune_time: float = 0.0


def _prune_stale_buckets() -> None:
    """Remove empty or fully-expired buckets to prevent memory leaks."""
    global _last_prune_time
    now = time.time()
    if now - _last_prune_time < _PRUNE_INTERVAL:
        return
    _last_prune_time = now
    window = _get_rate_limit_window()
    stale_keys = [k for k, v in _rate_limit_buckets.items() if not v or v[-1] < now - window]
    for k in stale_keys:
        del _rate_limit_buckets[k]
    if stale_keys:
        logger.debug(f"[ROUTER] Pruned {len(stale_keys)} stale rate-limit buckets")


def _check_rate_limit(key: str, max_msgs: Optional[int] = None, window: Optional[int] = None) -> bool:
    """Returns True if allowed, False if rate limited."""
    _prune_stale_buckets()
    now = time.time()
    window = window or _get_rate_limit_window()
    max_msgs = max_msgs or _get_rate_limit_max()
    bucket = _rate_limit_buckets[key]
    # Remove expired entries
    _rate_limit_buckets[key] = [t for t in bucket if now - t < window]
    if len(_rate_limit_buckets[key]) >= max_msgs:
        return False
    _rate_limit_buckets[key].append(now)
    return True


def _format_file_size(size_bytes: int) -> str:
    return format_file_size(size_bytes)


def _sanitize_filename(name: str, file_id: str, used_names: set) -> str:
    return sanitize_filename(name, file_id, used_names)


# Filename sanitization (Issue #487)
_FILENAME_MAX_LENGTH = 200          # POSIX-safe; leaves room for collision suffix
_FILENAME_SAFE_CHARS_RE = re.compile(r'[^\w.\-()]')  # keep word chars, dot, hyphen, parens


def _sanitize_filename(name: str, file_id: str, used_names: set) -> str:
    """
    Sanitize a user-supplied filename for safe placement in the agent workspace.

    Steps:
    1. NFKC unicode normalize — collapses fullwidth/halfwidth and combining
       sequences so path-traversal sequences encoded with unicode variants
       can't slip past the basename check.
    2. ``os.path.basename`` — drop any leading directories.
    3. Strip non-safe chars to underscores (keep word chars, dot, hyphen, parens).
    4. Fall back to ``file_{file_id}`` if the result is empty or pure dots/whitespace.
    5. Truncate to 200 chars preserving the extension.
    6. De-dupe against ``used_names`` by appending ``-1``, ``-2``, … before the
       extension on collision.

    The caller is responsible for adding the returned name to ``used_names``.
    """
    normalized = unicodedata.normalize("NFKC", name or "")
    base = os.path.basename(normalized)
    safe = _FILENAME_SAFE_CHARS_RE.sub('_', base)

    # Reject names that are empty, dot-only/underscore-only, or hidden
    # dotfiles (`.env`, `.gitignore`, …). Per-session upload dir already
    # isolates uploads from the agent's own dotfiles, but rejecting hidden
    # names preserves the existing security posture (#222) and avoids
    # surprising agents whose Read tool sees a ``.env``-shaped file in
    # their workspace.
    stripped = safe.strip('._')
    if not stripped or safe.startswith('.'):
        safe = f"file_{file_id}"

    # Truncate to length cap, preserving extension where possible.
    if len(safe) > _FILENAME_MAX_LENGTH:
        stem, dot, ext = safe.rpartition('.')
        if dot and len(ext) <= 16:
            keep = _FILENAME_MAX_LENGTH - len(ext) - 1
            safe = f"{stem[:keep]}.{ext}"
        else:
            safe = safe[:_FILENAME_MAX_LENGTH]

    # Collision dedup: append -1, -2, … before the extension.
    if safe in used_names:
        stem, dot, ext = safe.rpartition('.')
        if not dot:
            stem, ext = safe, ""
        suffix_n = 1
        while True:
            suffix = f"-{suffix_n}"
            candidate_stem = stem
            # Trim stem so candidate stays within length cap.
            max_stem = _FILENAME_MAX_LENGTH - len(suffix) - (len(ext) + 1 if ext else 0)
            if len(candidate_stem) > max_stem:
                candidate_stem = candidate_stem[:max_stem]
            candidate = f"{candidate_stem}{suffix}.{ext}" if ext else f"{candidate_stem}{suffix}"
            if candidate not in used_names:
                safe = candidate
                break
            suffix_n += 1

    return safe


class ChannelMessageRouter:
    """Channel-agnostic message dispatcher."""

    async def handle_message(self, adapter: ChannelAdapter, message: NormalizedMessage) -> None:
        """Process an incoming message through the full pipeline."""
        try:
            await self._handle_message_inner(adapter, message)
        except Exception as e:
            logger.error(f"[ROUTER] Unhandled error in handle_message: {e}", exc_info=True)

    async def _handle_message_inner(self, adapter: ChannelAdapter, message: NormalizedMessage) -> None:
        channel = adapter.channel_type
        logger.info(f"[ROUTER:{channel}] START: sender={message.sender_id}, channel={message.channel_id}")

        # 1. Resolve agent
        agent_name = await adapter.get_agent_name(message)
        logger.debug(f"[ROUTER:{channel}] Step 1 - resolved agent: {agent_name}")
        if not agent_name:
            logger.warning(f"[ROUTER:{channel}] No agent found for channel {message.channel_id}")
            return

        # 2. Resolve bot token (needed for all responses)
        bot_token = adapter.get_bot_token(message)
        logger.debug(f"[ROUTER:{channel}] Step 2 - bot_token: {'yes' if bot_token else 'NO'}")
        if not bot_token:
            logger.error(f"[ROUTER:{channel}] No bot token for message in {message.channel_id}")
            return

        # 2b. Process voice messages (Telegram only) — transcribe before agent sees it
        if channel == "telegram":
            raw_msg = message.metadata.get("raw_message", {})
            if "voice" in raw_msg and bot_token:
                logger.debug(f"[ROUTER:{channel}] Step 2b - transcribing voice message")
                voice_text = await process_voice(bot_token, raw_msg["voice"])
                # Replace placeholder in message text with transcription
                placeholder = "[User sent a voice message — voice transcription is not yet available]"
                if placeholder in message.text:
                    message = message.model_copy(update={"text": message.text.replace(placeholder, voice_text)})
                    logger.info(f"[ROUTER:{channel}] Voice transcribed: {voice_text[:100]}...")

        # 3. Rate limiting per channel user
        rate_key = adapter.get_rate_key(message)
        is_group = message.metadata.get("is_group", False)
        if not _check_rate_limit(rate_key):
            logger.warning(f"[ROUTER:{channel}] Rate limited: {rate_key}")
            # In groups, silently drop to avoid spamming the group with error messages
            if is_group:
                return
            await adapter.send_response(
                message.channel_id,
                ChannelResponse(
                    text="You're sending messages too quickly. Please wait a moment.",
                    metadata={"bot_token": bot_token, "agent_name": agent_name}
                ),
                thread_id=message.thread_id,
            )
            return

        # 3b. File upload rate limiting (stricter than message rate limit)
        if message.files:
            file_rate_key = f"{channel}-files:{message.channel_id}:{message.sender_id}"
            if not _check_rate_limit(file_rate_key, max_msgs=_FILE_UPLOAD_RATE_LIMIT_MAX, window=_FILE_UPLOAD_RATE_LIMIT_WINDOW):
                logger.warning(f"[ROUTER] File upload rate limited: {file_rate_key}")
                await adapter.send_response(
                    message.channel_id,
                    ChannelResponse(
                        text="You're uploading files too quickly. Please wait a moment.",
                        metadata={"bot_token": bot_token, "agent_name": agent_name}
                    ),
                    thread_id=message.thread_id,
                )
                return

        # 4. Check agent availability
        container = get_agent_container(agent_name)
        container_status = container.status if container else "not_found"
        logger.debug(f"[ROUTER:{channel}] Step 4 - container: {container_status}")
        if not container or container.status != "running":
            await adapter.send_response(
                message.channel_id,
                ChannelResponse(
                    text="Sorry, I'm not available right now. Please try again later.",
                    metadata={"bot_token": bot_token, "agent_name": agent_name}
                ),
            )
            return

        # 5. Handle verification (base class default: always verified)
        logger.debug(f"[ROUTER:{channel}] Step 5 - running verification")
        verified = await adapter.handle_verification(message)
        logger.debug(f"[ROUTER:{channel}] Step 5 - verified: {verified}")
        if not verified:
            return

        # 5b. Unified cross-channel access gate (Issue #311).
        # Resolve a verified email via the adapter, then apply the agent's
        # access policy. Group chats have separate auth logic via group_auth_mode.
        policy = db.get_access_policy(agent_name)
        verified_email: Optional[str] = None

        if is_group:
            # Group chat auth: apply group_auth_mode policy
            group_auth_mode = policy.get("group_auth_mode", "none")

            if group_auth_mode == "any_verified":
                # Check if group is already verified by any member
                group_verified = await adapter.is_group_verified(message, agent_name)

                if not group_verified:
                    # Group not verified — check if sender can verify it
                    try:
                        verified_email = await adapter.resolve_verified_email(message)
                    except Exception as e:
                        logger.warning(f"[ROUTER:{channel}] resolve_verified_email error: {e}")
                        verified_email = None

                    # #446 defensive normalization at router boundary.
                    if verified_email:
                        verified_email = verified_email.strip().lower() or None

                    if verified_email:
                        # Sender is verified — unlock the group
                        await adapter.set_group_verified(message, agent_name, verified_email)
                        logger.info(
                            f"[ROUTER:{channel}] Group {message.channel_id} verified by {verified_email}"
                        )
                    else:
                        # No one verified — prompt for auth
                        logger.info(
                            f"[ROUTER:{channel}] Group access denied: agent={agent_name} "
                            f"requires verified member, group={message.channel_id} not verified"
                        )
                        await adapter.prompt_group_auth(message, agent_name, bot_token)
                        return
            # else: group_auth_mode == "none" — allow all group messages (legacy behavior)
        else:
            # DM auth: apply require_email / open_access policy
            try:
                verified_email = await adapter.resolve_verified_email(message)
            except Exception as e:
                logger.warning(f"[ROUTER:{channel}] resolve_verified_email error: {e}")
                verified_email = None

            # Defensive normalization (#446): downstream `email_has_agent_access`
            # already lowercases, but normalizing at the router boundary keeps
            # all gate checks and any logging consistent.
            if verified_email:
                verified_email = verified_email.strip().lower() or None

            require_email = policy.get("require_email", False)
            open_access = policy.get("open_access", False)

            if require_email and not verified_email:
                logger.info(
                    f"[ROUTER:{channel}] Access denied: agent={agent_name} requires email "
                    f"and sender={message.sender_id} not verified"
                )
                await adapter.prompt_auth(message, agent_name, bot_token)
                return

            if verified_email and db.email_has_agent_access(agent_name, verified_email):
                logger.debug(f"[ROUTER:{channel}] Access granted via owner/admin/sharing: {verified_email}")
            elif open_access:
                logger.debug(
                    f"[ROUTER:{channel}] Access granted via open_access "
                    f"(email={verified_email or 'none'})"
                )
            elif verified_email:
                # Verified email + restrictive policy → record access request
                try:
                    db.upsert_access_request(agent_name, verified_email, channel)
                except Exception as e:
                    logger.error(f"[ROUTER:{channel}] Failed to upsert access_request: {e}")
                logger.info(
                    f"[ROUTER:{channel}] Pending access request: "
                    f"agent={agent_name}, email={verified_email}"
                )
                await adapter.send_response(
                    message.channel_id,
                    ChannelResponse(
                        text=(
                            "🔒 Your access request is pending approval. "
                            "I'll let you know once the agent owner responds."
                        ),
                        metadata={"bot_token": bot_token, "agent_name": agent_name},
                    ),
                    thread_id=message.thread_id,
                )
                return
            # else: no verified email and policy not set → legacy permissive
            # (preserves backward compat for agents that haven't opted in).

        # 6. Get/create session
        logger.debug(f"[ROUTER:{channel}] Step 6 - creating session")
        session_identifier = adapter.get_session_identifier(message)
        session = db.get_or_create_public_chat_session(
            agent_name, session_identifier, channel
        )
        session_id = session.id if hasattr(session, 'id') else session["id"]
        logger.debug(f"[ROUTER:{channel}] Step 6 - session_id: {session_id}")

        # 7. Build context prompt (same as web public chat)
        # In group chats, use fresh context (no prior history) to prevent
        # leaking prior private conversation context into public group replies.
        # Issue #349: Include sender identity in group messages so agent knows who is speaking.
        if is_group:
            sender_context = _format_group_sender(message)
            context_prompt = f"{sender_context}\n\n{message.text}"
        else:
            context_prompt = db.build_public_chat_context(session_id, message.text)
        logger.debug(f"[ROUTER:{channel}] Step 7 - context built ({len(context_prompt)} chars, group={is_group})")

        # 7b. Handle file uploads — download via adapter, copy into agent container.
        # Issue #487: workspace-write failures abort execution and surface a
        # channel-native error so the user knows the upload didn't land.
        upload_dir = None  # Track for cleanup
        image_data: list = []
        if message.files:
            file_descriptions, upload_dir, all_writes_failed, image_data = await self._handle_file_uploads(
                adapter, message, agent_name, container, session_id,
                verified_email=verified_email,
            )
            if all_writes_failed:
                logger.warning(
                    f"[ROUTER:{channel}] All file writes failed for {agent_name}; "
                    f"replying with error and aborting execution"
                )
                await adapter.send_response(
                    message.channel_id,
                    ChannelResponse(
                        text="Sorry, I couldn't save the file(s) you sent. Please try again in a moment.",
                        metadata={"bot_token": bot_token, "agent_name": agent_name},
                    ),
                    thread_id=message.thread_id,
                )
                await self._cleanup_uploads(container, upload_dir)
                return
            if file_descriptions:
                file_block = "\n".join(file_descriptions)
                context_prompt = f"{context_prompt}\n\n{file_block}"
                logger.info(f"[ROUTER] Step 7b - {len(file_descriptions)} file(s) processed for agent")

        # 8. Show processing indicator (⏳ on Slack, typing on Telegram, etc.)
        await adapter.indicate_processing(message)

        # 9. Execute via TaskExecutionService (same path as web public chat)
        logger.debug(f"[ROUTER:{channel}] Step 9 - executing via TaskExecutionService")
        # Prefer the verified email (Issue #311) so MEM-001 keys cross-channel
        # off the same identity. Fall back to the channel-native source id.
        source_email = verified_email or adapter.get_source_identifier(message)

        # MEM-001 (#895): inject per-user memory for verified channel users.
        # Excluded in group mode because `verified_email` there is the
        # *unlocker's* email (set once per group, not per-speaker), so
        # injecting their memory into replies addressed to other group
        # members would leak PII across users.
        memory_system_prompt: Optional[str] = None
        if verified_email and not is_group:
            try:
                user_memory = db.get_or_create_public_user_memory(
                    agent_name, verified_email
                )
                memory_system_prompt = format_user_memory_block(user_memory)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[ROUTER:{channel}] memory fetch failed for {verified_email}: {e}"
                )

        # Security: restrict tools for public channel users
        # No file access (Read exposes .env/credentials), no Bash, no Write/Edit
        # Configurable via settings_service (default: WebSearch, WebFetch)
        public_allowed_tools = _get_channel_allowed_tools()

        # Non-image files land at upload_dir in the container; agent needs Read
        # to access them. Images are delivered as vision content blocks via
        # --input-format stream-json (#562), so no Read is needed for them.
        _IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}
        has_readable_files = message.files and any(
            f.mimetype not in _IMAGE_MIMES for f in message.files
        )
        if has_readable_files and "Read" not in public_allowed_tools:
            public_allowed_tools = public_allowed_tools + ["Read"]

        try:
            task_execution_service = get_task_execution_service()

            result = await task_execution_service.execute_task(
                agent_name=agent_name,
                message=context_prompt,
                triggered_by=channel,
                source_user_email=source_email,
                timeout_seconds=None,  # Uses agent's configured timeout (TIMEOUT-001)
                allowed_tools=public_allowed_tools,
                system_prompt=memory_system_prompt,
                images=image_data or None,
            )

            if result.status == "failed":
                error_msg = result.error or "Unknown error"
                logger.error(f"[ROUTER:{channel}] Step 9 - task failed: {error_msg}")
                await adapter.indicate_done(message)

                # Reply with the actual error if available, otherwise generic message
                if error_msg and error_msg != "Unknown error":
                    response_text = error_msg
                else:
                    response_text = "Sorry, I encountered an error processing your message."

                await adapter.send_response(
                    message.channel_id,
                    ChannelResponse(text=response_text, metadata={"bot_token": bot_token, "agent_name": agent_name}),
                    thread_id=message.thread_id,
                )
                await self._cleanup_uploads(container, upload_dir)
                return

            response_text = result.response or ""
            logger.debug(f"[ROUTER:{channel}] Step 9 - agent responded ({len(response_text)} chars, cost=${result.cost or 0:.4f})")

        except Exception as e:
            logger.error(f"[ROUTER:{channel}] Step 9 - execution error: {e}", exc_info=True)
            await adapter.indicate_done(message)
            await adapter.send_response(
                message.channel_id,
                ChannelResponse(
                    text="Sorry, I encountered an error processing your message. Please try again.",
                    metadata={"bot_token": bot_token, "agent_name": agent_name}
                ),
                thread_id=message.thread_id,
            )
            await self._cleanup_uploads(container, upload_dir)
            return

        # 10. Done processing — show completion indicator
        await adapter.indicate_done(message)

        # 11. Persist messages in session
        logger.debug(f"[ROUTER:{channel}] Step 11 - persisting messages")
        db.add_public_chat_message(session_id, "user", message.text)
        db.add_public_chat_message(session_id, "assistant", response_text, cost=result.cost)

        # 11a. MEM-001 (#895): mirror the web path — increment per-user count
        # and fire-and-forget the conversation summarizer every 5 messages.
        # Same gating as the injection above (verified + not group).
        if verified_email and not is_group:
            try:
                new_count = db.increment_public_user_memory_count(
                    agent_name, verified_email
                )
                if new_count and new_count % 5 == 0:
                    asyncio.create_task(summarize_user_memory_background(
                        agent_name=agent_name,
                        user_email=verified_email,
                        session_id=session_id,
                    ))
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[ROUTER:{channel}] memory count/summarize trigger failed "
                    f"for {verified_email}: {e}"
                )

        # 11b. Extract code blocks as outbound files (after persisting, before sending)
        outbound_files = []
        send_text = response_text
        try:
            send_text, outbound_files = self._extract_outbound_files(response_text)
            if outbound_files:
                logger.info(
                    f"[ROUTER:{channel}] Extracted {len(outbound_files)} outbound file(s) "
                    f"({sum(len(f.content) for f in outbound_files)} bytes total)"
                )
        except Exception as e:
            logger.error(f"[ROUTER:{channel}] Outbound file extraction failed: {e}", exc_info=True)

        # 12. Send response to channel
        # Issue #349: Check for [NO_REPLY] marker (observation mode)
        # Agent can return "[NO_REPLY]" to indicate it observed the message
        # but chooses not to respond. This enables selective engagement in groups.
        if send_text.strip() == _NO_REPLY_MARKER:
            logger.info(f"[ROUTER:{channel}] Agent returned {_NO_REPLY_MARKER}, skipping response")
            # Still run cleanup but skip sending
            await self._cleanup_uploads(container, upload_dir)
            logger.info(f"[ROUTER:{channel}] DONE (no reply): {agent_name}, execution_id={result.execution_id}")
            return

        logger.debug(f"[ROUTER:{channel}] Step 12 - sending response")
        response_metadata = {"bot_token": bot_token, "agent_name": agent_name}
        if is_group:
            response_metadata["is_group"] = True
        await adapter.send_response(
            message.channel_id,
            ChannelResponse(text=send_text, files=outbound_files, metadata=response_metadata),
            thread_id=message.thread_id,
        )

        # 13. Post-response hook (thread tracking, etc.)
        await adapter.on_response_sent(message, agent_name)

        # 14. Clean up uploaded files (per-session directory)
        await self._cleanup_uploads(container, upload_dir)

        logger.info(f"[ROUTER:{channel}] DONE: {agent_name}, execution_id={result.execution_id}")

    # =========================================================================
    # Private helpers
    # =========================================================================

    @staticmethod
    def _extract_outbound_files(response_text: str) -> Tuple[str, List[OutboundFile]]:
        """
        Extract large fenced code blocks from agent response text.

        Scans for ```lang ... ``` blocks with recognized language hints.
        Blocks exceeding the minimum size threshold are extracted as
        OutboundFile objects and replaced with a placeholder in the text.

        Returns (cleaned_text, files).
        """
        files: List[OutboundFile] = []
        total_bytes = 0
        file_counter = 0

        def replace_block(match: re.Match) -> str:
            nonlocal total_bytes, file_counter

            lang_hint = match.group(1).lower()
            content = match.group(2)

            # Skip unrecognized languages
            ext = _OUTBOUND_LANG_MAP.get(lang_hint)
            if not ext:
                return match.group(0)

            # Skip small blocks
            if len(content) < _OUTBOUND_MIN_BLOCK_CHARS:
                return match.group(0)

            # Enforce per-file size limit
            content_bytes = content.encode("utf-8")
            if len(content_bytes) > _OUTBOUND_MAX_FILE_BYTES:
                return match.group(0)

            # Enforce total size limit
            if total_bytes + len(content_bytes) > _OUTBOUND_MAX_TOTAL_BYTES:
                return match.group(0)

            # Enforce max file count
            if file_counter >= _OUTBOUND_MAX_FILES:
                return match.group(0)

            file_counter += 1
            total_bytes += len(content_bytes)
            filename = f"response_{file_counter}.{ext}"

            files.append(OutboundFile(
                filename=filename,
                content=content_bytes,
                language=lang_hint,
            ))

            return f"(see attached: {filename})"

        cleaned_text = _CODE_BLOCK_RE.sub(replace_block, response_text)
        return cleaned_text, files

    @staticmethod
    async def _cleanup_uploads(container, upload_dir: Optional[str]) -> None:
        """Remove per-session upload directory from agent container."""
        if not upload_dir:
            return
        try:
            await container_exec_run(container, f"rm -rf {upload_dir}", user="developer")
            logger.debug(f"[ROUTER] Cleaned up {upload_dir}")
        except Exception as e:
            logger.warning(f"[ROUTER] Upload cleanup failed: {e}")

    async def _handle_file_uploads(
        self,
        adapter: ChannelAdapter,
        message: NormalizedMessage,
        agent_name: str,
        container,
        session_id: str,
        verified_email: Optional[str] = None,
    ) -> tuple:
        """
        Download files via adapter then delegate to the shared upload service.

        Returns (descriptions, upload_dir, all_writes_failed, image_data).
        """
        uploader = verified_email or adapter.get_source_identifier(message)

        # Download each file via the channel adapter before calling the shared service
        raw_files = []
        for f in message.files:
            data = await adapter.download_file(f, message)
            raw_files.append({
                "name": f.name,
                "mimetype": f.mimetype,
                "size": f.size,
                "data": data,  # None signals download failure to the service
                "id": f.id,
            })

        return await process_file_uploads(
            raw_files=raw_files,
            agent_name=agent_name,
            container=container,
            session_id=session_id,
            uploader=uploader,
            source=adapter.channel_type,
            sender_id=message.sender_id,
            channel_id=message.channel_id,
        )


# Singleton instance
message_router = ChannelMessageRouter()
