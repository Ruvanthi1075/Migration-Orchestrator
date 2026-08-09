"""
capability_tracker.py
Feature 5: Simple Capability Tracking

Tracks per-device-type migration success/failure counts, and exposes
a simple tie-breaker score for Feature 3's greedy optimizer to use
when two candidate links have equal coverage gain.

No probability distributions, no sampling -- just running counts,
per the spec.
"""

# Maps individual switch names to their network tier. Host nodes
# ("h1".."h8") are classified as "host" automatically by node_tier(),
# so they don't need an entry here. Matches the tiers described in
# network/topo.py (core / agg / edge).
SWITCH_TIER = {
    "s0": "core",
    "s1": "agg",
    "s2": "agg",
    "s3": "edge",
    "s4": "edge",
    "s5": "edge",
    "s6": "edge",
}


def node_tier(node):
    """Classify a single node (switch or host) into a tier label."""
    if node in SWITCH_TIER:
        return SWITCH_TIER[node]
    if node.startswith("h"):
        return "host"
    return "unknown"


def link_device_type(a, b):
    """
    Classify a link by the tiers of the two devices it connects, e.g.
    ("h1", "s3") -> "edge-host", ("s1", "s2") -> "agg-agg".
    Sorted alphabetically so the label is order-independent (a,b vs
    b,a always produce the same type string).
    """
    tiers = sorted([node_tier(a), node_tier(b)])
    return "-".join(tiers)


class CapabilityTracker:
    """
    Dict-based success/failure log, keyed by device type (see
    link_device_type above). Feature 3 consults this only as a
    tie-breaker when two links have equal coverage gain -- it never
    overrides the actual coverage-based ranking.
    """

    def __init__(self):
        # device_type -> {"success": int, "failure": int}
        self._log = {}

    def record_attempt(self, device_type, success):
        """Update the running count after a real migration attempt."""
        entry = self._log.setdefault(device_type, {"success": 0, "failure": 0})
        if success:
            entry["success"] += 1
        else:
            entry["failure"] += 1

    def record_link_attempt(self, a, b, success):
        """Convenience wrapper: classify the link, then record."""
        self.record_attempt(link_device_type(a, b), success)

    def success_rate(self, device_type):
        """
        Score in [0, 1] used for tie-breaking. A device type that has
        never been attempted returns 0.5 -- a neutral score, not an
        assumption of failure (per spec: "treat it as a normal
        candidate rather than assuming failure").
        """
        entry = self._log.get(device_type)
        if entry is None:
            return 0.5
        attempts = entry["success"] + entry["failure"]
        if attempts == 0:
            return 0.5
        return entry["success"] / attempts

    def link_success_rate(self, a, b):
        """Convenience wrapper: classify the link, then score it."""
        return self.success_rate(link_device_type(a, b))

    def summary(self):
        """Human-readable table, useful for the demo/report."""
        if not self._log:
            return "  (no attempts recorded yet)"
        lines = []
        for device_type, entry in sorted(self._log.items()):
            attempts = entry["success"] + entry["failure"]
            rate = entry["success"] / attempts if attempts else 0.0
            lines.append(
                f"  {device_type:>10}  success={entry['success']:<3} "
                f"failure={entry['failure']:<3} rate={rate:.2f}"
            )
        return "\n".join(lines)


if __name__ == "__main__":
    # self-test
    tracker = CapabilityTracker()

    assert link_device_type("h1", "s3") == "edge-host"
    assert link_device_type("s3", "h1") == "edge-host"  # order-independent
    assert link_device_type("s1", "s2") == "agg-agg"
    assert link_device_type("s1", "s0") == "agg-core"

    # untried device type -> neutral score, not zero
    assert tracker.success_rate("edge-host") == 0.5

    tracker.record_link_attempt("h1", "s3", success=True)
    tracker.record_link_attempt("h3", "s4", success=True)
    tracker.record_link_attempt("h5", "s5", success=False)
    # 2 success, 1 failure -> 0.666...
    rate = tracker.success_rate("edge-host")
    assert 0.66 < rate < 0.67, rate

    print("All self-checks passed.\n")
    print("Capability summary:")
    print(tracker.summary())
