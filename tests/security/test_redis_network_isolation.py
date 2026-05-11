"""Issue #589 — Redis lockdown integration tests.

Acceptance criteria covered:
  #3 — agent-network containers cannot reach Redis (network segment separation)
  #1 — requirepass enforced (NOAUTH from unauth client)
  #2 — backend ACL user lacks permissions for FLUSHALL / CONFIG (no admin)

Requires the platform stack to be running. Skipped (via tests/security/conftest.py)
when REDIS_PASSWORD / REDIS_BACKEND_PASSWORD are not available in the environment.
"""

import os

import pytest

docker = pytest.importorskip("docker")


# Network names match docker-compose.yml top-level `name:` field.
AGENT_NETWORK = "trinity-agent-network"
PLATFORM_NETWORK = "trinity-platform-network"


def _client():
    return docker.from_env()


@pytest.fixture
def backend_password() -> str:
    """Return the Redis ACL backend-user password, or skip if unset.

    Issue #764: the session-scoped autouse fixture in `conftest.py` already
    skips when this env var is missing, but only when ``.env`` is absent
    AND nothing exported the value. A per-test fixture here makes the
    requirement explicit at the call site and provides a clearer skip
    reason in pytest output. Contributors who don't run the security tests
    against a live stack see "skipped: REDIS_BACKEND_PASSWORD not set"
    instead of a hard ``KeyError``.
    """
    pwd = os.environ.get("REDIS_BACKEND_PASSWORD")
    if not pwd:
        pytest.skip(
            "REDIS_BACKEND_PASSWORD not set; "
            "platform-network ACL test requires a running stack — see tests/security/README.md"
        )
    return pwd


@pytest.mark.integration
def test_agent_network_container_cannot_reach_redis():
    """Acceptance #3: a container on the agent network has no route to Redis."""
    client = _client()
    out = client.containers.run(
        "alpine:3.19",
        # nc -z exits 0 on reach, 1 on refused/no-route. Wrap so output is
        # deterministic even when the connection is refused.
        command=[
            "sh",
            "-c",
            "nc -z -w 2 redis 6379 && echo REACHABLE || echo BLOCKED",
        ],
        network=AGENT_NETWORK,
        remove=True,
    )
    assert b"BLOCKED" in out, f"Redis is reachable from agent network: {out!r}"


@pytest.mark.integration
def test_redis_rejects_unauthenticated_connection():
    """Acceptance #1: even on the platform network, no creds → NOAUTH."""
    client = _client()
    out = client.containers.run(
        "redis:7-alpine",
        command=["redis-cli", "-h", "redis", "PING"],
        network=PLATFORM_NETWORK,
        remove=True,
    )
    assert b"NOAUTH" in out, f"Redis accepted unauth connection: {out!r}"


@pytest.mark.integration
def test_platform_container_can_authenticate(backend_password):
    """Sanity: the backend connection string works."""
    client = _client()
    out = client.containers.run(
        "redis:7-alpine",
        command=[
            "redis-cli",
            "-h", "redis",
            "--user", "backend",
            "-a", backend_password,
            "--no-auth-warning",
            "PING",
        ],
        network=PLATFORM_NETWORK,
        remove=True,
    )
    assert b"PONG" in out, f"backend ACL user cannot PING: {out!r}"


@pytest.mark.integration
def test_backend_acl_blocks_flushall(backend_password):
    """Acceptance #2: backend ACL user cannot wipe data."""
    client = _client()
    out = client.containers.run(
        "redis:7-alpine",
        command=[
            "redis-cli",
            "-h", "redis",
            "--user", "backend",
            "-a", backend_password,
            "--no-auth-warning",
            "FLUSHALL",
        ],
        network=PLATFORM_NETWORK,
        remove=True,
    )
    assert b"NOPERM" in out, f"backend user can run FLUSHALL: {out!r}"


@pytest.mark.integration
def test_backend_acl_blocks_config_get(backend_password):
    """Acceptance #2: backend ACL user cannot read CONFIG (would leak admin password)."""
    client = _client()
    out = client.containers.run(
        "redis:7-alpine",
        command=[
            "redis-cli",
            "-h", "redis",
            "--user", "backend",
            "-a", backend_password,
            "--no-auth-warning",
            "CONFIG", "GET", "requirepass",
        ],
        network=PLATFORM_NETWORK,
        remove=True,
    )
    assert b"NOPERM" in out, f"backend user can read CONFIG: {out!r}"
