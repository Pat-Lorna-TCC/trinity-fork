"""Unit tests for the Brain Orb static-render foundation (#58, trinity-enterprise).

Two surfaces, both mounted on a minimal FastAPI app and driven via TestClient
(real routing + dependency injection), with Docker / agent-HTTP mocked:

  * backend proxy  — routers/agent_brain_orb.py  (flag gate, authz, proxy + error map)
  * agent-server   — docker/base-image/agent_server/routers/brain_orb.py (file read)

These are true unit tests: no Docker daemon, no running backend, no agent
container. The full data path (real container export) is covered by /verify.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.agent_brain_orb as bo
from dependencies import get_authorized_agent_by_name

_AGENT = "cornelius"


# --- fakes -----------------------------------------------------------------

def _running():
    return types.SimpleNamespace(status="running", labels={})


def _stopped():
    return types.SimpleNamespace(status="exited", labels={})


class _FakeClientCM:
    """Stands in for `async with agent_httpx_client(...) as client`."""

    def __init__(self, *, result=None, exc=None):
        self._result = result
        self._exc = exc

    async def __aenter__(self):
        client = AsyncMock()
        if self._exc is not None:
            client.get = AsyncMock(side_effect=self._exc)
        else:
            client.get = AsyncMock(return_value=self._result)
        return client

    async def __aexit__(self, *_a):
        return False


def _fake_httpx(*, result=None, exc=None):
    def _factory(*_args, **_kwargs):
        return _FakeClientCM(result=result, exc=exc)
    return _factory


def _resp(status_code: int, content: bytes = b""):
    return types.SimpleNamespace(status_code=status_code, content=content)


# --- backend proxy fixture -------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(bo.router)
    app.dependency_overrides[get_authorized_agent_by_name] = lambda: _AGENT
    # Flag ON by default; individual tests flip it off. container_reload is async.
    monkeypatch.setattr(bo, "BRAIN_ORB_ENABLED", True)
    monkeypatch.setattr(bo, "container_reload", AsyncMock())
    return TestClient(app, raise_server_exceptions=True)


_URL = f"/api/agents/{_AGENT}/brain-orb/data"


# --- backend proxy: gating -------------------------------------------------

def test_flag_off_returns_404(client, monkeypatch):
    """Platform flag is the single source of truth — off ⇒ 404, never a 5xx."""
    monkeypatch.setattr(bo, "BRAIN_ORB_ENABLED", False)
    with patch.object(bo, "get_agent_container", return_value=_running()):
        r = client.get(_URL)
    assert r.status_code == 404
    assert "not enabled" in r.json()["detail"]


def test_agent_not_found_returns_404(client):
    with patch.object(bo, "get_agent_container", return_value=None):
        r = client.get(_URL)
    assert r.status_code == 404
    assert "Agent not found" in r.json()["detail"]


def test_agent_stopped_returns_503(client):
    with patch.object(bo, "get_agent_container", return_value=_stopped()):
        r = client.get(_URL)
    assert r.status_code == 503


# --- backend proxy: happy path + pass-through ------------------------------

def test_success_passes_bytes_through(client):
    payload = b'{"nodes":[{"id":"n1"}],"edges":[]}'
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(200, payload))
    ):
        r = client.get(_URL)
    assert r.status_code == 200
    assert r.content == payload  # byte-identical, never re-serialized
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers.get("cache-control") == "no-store"


# --- backend proxy: error mapping ------------------------------------------

def test_agent_404_maps_to_404(client):
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(404))
    ):
        r = client.get(_URL)
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_agent_500_maps_to_502(client):
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(500, b"boom"))
    ):
        r = client.get(_URL)
    assert r.status_code == 502


def test_connect_error_maps_to_503(client):
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(exc=httpx.ConnectError("down"))
    ):
        r = client.get(_URL)
    assert r.status_code == 503


def test_timeout_maps_to_504(client):
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(exc=httpx.TimeoutException("slow"))
    ):
        r = client.get(_URL)
    assert r.status_code == 504


# --- agent-server route: file read -----------------------------------------

@pytest.fixture
def agent_client(tmp_path, monkeypatch):
    from agent_server.routers import brain_orb as asbo
    monkeypatch.setattr(asbo, "DATA_PATH", tmp_path / "data.json")
    app = FastAPI()
    app.include_router(asbo.router)
    # NB: AgentAuthMiddleware intentionally omitted — covered by its own tests;
    # here we exercise the route's read/404 logic in isolation.
    return TestClient(app), tmp_path / "data.json"


def test_agent_server_serves_data_when_present(agent_client):
    client, path = agent_client
    path.write_text('{"ok":1}')
    r = client.get("/api/brain-orb/data")
    assert r.status_code == 200
    assert r.json() == {"ok": 1}
    assert r.headers["content-type"].startswith("application/json")


def test_agent_server_404_when_absent(agent_client):
    client, _path = agent_client
    r = client.get("/api/brain-orb/data")
    assert r.status_code == 404
