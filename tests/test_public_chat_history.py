"""
Tests for public chat history endpoints (issue #587).

Run with: pytest tests/test_public_chat_history.py -v
"""
import os
import pytest
import httpx

BASE_URL = os.getenv("TRINITY_API_URL", "http://localhost:8000")


@pytest.fixture
def auth_headers():
    """Get JWT auth headers for authenticated requests."""
    password = os.getenv("TRINITY_TEST_PASSWORD", "password")
    response = httpx.post(
        f"{BASE_URL}/api/token",
        data={"username": "admin", "password": password},
    )
    if response.status_code != 200:
        pytest.skip("Could not authenticate — check admin credentials")
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class TestPublicSessionsEndpoints:
    """Tests for GET /api/public/sessions/{token} and /{token}/{session_id}."""

    def test_sessions_requires_auth(self):
        """List sessions endpoint must return 401/403 without JWT."""
        response = httpx.get(f"{BASE_URL}/api/public/sessions/some-token")
        assert response.status_code in (401, 403)

    def test_sessions_invalid_link_returns_404(self, auth_headers):
        """Invalid public link token returns 404 even when authenticated."""
        response = httpx.get(
            f"{BASE_URL}/api/public/sessions/definitely-not-a-real-token-xyz",
            headers=auth_headers,
        )
        assert response.status_code == 404

    def test_session_detail_requires_auth(self):
        """Session detail endpoint must return 401/403 without JWT."""
        response = httpx.get(f"{BASE_URL}/api/public/sessions/some-token/some-session-id")
        assert response.status_code in (401, 403)

    def test_session_detail_invalid_link_returns_404(self, auth_headers):
        """Invalid public link token on detail endpoint returns 404."""
        response = httpx.get(
            f"{BASE_URL}/api/public/sessions/definitely-not-a-real-token-xyz/some-id",
            headers=auth_headers,
        )
        assert response.status_code == 404

    def test_sessions_returns_list_shape(self, auth_headers):
        """Authenticated call with a valid public link returns proper list shape.

        This test requires at least one public link in the system. It skips if
        no public links exist rather than failing.
        """
        # Discover any public link from the agent list
        agents_resp = httpx.get(f"{BASE_URL}/api/agents", headers=auth_headers)
        if agents_resp.status_code != 200:
            pytest.skip("Could not list agents")

        agents = agents_resp.json()
        if not agents:
            pytest.skip("No agents available")

        agent_name = agents[0]["name"]
        links_resp = httpx.get(
            f"{BASE_URL}/api/agents/{agent_name}/public-links",
            headers=auth_headers,
        )
        if links_resp.status_code != 200:
            pytest.skip(f"Could not get public links for agent {agent_name}")

        links = links_resp.json().get("links", [])
        if not links:
            pytest.skip(f"No public links for agent {agent_name}")

        token = links[0]["token"]
        response = httpx.get(
            f"{BASE_URL}/api/public/sessions/{token}",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert "sessions" in data
        assert "session_count" in data
        assert isinstance(data["sessions"], list)
        assert data["session_count"] == len(data["sessions"])

    def test_sessions_limit_param(self, auth_headers):
        """limit query param is accepted without error (validation test)."""
        # Any valid-looking token; will 404, but limit param should not cause 422
        response = httpx.get(
            f"{BASE_URL}/api/public/sessions/not-a-real-token?limit=5",
            headers=auth_headers,
        )
        # 404 = token invalid (expected). 422 = validation error (not expected).
        assert response.status_code != 422
