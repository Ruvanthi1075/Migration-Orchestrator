"""
ledger.py
Feature 4/6 -- Migration Metadata Ledger, MML (Algorithm 5)

Ownership (guide QSMO_Implementation_Guide.docx §4, §8.5, §9.2):
    Person C creates this skeleton on day 1 so migrate_link.py has a
    real object to log against instead of a throwaway fake. Person D
    extends it with whatever query methods monitor.py's rollback-rate
    tuning needs (guide §9.2 already specifies get_history and
    compute_rollback_rate, both implemented below -- extend rather
    than replace them unless the interface in guide §5.5 changes).

Interface (guide §5.5, frozen):
    log_event(dpid, baseline, post, outcome, reason=None,
              associated_flows=None) -> None
    get_history(dpid) -> list[record]
    compute_rollback_rate() -> float     # reverted / (success + reverted)

Who calls this (guide §2.3 state-ownership rule, restated for logging):
    - migrate_link.py (Person C) calls log_event() on every Stage 1/2
      outcome: "success" or "failed" -- see migrate_link.MigrationExecutor.migrate().
    - monitor.py (Person D) calls log_event() with outcome="reverted"
      when BRD's retry limit is exceeded (Algorithm 4 line 4).
    No other module writes to the ledger. coverage.py/optimizer.py
    never touch this file.

This is intentionally a plain in-memory, append-only list, matching
the paper's Algorithm 5 exactly (§9.2's docstring: "record <- {...};
append record to migration log"). Swap the storage backend (e.g. a
JSON file or sqlite table for the final report's persistence needs)
without changing this class's public methods -- every other module is
written against log_event/get_history/compute_rollback_rate only.
"""

from __future__ import annotations

import datetime


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class MigrationLedger:
    """Append-only audit log for every migration attempt.

    One instance is shared across the whole orchestrator run --
    construct it once in orchestrator_main.py (guide §10, last
    integration step) and pass the same object into both
    migrate_link.MigrationExecutor(..., ledger=ledger) and
    monitor.DegradationMonitor(..., ledger=ledger).
    """

    def __init__(self):
        self.log: list[dict] = []

    def log_event(
        self,
        dpid: str,
        baseline: dict | None,
        post: dict | None,
        outcome: str,
        reason: str | None = None,
        associated_flows: list[str] | None = None,
    ) -> None:
        """Append one record. Matches Algorithm 5 / guide §5.5 exactly.

        outcome must be one of "success", "failed", "reverted" -- the
        same three values migrate_link.py's `migrate()` result and
        monitor.py's BRD rollback path already use, so no translation
        layer is needed between either module and this one.
        """
        assert outcome in ("success", "failed", "reverted"), (
            f"unexpected outcome {outcome!r} -- must be 'success', 'failed', or 'reverted' "
            f"(guide §5.5 / Algorithm 5); if this is a genuinely new outcome type, that's a "
            f"group decision (README: 'these schemas are frozen'), not a silent addition here"
        )
        self.log.append({
            "dpid": dpid,
            "timestamp": _now_iso(),
            "baseline": baseline,
            "post": post,
            "outcome": outcome,
            "reason": reason,
            "associated_flows": list(associated_flows) if associated_flows else [],
        })

    def get_history(self, dpid: str) -> list[dict]:
        """All records for one switch, in the order they were logged."""
        return [r for r in self.log if r["dpid"] == dpid]

    def compute_rollback_rate(self) -> float:
        """reverted / (success + reverted), per guide §9.2.

        Deliberately excludes "failed" (a migration that never became
        Hybrid in the first place) from the denominator -- this metric
        answers "of the migrations that DID succeed, how many later
        had to be rolled back," which is what tuning tau_L/tau_F/kappa
        (guide §9.1) actually needs. A high raw failure rate (Stage
        1/2 never connecting) is a different problem from a high
        rollback rate (connected fine, then degraded) and the report
        should not conflate them -- track failures separately via
        get_history() / len(self.log) if that number is also needed.
        """
        relevant = [r for r in self.log if r["outcome"] in ("success", "reverted")]
        if not relevant:
            return 0.0
        reverted = sum(1 for r in relevant if r["outcome"] == "reverted")
        return reverted / len(relevant)


# ---------------------------------------------------------------------
# Self-test -- exercises log_event/get_history/compute_rollback_rate
# in isolation, matching the self-check style already used in
# migrate_link.py/optimizer.py/capability_tracker.py.
# ---------------------------------------------------------------------

if __name__ == "__main__":
    ledger = MigrationLedger()

    base = {"latency_ms": 18.5, "failure_rate": 0.0, "overhead_bytes": 900}
    post = {"latency_ms": 24.8, "failure_rate": 0.0, "overhead_bytes": 4200}

    print("=== Test 1: log a success, a failure, and a later revert ===")
    ledger.log_event("0000000000000003", base, post, outcome="success",
                      associated_flows=["ctrl_s3"])
    ledger.log_event("0000000000000005", base, None, outcome="failed",
                      reason="Stage 1: negotiated group was X25519, expected X25519MLKEM768")
    ledger.log_event("0000000000000003", base, post, outcome="reverted",
                      reason="degradation-retry-limit-exceeded",
                      associated_flows=["ctrl_s3"])

    assert len(ledger.log) == 3
    assert len(ledger.get_history("0000000000000003")) == 2
    assert ledger.get_history("0000000000000003")[0]["outcome"] == "success"
    assert ledger.get_history("0000000000000003")[1]["outcome"] == "reverted"
    assert ledger.get_history("0000000000000099") == []
    print("  OK: log_event appends, get_history filters per-dpid correctly")

    print("\n=== Test 2: compute_rollback_rate excludes 'failed', includes success+reverted ===")
    # s3: 1 success + 1 reverted -> counted. s5: 1 failed -> excluded entirely.
    rate = ledger.compute_rollback_rate()
    assert rate == 0.5, rate
    print(f"  OK: rollback rate = {rate} (1 reverted / (1 success + 1 reverted), 'failed' excluded)")

    print("\n=== Test 3: empty ledger -> rate is 0.0, not a ZeroDivisionError ===")
    empty = MigrationLedger()
    assert empty.compute_rollback_rate() == 0.0
    print("  OK: empty ledger handled without crashing")

    print("\n=== Test 4: outcome validation rejects an unrecognized outcome string ===")
    try:
        ledger.log_event("0000000000000004", base, None, outcome="rolled_back_lol")
        raise AssertionError("expected an AssertionError for a bad outcome string")
    except AssertionError as exc:
        assert "unexpected outcome" in str(exc)
        print("  OK: bad outcome string rejected instead of silently logged")

    print("\nAll ledger.py self-checks passed.")
