"""
optimizer.py
Feature 3: Migration Order Optimizer

total_coverage() (Feature 2) only credits a flow once EVERY link on its
path is Hybrid -- a weakest-link / AND-of-links model. That makes
total_coverage a SUPERMODULAR set function over "which links are
Hybrid" (increasing, not diminishing, returns), not submodular:
upgrading one edge of a path is worth nothing until the last edge of
that same path is also upgraded. A single-link-at-a-time greedy score
is therefore flat at 0 right up until a path's final hop, and
systematically starves long/important paths in favor of short/
unimportant ones that pay off immediately. (The SDN migration paper we
discussed formalizes this identical all-links-in-a-group-or-nothing
objective as its Obj2/TE-flexibility case, proves it has bounded
supermodular degree D+ rather than submodularity, and gives the
Super-greedy algorithm below as the fix, with a provable approximation
ratio 1/(2(D+ + 1) + 1) -- weaker than the classic 1-1/e submodular
bound, but a real guarantee, unlike naive single-link greedy.)

This module keeps both:

  * marginal_gain / pick_best_link / run_greedy_migration
      Naive single-link-at-a-time greedy. No approximation guarantee
      on this objective -- kept only for comparison.

  * supermodular_degree / super_greedy_ratio_bound
      D+ for the current topology+flows, and the resulting Super-greedy
      approximation ratio.

  * run_super_greedy_migration
      Recommended optimizer. Each round it completes whichever flow's
      full set of remaining Legacy links scores best, migrating that
      whole batch together -- instead of scoring links one at a time,
      it acts on the complementary sets the objective actually needs.
      Falls back to partial-credit scoring (_pick_progress_link) when
      no full batch fits the remaining budget, so budget is never
      wasted on a 0-gain link.

      Batch scoring is gain / cost**cost_weight, NOT plain gain/cost --
      plain cost-benefit ratio (Objective A) can tie or even rank a
      much-more-important flow BELOW a cheaper, far-less-important one
      (e.g. importance=1000 needing 20 links ties in ratio with
      importance=100 needing 2 links -- 50 vs 50 -- despite a 10x
      difference in what's actually protected). The default,
      cost_weight=0.5 (Objective C), still discounts for cost but far
      less aggressively, so importance dominates cost differences of a
      few links. Pass cost_weight=1.0 for pure efficiency (Objective A)
      or 0.0 to always prioritize raw importance regardless of cost
      (Objective B) -- see pick_best_batch's docstring for the exact
      numbers on this tradeoff.

  * programmable_coverage
      Obj1-style secondary metric ("at least one hop on the path is
      Hybrid"). This IS genuinely submodular (classic weighted
      coverage), so it's reported for visibility -- but it is NOT what
      run_super_greedy_migration optimizes, since "one hop protected"
      is not what weakest-link security means.
"""

import os
import sys

# network/ holds topology.py and coverage.py (Feature 1/2). optimizer/
# is a sibling folder, so make network/ importable without needing a
# package/__init__.py setup, matching this repo's flat-script style.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "network"))

from coverage import total_coverage, get_path_links, flow_weight
from capability_tracker import CapabilityTracker, link_device_type


# ---------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------

def get_legacy_links(graph):
    """All links currently in the Legacy state, as (a, b) tuples."""
    return [
        (a, b) for a, b, data in graph.edges(data=True)
        if data["state"] == "Legacy"
    ]


def marginal_gain(graph, flows, link):
    """
    How much total_coverage() would improve if `link` alone were
    upgraded to Hybrid. Side-effect free: state is restored before
    returning. Flat at 0 for any link that isn't the LAST Legacy link
    remaining on some flow's path (see module docstring).
    """
    a, b = link
    baseline = total_coverage(graph, flows)

    original_state = graph.edges[a, b]["state"]
    graph.edges[a, b]["state"] = "Hybrid"
    upgraded = total_coverage(graph, flows)
    graph.edges[a, b]["state"] = original_state  # restore

    return upgraded - baseline


def joint_marginal_gain(graph, flows, links):
    """
    Same as marginal_gain, but for a whole SET of links flipped to
    Hybrid together. A batch of links can be worth far more together
    than the sum of their individual marginal_gain() values, because
    of the AND structure in total_coverage.
    """
    if not links:
        return 0
    baseline = total_coverage(graph, flows)

    originals = {}
    for a, b in links:
        originals[(a, b)] = graph.edges[a, b]["state"]
        graph.edges[a, b]["state"] = "Hybrid"

    upgraded = total_coverage(graph, flows)

    for (a, b), state in originals.items():
        graph.edges[a, b]["state"] = state  # restore

    return upgraded - baseline


def programmable_coverage(graph, flows):
    """
    Obj1-style secondary metric: weight(f) is earned if AT LEAST ONE
    link on flow f's path is Hybrid, rather than requiring the whole
    path. Genuinely submodular (classic weighted coverage) -- reported
    for visibility only, never used to drive migration decisions.
    """
    score = 0
    for flow in flows:
        links = get_path_links(graph, flow["source"], flow["destination"])
        if any(graph.edges[a, b]["state"] == "Hybrid" for a, b in links):
            score += flow.get("weight", flow_weight(flow))
    return score


# ---------------------------------------------------------------------
# Naive single-link greedy (kept for comparison -- no guarantee)
# ---------------------------------------------------------------------

def pick_best_link(graph, flows, tracker=None):
    """
    Returns (link, gain) for the single best Legacy link by
    marginal_gain, ties broken by Feature 5's success rate. No
    approximation guarantee on this objective -- see module docstring.
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
            best_link, best_gain, best_rate = link, gain, rate

    return best_link, best_gain


def run_greedy_migration(graph, flows, tracker=None, budget=None, verbose=True):
    """
    Naive one-link-per-round greedy. Kept for comparison against
    run_super_greedy_migration -- prefer the latter in production.
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
            break

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


# ---------------------------------------------------------------------
# Supermodular degree / approximation ratio
# ---------------------------------------------------------------------

def supermodular_degree(graph, flows):
    """
    D+ for this topology+flow set: the largest number of OTHER links
    any single link shares a flow-path with. A link on a k-hop path
    has up to (k-1) companions on that path; D+ is the max of that
    over every flow. Bounds how "entangled" the AND-dependencies get.
    """
    max_degree = 0
    for flow in flows:
        links = get_path_links(graph, flow["source"], flow["destination"])
        degree = max(len(links) - 1, 0)
        max_degree = max(max_degree, degree)
    return max_degree


def super_greedy_ratio_bound(graph, flows):
    """
    Approximation ratio for Super-greedy on a function with bounded
    supermodular degree D+, under a cardinality (matroid) budget
    constraint -- 1 / (2*(D+ + 1) + 1). Applies as long as every
    migration is treated as unit cost (uniform `budget` spend).
    """
    d_plus = supermodular_degree(graph, flows)
    return 1 / (2 * (d_plus + 1) + 1)


# ---------------------------------------------------------------------
# Super-greedy: batches a link with its path-companions
# ---------------------------------------------------------------------

def candidate_batches(graph, flows):
    """
    One candidate batch per flow that still has Legacy links on its
    path: the FULL set of that flow's remaining Legacy links (its
    key-link completion set). Migrating all of them together is what
    actually earns weight(f) under the weakest-link/AND objective,
    instead of scoring links one at a time.
    """
    batches = []
    seen = set()
    for flow in flows:
        links = get_path_links(graph, flow["source"], flow["destination"])
        legacy = tuple(sorted(
            (a, b) for a, b in links if graph.edges[a, b]["state"] == "Legacy"
        ))
        if legacy and legacy not in seen:
            seen.add(legacy)
            batches.append(legacy)
    return batches


def pick_best_batch(graph, flows, remaining_budget, tracker=None, cost_weight=0.5):
    """
    Among all flows' remaining-Legacy-link batches that fit within
    remaining_budget, pick the one with the highest score, where:

        score(batch) = gain / (cost ** cost_weight)      cost = len(batch)

    cost_weight is NOT a free knob to tune blindly -- it picks between
    three explicit objectives, because plain gain/cost (cost_weight=1)
    can rank a much-more-important flow BELOW a cheaper, far-less-
    important one, or tie with it outright:

      cost_weight=1.0  Objective A -- pure efficiency (coverage gained
                        per link spent; matches the paper's Algorithm 4
                        cost-benefit ratio). Importance=1000/cost=20
                        (ratio 50) ties with importance=100/cost=2
                        (ratio 50) -- a 10x-more-important flow gets NO
                        priority over one worth a tenth as much, and a
                        slightly cheaper low-importance flow can beat
                        it outright. Can starve important-but-long
                        flows indefinitely under a recurring budget.
      cost_weight=0.0  Objective B -- pure security value. Always
                        prefers the batch with higher total importance
                        regardless of cost, as long as it's affordable
                        this round. Never starves a high-importance
                        flow for a cheap low-importance one, but can
                        spend budget inefficiently.
      cost_weight=0.5  Objective C (default) -- balances both: cost
                        still discounts a batch's score, but far less
                        aggressively than full division, so a flow an
                        order of magnitude more important still wins
                        even if it costs several times more links.
                        importance=1000/cost=20 -> 223.6 vs.
                        importance=100/cost=2 -> 70.7: the important
                        flow wins clearly instead of tying.

    Ties broken by Feature 5's tracker, averaged over the batch's links.

    Returns (batch, gain) for the winning batch, or None if no flow's
    remaining links fit within remaining_budget.
    """
    batches = [b for b in candidate_batches(graph, flows) if len(b) <= remaining_budget]
    if not batches:
        return None

    best_batch = None
    best_score = None
    best_gain = None
    best_rate = None

    for batch in batches:
        gain = joint_marginal_gain(graph, flows, batch)
        score = gain / (len(batch) ** cost_weight)
        rate = (
            sum(tracker.link_success_rate(*link) for link in batch) / len(batch)
            if tracker else 0.5
        )

        if best_score is None or score > best_score:
            best_batch, best_score, best_gain, best_rate = batch, score, gain, rate
        elif score == best_score and rate > best_rate:
            best_batch, best_score, best_gain, best_rate = batch, score, gain, rate

    return best_batch, best_gain


def _pick_progress_link(graph, flows, tracker=None):
    """
    Fallback scorer for when no flow's full batch fits the remaining
    budget: sum weight(f)/remaining_legacy_count(f) over every flow a
    Legacy link still blocks. Gives partial credit for progress
    instead of marginal_gain's hard 0 until a path's last hop, so
    leftover budget still moves the network toward completing its
    highest-value paths.

    Uses flow["weight"] (precomputed by coverage.load_flows) when
    present, otherwise falls back to flow_weight(flow) -- this way it
    works for both the legacy categorical "importance" format and the
    Feature 4.2 multi-factor SAW format, matching every other scoring
    function in this module instead of hard-coding IMPORTANCE_WEIGHT.
    """
    legacy = get_legacy_links(graph)
    if not legacy:
        return None

    scores = {}
    for flow in flows:
        links = get_path_links(graph, flow["source"], flow["destination"])
        remaining = [(a, b) for a, b in links if graph.edges[a, b]["state"] == "Legacy"]
        if not remaining:
            continue
        weight = flow["weight"] if "weight" in flow else flow_weight(flow)
        share = weight / len(remaining)
        for a, b in remaining:
            scores[(a, b)] = scores.get((a, b), 0) + share

    best_link = max(legacy, key=lambda l: scores.get(l, 0))
    return best_link, scores.get(best_link, 0)


def run_super_greedy_migration(graph, flows, tracker=None, budget=None, verbose=True, cost_weight=0.5):
    """
    Recommended optimizer. Each round, completes the single flow-path
    whose remaining Legacy links score best (see pick_best_batch's
    cost_weight -- default is Objective C, balancing efficiency against
    raw importance; pass cost_weight=1.0 for pure efficiency or 0.0 for
    pure importance-first if this deployment needs a different one),
    migrating that whole batch at once -- fixing naive greedy's myopia
    toward long/important paths. Comes with a provable approximation
    ratio (see super_greedy_ratio_bound) instead of none.

    If remaining budget can't fit ANY flow's full completion batch,
    spends the remainder via _pick_progress_link (partial progress
    toward the nearest-to-done path) one link at a time, rather than
    leaving budget unused or wasting it on an unrelated low-value link.

    history entries are (links_migrated_this_round, gain) tuples --
    links_migrated_this_round is a list of one or more (a, b) tuples.
    """
    if tracker is None:
        tracker = CapabilityTracker()

    history = []
    links_used = 0

    while True:
        remaining = None if budget is None else budget - links_used
        if remaining is not None and remaining <= 0:
            break

        search_budget = remaining if remaining is not None else float("inf")
        result = pick_best_batch(graph, flows, search_budget, tracker, cost_weight=cost_weight)

        if result is None:
            if not get_legacy_links(graph):
                break  # every link already Hybrid
            fallback = _pick_progress_link(graph, flows, tracker)
            if fallback is None:
                break
            link, score = fallback
            a, b = link
            graph.edges[a, b]["state"] = "Hybrid"
            tracker.record_link_attempt(a, b, success=True)
            history.append(([link], score))
            links_used += 1
            if verbose:
                cov = total_coverage(graph, flows)
                print(
                    f"Round (fallback): migrated {a}-{b} "
                    f"(type={link_device_type(a, b)}, progress_score={score:.2f}) "
                    f"-> total coverage = {cov}"
                )
            continue

        batch, gain = result
        for a, b in batch:
            graph.edges[a, b]["state"] = "Hybrid"
            tracker.record_link_attempt(a, b, success=True)
        history.append((list(batch), gain))
        links_used += len(batch)

        if verbose:
            cov = total_coverage(graph, flows)
            names = ", ".join(f"{a}-{b}" for a, b in batch)
            print(f"Round: migrated batch [{names}] (gain=+{gain}) -> total coverage = {cov}")

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
    print("Starting programmable coverage (Obj1):", programmable_coverage(graph, flows))
    assert total_coverage(graph, flows) == 0

    d_plus = supermodular_degree(graph, flows)
    ratio = super_greedy_ratio_bound(graph, flows)
    print(f"\nSupermodular degree D+ = {d_plus}")
    print(f"Super-greedy approximation ratio: 1/(2*({d_plus}+1)+1) = {ratio:.3f}")

    history = run_super_greedy_migration(graph, flows, tracker)

    final_score = total_coverage(graph, flows)
    print("\nFinal coverage:", final_score)
    # matches visualize.py Stage 2 (all links migrated -> total = 9)
    assert final_score == 9

    all_hybrid = all(
        data["state"] == "Hybrid" for _, _, data in graph.edges(data=True)
    )
    assert all_hybrid

    print("\nMigration order (%d rounds):" % len(history))
    for batch, gain in history:
        names = ", ".join(f"{a}-{b}" for a, b in batch)
        print(f"  [{names}]  gain=+{gain}")

    print("\nCapability tracker summary:")
    print(tracker.summary())

    print("\nAll self-checks passed.")
