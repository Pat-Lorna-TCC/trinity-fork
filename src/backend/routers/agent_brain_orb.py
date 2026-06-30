"""Brain Orb backend proxy (#58, trinity-enterprise).

Read-only broker between the first-party orb page (frontend origin) and the
agent-produced `data.json` inside the container. Replaces the old localhost-CORS
+ per-start token model with the platform trust boundary:

  * authz via ``AuthorizedAgentByName`` (owner / shared / admin — read access);
  * transport via ``agent_httpx_client`` (per-agent agent-auth token, #1159);
  * the agent owns generation (Invariant #8) — Trinity only reads.

Isolated in its own router (no edits to agent_files.py) to keep the blast radius
small. The path nests under ``/api/agents/{name}/...`` per Invariant #15; its 5
segments never collide with the ``/api/agents/{name}`` catch-all (Invariant #4).
"""
import httpx
from fastapi import APIRouter, HTTPException, Response

from config import BRAIN_ORB_ENABLED
from dependencies import AuthorizedAgentByName
from services.agent_auth import agent_httpx_client
from services.docker_service import get_agent_container
from services.docker_utils import container_reload

router = APIRouter(prefix="/api/agents", tags=["brain-orb"])


@router.get("/{agent_name}/brain-orb/data")
async def get_brain_orb_data(agent_name: AuthorizedAgentByName):
    """Proxy the agent's visualization data.json to the orb page.

    404 when the platform flag is off (mirrors the session-tab routes — the flag
    is the single source of truth) or when the agent has produced no export yet,
    so the frontend renders its empty state rather than surfacing a 5xx.
    """
    if not BRAIN_ORB_ENABLED:
        raise HTTPException(status_code=404, detail="Brain Orb is not enabled")

    container = get_agent_container(agent_name)
    if not container:
        raise HTTPException(status_code=404, detail="Agent not found")
    await container_reload(container)
    if container.status != "running":
        raise HTTPException(status_code=503, detail="Agent is not running")

    agent_url = f"http://agent-{agent_name}:8000/api/brain-orb/data"
    try:
        async with agent_httpx_client(agent_name, timeout=30.0) as client:
            response = await client.get(agent_url)
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Agent is not reachable")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Agent timed out")

    if response.status_code == 404:
        # Agent hasn't run its exporter yet — surface as 404 (not 502).
        raise HTTPException(status_code=404, detail="Brain Orb data not found")
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="Agent returned an error")

    # Byte pass-through — never re-serialize the multi-MB JSON.
    return Response(
        content=response.content,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )
