"""
Brain Orb data endpoint (#58, trinity-enterprise).

Read-only: serves the agent-produced visualization (`data.json`) that the
Brain Orb page renders. The agent owns generation (export_data.py) — Invariant
#8; this server only reads the last-written export. The path is fixed (no user
input), so there is no traversal surface. Inbound auth is enforced globally by
AgentAuthMiddleware (#1159) — only /health is exempt.
"""
import asyncio
import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)
router = APIRouter()

# Cornelius-class convention: the read-only exporter writes here.
DATA_PATH = Path("/home/developer/resources/agent-visualization/data.json")

# #58 Phase 2 — live scope control. The agent provides two executable convention
# hooks (mirrors ~/.trinity/pre-check, #454); Trinity stays generic and never
# contains agent-specific scope logic (Invariant #8). Absent hooks ⇒ 404 (the
# agent doesn't support scope control), and the orb's scope panel degrades.
HOOK_DIR = Path("/home/developer/.trinity/brain-orb")
_SCOPES_HOOK = HOOK_DIR / "scopes"   # GET: print JSON {active, available}
_SCOPE_HOOK = HOOK_DIR / "scope"     # POST: read JSON {tokens|mount|unmount} on stdin,
                                     #       mutate + re-export (rewrites data.json),
                                     #       print JSON {ok, active, nodes, edges}
_HOME = Path("/home/developer")
_MAX_HOOK_BODY = 64 * 1024           # scope requests are tiny token lists
_MAX_HOOK_OUT = 4 * 1024 * 1024      # scope hooks return small JSON; cap defensively


def _hook_ready(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


async def _run_hook(path: Path, *, stdin: bytes = b"", timeout: float):
    """Run an agent convention hook (shebang-selected interpreter) and parse its
    JSON stdout. Hardened: timeout-kill, output cap, JSON-parse + non-zero-exit
    guards. Never trusts the hook beyond returning structured JSON."""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(path),
            cwd=str(_HOME),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        logger.warning("brain-orb hook %s not executable: %s", path.name, e)
        raise HTTPException(status_code=502, detail="Scope hook not executable")
    try:
        out, err = await asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(status_code=504, detail="Scope hook timed out")
    if proc.returncode != 0:
        logger.warning(
            "brain-orb hook %s exit %s: %s",
            path.name, proc.returncode, (err or b"")[:500].decode(errors="replace"),
        )
        raise HTTPException(status_code=502, detail="Scope hook failed")
    if len(out) > _MAX_HOOK_OUT:
        raise HTTPException(status_code=502, detail="Scope hook output too large")
    try:
        return json.loads(out.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=502, detail="Scope hook returned invalid JSON")


@router.get("/api/brain-orb/data")
async def get_brain_orb_data():
    """Stream the agent's visualization data.json (multi-MB; FileResponse avoids
    buffering it in memory). 404 when the agent hasn't produced an export yet —
    the frontend renders an empty state, never a 500."""
    if not DATA_PATH.is_file():
        raise HTTPException(status_code=404, detail="Brain Orb data not found")
    return FileResponse(
        path=str(DATA_PATH),
        media_type="application/json",
        # The orb fetch sets no filename expectation; inline keeps it a data read.
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/brain-orb/scopes")
async def get_brain_orb_scopes():
    """List the agent's selectable + active vault scopes (for the orb's scope
    panel). 404 when the agent ships no `scopes` hook (scope control unsupported)."""
    if not _hook_ready(_SCOPES_HOOK):
        raise HTTPException(status_code=404, detail="Scope control not supported")
    return await _run_hook(_SCOPES_HOOK, timeout=30)


@router.post("/api/brain-orb/scope")
async def post_brain_orb_scope(request: Request):
    """Mutate the active scope set (mount/unmount), re-export, and return the new
    state. The hook rewrites data.json; the orb then re-fetches /brain-orb/data and
    rebuilds. 404 when the agent ships no `scope` hook. The mutating side is
    owner-gated upstream at the backend proxy."""
    if not _hook_ready(_SCOPE_HOOK):
        raise HTTPException(status_code=404, detail="Scope control not supported")
    body = await request.body()
    if len(body) > _MAX_HOOK_BODY:
        raise HTTPException(status_code=413, detail="Request too large")
    # 180s: a re-export over a large vault can be slow; the orb shows a spinner.
    return await _run_hook(_SCOPE_HOOK, stdin=body, timeout=180)
