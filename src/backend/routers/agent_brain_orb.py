"""Brain Orb backend proxy (#58, trinity-enterprise).

Read-only broker between the first-party orb page (frontend origin) and the
agent container. Replaces the old localhost-CORS + per-start `X-Orb-Token` model
with the platform trust boundary:

  * read authz via ``AuthorizedAgentByName`` (owner / shared / admin);
  * the mutating scope route is ``OwnedAgentByName`` (owner / admin only);
  * transport via ``agent_httpx_client`` (per-agent agent-auth token, #1159);
  * the agent owns generation + scope state (Invariant #8) — Trinity only brokers.

Phase 1 shipped the read-only data proxy. Phase 2 (#58) adds the live scope
control loop: list scopes (read) and mutate the active set → agent re-export
(owner-gated). Isolated in its own router (no edits to agent_files.py). The 5-
segment paths never collide with the ``/api/agents/{name}`` catch-all (Inv #4).
"""
import json
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

from config import BRAIN_ORB_ENABLED, BRAIN_ORB_VOICE_ENABLED, GEMINI_API_KEY
from database import db
from dependencies import AuthorizedAgentByName, CurrentUser, OwnedAgentByName
from services import brain_orb_voice_service, rate_limiter
from services.agent_auth import agent_httpx_client
from services.docker_service import get_agent_container
from services.docker_utils import container_reload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["brain-orb"])

_AGENT_PORT = 8000
_MAX_SCOPE_BODY = 64 * 1024
_MAX_TOOL_BODY = 16 * 1024        # read-tool requests are tiny (a query + scope)
_NO_STORE = {"Cache-Control": "no-store"}
# Per-(user, agent) voice-token mint budget — a leaked JWT can't spin up unbounded
# Gemini Live sessions on the platform key. Mirrors the VoIP spend-control precedent.
_VOICE_TOKEN_RATE_LIMIT = 10
_VOICE_TOKEN_RATE_WINDOW = 60


async def _agent_request(agent_name: str, method: str, path: str, *, content: bytes | None = None, timeout: float) -> httpx.Response:
    """Shared gate + proxy: flag-gate → agent running → agent-auth'd request.

    Returns the upstream ``httpx.Response`` for the caller to map. Raises the
    mapped ``HTTPException`` on flag-off (404 — flag is the single source of
    truth), missing/stopped agent (404 / 503), or transport failure (503 / 504).
    """
    if not BRAIN_ORB_ENABLED:
        raise HTTPException(status_code=404, detail="Brain Orb is not enabled")
    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")
    await container_reload(container)
    if container.status != "running":
        raise HTTPException(status_code=503, detail="Agent is not running")
    url = f"http://agent-{agent_name}:{_AGENT_PORT}{path}"
    try:
        async with agent_httpx_client(agent_name, timeout=timeout) as client:
            return await client.request(method, url, content=content)
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Agent is not reachable")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Agent timed out")


def _passthrough(response: httpx.Response, *, not_found: str) -> Response:
    """Map a brain-orb agent response to a byte pass-through, or a mapped error:
    404 (flag off / no export / unsupported), 413 (too large), 502 (other)."""
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail=not_found)
    if response.status_code == 413:
        raise HTTPException(status_code=413, detail="Request too large")
    if response.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail="Agent returned an error")
    # Byte pass-through — never re-serialize (the data payload is multi-MB).
    return Response(content=response.content, media_type="application/json", headers=_NO_STORE)


@router.get("/{agent_name}/brain-orb/data")
async def get_brain_orb_data(agent_name: AuthorizedAgentByName):
    """Proxy the agent's visualization data.json to the orb page (read access)."""
    response = await _agent_request(agent_name, "GET", "/api/brain-orb/data", timeout=30.0)
    return _passthrough(response, not_found="Brain Orb data not found")


@router.get("/{agent_name}/brain-orb/scopes")
async def get_brain_orb_scopes(agent_name: AuthorizedAgentByName):
    """List the agent's selectable + active vault scopes for the scope panel
    (read access). 404 when the agent ships no scope hook."""
    response = await _agent_request(agent_name, "GET", "/api/brain-orb/scopes", timeout=30.0)
    return _passthrough(response, not_found="Scope control not supported")


@router.post("/{agent_name}/brain-orb/scope")
async def post_brain_orb_scope(agent_name: OwnedAgentByName, request: Request):
    """Mutate the agent's active scope set (mount/unmount) → agent re-export.

    **Owner/admin only** — this is the one mutating brain-orb route. Forwards the
    raw JSON body to the agent hook; the agent owns the scope state + the re-export
    (Invariant #8). 200s timeout sits just above the agent-server's 180s hook
    timeout so a slow re-export surfaces as the agent's 504, not a premature one.
    """
    body = await request.body()
    if len(body) > _MAX_SCOPE_BODY:
        raise HTTPException(status_code=413, detail="Request too large")
    response = await _agent_request(agent_name, "POST", "/api/brain-orb/scope", content=body, timeout=200.0)
    return _passthrough(response, not_found="Scope control not supported")


@router.post("/{agent_name}/brain-orb/voice-token")
async def post_brain_orb_voice_token(agent_name: AuthorizedAgentByName, current_user: CurrentUser):
    """Mint a short-lived, config-locked Gemini Live **ephemeral token** for the
    orb's client-held voice tile (#58 Phase 3).

    The browser connects DIRECTLY to Gemini Live with this token — Trinity never
    proxies the audio. The token's own constraints (model lock, locked tool
    surface, single new-session use, short expiry) are the security envelope; this
    route's job is the JWT gate + a per-user mint budget.

    Gated on the **voice** flag (distinct from ``BRAIN_ORB_ENABLED``): 404 when
    ``BRAIN_ORB_VOICE_ENABLED`` is off, 503 when no Gemini key, 502 on mint error.

    The response field is deliberately **not** named ``token`` — orb.js's
    ``initActions()`` reads ``.token`` from the (deferred) Phase-4 write surface,
    and a ``token`` here would silently enable KB writes (review F1). The agent is
    not contacted (no container check) — the mint is a Google call, not an agent call.
    """
    if not BRAIN_ORB_VOICE_ENABLED:
        raise HTTPException(status_code=404, detail="Brain Orb voice is not enabled")
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=503, detail="Voice is not configured")
    rate_limiter.enforce(
        f"brain_orb_voice_token:{current_user.id}:{agent_name}",
        _VOICE_TOKEN_RATE_LIMIT,
        _VOICE_TOKEN_RATE_WINDOW,
        detail="Too many voice sessions.",
    )
    try:
        result = await brain_orb_voice_service.mint_voice_token(
            agent_name,
            voice_name=db.get_voice_name(agent_name),
            agent_prompt=db.get_voice_system_prompt(agent_name),
        )
    except ValueError:
        # No Gemini key surfaced from the service layer — treat as unconfigured.
        raise HTTPException(status_code=503, detail="Voice is not configured")
    except Exception as exc:  # SDK / network / quota — never leak internals.
        logger.warning("brain-orb voice-token mint failed for %s: %s", agent_name, exc)
        raise HTTPException(status_code=502, detail="Could not mint a voice session")
    return Response(
        content=json.dumps(result),
        media_type="application/json",
        headers=_NO_STORE,
    )


@router.post("/{agent_name}/brain-orb/tool")
async def post_brain_orb_tool(agent_name: AuthorizedAgentByName, request: Request):
    """Read-only KB-search broker (#58 Phase 3). Proxies to the agent-server, which
    runs the agent's ``~/.trinity/brain-orb/search`` convention hook (scope-aware,
    read-only). Read access (``AuthorizedAgentByName``) — search never writes. 404
    when the agent ships no ``search`` hook."""
    body = await request.body()
    if len(body) > _MAX_TOOL_BODY:
        raise HTTPException(status_code=413, detail="Request too large")
    response = await _agent_request(agent_name, "POST", "/api/brain-orb/tool", content=body, timeout=30.0)
    return _passthrough(response, not_found="KB search not supported")
