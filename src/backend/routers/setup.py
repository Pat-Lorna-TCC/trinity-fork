"""
First-time setup routes for the Trinity backend.

Provides endpoints for initial admin password setup on first launch.
These endpoints require NO authentication and only work before setup is completed.
"""
import logging
import secrets
import threading
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from database import db
from dependencies import hash_password
from utils.password_validation import validate_password_strength, PASSWORD_REQUIREMENTS_MESSAGE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/setup", tags=["setup"])

# --- Shared cross-worker first-time-setup token (#1165) ---------------------
#
# Prevents installation hijack: only someone with access to the server logs can
# read the token and complete setup (SEC #177).
#
# Production runs uvicorn with `--workers 2`, so a per-process module global
# (`secrets.token_urlsafe(24)` at import) would differ per worker — the operator
# copies one worker's token but POST /api/setup/admin-password load-balances and
# 403s ~50% of the time on the other worker (#1165). The token therefore lives
# in Redis (first-writer-wins) so every worker reads the SAME value, and
# validation reads it live at request time (no per-worker cached copy → no
# drift). Redis is a mandatory platform dependency; when it is unreachable setup
# is *blocked* — GET /api/setup/status reports `setup_available: false` and the
# endpoint returns 503 — rather than silently falling back to a per-worker token.
_SETUP_TOKEN_KEY = "trinity:setup:token"

# TTL on the shared token so an abandoned install (token issued, setup never
# completed) doesn't leave a valid secret in Redis + logs forever — the old
# per-process global died on every restart; the Redis key would otherwise
# persist across restarts. Safe to expire because validation reads the token
# live (no cached per-worker copy to drift): once it lapses the next status
# poll re-issues and re-prints one. 24h easily covers a minutes-long setup.
_SETUP_TOKEN_TTL_SECONDS = 86400

# This worker's candidate for the SETNX claim. Only the first worker to boot
# wins; the rest read the winner. Never used directly for validation.
_candidate_token: str = secrets.token_urlsafe(24)

_redis_client = None
_redis_client_lock = threading.Lock()


def reset_redis_client() -> None:
    """Drop the cached client so the next call rebuilds it.

    Without this, a client cached while Redis was healthy stays cached after a
    Redis restart/failover — every later op throws and setup stays blocked until
    the *process* restarts. Mirrors services/rate_limiter.reset_redis_client so
    ensure_setup_token() can actually "self-heal without a restart".
    """
    global _redis_client
    with _redis_client_lock:
        _redis_client = None


def _get_redis():
    """Return a cached Redis client, or None if Redis is unreachable.

    Mirrors services/rate_limiter._get_redis (1s timeouts, AUTH-error logging).
    """
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        import redis as _redis
        from config import REDIS_URL
    except Exception as e:  # import-time failure (config.py raises on bad URL)
        logger.error("Setup token: cannot import Redis client/config: %s", e)
        return None

    from redis.exceptions import (
        AuthenticationError,
        AuthenticationWrongNumberOfArgsError,
        ConnectionError as RedisConnectionError,
        ResponseError,
        TimeoutError as RedisTimeoutError,
    )

    with _redis_client_lock:
        if _redis_client is not None:  # racy double-check
            return _redis_client
        try:
            r = _redis.from_url(
                REDIS_URL, decode_responses=True,
                socket_connect_timeout=1, socket_timeout=1,
            )
            r.ping()
            _redis_client = r
            return _redis_client
        except (AuthenticationError, AuthenticationWrongNumberOfArgsError):
            logger.error("Setup token: Redis AUTH failed — check REDIS_URL/ACL")
            return None
        except ResponseError as e:
            logger.error("Setup token: Redis ResponseError: %s", e)
            return None
        except (RedisConnectionError, RedisTimeoutError) as e:
            logger.warning("Setup token: Redis transient error: %s", e)
            return None
        except Exception as e:
            logger.error("Setup token: unexpected Redis error: %s", type(e).__name__)
            return None


def ensure_setup_token():
    """Idempotently ensure a shared setup token exists in Redis and return it.

    The first worker to call claims its candidate (atomic `SET ... NX`); all
    callers then read the single winner. The token is printed to the logs
    exactly once — by the worker that issues it — so the operator can read it
    from `docker logs`. Safe to call from startup AND from the status endpoint:
    if Redis was down at boot and later recovers, the next status poll re-issues
    and prints the token, so setup self-heals without a restart (#1165).

    Returns the shared token, or None if Redis is unreachable (setup is blocked
    until Redis recovers — never a silent per-worker fallback).
    """
    r = _get_redis()
    if r is None:
        logger.error(
            "Setup token: Redis unreachable — first-time setup is blocked until "
            "Redis is reachable."
        )
        return None
    try:
        # First-writer-wins. Two round-trips rather than `SET NX GET` (the
        # NX+GET combo is only valid on Redis 7.0+): the `SET NX` is the atomic
        # claim, and the follow-up `GET` always converges on the single winner,
        # so reading it is race-free regardless of which worker wins.
        issued = r.set(
            _SETUP_TOKEN_KEY, _candidate_token, nx=True, ex=_SETUP_TOKEN_TTL_SECONDS
        )
        token = r.get(_SETUP_TOKEN_KEY)
    except Exception as e:
        logger.error(
            "Setup token: Redis op failed (%s) — setup blocked.", type(e).__name__
        )
        reset_redis_client()  # rebuild on the next call so a Redis blip self-heals
        return None
    if issued:
        # Only the issuing worker logs — losers read the winner silently.
        logger.warning(
            "TRINITY FIRST-TIME SETUP REQUIRED\n"
            "Setup token: %s\n"
            "Visit the Trinity UI and enter this token to set the admin password.\n"
            "Valid until first-time setup completes.",
            token,
        )
    return token


def clear_setup_token() -> None:
    """Best-effort delete of the shared token once setup completes (#1165).

    After setup_completed=true the endpoint 403s regardless of the token, so
    this is hygiene — it stops the now-useless secret lingering in Redis.
    """
    r = _get_redis()
    if r is None:
        return
    try:
        r.delete(_SETUP_TOKEN_KEY)
    except Exception:
        reset_redis_client()  # stale client → rebuild next time (TTL still bounds the key)


class SetAdminPasswordRequest(BaseModel):
    """Request body for setting admin password."""
    password: str = Field(..., max_length=128)
    confirm_password: str = Field(..., max_length=128)
    setup_token: str


@router.get("/status")
async def get_setup_status():
    """
    Check if initial setup is complete. No auth required.

    Returns:
        - setup_completed: Whether the admin password has been set
        - setup_available: Whether setup can be completed right now. Always true
          once setup is done; while pending it is false when Redis is unreachable
          (the shared setup token lives there) so the UI can show a
          "waiting for Redis" state instead of a form that would 503 (#1165).
    """
    setup_completed = db.get_setting_value('setup_completed', 'false') == 'true'
    # While setup is pending, probe Redis via ensure_setup_token() — this both
    # reports availability AND self-heals: if Redis just recovered and no token
    # exists yet, it re-issues and prints one (#1165).
    setup_available = True
    if not setup_completed:
        setup_available = ensure_setup_token() is not None
    return {
        "setup_completed": setup_completed,
        "setup_available": setup_available,
    }


@router.post("/admin-password")
async def set_admin_password(data: SetAdminPasswordRequest, request: Request):
    """
    Set admin password on first launch. No auth required, only works once.

    Requires the setup token printed to server logs at startup (prevents installation hijack).
    Once setup_completed=true is set, this endpoint returns 403.

    Requirements:
    - setup_token must match the token printed in server logs at startup
    - Password must meet OWASP ASVS 2.1 complexity requirements
    - Password and confirm_password must match

    Returns:
        - success: true if password was set
    """
    # Check setup not already completed
    if db.get_setting_value('setup_completed', 'false') == 'true':
        raise HTTPException(
            status_code=403,
            detail="Setup already completed. Password cannot be changed through this endpoint."
        )

    # Resolve the shared cross-worker setup token from Redis (#1165). If Redis
    # is unreachable, block with 503 rather than silently validating against a
    # per-worker token — the latter is the exact bug #1165 fixes.
    shared_token = ensure_setup_token()
    if shared_token is None:
        raise HTTPException(
            status_code=503,
            detail="Setup temporarily unavailable: the backend cannot reach Redis. "
                   "Try again once it has recovered.",
        )

    # Validate setup token to prevent installation hijack.
    # Use constant-time comparison to guard against timing attacks.
    if not secrets.compare_digest(data.setup_token, shared_token):
        raise HTTPException(
            status_code=403,
            detail="Invalid setup token. Check server logs for the setup token printed at startup."
        )

    # Validate password complexity (OWASP ASVS 2.1)
    errors = validate_password_strength(data.password)
    if errors:
        # Return generic message — don't reveal which specific rules failed
        # on this unauthenticated endpoint (CSO review finding #1)
        raise HTTPException(
            status_code=400,
            detail=PASSWORD_REQUIREMENTS_MESSAGE,
        )

    if data.password != data.confirm_password:
        raise HTTPException(
            status_code=400,
            detail="Passwords do not match"
        )

    # Hash the password and update admin user
    hashed_password = hash_password(data.password)

    # Update admin user's password in database
    db.update_user_password('admin', hashed_password)

    # Mark setup as completed
    db.set_setting('setup_completed', 'true')

    # The token is now useless (endpoint 403s henceforth); remove it from Redis (#1165).
    clear_setup_token()

    return {"success": True}
