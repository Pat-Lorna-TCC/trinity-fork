"""
Brain Orb data endpoint (#58, trinity-enterprise).

Read-only: serves the agent-produced visualization (`data.json`) that the
Brain Orb page renders. The agent owns generation (export_data.py) — Invariant
#8; this server only reads the last-written export. The path is fixed (no user
input), so there is no traversal surface. Inbound auth is enforced globally by
AgentAuthMiddleware (#1159) — only /health is exempt.
"""
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)
router = APIRouter()

# Cornelius-class convention: the read-only exporter writes here.
DATA_PATH = Path("/home/developer/resources/agent-visualization/data.json")


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
