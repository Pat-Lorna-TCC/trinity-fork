"""Regression test for #804 — security conftest's `.env` overlay must
defeat the parent conftest's `"test"` sentinel.

`tests/conftest.py` setdefaults `REDIS_PASSWORD` / `REDIS_BACKEND_PASSWORD`
to the literal string `"test"` at global pytest import so backend module
imports don't blow up. Before #804 was fixed, `tests/security/conftest.py`
used `os.environ.setdefault(...)` to overlay real values from `.env` —
which is a no-op once the sentinel is set. Result: `redis-cli` ran with
`-a test` against a healthy stack and the ACL tests failed for the wrong
reason. The fix in `tests/security/conftest.py:_load_redis_env` pops the
sentinel before overlaying, and uses direct assignment.

This test relies on the session-autouse `_load_redis_env` having already
fired — which either skipped the whole suite (when no creds are available
anywhere) or populated `os.environ` with real values. If we got here, the
fixture decided creds are present; they must not be the sentinel.
"""

import os
from pathlib import Path

import pytest

try:
    from dotenv import dotenv_values  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    dotenv_values = None  # type: ignore[assignment]


@pytest.mark.integration
def test_security_conftest_overlays_test_sentinel() -> None:
    for key in ("REDIS_PASSWORD", "REDIS_BACKEND_PASSWORD"):
        actual = os.environ.get(key)
        assert actual is not None and actual != "", (
            f"{key} missing — the autouse `_load_redis_env` should have "
            f"skipped the session before this test ran"
        )
        assert actual != "test", (
            f'{key} is still the parent conftest\'s "test" sentinel — '
            f"tests/security/conftest.py:_load_redis_env failed to override "
            f"it (regression of #804: setdefault is a no-op once the parent "
            f"conftest has set the key)"
        )


@pytest.mark.integration
def test_dotenv_value_won_when_env_present() -> None:
    """Stronger check: when `.env` defines either key, the live value
    must equal the `.env` value (not the `"test"` sentinel, not a stale
    earlier value). Skips per-key when `.env` is absent or doesn't define it.
    """
    if dotenv_values is None:
        pytest.skip("python-dotenv not installed")

    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        pytest.skip("project .env not present — precondition unmet")

    env_values = dotenv_values(env_path)
    checked = 0
    for key in ("REDIS_PASSWORD", "REDIS_BACKEND_PASSWORD"):
        expected = env_values.get(key)
        if not expected:
            continue
        checked += 1
        actual = os.environ.get(key)
        assert actual == expected, (
            f"{key} in os.environ does not match .env: "
            f"expected={expected!r}, got={actual!r} — "
            f"tests/security/conftest.py:_load_redis_env failed to overlay "
            f"the .env value over the prior os.environ value (regression of #804)"
        )

    if checked == 0:
        pytest.skip(".env defines neither REDIS_PASSWORD nor REDIS_BACKEND_PASSWORD")
