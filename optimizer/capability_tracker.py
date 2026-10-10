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

PER-SWITCH COST MODEL (SGBM_Literature_Backed_Formulation.docx, Sec. 7)
-----------------------------------------------------------------------
cost(s) replaces the old uniform base_cost=1.0 and is measured, not
assumed:

    cost_base(s) = norm_dlat(s) / 3 + COST_FLOOR        in [0.259, 0.592]
    norm_dlat(s) = min-max over switches of
                   median(hybrid handshake ms) - median(classical ms)
    COST_FLOOR   = (D_COMPUTE + D_PAYLOAD) / 3 = (0.309 + 0.468) / 3 = 0.259
    cost(s)      = cost_base(s) * (1 + 2 * failure_rate(s))   (unchanged)

budget_costs() below turns a calibration pass's raw handshake timings
into the base_costs dict. The total budget B is NOT computed here: it
is the knob swept for Table I / Fig. 2, B = beta * sum(cost(s)) with
beta in {0.25, 0.5, 0.75, 1.0}, set by whatever script calls
optimizer.run_sgbm() (see optimizer/run_experiment.py).

Contract (guide section 7.1; constructor changed from base_cost=1.0 to
the per-switch form below -- record()/cost() are unchanged in shape):
    CapabilityTracker(base_costs=None, default_cost=COST_FLOOR)
    .record(dpid, outcome)      outcome in {"success", "failed"}
    .cost(dpid)  -> float       migration cost optimizer.py should charge
                                 for this switch this round
"""

import statistics

# Constants from the formulation doc (Sec. 7). D_COMPUTE / D_PAYLOAD are
# the normalized compute and payload deltas; their mean over 3 is the
# minimum a switch can cost, i.e. the cost of a zero-extra-latency one.
D_COMPUTE, D_PAYLOAD = 0.309, 0.468
COST_FLOOR = (D_COMPUTE + D_PAYLOAD) / 3          # 0.259


def budget_costs(classical_ms, hybrid_ms):
    """
    classical_ms, hybrid_ms : dict dpid -> list of handshake times (ms)
    Returns dict dpid -> cost_base(s) in [COST_FLOOR, COST_FLOOR + 1/3].

    delta(s) = median(hybrid) - median(classical), min-max normalized
    across switches, then cost = norm/3 + COST_FLOOR. If every switch
    has the same delta (no spread to normalize), all get COST_FLOOR.
    """
    delta = {
        s: statistics.median(hybrid_ms[s]) - statistics.median(classical_ms[s])
        for s in hybrid_ms
    }
    if not delta:
        return {}
    lo, hi = min(delta.values()), max(delta.values())
    return {
        s: (0.0 if hi - lo < 1e-9 else (v - lo) / (hi - lo)) / 3 + COST_FLOOR
        for s, v in delta.items()
    }



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
    its base cost, i.e. no assumption of failure before any evidence
    exists.

    base_costs : dict dpid -> float, optional
        Measured per-switch base cost, from budget_costs(). A dpid not
        in the dict falls back to `default_cost`.
    default_cost : float
        Base cost for a dpid with no calibration entry (COST_FLOOR).
    """

    def __init__(self, base_costs=None, default_cost=COST_FLOOR):
        if default_cost <= 0:
            raise ValueError("default_cost must be positive")
        base_costs = dict(base_costs or {})
        if any(v <= 0 for v in base_costs.values()):
            raise ValueError("every base cost must be positive")
        self.base_costs = base_costs          # dpid -> (norm_dlat/3 + 0.259)
        self.default_cost = float(default_cost)
        # dpid (str) -> {"success": int, "failed": int}
        self.history = {}

    def _base(self, dpid):
        return self.base_costs.get(dpid, self.default_cost)

    def total_cost(self, dpids):
        """Sum of cost(s) over `dpids` -- use for B = beta * total_cost(all)."""
        return sum(self.cost(d) for d in dpids)

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
        round. A switch with no attempts costs exactly its base cost.
        Otherwise the base cost is scaled by (1 + 2 * failure_rate), so
        a switch that has failed every attempt costs 3x its base.

        The 2.0x multiplier is a starting point, not a derived
        constant -- tune it from real handshake failure data and state
        that in the report rather than presenting it as calibrated.
        """
        h = self.history.get(dpid)
        n = (h["success"] + h["failed"]) if h else 0
        if n == 0:
            return self._base(dpid)
        return self._base(dpid) * (1.0 + 2.0 * h["failed"] / n)

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
    tracker = CapabilityTracker()

    dpid_a = "0000000000000003"
    dpid_b = "0000000000000004"

    assert abs(COST_FLOOR - 0.259) < 1e-9
    # Never-attempted dpid: default (floor) cost, neutral success rate.
    assert abs(tracker.cost(dpid_a) - COST_FLOOR) < 1e-9
    assert tracker.success_rate(dpid_a) == 0.5

    tracker.record(dpid_a, "success")
    tracker.record(dpid_a, "success")
    assert abs(tracker.cost(dpid_a) - COST_FLOOR) < 1e-9
    assert tracker.success_rate(dpid_a) == 1.0

    tracker.record(dpid_b, "failed")
    tracker.record(dpid_b, "failed")
    tracker.record(dpid_b, "success")
    # failure_rate = 2/3 -> cost = floor * (1 + 2*(2/3))
    cost_b = tracker.cost(dpid_b)
    assert abs(cost_b - COST_FLOOR * (1.0 + 2.0 * (2 / 3))) < 1e-9, cost_b
    assert abs(tracker.success_rate(dpid_b) - (1 / 3)) < 1e-9

    # Per-switch base cost is honored, and failures scale it.
    t2 = CapabilityTracker(base_costs={dpid_a: 0.5})
    assert t2.cost(dpid_a) == 0.5
    assert abs(t2.cost(dpid_b) - COST_FLOOR) < 1e-9      # not in dict -> default
    t2.record(dpid_a, "failed")
    assert abs(t2.cost(dpid_a) - 0.5 * 3.0) < 1e-9
    assert abs(t2.total_cost([dpid_a, dpid_b]) - (1.5 + COST_FLOOR)) < 1e-9

    # budget_costs: range is [0.259, 0.592], min/max switches hit the ends.
    classical = {"a": [10, 11, 10], "b": [10, 10, 10], "c": [10, 10, 10]}
    hybrid = {"a": [12, 12, 12], "b": [20, 20, 20], "c": [32, 32, 32]}
    bc = budget_costs(classical, hybrid)      # deltas: 2, 10, 22
    assert abs(bc["a"] - COST_FLOOR) < 1e-9
    assert abs(bc["c"] - (COST_FLOOR + 1 / 3)) < 1e-9   # 0.5923
    assert all(COST_FLOOR - 1e-9 <= v <= 0.5924 for v in bc.values())
    assert COST_FLOOR < bc["b"] < bc["c"]
    # degenerate: identical deltas -> everyone at the floor
    flat = budget_costs({"a": [1], "b": [1]}, {"a": [2], "b": [2]})
    assert all(abs(v - COST_FLOOR) < 1e-9 for v in flat.values())

    for bad in ({"default_cost": 0}, {"base_costs": {"x": 0}}):
        try:
            CapabilityTracker(**bad)
            raise AssertionError(f"expected ValueError for {bad}")
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

