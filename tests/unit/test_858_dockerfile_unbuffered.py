"""
Issue #858 — First-time setup token silently lost (block-buffered stdout).

Regression guards for both layers of the fix.

Layer 1 (Dockerfile): `docker/backend/Dockerfile` had drifted and lost
`ENV PYTHONUNBUFFERED=1` while `docker/scheduler/Dockerfile` kept it. Without
the env var, CPython block-buffers stdout to the Docker log pipe (~8KB) and the
first-time setup token printed during the backend lifespan never reaches
`docker logs`, deadlocking fresh-install onboarding. The Dockerfile tests parse
an actual `ENV` instruction (a bare substring match would be satisfiable by a
comment) and assert backend/scheduler parity so the two cannot re-drift.

Layer 2 (lifespan source): the setup token must be emitted via `logger.warning`
(the logging StreamHandler flushes per record — immune to Dockerfile drift),
positioned after `setup_logging()` but BEFORE the event-bus startup so a hang
there can't suppress it; and the lifespan must contain no `print()` calls at
all (a reintroduced print would silently regress to the buffered path). These
are AST checks on `src/backend/main.py` — unit tests never import `main`
(too heavy; see tests/unit/test_websocket_auth.py for the precedent).

True unit tests — no Docker, no backend.

Issue: https://github.com/Abilityai/trinity/issues/858
"""
from __future__ import annotations

import ast
import re
from collections.abc import Callable
from pathlib import Path

import pytest

# tests/unit/ lives two levels under the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DOCKERFILE = REPO_ROOT / "docker" / "backend" / "Dockerfile"
SCHEDULER_DOCKERFILE = REPO_ROOT / "docker" / "scheduler" / "Dockerfile"
BACKEND_MAIN = REPO_ROOT / "src" / "backend" / "main.py"

# Matches a real Docker `ENV PYTHONUNBUFFERED=1` instruction (line-leading `ENV`,
# `KEY=VALUE` form), not a commented-out line or prose mentioning the variable.
_ENV_PATTERN = re.compile(
    r"^\s*ENV\s+PYTHONUNBUFFERED=(?P<value>\S+)\s*$",
    re.MULTILINE,
)


def _unbuffered_value(dockerfile: Path) -> str | None:
    """Return the value assigned to PYTHONUNBUFFERED via a real ENV instruction.

    Ignores comments and prose; returns None when no such instruction exists.
    """
    text = dockerfile.read_text(encoding="utf-8")
    match = _ENV_PATTERN.search(text)
    return match.group("value") if match else None


@pytest.mark.unit
def test_backend_dockerfile_sets_pythonunbuffered() -> None:
    """The backend image must declare ENV PYTHONUNBUFFERED=1 (the #858 fix)."""
    assert BACKEND_DOCKERFILE.is_file(), f"missing {BACKEND_DOCKERFILE}"
    value = _unbuffered_value(BACKEND_DOCKERFILE)
    assert value == "1", (
        "docker/backend/Dockerfile must set `ENV PYTHONUNBUFFERED=1` so the "
        "first-time setup token reaches `docker logs` (#858); "
        f"found {value!r}."
    )


@pytest.mark.unit
def test_backend_matches_scheduler_unbuffered() -> None:
    """Backend and scheduler must declare the same value so they can't re-drift."""
    backend_value = _unbuffered_value(BACKEND_DOCKERFILE)
    scheduler_value = _unbuffered_value(SCHEDULER_DOCKERFILE)
    assert scheduler_value is not None, (
        "docker/scheduler/Dockerfile lost its ENV PYTHONUNBUFFERED declaration — "
        "the parity baseline for #858 is gone."
    )
    assert backend_value == scheduler_value, (
        "backend and scheduler Dockerfiles disagree on PYTHONUNBUFFERED "
        f"(backend={backend_value!r}, scheduler={scheduler_value!r}); they must "
        "stay in parity (#858)."
    )


# ---- Layer 2: lifespan emits the setup token via the flushing logger ----


def _lifespan_body() -> list[ast.stmt]:
    """Return the top-level statements of main.py's `lifespan` handler."""
    tree = ast.parse(BACKEND_MAIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            return node.body
    pytest.fail("async def lifespan(...) not found in src/backend/main.py")


def _string_content(node: ast.AST) -> str:
    """Concatenate every string constant under a node (handles f-strings)."""
    return "".join(
        sub.value
        for sub in ast.walk(node)
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
    )


def _first_statement_index(
    body: list[ast.stmt], matches: Callable[[ast.Call], bool]
) -> int | None:
    """Index of the first top-level statement containing a matching Call."""
    for i, stmt in enumerate(body):
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and matches(node):
                return i
    return None


def _is_name_call(call: ast.Call, name: str) -> bool:
    return isinstance(call.func, ast.Name) and call.func.id == name


def _is_method_call(call: ast.Call, obj: str, method: str) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == method
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == obj
    )


@pytest.mark.unit
def test_lifespan_emits_setup_token_via_logger_before_event_bus() -> None:
    """The token must go through logger.warning, after setup_logging() and
    before event_bus.start() — a hang in later startup must not suppress it."""
    body = _lifespan_body()

    logging_idx = _first_statement_index(
        body, lambda c: _is_name_call(c, "setup_logging")
    )
    token_idx = _first_statement_index(
        body,
        lambda c: _is_method_call(c, "logger", "warning")
        and "Setup token:" in _string_content(c),
    )
    event_bus_idx = _first_statement_index(
        body, lambda c: _is_method_call(c, "event_bus", "start")
    )

    assert token_idx is not None, (
        "lifespan no longer emits the first-time setup token via "
        "logger.warning('... Setup token: ...') — fresh installs would have no "
        "way to read the token from `docker logs` (#858)."
    )
    assert logging_idx is not None and logging_idx < token_idx, (
        "the setup-token emission must come after setup_logging() so the "
        "structured handler is configured (#858)."
    )
    assert event_bus_idx is not None and token_idx < event_bus_idx, (
        "the setup-token emission must come before event_bus.start() so a hang "
        "in event-bus/audit startup cannot suppress it (#858)."
    )


@pytest.mark.unit
def test_lifespan_has_no_print_calls() -> None:
    """All lifespan print() calls were converted to logger.* in #858 — print()
    output is block-buffered to the Docker pipe and is the bug class that lost
    the setup token. A reintroduced print would silently regress."""
    offenders = [
        node.lineno
        for stmt in _lifespan_body()
        for node in ast.walk(stmt)
        if isinstance(node, ast.Call) and _is_name_call(node, "print")
    ]
    assert not offenders, (
        f"print() calls found in main.py lifespan at lines {offenders}; use "
        "logger.info/warning/error instead — print() is block-buffered to the "
        "Docker log pipe and was the #858 bug class."
    )
