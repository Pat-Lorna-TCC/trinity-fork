"""Session fixture for security integration tests (Issue #589).

Loads real REDIS_PASSWORD / REDIS_BACKEND_PASSWORD from the project's .env
(if present) and skips the suite when neither env nor file provides them.
"""

import os
from pathlib import Path

import pytest

try:
    from dotenv import dotenv_values  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    dotenv_values = None  # type: ignore[assignment]


@pytest.fixture(scope="session", autouse=True)
def _load_redis_env():
    # tests/conftest.py setdefaults REDIS_PASSWORD/REDIS_BACKEND_PASSWORD to the
    # literal "test" at global pytest import so backend modules can be imported
    # without real Redis creds. That sentinel must be cleared here before we
    # overlay real values from .env, otherwise setdefault is a no-op and the
    # skip-guard below sees "test" and lets the suite run with wrong creds (#804).
    for key in ("REDIS_PASSWORD", "REDIS_BACKEND_PASSWORD"):
        if os.environ.get(key) == "test":
            del os.environ[key]

    if dotenv_values is not None:
        env_path = Path(__file__).resolve().parents[2] / ".env"
        if env_path.exists():
            for key, value in dotenv_values(env_path).items():
                if key in {"REDIS_PASSWORD", "REDIS_BACKEND_PASSWORD"} and value:
                    os.environ[key] = value

    for required in ("REDIS_PASSWORD", "REDIS_BACKEND_PASSWORD"):
        if required not in os.environ or not os.environ[required]:
            pytest.skip(
                f"{required} not set; integration test requires .env or env vars"
            )
