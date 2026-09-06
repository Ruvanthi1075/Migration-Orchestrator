"""
capability_tracker.py
Feature 5 -- Simple Capability Tracking (Person B)

CORRECTED MODEL (see QSMO_Implementation_Guide.docx, section 2 and 7.1)
------------------------------------------------------------------------
The control-plane channel is a single out-of-band hop per switch, so
the thing that ever gets migrated is a SWITCH (identified by its
dpid), not a link/edge. This tracker is therefore keyed by dpid, not
by a (a, b) link tuple or a device-tier label -- there is no "link
type" left to classify once state lives on switches instead of edges.

Replaces the previous (pre-correction) draft of this file, which
tracked success/failure by link-endpoint tier ("edge-host", "agg-agg",
...) against a `graph.edges[a, b]["state"]` model. That model doesn't
exist anymore: topology.py (Person A) stores state on graph NODES
(`G.nodes[dpid]["state"]`), and coverage.py's frozen interface
(compute_legacy_set, compute_gain, etc.) all operate on dpid sets, not
edge tuples. Importing the old file against the real coverage.py /
topology.py would fail immediately (no `graph.edges[...]["state"]`,
no `get_path_links`) -- this rewrite is what makes optimizer.py
importable and runnable with zero errors against Person A's actual
Feature 1/2 code.

No dependency on Feature 1 (topology.py) or Feature 2 (coverage.py):
this module only ever sees a dpid string and an outcome string, so
Person B can build and test it (see the __main__ block below) before
either of those modules exists, per the guide's section 10 build
order.

Contract (guide section 7.1, frozen -- optimizer.py is written
against this exact shape):
    CapabilityTracker(base_cost=1.0)
    .record(dpid, outcome)      outcome in {"success", "failed"}
    .cost(dpid)  -> float       migration cost optimizer.py should charge
                                 for this switch this round
"""


class CapabilityTracker:
    """
    Per-dpid running count of migration outcomes, and the resulting
    cost multiplier optimizer.py applies on top of a uniform base
    cost. This stands in for the paper's "varying device capability"
    (Section I): a switch that keeps failing its handshake gets
    progressively more expensive to keep retrying, so the greedy loop
    (Feature 3) naturally deprioritizes it instead of hammering the
    same weak device round after round.

    No probability distributions, no sampling -- just running counts,
    per the spec. A dpid that has never been attempted costs exactly
    `base_cost`, i.e. no assumption of failure before any evidence
    exists.
    """

    def __init__(self, base_cost=1.0):
        if base_cost <= 0:
            raise ValueError("base_cost must be positive")
        self.base_cost = float(base_cost)
        # dpid (str) -> {"success": int, "failed": int}
        self.history = {}

    def record(self, dpid, outcome):
        """
        Log one real migration attempt's outcome for `dpid`. Called by
        optimizer.py immediately after every migrate_fn(dpid) call --
        both on success AND on failure (a failed attempt is exactly
        the signal that should raise this switch's future cost).
        """
        if outcome not in ("success", "failed"):
            raise ValueError(f"invalid outcome: {outcome!r}")
        h = self.history.setdefault(dpid, {"success": 0, "failed": 0})
        h[outcome] += 1

    def cost(self, dpid):
        """
        Migration cost to charge against the budget for `dpid` this
        round. Unattempted switches cost exactly base_cost. Otherwise
        base_cost is scaled up by (1 + 2 * failure_rate), so a switch
        that has failed every attempt so far costs 3x base_cost, while
        one with a clean record still costs exactly base_cost.

        The 2.0x multiplier is a starting point, not a derived
        constant -- tune it from real handshake failure data once
        Feature 4 (migrate_link.py) is producing live outcomes; note
        this explicitly in the report rather than presenting it as a
        calibrated value (matches the guide's honesty-note convention
        in section 2.2/12).
        """
        h = self.history.get(dpid)
        if h is None:
            return self.base_cost
        attempts = h["success"] + h["failed"]
        if attempts == 0:
            return self.base_cost
        failure_rate = h["failed"] / attempts
        return self.base_cost * (1.0 + 2.0 * failure_rate)

    def success_rate(self, dpid):
        """
        Score in [0, 1], useful as a tie-breaker when two candidates
        score equally in optimizer.py. A dpid with no history returns
        0.5 -- a neutral score, not an assumption of failure.
        """
        h = self.history.get(dpid)
        if h is None:
            return 0.5
        attempts = h["success"] + h["failed"]
        if attempts == 0:
            return 0.5
        return h["success"] / attempts

    def summary(self):
        """Human-readable per-dpid table, useful for the demo/report."""
        if not self.history:
            return "  (no migration attempts recorded yet)"
        lines = []
        for dpid, h in sorted(self.history.items()):
            attempts = h["success"] + h["failed"]
            rate = h["success"] / attempts if attempts else 0.0
            lines.append(
                f"  {dpid}  success={h['success']:<3} failed={h['failed']:<3} "
                f"rate={rate:.2f}  cost={self.cost(dpid):.2f}"
            )
        return "\n".join(lines)


if __name__ == "__main__":
    # Self-test -- no Feature 1 / Feature 2 dependency, per guide 7.1.
    tracker = CapabilityTracker(base_cost=1.0)

    dpid_a = "0000000000000003"
    dpid_b = "0000000000000004"

    # Never-attempted dpid: neutral cost and neutral success rate.
    assert tracker.cost(dpid_a) == 1.0
    assert tracker.success_rate(dpid_a) == 0.5

    tracker.record(dpid_a, "success")
    tracker.record(dpid_a, "success")
    # 2 success, 0 failed -> cost unchanged, success_rate == 1.0
    assert tracker.cost(dpid_a) == 1.0
    assert tracker.success_rate(dpid_a) == 1.0

    tracker.record(dpid_b, "failed")
    tracker.record(dpid_b, "failed")
    tracker.record(dpid_b, "success")
    # 1 success, 2 failed -> failure_rate = 2/3 -> cost = 1 + 2*(2/3) = 2.333...
    cost_b = tracker.cost(dpid_b)
    assert abs(cost_b - (1.0 + 2.0 * (2 / 3))) < 1e-9, cost_b
    rate_b = tracker.success_rate(dpid_b)
    assert abs(rate_b - (1 / 3)) < 1e-9, rate_b

    # base_cost is honored as a scale factor, not just a default of 1.0
    tracker2 = CapabilityTracker(base_cost=2.0)
    tracker2.record(dpid_a, "failed")
    assert abs(tracker2.cost(dpid_a) - (2.0 * (1.0 + 2.0 * 1.0))) < 1e-9

    try:
        CapabilityTracker(base_cost=0)
        raise AssertionError("expected ValueError for non-positive base_cost")
    except ValueError:
        pass

    try:
        tracker.record(dpid_a, "bogus")
        raise AssertionError("expected ValueError for invalid outcome")
    except ValueError:
        pass

    print("All capability_tracker.py self-checks passed.\n")
    print("Summary:")
    print(tracker.summary())
