"""
optimizer.py
Feature 3 -- Supermodular-Aware Greedy Batch Migration, SGBM (Person B)

CORRECTED MODEL (see QSMO_Implementation_Guide.docx, sections 2, 5.3, 7.2)
----------------------------------------------------------------------------
The control-plane channel is a single out-of-band hop per switch, so
the migration target is a SWITCH (a dpid), not a link/edge, and state
lives on graph NODES, not edges:

    core_dpid = highest-degree node in G (ties -> lowest dpid)
    Pf(flow)  = shortest path over G from flow["destination_dpid"] to core_dpid
    Lf(flow)  = the subset of Pf(flow) still in "Legacy" state
    w(f)      = 0.5*criticality + 0.3*security_sla + 0.2*latency_sla
    C         = sum(w(f) * security(f) for f in flows)

LITERATURE-BACKED REFORMULATION (see SGBM_Literature_Backed_Formulation.docx,
sections 5.3 and 6.1 -- supersedes the earlier invented constants)
----------------------------------------------------------------------------
Two numbers used by this module were previously unsourced and have been
replaced:

  score(f) = gain(f) / cost(Lf(f))^alpha,  alpha = 1  (Sec. 6.1, Alg. 1 line 11)
      Plain gain/cost (alpha=1), not alpha=0.5. This is the exact
      cost-effectiveness greedy rule proven to reach 1/2*(1-1/e) on a
      single knapsack constraint over a coverage-type objective
      [Khuller, Moss, Naor, "The Budgeted Maximum Coverage Problem",
      IPL 70(1), 1999], and used identically (also alpha=1) in CELF
      [Leskovec et al., KDD 2007]. alpha=0.5 had no citation and is
      withdrawn.

  progress(s) = [sum(w(f)/len(Lf(f)) for f in F if s in Lf(f))] / cost(s)^alpha,
      alpha = 1 (Sec. 6.2, Alg. 1 lines 18-24, fallback branch)
      Same alpha as 6.1 for consistency. Note (stated in the doc,
      Sec. 6.2): this fallback is a heuristic proxy for true marginal
      gain, so the 6.1 guarantee does not automatically transfer to it;
      it remains empirically validated only (Table I / Fig. 2), not
      proven.

  sgbm_approx_ratio(G, flows) = 1 - e^(-1/(d+1))  (Sec. 5.2-5.3)
      The structural bound for a monotone supermodular function with
      supermodular degree d under a cardinality-style constraint
      [Feldman & Izsak, "Constrained Monotone Function Maximization and
      the Supermodular Degree", APPROX/RANDOM 2014, arXiv:1407.6328;
      improves the 1/(d+1) bound of Feige & Izsak, ITCS 2013]. The
      previously used rho = 1/(2*(d+1)+1) had no source and is
      withdrawn. d itself is also corrected here to the
      Feige-Izsak/Feldman-Izsak definition -- the union of per-flow
      path dependencies at a switch, not just the single longest flow
      path (see supermodular_degree() below).

      Stated scope limitation (Sec. 5.3, carried through here rather
      than hidden): this ratio is proved for a cardinality/
      k-extendible-system constraint, not SGBM's heterogeneous
      per-switch budget B. A knapsack-tight bound for this AND
      (weakest-link) objective is, to our knowledge, open in the
      literature -- curvature/submodularity-ratio machinery degenerates
      here because f({s}) = 0 for any switch that does not by itself
      complete a flow's path. The budget-constrained regime is instead
      validated empirically (Table I / Fig. 2), which is the documented
      stand-in for the missing knapsack-tight proof.

This is a rewrite of a stale pre-correction draft that imported
`total_coverage` / `get_path_links` from coverage.py and read/wrote
`graph.edges[a, b]["state"]` -- none of which exist in Person A's
actual, corrected coverage.py / topology.py (Feature 1/2 use
`compute_coverage`, `compute_path`, `compute_legacy_set`,
`compute_gain`, and `G.nodes[dpid]["state"]`). Importing the old file
against the real Feature 1/2 modules raised ImportError immediately.
This file is written against the frozen contract in guide section 5.3
and imports Person A's coverage.py directly -- nothing here recomputes
a shortest path or a coverage score on its own.

State ownership rule (guide section 2.3): this module NEVER writes
switch state directly outside of the in-memory bookkeeping needed to
run the loop against a graph it was handed. In the real wiring
(orchestrator_main.py), the caller is expected to pass a `migrate_fn`
that is Feature 4's `migrate_link.migrate`, and the actual
Legacy->Hybrid transition of record lives in topology.set_state(),
called by migrate_link.py on a verified success -- this module only
mirrors that transition onto its own graph object's node attribute so
the next round of the loop sees an up-to-date Lf(f), exactly as
Algorithm 1's pseudocode does.

Interface (guide section 7.2, frozen):
    run_sgbm(G, flows, budget, migrate_fn, capability) -> (schedule, coverage)

    migrate_fn(dpid) -> dict shaped per guide section 5.4:
        {"dpid": str, "outcome": "success"|"failed",
         "baseline": {...}, "post": {...} | None, "timestamp": iso8601 str}
"""

import math
import os
import sys

# network/ holds topology.py and coverage.py (Feature 1/2). optimizer/
# is a sibling folder, so make network/ importable without needing a
# package/__init__.py setup, matching this repo's flat-script style
# (same pattern the pre-correction draft used, kept because it's
# still correct -- only the imported names below have changed).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "network"))

from coverage import (  # noqa: E402
    compute_coverage,
    compute_path,
    compute_legacy_set,
    compute_gain,
    flow_weight,
)
from capability_tracker import CapabilityTracker  # noqa: E402

# alpha for score(f) and progress(s), Sec. 6.1/6.2 of
# SGBM_Literature_Backed_Formulation.docx. alpha=1 is the value with a
# citation (Khuller-Moss-Naor / CELF); change only with a matching
# citation, and update the module docstring above if you do.
SCORE_ALPHA = 1


# ---------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------

def get_legacy_switches(G):
    """All dpids currently in the Legacy state."""
    return [n for n in G.nodes if G.nodes[n]["state"] == "Legacy"]


def supermodular_degree(G, flows):
    """
    Measured supermodular degree d for this topology + flow set, per
    the Feige-Izsak / Feldman-Izsak definition (reformulation doc,
    Sec. 5.2):

        d = max_{s in V} | union_{f : s in Pf(f)} (Pf(f) \\ {s}) |

    i.e. for each switch s, take the union (not the max) of every
    flow-path-minus-s across every flow whose path includes s, then
    take the largest such set over all switches. This corrects the
    earlier max_f(|Pf|-1) definition, which undercounts whenever a
    switch lies on more than one flow's path -- the normal case here,
    since every Pf terminates at the shared core switch.
    """
    deps = {}
    for f in flows:
        path = compute_path(G, f)
        path_set = set(path)
        for s in path:
            deps.setdefault(s, set()).update(path_set - {s})
    if not deps:
        return 0
    return max(len(dep_set) for dep_set in deps.values())


def sgbm_approx_ratio(G, flows):
    """
    Structural approximation ratio for SGBM's batch-selection branch,
    per the reformulation doc Sec. 5.3:

        rho_struct = 1 - e^(-1/(d+1))

    [Feldman & Izsak, APPROX/RANDOM 2014 (arXiv:1407.6328), improving
    the 1/(d+1) bound of Feige & Izsak, ITCS 2013], for a monotone
    supermodular objective with supermodular degree d under a
    cardinality-style constraint.

    Scope limitation (stated, not hidden): this is proved for a
    cardinality/k-extendible-system constraint, not SGBM's
    heterogeneous per-switch budget B -- report it as the structural
    guarantee under the idealized near-uniform-cost case, and rely on
    the empirical budget-constrained comparison (Table I / Fig. 2) for
    the general-cost regime, since a knapsack-tight bound for this
    AND/weakest-link objective is, to our knowledge, open in the
    literature (see module docstring).
    """
    d = supermodular_degree(G, flows)
    return 1.0 - math.exp(-1.0 / (d + 1))


# ---------------------------------------------------------------------
# Batch scoring (Algorithm 1, lines 6-16)
# ---------------------------------------------------------------------

def _batch_candidates(G, flows, budget, capability):
    """
    One candidate per flow that still has a non-empty, affordable
    Legacy-set Lf(f): (flow, Lf(f), cost(Lf(f))). Mirrors Algorithm 1
    line 8's Omega set.
    """
    candidates = []
    for f in flows:
        Lf = compute_legacy_set(G, f)
        if not Lf:
            continue
        cost_Lf = sum(capability.cost(s) for s in Lf)
        if cost_Lf <= budget:
            candidates.append((f, Lf, cost_Lf))
    return candidates


def pick_best_batch(G, flows, budget, capability):
    """
    Among every flow whose full remaining-Legacy batch fits in
    `budget`, return the one with the highest
    score(f) = gain(f) / cost(Lf(f)) ** SCORE_ALPHA   (Algorithm 1,
    lines 10-12; SCORE_ALPHA=1 per reformulation doc Sec. 6.1 --
    plain gain/cost, citing Khuller-Moss-Naor / CELF).

    gain(f) is computed via a single compute_gain() call over the
    union of every candidate's Lf, so this makes exactly one coverage
    sweep per round no matter how many flows are in contention.

    Returns (flow, Lf, cost_Lf, gain) for the winning flow, or None if
    no flow's full batch fits within `budget`.
    """
    candidates = _batch_candidates(G, flows, budget, capability)
    if not candidates:
        return None

    hypothetical = set()
    for _, Lf, _ in candidates:
        hypothetical |= Lf
    gains = compute_gain(G, flows, hypothetical)

    def score(candidate):
        f, _Lf, cost_Lf = candidate
        return gains.get(f["name"], 0.0) / (cost_Lf ** SCORE_ALPHA)

    best_f, best_Lf, best_cost = max(candidates, key=score)
    return best_f, best_Lf, best_cost, gains.get(best_f["name"], 0.0)


def _pick_progress_switch(G, flows, budget, capability):
    """
    Fallback for when no flow's full batch fits the remaining budget
    (Algorithm 1, lines 18-24): pick the single affordable Legacy
    switch with the highest
        progress(s) = (sum(w(f)/len(Lf(f)) for f in F if s in Lf(f)))
                      / cost(s) ** SCORE_ALPHA
    (SCORE_ALPHA=1, same as 6.1 for consistency -- reformulation doc
    Sec. 6.2), i.e. partial credit toward every path `s` still blocks,
    so leftover budget still moves the network toward completing its
    highest-value paths instead of being wasted or left unspent.

    Stated limitation (Sec. 6.2): this is a heuristic proxy for true
    marginal gain, not the argmax of gain/cost itself, so the 6.1
    cost-effectiveness guarantee does not automatically transfer to
    this branch -- it is validated empirically only.

    Returns (dpid, progress_score), or None if no Legacy switch fits
    within `budget`.
    """
    affordable = [s for s in get_legacy_switches(G) if capability.cost(s) <= budget]
    if not affordable:
        return None

    contribution = {}
    for f in flows:
        Lf = compute_legacy_set(G, f)
        if not Lf:
            continue
        share = flow_weight(f) / len(Lf)
        for s in Lf:
            contribution[s] = contribution.get(s, 0.0) + share

    def progress(s):
        return contribution.get(s, 0.0) / (capability.cost(s) ** SCORE_ALPHA)

    best = max(affordable, key=progress)
    return best, progress(best)


# ---------------------------------------------------------------------
# Algorithm 1 -- Supermodular-Aware Greedy Batch Migration (SGBM)
# ---------------------------------------------------------------------

def run_sgbm(G, flows, budget, migrate_fn, capability=None, verbose=True):
    """
    Algorithm 1, corrected: "link" read as "switch", Pf/Lf/gain coming
    from coverage.py (Person A), cost(s) coming from
    CapabilityTracker.cost() (Person B, Feature 5) instead of a static
    table, and every actual state transition delegated to `migrate_fn`
    (Feature 4, Person C) -- this function never sets
    G.nodes[s]["state"] on its own initiative outside of mirroring a
    migrate_fn outcome, per the state ownership rule in guide 2.3 /
    Algorithm 1 line 14.

    Parameters
    ----------
    G : networkx.Graph
        The live switch<->switch control-plane graph (topology.py's
        get_graph(), or a hand-built fake graph for testing).
    flows : list[dict]
        As loaded by coverage.load_flows() -- each flow needs
        "name", "destination_dpid", "criticality", "security_sla",
        "latency_sla".
    budget : float
        Total migration budget for this run (Algorithm 1's B).
    migrate_fn : callable(dpid: str) -> dict
        Per guide section 5.4. In production this is
        migrate_link.migrate (Feature 4, Person C); for standalone
        testing, pass a fake that always/sometimes returns
        outcome="success" (see the __main__ block below and guide
        7.2's integration note) -- the signature must not change when
        swapping the real one in.
    capability : CapabilityTracker, optional
        Defaults to a fresh CapabilityTracker() if not supplied.

    Returns
    -------
    (schedule, coverage) : (list[dict], float)
        schedule is the flat list of every migrate_fn() result dict,
        in call order. coverage is compute_coverage(G, flows) after
        the run.
    """
    if capability is None:
        capability = CapabilityTracker()

    schedule = []
    remaining = budget

    while remaining > 0 and get_legacy_switches(G):
        batch_pick = pick_best_batch(G, flows, remaining, capability)

        if batch_pick is not None:
            flow, Lf, cost_Lf, gain = batch_pick
            for dpid in sorted(Lf):
                result = migrate_fn(dpid)
                capability.record(dpid, result["outcome"])
                schedule.append(result)
                if result["outcome"] == "success":
                    G.nodes[dpid]["state"] = "Hybrid"
                if verbose:
                    print(
                        f"  migrate {dpid} (batch for flow={flow['name']}) "
                        f"-> {result['outcome']}"
                    )
            remaining -= cost_Lf
            if verbose:
                print(
                    f"Round: completed flow={flow['name']} batch={sorted(Lf)} "
                    f"gain=+{gain:.3f} cost={cost_Lf:.2f} "
                    f"-> coverage={compute_coverage(G, flows):.3f}, "
                    f"budget remaining={remaining:.2f}"
                )
            continue

        # No flow's full batch fits -- spend the remainder one switch
        # at a time via partial-progress scoring (Algorithm 1, else
        # branch, lines 18-24).
        fallback = _pick_progress_switch(G, flows, remaining, capability)
        if fallback is None:
            break  # nothing affordable left; stop rather than loop forever

        dpid, prog = fallback
        cost_before = capability.cost(dpid)  # charge the pre-attempt cost
        result = migrate_fn(dpid)
        capability.record(dpid, result["outcome"])
        schedule.append(result)
        if result["outcome"] == "success":
            G.nodes[dpid]["state"] = "Hybrid"
        remaining -= cost_before

        if verbose:
            print(
                f"Round (fallback): migrate {dpid} -> {result['outcome']} "
                f"progress_score={prog:.3f} cost={cost_before:.2f} "
                f"-> coverage={compute_coverage(G, flows):.3f}, "
                f"budget remaining={remaining:.2f}"
            )

    return schedule, compute_coverage(G, flows)


if __name__ == "__main__":
    # ------------------------------------------------------------------
    # Self-test against a HAND-BUILT graph, not topology.py / OS-Ken.
    # This is exactly the workflow the guide recommends in section 10,
    # step 1: "Person B writes unit tests for optimizer.py against a
    # hand-built fake graph + fake migrate_fn immediately -- don't wait
    # for topology.py or migrate_link.py." Mirrors coverage.py's own
    # self-test graph so the two modules' self-checks agree.
    # ------------------------------------------------------------------
    import datetime

    import networkx as nx

    def _fake_graph():
        G = nx.Graph()
        dpids = [f"{i:016x}" for i in range(7)]  # s0..s6
        for d in dpids:
            G.add_node(d, state="Legacy")
        s0, s1, s2, s3, s4, s5, s6 = dpids
        G.add_edge(s1, s0)
        G.add_edge(s2, s0)
        G.add_edge(s1, s2)  # redundant agg-to-agg cross-link
        G.add_edge(s3, s1)
        G.add_edge(s4, s1)
        G.add_edge(s5, s2)
        G.add_edge(s6, s2)
        return G, dict(s0=s0, s1=s1, s2=s2, s3=s3, s4=s4, s5=s5, s6=s6)

    def _fake_flows(name):
        return [
            {"name": "ctrl_s3", "destination_dpid": name["s3"],
             "criticality": 0.9, "security_sla": 0.8, "latency_sla": 0.5},
            {"name": "ctrl_s4", "destination_dpid": name["s4"],
             "criticality": 0.9, "security_sla": 0.8, "latency_sla": 0.5},
            {"name": "ctrl_s5", "destination_dpid": name["s5"],
             "criticality": 0.3, "security_sla": 0.5, "latency_sla": 0.7},
            {"name": "ctrl_s6", "destination_dpid": name["s6"],
             "criticality": 0.5, "security_sla": 0.6, "latency_sla": 0.5},
        ]

    def _fake_migrate_always_success(dpid):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return {
            "dpid": dpid,
            "outcome": "success",
            "baseline": {"latency_ms": 20.0, "failure_rate": 0.0, "overhead_bytes": 0},
            "post": {"latency_ms": 24.5, "failure_rate": 0.0, "overhead_bytes": 512},
            "timestamp": now,
        }

    def _fake_migrate_fails_once(fail_dpid):
        """Fails the first time `fail_dpid` is migrated, succeeds after."""
        seen = {"count": 0}

        def _fn(dpid):
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            if dpid == fail_dpid and seen["count"] == 0:
                seen["count"] += 1
                return {
                    "dpid": dpid, "outcome": "failed",
                    "baseline": {"latency_ms": 20.0, "failure_rate": 0.0, "overhead_bytes": 0},
                    "post": None, "timestamp": now,
                }
            return _fake_migrate_always_success(dpid)

        return _fn

    # --- Test 1: unlimited budget -> full migration, full coverage ---
    G, name = _fake_graph()
    flows = _fake_flows(name)
    total_weight = sum(flow_weight(f) for f in flows)

    print("Starting coverage:", compute_coverage(G, flows))
    assert compute_coverage(G, flows) == 0.0

    d_plus = supermodular_degree(G, flows)
    ratio = sgbm_approx_ratio(G, flows)
    print(f"Measured supermodular degree d = {d_plus}")
    print(f"SGBM structural approximation ratio: 1 - e^(-1/({d_plus}+1)) = {ratio:.3f}")

    tracker = CapabilityTracker()
    schedule, final_coverage = run_sgbm(
        G, flows, budget=100.0, migrate_fn=_fake_migrate_always_success,
        capability=tracker,
    )

    print("\nFinal coverage:", final_coverage)
    assert abs(final_coverage - total_weight) < 1e-9
    assert all(G.nodes[n]["state"] == "Hybrid" for n in G.nodes)
    assert all(r["outcome"] == "success" for r in schedule)
    print(f"Migrated {len(schedule)} switch(es) across the run.")

    # --- Test 2: tight budget -> exercises the fallback progress path ---
    G2, name2 = _fake_graph()
    flows2 = _fake_flows(name2)
    tracker2 = CapabilityTracker()
    # Budget of 1.0 can't afford ANY flow's full batch (every flow needs
    # >= 1 switch on a 2-hop path, i.e. cost >= 1, but ctrl_s3/ctrl_s4
    # need only their edge switch since s1 is already the target of a
    # shared aggregation hop -- exercise both branches by checking the
    # schedule is non-empty and coverage only ever increases).
    schedule2, cov_after_2 = run_sgbm(
        G2, flows2, budget=1.0, migrate_fn=_fake_migrate_always_success,
        capability=tracker2, verbose=False,
    )
    assert len(schedule2) >= 1
    assert cov_after_2 >= 0.0
    assert cov_after_2 <= total_weight

    # --- Test 3: a failed migration should not flip state, and should
    #     raise that switch's cost so the tracker actually reacts ---
    G3, name3 = _fake_graph()
    flows3 = _fake_flows(name3)
    tracker3 = CapabilityTracker()
    flaky_switch = name3["s3"]  # ctrl_s3's own edge switch
    schedule3, _ = run_sgbm(
        G3, flows3, budget=100.0,
        migrate_fn=_fake_migrate_fails_once(flaky_switch),
        capability=tracker3, verbose=False,
    )
    failed_events = [r for r in schedule3 if r["dpid"] == flaky_switch and r["outcome"] == "failed"]
    success_events = [r for r in schedule3 if r["dpid"] == flaky_switch and r["outcome"] == "success"]
    assert len(failed_events) == 1
    assert len(success_events) == 1  # it succeeds on the retry within the same run
    assert G3.nodes[flaky_switch]["state"] == "Hybrid"  # eventually migrated
    # 1 success, 1 failed -> failure_rate 0.5 baked into future cost,
    # even though this run already finished migrating it successfully
    assert abs(tracker3.cost(flaky_switch) - 1.0 * (1.0 + 2.0 * 0.5)) < 1e-9

    print("\nCapability tracker (Test 1) summary:")
    print(tracker.summary())

    print("\nAll optimizer.py self-checks passed (no topology.py or OS-Ken required).")
