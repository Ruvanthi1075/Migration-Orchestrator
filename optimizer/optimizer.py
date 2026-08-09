"""
optimizer.py
Feature 3: Greedy Migration Order Optimizer

At each round, tests every remaining Legacy link, measures how much
total_coverage() (Feature 2) would improve if that single link were
upgraded to Hybrid, and actually upgrades whichever link gives the
biggest improvement. Ties are broken using Feature 5's capability
track record.

This is a greedy algorithm over a submodular set function (weakest-
link coverage), which is why greedy selection is a defensible choice
here rather than an arbitrary heuristic (Nemhauser, Wolsey & Fisher,
1978): greedy is guaranteed to reach at least ~63% of the optimal
achievable coverage.
"""

import os
import sys

# network/ holds topology.py and coverage.py (Feature 1/2). optimizer/
# is a sibling folder, so make network/ importable without needing a
# package/__init__.py setup, matching this repo's flat-script style.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "network"))

from coverage import total_coverage
from capability_tracker import CapabilityTracker, link_device_type


def get_legacy_links(graph):
    """All links currently in the Legacy state, as (a, b) tuples."""
    return [
        (a, b) for a, b, data in graph.edges(data=True)
        if data["state"] == "Legacy"
    ]


def marginal_gain(graph, flows, link):
    """
    How much total_coverage() would improve if `link` were upgraded
    to Hybrid, without permanently changing the graph. Side-effect
    free: the link's state is always restored before returning, so
    Feature 3 can safely call this once per candidate per round.
    """
    a, b = link
    baseline = total_coverage(graph, flows)

    original_state = graph.edges[a, b]["state"]
    graph.edges[a, b]["state"] = "Hybrid"
    upgraded = total_coverage(graph, flows)
    graph.edges[a, b]["state"] = original_state  # restore

    return upgraded - baseline


def pick_best_link(graph, flows, tracker=None):
    """
    Returns (link, gain) for the single best Legacy link to migrate
    next: the one with the highest marginal_gain. Ties are broken by
    Feature 5's capability track record (higher success rate wins);
    if tracker is None the first candidate found at the best gain
    wins.

    Returns None if there are no Legacy links left.
    """
    candidates = get_legacy_links(graph)
    if not candidates:
        return None

    best_link = None
    best_gain = None
    best_rate = None

    for link in candidates:
        gain = marginal_gain(graph, flows, link)
        rate = tracker.link_success_rate(*link) if tracker else 0.5

        if best_gain is None or gain > best_gain:
            best_link, best_gain, best_rate = link, gain, rate
        elif gain == best_gain and rate > best_rate:
            # tie on coverage gain -> prefer the device type with the
            # better track record (Feature 5)
            best_link, best_gain, best_rate = link, gain, rate

    return best_link, best_gain


def run_greedy_migration(graph, flows, tracker=None, budget=None, verbose=True):
    """
    Repeatedly picks and migrates the single best Legacy link until
    either `budget` links have been migrated this call, or no Legacy
    links remain (whole network upgraded).

    tracker: a CapabilityTracker (Feature 5). If omitted, a fresh one
    is created. This beginner-scope build has no Feature 6 rollback
    yet, so every migration here is recorded as a success -- wire in
    real pass/fail once Feature 6's threshold check exists.

    Returns a list of (link, gain) tuples in the order they were
    migrated.
    """
    if tracker is None:
        tracker = CapabilityTracker()

    history = []
    round_num = 0

    while True:
        if budget is not None and round_num >= budget:
            break

        result = pick_best_link(graph, flows, tracker)
        if result is None:
            break  # every link already Hybrid

        link, gain = result
        a, b = link
        graph.edges[a, b]["state"] = "Hybrid"
        tracker.record_link_attempt(a, b, success=True)

        history.append((link, gain))
        round_num += 1

        if verbose:
            score = total_coverage(graph, flows)
            print(
                f"Round {round_num}: migrated {a}-{b} "
                f"(type={link_device_type(a, b)}, gain=+{gain}) "
                f"-> total coverage = {score}"
            )

    return history


if __name__ == "__main__":
    from topology import build_graph, PROJECT_TOPO
    from coverage import load_flows

    graph = build_graph(PROJECT_TOPO)
    flows_path = os.path.join(
        os.path.dirname(__file__), "..", "network", "flows_config.json"
    )
    flows = load_flows(flows_path)
    tracker = CapabilityTracker()

    print("Starting coverage:", total_coverage(graph, flows))
    assert total_coverage(graph, flows) == 0

    history = run_greedy_migration(graph, flows, tracker)

    final_score = total_coverage(graph, flows)
    print("\nFinal coverage:", final_score)
    # matches visualize.py Stage 2 (all links migrated -> total = 9)
    assert final_score == 9

    all_hybrid = all(
        data["state"] == "Hybrid" for _, _, data in graph.edges(data=True)
    )
    assert all_hybrid

    print("\nMigration order (%d links):" % len(history))
    for (a, b), gain in history:
        print(f"  {a}-{b}  gain=+{gain}")

    print("\nCapability tracker summary:")
    print(tracker.summary())

    print("\nAll self-checks passed.")
