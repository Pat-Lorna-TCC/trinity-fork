"""Unit tests for the Brain Orb static-render foundation (#58, trinity-enterprise).

Two surfaces, both mounted on a minimal FastAPI app and driven via TestClient
(real routing + dependency injection), with Docker / agent-HTTP mocked:

  * backend proxy  — routers/agent_brain_orb.py  (flag gate, authz, proxy + error map)
  * agent-server   — docker/base-image/agent_server/routers/brain_orb.py (file read)

These are true unit tests: no Docker daemon, no running backend, no agent
container. The full data path (real container export) is covered by /verify.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import types
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routers.agent_brain_orb as bo
from dependencies import get_authorized_agent_by_name, get_current_user, get_owned_agent_by_name

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
        # The proxy calls client.request(method, url, content=...) for all routes.
        if self._exc is not None:
            client.request = AsyncMock(side_effect=self._exc)
        else:
            client.request = AsyncMock(return_value=self._result)
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
    # Read routes use AuthorizedAgentByName; the mutating /scope uses OwnedAgentByName.
    app.dependency_overrides[get_authorized_agent_by_name] = lambda: _AGENT
    app.dependency_overrides[get_owned_agent_by_name] = lambda: _AGENT
    app.dependency_overrides[get_current_user] = lambda: types.SimpleNamespace(id=7, email="u@example.com")
    # Flags ON by default; individual tests flip them off. container_reload is async.
    monkeypatch.setattr(bo, "BRAIN_ORB_ENABLED", True)
    monkeypatch.setattr(bo, "BRAIN_ORB_VOICE_ENABLED", True)   # #60 Phase 3
    monkeypatch.setattr(bo, "GEMINI_API_KEY", "test-key")      # #60 Phase 3
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


# --- backend proxy: scopes (read) + scope (owner mutation) — #58 Phase 2 ----

_SCOPES_URL = f"/api/agents/{_AGENT}/brain-orb/scopes"
_SCOPE_URL = f"/api/agents/{_AGENT}/brain-orb/scope"


def test_scopes_success_passes_through(client):
    payload = b'{"active":["core"],"available":[{"token":"core"}]}'
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(200, payload))
    ):
        r = client.get(_SCOPES_URL)
    assert r.status_code == 200
    assert r.json() == {"active": ["core"], "available": [{"token": "core"}]}


def test_scopes_flag_off_404(client, monkeypatch):
    monkeypatch.setattr(bo, "BRAIN_ORB_ENABLED", False)
    with patch.object(bo, "get_agent_container", return_value=_running()):
        r = client.get(_SCOPES_URL)
    assert r.status_code == 404


def test_scopes_unsupported_maps_to_404(client):
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(404))
    ):
        r = client.get(_SCOPES_URL)
    assert r.status_code == 404


def test_scope_post_success_passes_through(client):
    payload = b'{"ok":true,"active":["core","Books"],"nodes":1200,"edges":3000}'
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(200, payload))
    ):
        r = client.post(_SCOPE_URL, json={"tokens": ["core", "Books"]})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["active"] == ["core", "Books"]


def test_scope_post_flag_off_404(client, monkeypatch):
    monkeypatch.setattr(bo, "BRAIN_ORB_ENABLED", False)
    with patch.object(bo, "get_agent_container", return_value=_running()):
        r = client.post(_SCOPE_URL, json={"tokens": []})
    assert r.status_code == 404


def test_scope_post_body_too_large_413(client):
    # > 64 KiB raw body — rejected before any agent call (no patches needed).
    r = client.post(_SCOPE_URL, json={"tokens": ["x" * 70_000]})
    assert r.status_code == 413


def test_scope_post_unsupported_maps_to_404(client):
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(404))
    ):
        r = client.post(_SCOPE_URL, json={"tokens": []})
    assert r.status_code == 404


# --- backend proxy: voice-token mint (#60 Phase 3) -------------------------

_VOICE_TOKEN_URL = f"/api/agents/{_AGENT}/brain-orb/voice-token"


def test_voice_token_success(client):
    """Happy path: mints via the service, passes the body through no-store, and the
    token field is NOT named `token` (would flip orb.js's Phase-4 write surface on)."""
    minted = {
        "ephemeral_token": "auth_tokens/abc123",
        "model": "models/gemini-3.1-flash-live-preview",
        "voice_name": "Kore",
        "expires_at": "2026-07-01T00:05:00+00:00",
        "tools": ["highlight_related_notes", "mount_scope"],
    }
    with patch.object(bo.db, "get_voice_name", return_value="Kore"), patch.object(
        bo.db, "get_voice_system_prompt", return_value=None
    ), patch.object(bo.rate_limiter, "enforce") as enforce, patch.object(
        bo.brain_orb_voice_service, "mint_voice_token", AsyncMock(return_value=minted)
    ):
        r = client.post(_VOICE_TOKEN_URL)
    assert r.status_code == 200
    body = r.json()
    assert body["ephemeral_token"] == "auth_tokens/abc123"
    assert "token" not in body  # F1: never a bare `token` field
    assert body["tools"] == ["highlight_related_notes", "mount_scope"]
    assert r.headers.get("cache-control") == "no-store"
    enforce.assert_called_once()  # per-(user,agent) mint budget is enforced


def test_voice_token_flag_off_404(client, monkeypatch):
    """Gated on the VOICE flag specifically — distinct from BRAIN_ORB_ENABLED."""
    monkeypatch.setattr(bo, "BRAIN_ORB_VOICE_ENABLED", False)
    r = client.post(_VOICE_TOKEN_URL)
    assert r.status_code == 404


def test_voice_token_no_key_503(client, monkeypatch):
    monkeypatch.setattr(bo, "GEMINI_API_KEY", "")
    r = client.post(_VOICE_TOKEN_URL)
    assert r.status_code == 503


def test_voice_token_mint_failure_502(client):
    with patch.object(bo.db, "get_voice_name", return_value="Kore"), patch.object(
        bo.db, "get_voice_system_prompt", return_value=None
    ), patch.object(bo.rate_limiter, "enforce"), patch.object(
        bo.brain_orb_voice_service, "mint_voice_token", AsyncMock(side_effect=RuntimeError("gemini boom"))
    ):
        r = client.post(_VOICE_TOKEN_URL)
    assert r.status_code == 502


def test_voice_token_value_error_maps_503(client):
    """Service raising ValueError (no key surfaced late) → 503, never a 500."""
    with patch.object(bo.db, "get_voice_name", return_value="Kore"), patch.object(
        bo.db, "get_voice_system_prompt", return_value=None
    ), patch.object(bo.rate_limiter, "enforce"), patch.object(
        bo.brain_orb_voice_service, "mint_voice_token", AsyncMock(side_effect=ValueError("no key"))
    ):
        r = client.post(_VOICE_TOKEN_URL)
    assert r.status_code == 503


def test_voice_token_rate_limited_429(client):
    """Over the per-user mint budget → 429 (enforce raises), before any mint."""
    from fastapi import HTTPException as _HE
    mint = AsyncMock()
    with patch.object(bo.db, "get_voice_name", return_value="Kore"), patch.object(
        bo.db, "get_voice_system_prompt", return_value=None
    ), patch.object(bo.rate_limiter, "enforce", side_effect=_HE(status_code=429, detail="slow down")), patch.object(
        bo.brain_orb_voice_service, "mint_voice_token", mint
    ):
        r = client.post(_VOICE_TOKEN_URL)
    assert r.status_code == 429
    mint.assert_not_called()  # rate gate fires before the mint


# --- backend proxy: read-only /tool search broker (#60 Phase 3) ------------

_TOOL_URL = f"/api/agents/{_AGENT}/brain-orb/tool"


def test_tool_success_passes_through(client):
    payload = b'{"results":[{"title":"Dopamine","content":"..."}]}'
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(200, payload))
    ):
        r = client.post(_TOOL_URL, json={"query": "dopamine"})
    assert r.status_code == 200
    assert r.json()["results"][0]["title"] == "Dopamine"


def test_tool_unsupported_maps_to_404(client):
    with patch.object(bo, "get_agent_container", return_value=_running()), patch.object(
        bo, "agent_httpx_client", _fake_httpx(result=_resp(404))
    ):
        r = client.post(_TOOL_URL, json={"query": "x"})
    assert r.status_code == 404


def test_tool_flag_off_404(client, monkeypatch):
    monkeypatch.setattr(bo, "BRAIN_ORB_ENABLED", False)
    with patch.object(bo, "get_agent_container", return_value=_running()):
        r = client.post(_TOOL_URL, json={"query": "x"})
    assert r.status_code == 404


def test_tool_body_too_large_413(client):
    r = client.post(_TOOL_URL, json={"query": "x" * 20_000})
    assert r.status_code == 413


# --- agent-server routes: data read + scope hooks --------------------------

def _write_hook(path, script: str):
    """Write an executable convention hook (shebang-selected) for the agent-server."""
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    from agent_server.routers import brain_orb as asbo
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    monkeypatch.setattr(asbo, "DATA_PATH", tmp_path / "data.json")
    monkeypatch.setattr(asbo, "_SCOPES_HOOK", hooks / "scopes")
    monkeypatch.setattr(asbo, "_SCOPE_HOOK", hooks / "scope")
    monkeypatch.setattr(asbo, "_SEARCH_HOOK", hooks / "search")   # #60 Phase 3
    monkeypatch.setattr(asbo, "_HOME", tmp_path)   # subprocess cwd must exist on the test host
    app = FastAPI()
    app.include_router(asbo.router)
    # NB: AgentAuthMiddleware intentionally omitted — covered by its own tests;
    # here we exercise the route read / hook-exec logic in isolation.
    return types.SimpleNamespace(
        client=TestClient(app), asbo=asbo, data=tmp_path / "data.json",
        scopes_hook=hooks / "scopes", scope_hook=hooks / "scope",
        search_hook=hooks / "search",
    )


def test_agent_server_serves_data_when_present(agent_env):
    agent_env.data.write_text('{"ok":1}')
    r = agent_env.client.get("/api/brain-orb/data")
    assert r.status_code == 200
    assert r.json() == {"ok": 1}
    assert r.headers["content-type"].startswith("application/json")


def test_agent_server_404_when_absent(agent_env):
    r = agent_env.client.get("/api/brain-orb/data")
    assert r.status_code == 404


def test_agent_server_scopes_present(agent_env):
    _write_hook(agent_env.scopes_hook,
                '#!/bin/sh\necho \'{"active":["core"],"available":[{"token":"core"}]}\'\n')
    r = agent_env.client.get("/api/brain-orb/scopes")
    assert r.status_code == 200
    assert r.json() == {"active": ["core"], "available": [{"token": "core"}]}


def test_agent_server_scopes_absent_404(agent_env):
    r = agent_env.client.get("/api/brain-orb/scopes")
    assert r.status_code == 404


def test_agent_server_scope_forwards_stdin(agent_env):
    # The hook echoes the received stdin body back, proving forwarding end-to-end.
    _write_hook(agent_env.scope_hook,
                '#!/bin/sh\nbody=$(cat)\necho "{\\"ok\\":true,\\"received\\":$body}"\n')
    r = agent_env.client.post("/api/brain-orb/scope", json={"tokens": ["core", "Books"]})
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is True
    assert out["received"] == {"tokens": ["core", "Books"]}


def test_agent_server_scope_absent_404(agent_env):
    r = agent_env.client.post("/api/brain-orb/scope", json={"tokens": []})
    assert r.status_code == 404


def test_agent_server_scope_invalid_json_502(agent_env):
    _write_hook(agent_env.scope_hook, '#!/bin/sh\necho "not json at all"\n')
    r = agent_env.client.post("/api/brain-orb/scope", json={"tokens": []})
    assert r.status_code == 502


def test_agent_server_scope_nonzero_exit_502(agent_env):
    _write_hook(agent_env.scope_hook, '#!/bin/sh\necho "{}"\nexit 3\n')
    r = agent_env.client.post("/api/brain-orb/scope", json={"tokens": []})
    assert r.status_code == 502


def test_run_hook_timeout_504(agent_env):
    _write_hook(agent_env.scope_hook, '#!/bin/sh\nsleep 5\necho "{}"\n')
    with pytest.raises(HTTPException) as ei:
        asyncio.run(agent_env.asbo._run_hook(agent_env.scope_hook, timeout=0.5))
    assert ei.value.status_code == 504


# --- agent-server: read-only search hook (#60 Phase 3) ---------------------

def test_agent_server_search_forwards_stdin(agent_env):
    # Echoes the received query back, proving the read-only search hook forwards.
    _write_hook(agent_env.search_hook,
                '#!/bin/sh\nbody=$(cat)\necho "{\\"results\\":[],\\"query_received\\":$body}"\n')
    r = agent_env.client.post("/api/brain-orb/tool", json={"query": "dopamine"})
    assert r.status_code == 200
    out = r.json()
    assert out["query_received"] == {"query": "dopamine"}


def test_agent_server_search_absent_404(agent_env):
    r = agent_env.client.post("/api/brain-orb/tool", json={"query": "x"})
    assert r.status_code == 404


def test_agent_server_search_invalid_json_502(agent_env):
    _write_hook(agent_env.search_hook, '#!/bin/sh\necho "not json"\n')
    r = agent_env.client.post("/api/brain-orb/tool", json={"query": "x"})
    assert r.status_code == 502


def test_agent_server_search_nonzero_exit_502(agent_env):
    _write_hook(agent_env.search_hook, '#!/bin/sh\necho "{}"\nexit 2\n')
    r = agent_env.client.post("/api/brain-orb/tool", json={"query": "x"})
    assert r.status_code == 502


# --- mint service: v1alpha client + locked constraints (#60 Phase 3) -------

def test_mint_service_uses_v1alpha_and_locks_config(monkeypatch):
    """The mint must build its OWN v1alpha client (not the voice singleton) and
    lock the model + config, and return the token under `ephemeral_token`."""
    import services.brain_orb_voice_service as svc

    captured = {}

    class _FakeAuthTokens:
        def create(self, *, config):
            captured["config"] = config
            return types.SimpleNamespace(name="auth_tokens/xyz")

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            captured["client_kwargs"] = kwargs
            self.auth_tokens = _FakeAuthTokens()

    monkeypatch.setattr(svc, "_client", None)  # reset the module singleton
    monkeypatch.setattr(svc, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(svc.genai, "Client", _FakeClient)

    out = asyncio.run(svc.mint_voice_token("cornelius", voice_name="Kore", agent_prompt=None))

    # Own client built with api_version='v1alpha' (the singleton-reuse bug guard).
    http_options = captured["client_kwargs"]["http_options"]
    assert http_options.api_version == "v1alpha"
    # Constraints lock the model + a config carrying the (non-write) tool surface.
    cfg = captured["config"]
    assert cfg.uses == 1
    assert cfg.live_connect_constraints.model == svc.VOICE_MODEL
    tool_names = {fd.name for t in cfg.live_connect_constraints.config.tools for fd in t.function_declarations}
    assert "mount_scope" in tool_names
    assert "capture_note" not in tool_names        # F3: no write tools in the manifest
    assert "find_connections" not in tool_names
    # Token surfaced under `ephemeral_token`, never `token` (F1).
    assert out["ephemeral_token"] == "auth_tokens/xyz"
    assert "token" not in out
