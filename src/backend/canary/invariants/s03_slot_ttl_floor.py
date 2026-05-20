"""
S-03 — Slot TTL ≥ execution timeout (CANARY-001 / Issue #411 — Phase 3).

For every member of `agent:slots:A`, the companion `agent:slot:A:{eid}`
HASH must have a Redis TTL of at least the agent's
`execution_timeout_seconds + 300` (SLOT_TTL_BUFFER). A TTL below that
floor means the slot will expire while the execution might still
legitimately be running — exactly the premature-expiry bug class behind
Issue #226.

## TTL sentinel values from `redis.ttl()`

Three special return values, each meaning a different failure mode:

- `>0` (normal case): seconds until expiry. Compare against the floor.
- `-1`: key exists with **no expiry**. The slot has been turned into a
  leak — `redis.expire()` was never called or got cleared. Violation
  regardless of the floor (a slot with no TTL eventually traps capacity
  forever once cleanup misses it).
- `-2`: key **does not exist**. The metadata HASH expired before the
  slot was released, leaving the ZSET pointing at nothing. This is the
  load-bearing #226 case — the slot will never get cleaned up, and the
  bijection check (S-01) doesn't catch it because the ZSET membership
  is fine.

All three count as violations. The observed_state distinguishes them so
the alert reader can tell at a glance which of the three is happening.

## Drain sentinels

The snapshot collector skips drain sentinels (`drain-*` members) when
populating `slot_ttls`. They're intentionally short-lived; the metadata
HASH for a sentinel is written with the same TTL as a real slot but
they're cycled fast enough that catching them mid-flight would be
noise, not signal.

## Why the floor is hard-coded

300s matches `services/slot_service.py:SLOT_TTL_BUFFER`. Hard-coded here
rather than imported so a change to the runtime buffer is a deliberate
review — same pattern as E-01's `SLOT_TTL_BUFFER_SECONDS`. If the two
constants ever drift, the next cycle will fire S-03 violations and
force the conversation.

Tier A, severity critical. A slot whose TTL is below the floor is a
ticking timebomb on capacity correctness.
"""

from typing import List

from ..snapshot import Snapshot, ViolationReport


INVARIANT_ID = "S-03"
TIER = "A"
SEVERITY = "critical"

# Matches services/slot_service.py SLOT_TTL_BUFFER. See module docstring.
SLOT_TTL_BUFFER_SECONDS = 300

DRAIN_PREFIX = "drain-"


def _kind_for(ttl: int, floor: int) -> str:
    """Map the TTL value to a short tag for the violation report."""
    if ttl == -2:
        return "missing"          # Metadata HASH already expired (#226).
    if ttl == -1:
        return "no_expiry"        # Key exists but redis.expire() never set.
    if 0 < ttl < floor:
        return "below_floor"      # Real TTL, but lower than configured floor.
    return "ok"                   # Should not appear in violations.


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """Emit one violation per slot whose TTL is below the configured floor."""
    violations: List[ViolationReport] = []

    # Same gate as S-01 / S-02 / E-02: Redis failed → skip cleanly.
    if any(s.startswith("redis") for s in snapshot.sources_unavailable):
        return violations

    for agent in snapshot.agents:
        floor = agent.execution_timeout_seconds + SLOT_TTL_BUFFER_SECONDS

        for eid in sorted(agent.slot_ids):
            # Drain sentinels are filtered upstream — defence in depth.
            if eid.startswith(DRAIN_PREFIX):
                continue
            # If the collector skipped the slot (per-slot TTL call failed),
            # don't fabricate a violation — operators rely on Redis errors
            # surfacing through `sources_unavailable`, not via false-fire.
            if eid not in agent.slot_ttls:
                continue

            ttl = agent.slot_ttls[eid]
            kind = _kind_for(ttl, floor)
            if kind == "ok":
                continue

            violations.append(
                ViolationReport(
                    invariant_id=INVARIANT_ID,
                    tier=TIER,
                    severity=SEVERITY,
                    observed_state={
                        "agent_name": agent.name,
                        "execution_id": eid,
                        "redis_ttl_seconds": ttl,
                        "execution_timeout_seconds": agent.execution_timeout_seconds,
                        "slot_ttl_buffer_seconds": SLOT_TTL_BUFFER_SECONDS,
                        "floor_seconds": floor,
                        "kind": kind,
                        "snapshot_time": snapshot.snapshot_time,
                    },
                    signal_query=(
                        f"TTL(agent:slot:{agent.name}:{eid}) = {ttl} "
                        f"({kind}); floor = {floor}s"
                    ),
                )
            )

    return violations
