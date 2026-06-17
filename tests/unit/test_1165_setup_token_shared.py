"""#1165 — shared cross-worker first-time setup token.

Prod runs uvicorn with `--workers 2`. A per-process module-global token would
differ per worker, so POST /api/setup/admin-password 403s ~50% of the time
depending on which worker handles the request. The token now lives in Redis
(first-writer-wins) so every worker reads the SAME value; validation reads it
live. When Redis is unreachable, setup is *blocked* (token == None) rather than
silently falling back to a per-worker token.

These tests exercise the token machinery in `routers.setup` directly with a
fakeredis instance shared across simulated "workers" (distinct candidates).
"""
import secrets

import pytest
import fakeredis

pytestmark = pytest.mark.unit

import routers.setup as setup


@pytest.fixture
def fake_redis(monkeypatch):
    """Point routers.setup at a fresh in-process fakeredis shared by all callers."""
    setup._redis_client = None
    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(setup, "_get_redis", lambda: fake)
    # Restore a deterministic candidate after each test mutates it.
    monkeypatch.setattr(setup, "_candidate_token", secrets.token_urlsafe(24))
    return fake


def test_first_writer_wins_across_workers(fake_redis, monkeypatch):
    """Worker A claims; worker B (later, different candidate) reads A's winner."""
    monkeypatch.setattr(setup, "_candidate_token", "worker-A-token")
    tok_a = setup.ensure_setup_token()
    assert tok_a == "worker-A-token"

    monkeypatch.setattr(setup, "_candidate_token", "worker-B-token")
    tok_b = setup.ensure_setup_token()

    assert tok_b == "worker-A-token"  # B did NOT overwrite the winner
    assert fake_redis.get(setup._SETUP_TOKEN_KEY) == "worker-A-token"


def test_validation_succeeds_regardless_of_worker(fake_redis, monkeypatch):
    """The bug's symptom: a token issued on one worker validates on another."""
    monkeypatch.setattr(setup, "_candidate_token", "issued-on-A")
    issued = setup.ensure_setup_token()  # operator copies this from the logs

    # Now a DIFFERENT worker (own candidate) handles the validation request.
    monkeypatch.setattr(setup, "_candidate_token", "different-on-B")
    shared = setup.ensure_setup_token()

    assert secrets.compare_digest(issued, shared)          # operator's token matches
    assert not secrets.compare_digest("wrong-token", shared)


def test_idempotent_resolve(fake_redis):
    assert setup.ensure_setup_token() == setup.ensure_setup_token()


def test_clear_deletes_key(fake_redis):
    setup.ensure_setup_token()
    assert fake_redis.get(setup._SETUP_TOKEN_KEY) is not None
    setup.clear_setup_token()
    assert fake_redis.get(setup._SETUP_TOKEN_KEY) is None


def test_redis_unreachable_blocks_setup(monkeypatch):
    """No client → None (blocked), never a silent per-worker fallback."""
    setup._redis_client = None
    monkeypatch.setattr(setup, "_get_redis", lambda: None)
    assert setup.ensure_setup_token() is None


def test_redis_op_error_blocks_setup(monkeypatch):
    """Client present but ops raise → None (blocked)."""
    class Boom:
        def set(self, *a, **k):
            raise RuntimeError("redis exploded")

        def get(self, *a, **k):
            raise RuntimeError("redis exploded")

    setup._redis_client = None
    monkeypatch.setattr(setup, "_get_redis", lambda: Boom())
    assert setup.ensure_setup_token() is None


def test_clear_is_safe_when_redis_down(monkeypatch):
    """clear_setup_token never raises even if Redis is unreachable."""
    setup._redis_client = None
    monkeypatch.setattr(setup, "_get_redis", lambda: None)
    setup.clear_setup_token()  # no exception


def test_token_has_bounded_ttl(fake_redis):
    """The key carries a TTL so an abandoned install doesn't leave a forever-secret."""
    setup.ensure_setup_token()
    ttl = fake_redis.ttl(setup._SETUP_TOKEN_KEY)
    assert 0 < ttl <= setup._SETUP_TOKEN_TTL_SECONDS


def test_op_error_resets_cached_client(monkeypatch):
    """A Redis op failure drops the cached client so the next call rebuilds it
    (self-heal after a Redis restart, rather than a permanently-dead client)."""
    class Boom:
        def set(self, *a, **k):
            raise RuntimeError("redis exploded")

        def get(self, *a, **k):
            raise RuntimeError("redis exploded")

    setup._redis_client = Boom()  # pretend a healthy client was cached, now dead
    monkeypatch.setattr(setup, "_get_redis", lambda: setup._redis_client)

    assert setup.ensure_setup_token() is None
    assert setup._redis_client is None  # reset_redis_client() ran on the failure
