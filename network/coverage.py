"""
coverage.py
Feature 2 -- Weakest-Link Security Coverage (Person A)

Pure functions only: no OS-Ken imports, no state mutation, no I/O
beyond flows_config.json loading. Every function here takes a
networkx.Graph and/or a flow dict and returns a value -- nothing is
read from or written to global state. This is deliberate (guide
section 6.2): it lets Person B unit-test optimizer.py against a
hand-built fake graph before topology.py exists, and it lets these
functions be called many times per optimizer round without side
effects piling up.

CORRECTED MODEL (guide section 2.2)
-------------------------------------
Everywhere the original paper's Algorithm 1 says "link e", read
"switch s". Everywhere it says "path Pf", read: "the switches that
must be Hybrid before flow f is fully protected", computed over the
control-plane LINK graph (topology.py's graph -- switch<->switch
adjacency), not over host-to-host data-plane paths.

    core_dpid   = the switch with the highest degree in the discovered
                  graph (ties -> lowest dpid), recomputed every call --
                  never hardcoded, never cached across topology changes.
    Pf(flow)    = shortest path, over the LINK graph, from the flow's
                  destination_dpid to core_dpid.
    Lf(flow)    = the subset of Pf(flow) still in "Legacy" state.
    weight(f)   = 0.5*criticality + 0.3*security_sla + 0.2*latency_sla
    security(f) = min(state(s) for s in Pf(f))      Legacy=0, Hybrid=1
    C           = sum(weight(f) * security(f) for f in F)

LITERATURE-BACKED CRITICALITY/SECURITY/LATENCY PIPELINE
(see SGBM_Literature_Backed_Formulation.docx, Sec. 2.1-2.3)
-------------------------------------------------------------------
weight(f)'s linear form and its 0.5/0.3/0.2 split are QSMO's own
design choice, unchanged and not attributed to any source -- only the
three component terms it reads (flow["criticality"], flow["security_sla"],
flow["latency_sla"]) are literature-derived, and this module now
computes them instead of assuming they arrive pre-populated in the
flow config:

    crit(f)     -- Sec. 2.1, adapted from X. Xin et al., "Taming
                   Imbalance and Complexity in WAN Traffic Engineering"
                   (per-link criticality score, assigned to each flow
                   via its own bottleneck link). See compute_criticality().
    sec_req(f)  -- Sec. 2.2, NIST FIPS 199 high-water-mark rule over
                   confidentiality/integrity/availability impact, with
                   QSMO's own Low/Moderate/High -> 0.33/0.67/1.00
                   mapping. See compute_security_requirement().
    lat_sens(f) -- Sec. 2.3, adapted from A. Saha et al., "Sway:
                   Traffic-Aware QoS Routing in Software-Defined IoT"
                   (DOI: 10.1109/TETC.2018.2847296): a flow's own delay
                   budget, min-max normalized against Sway's own cited
                   per-class bounds. Sway's binary feasibility gate
                   D(f_k) <= q_delay_k is kept separate and upstream --
                   see is_latency_feasible(). See compute_latency_sensitivity().

annotate_flow_weights(G, flows, edge_utilization, flow_bandwidth) runs
all three and returns a NEW list of flow dicts (originals untouched,
per this module's no-mutation rule) with "criticality"/"security_sla"/
"latency_sla" populated, ready for flow_weight() / compute_coverage() /
compute_gain() below, none of which change.

Interface frozen by the guide, section 5.3 (this is what optimizer.py,
Person B, is written against -- do not rename these):
    compute_coverage(G, flows)                              -> float
    compute_path(G, flow)                                   -> list[str]
    compute_legacy_set(G, flow)                              -> set[str]
    compute_gain(G, flows, hypothetical_migrated: set[str])  -> dict[str, float]
"""

import json

import networkx as nx


def load_flows(path):
    """
    Load the flow list from flows_config.json. Each flow must carry
    "destination_dpid" (section 5.2) plus the three SAW factors.
    "source"/"destination" (human labels like "c0"/"s3") are kept in
    the file for documentation only -- nothing here reads them.
    """
    with open(path, "r") as f:
        data = json.load(f)
    return data["flows"]


def core_switch(G):
    """
    The switch with the highest node degree in the CURRENT graph.
    Recomputed on every call -- this is what makes the whole model
    topology-agnostic: swap topo.py for a different shape and every
    downstream computation adjusts automatically, no code changes.

    Ties resolve to the lowest dpid, deterministically, so repeated
    calls against an unchanged graph always agree.
    """
    if len(G) == 0:
        return None
    return max(G.nodes, key=lambda n: (G.degree[n], -int(n, 16)))


def compute_path(G, flow):
    """
    Pf for a flow: shortest path, over the LINK graph G, from the
    flow's anchor switch (destination_dpid) to the current core
    switch. Returns a list of dpids, core-ward, inclusive of both
    endpoints.

    If the graph is empty or the flow's switch hasn't been discovered
    yet (e.g. queried before OS-Ken has seen it), fail soft: return
    just the destination dpid on its own rather than raising, so a
    caller doing a coverage sweep over many flows doesn't crash on one
    not-yet-seen switch.
    """
    core = core_switch(G)
    dst = flow["destination_dpid"]
    if core is None or dst not in G:
        return [dst] if dst else []
    return nx.shortest_path(G, source=dst, target=core)


def compute_legacy_set(G, flow):
    """Lf: the subset of Pf(flow) that is still in the Legacy state."""
    return {s for s in compute_path(G, flow) if G.nodes[s]["state"] == "Legacy"}


def edge_key(u, v):
    """Undirected edge key, order-independent -- use this to build/look
    up entries in an `edge_utilization` dict so caller and this module
    always agree on key orientation regardless of path traversal
    direction."""
    return (u, v) if u <= v else (v, u)


def path_edges(path):
    """The consecutive-node edges of a path, as an ordered list of
    edge_key() tuples. Empty or single-node paths yield []."""
    return [edge_key(path[i], path[i + 1]) for i in range(len(path) - 1)]


# ---------------------------------------------------------------------
# Sec. 2.1 -- Criticality, adapted from Xin et al.
# ---------------------------------------------------------------------

def compute_criticality(G, flows, edge_utilization, flow_bandwidth):
    """
    crit(f), Sec. 2.1 of SGBM_Literature_Backed_Formulation.docx.

    Source: X. Xin et al., "Taming Imbalance and Complexity in WAN
    Traffic Engineering" -- a percolation-theory-inspired per-LINK
    criticality score:

        S_e = sum_{f in F : e = e*(f)} |b_f| / |F|

    where e*(f) is flow f's own most-loaded link (its bottleneck link)
    and b_f is f's bandwidth demand. This is a per-link score computed
    over the whole flow set, not itself a per-flow score, so it cannot
    be substituted into w(f) unchanged.

    Stated adaptation (explicit, minimal): each flow is assigned the
    criticality of its own bottleneck link, using the source paper's
    own e*(f) notation:

        crit(f) = S_{e*(f)}    [adapted from Xin et al. -- link score
                                 -> owning flow's score]

    Pipeline (per the doc): per-link utilization U_e comes from OVS
    port stats (ovs-ofctl dump-ports / Ryu port-stats); b_f comes from
    OVS flow stats or, for control-plane sessions, OpenFlow
    message-rate as the traffic proxy -- neither is computed by this
    function, both are supplied by the caller as already-measured
    values, keeping this function a pure transform with no I/O.

    Parameters
    ----------
    G : networkx.Graph
        The link graph. compute_path(G, f) supplies each flow's path;
        this function only reads the edges along it.
    flows : list[dict]
        Each flow needs "name" and "destination_dpid".
    edge_utilization : dict[(str, str), float]
        U_e per edge. Keys must be built with edge_key() so lookup
        orientation matches regardless of traversal direction. An edge
        missing from this dict is treated as U_e = 0.0 (never chosen
        as a bottleneck unless every edge on the path is also 0.0/missing).
    flow_bandwidth : dict[str, float]
        b_f per flow name. A flow missing from this dict is treated as
        b_f = 0.0 (contributes nothing to its bottleneck edge's S_e,
        but still gets a crit(f) via that edge's score from other flows).

    Returns
    -------
    dict[str, float]
        {flow_name: crit(f)}, min-max normalized to [0,1] across this
        flow set (per the doc: "normalize crit(f) to [0,1] via min-max
        scaling before use in w(f)"). If every flow's raw score ties
        (including the degenerate all-zero case, e.g. no
        edge_utilization/flow_bandwidth data supplied), every flow
        gets 0.0 -- there is no distinguishing signal to normalize.

    Note: the paper's companion network-level metric
    R = sum_{e in E} S_e / U_e is NOT computed here -- it is an
    independent, externally-reported resilience metric (paper Sec. V),
    not part of w(f). Compute it separately from edge_utilization and
    a per-edge S_e if/when needed.
    """
    if not flows:
        return {}
    num_flows = len(flows)

    # e*(f): the most-loaded edge on flow f's path (ties broken by
    # edge_key() order, i.e. deterministically, via max()'s stable pick).
    bottleneck_edge = {}
    for f in flows:
        edges = path_edges(compute_path(G, f))
        bottleneck_edge[f["name"]] = (
            max(edges, key=lambda e: edge_utilization.get(e, 0.0)) if edges else None
        )

    # S_e per edge: sum of |b_f| / |F| over every flow whose e*(f) == e.
    edge_score = {}
    for f in flows:
        e_star = bottleneck_edge[f["name"]]
        if e_star is None:
            continue
        b_f = abs(flow_bandwidth.get(f["name"], 0.0))
        edge_score[e_star] = edge_score.get(e_star, 0.0) + b_f / num_flows

    raw_crit = {
        f["name"]: (edge_score.get(bottleneck_edge[f["name"]], 0.0)
                     if bottleneck_edge[f["name"]] is not None else 0.0)
        for f in flows
    }

    values = list(raw_crit.values())
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return {name: 0.0 for name in raw_crit}
    return {name: (v - lo) / (hi - lo) for name, v in raw_crit.items()}


# ---------------------------------------------------------------------
# Sec. 2.2 -- Security requirement, FIPS 199 high-water mark
# ---------------------------------------------------------------------

# QSMO's own ordinal mapping, consistent with FIPS 199's three-level
# scale -- stated addition, not itself part of the standard.
_FIPS199_IMPACT_MAP = {"low": 0.33, "moderate": 0.67, "high": 1.00}


def compute_security_requirement(flow):
    """
    sec_req(f), Sec. 2.2. Source: NIST FIPS 199, Standards for Security
    Categorization of Federal Information and Information Systems. The
    standard's security category is the high-water mark across
    confidentiality, integrity, and availability impact:

        SC = max{(confidentiality, impact), (integrity, impact),
                  (availability, impact)}         [FIPS 199, unchanged structure]

    Stated adaptation: FIPS 199 impact levels are categorical (Low /
    Moderate / High), not numeric, so they are mapped to [0,1] via
    QSMO's own ordinal mapping before use in a weighted sum
    (Low->0.33, Moderate->0.67, High->1.00). Only this mapping is
    QSMO's addition -- the max (high-water-mark) rule itself is
    retained exactly as the standard states it, applied per flow:

        sec_req(f) = max{m(impact_C(f)), m(impact_I(f)), m(impact_A(f))}

    Parameters
    ----------
    flow : dict
        Must carry "impact_confidentiality", "impact_integrity", and
        "impact_availability", each "Low"/"Moderate"/"High"
        (case-insensitive), assigned per the flow's data type/purpose
        (e.g. control-channel authentication traffic vs. best-effort
        telemetry).

    Returns
    -------
    float in [0,1]
    """
    levels = (
        flow["impact_confidentiality"],
        flow["impact_integrity"],
        flow["impact_availability"],
    )
    return max(_FIPS199_IMPACT_MAP[str(level).strip().lower()] for level in levels)


# ---------------------------------------------------------------------
# Sec. 2.3 -- Latency sensitivity, adapted from Sway
# ---------------------------------------------------------------------

# Sway's own cited class bounds (q_min, q_max), in milliseconds -- not
# invented here. Delay-sensitive: 0.25-100 ms. Loss-sensitive:
# 1.6-10 s, expressed in ms for a single consistent unit.
_SWAY_CLASS_BOUNDS_MS = {
    "delay_sensitive": (0.25, 100.0),
    "loss_sensitive": (1600.0, 10000.0),
}


def is_latency_feasible(flow, measured_delay_ms):
    """
    Sway's binary delay-feasibility test (Sec. 2.3), retained unchanged
    as a hard gate UPSTREAM of lat_sens(f) -- it is not folded into the
    continuous score below:

        D(f_k) <= q_delay_k     [Sway -- pass/fail only]

    Parameters
    ----------
    flow : dict
        Must carry "q_delay_ms" (the flow's own delay budget/requirement).
    measured_delay_ms : float
        D(f_k), the flow's actually observed delay.

    Returns
    -------
    bool
        False means the path is infeasible regardless of lat_sens(f),
        consistent with Sway's own use of the test -- callers should
        exclude/handle such flows before they reach the optimizer, not
        rely on a low lat_sens(f) score to deprioritize them.
    """
    return measured_delay_ms <= flow["q_delay_ms"]


def compute_latency_sensitivity(flow):
    """
    lat_sens(f), Sec. 2.3. Source: A. Saha et al., "Sway: Traffic-Aware
    QoS Routing in Software-Defined IoT" (DOI: 10.1109/TETC.2018.2847296).
    Sway's own delay-feasibility test is binary (see
    is_latency_feasible()), not a graded score; it does not define a
    continuous priority weight, only that pass/fail test plus cited
    numeric delay-budget ranges -- 0.25-100 ms for delay-sensitive IoT
    flows, 1.6-10 s for loss-sensitive flows. Stated as a limitation of
    the source, not glossed over.

    Stated adaptation: to obtain a continuous lat_sens(f) in [0,1]
    without inventing new numbers, QSMO min-max normalizes the flow's
    own delay budget q_delay(f) against Sway's own cited class bounds
    [q_min, q_max] for that flow's traffic class, so tighter (smaller)
    budgets map to higher sensitivity:

        lat_sens(f) = (q_max - q_delay(f)) / (q_max - q_min)
        [normalization built on Sway's cited bounds; the mapping
        itself is QSMO's addition]

    Parameters
    ----------
    flow : dict
        Must carry "traffic_class" ("delay_sensitive" or
        "loss_sensitive") and "q_delay_ms" (the flow's own delay
        budget, in milliseconds).

    Returns
    -------
    float in [0,1]
        Clamped to [0,1]. A q_delay(f) outside the cited class bounds
        shouldn't occur if is_latency_feasible() gated the flow first,
        but is possible if the gate wasn't applied upstream; clamping
        keeps this component from breaking w(f)'s [0,1] assumption
        rather than silently letting it happen.
    """
    q_min, q_max = _SWAY_CLASS_BOUNDS_MS[flow["traffic_class"]]
    raw = (q_max - flow["q_delay_ms"]) / (q_max - q_min)
    return max(0.0, min(1.0, raw))


# ---------------------------------------------------------------------
# Sec. 2.1-2.3 pipeline entry point
# ---------------------------------------------------------------------

def annotate_flow_weights(G, flows, edge_utilization, flow_bandwidth):
    """
    Runs the Sec. 2.1-2.3 pipeline over `flows` and returns a NEW list
    of flow dicts with "criticality" (Sec. 2.1), "security_sla"
    (Sec. 2.2), and "latency_sla" (Sec. 2.3) populated from the
    literature-adapted formulas above -- ready for flow_weight() /
    compute_coverage() / compute_gain(), all of which are unchanged
    and still just read those three fields, so nothing downstream of
    this call needs to change.

    Per this module's no-mutation rule, the input `flows` list and its
    dicts are never modified -- this returns shallow copies with the
    three fields added/overwritten.

    A flow that fails Sway's binary feasibility gate
    (is_latency_feasible()) is NOT silently scored around -- if the
    flow dict carries "measured_delay_ms", the returned copy also
    carries "latency_feasible": bool, and callers must exclude/handle
    infeasible flows themselves (per Sec. 2.3, the gate is a hard fail,
    not something lat_sens(f) folds in). Flows without
    "measured_delay_ms" skip the gate entirely (no feasibility claim is
    made either way).

    Parameters
    ----------
    G : networkx.Graph
        The link graph, passed through to compute_criticality().
    flows : list[dict]
        Each flow needs "name", "destination_dpid" (for Pf(f)), plus
        the Sec. 2.1-2.3 raw inputs: "impact_confidentiality",
        "impact_integrity", "impact_availability" (Sec. 2.2),
        "traffic_class", "q_delay_ms" (Sec. 2.3), and optionally
        "measured_delay_ms" (Sec. 2.3 gate).
    edge_utilization : dict[(str, str), float]
        Passed through to compute_criticality().
    flow_bandwidth : dict[str, float]
        Passed through to compute_criticality().

    Returns
    -------
    list[dict]
    """
    crit_scores = compute_criticality(G, flows, edge_utilization, flow_bandwidth)

    annotated = []
    for f in flows:
        g = dict(f)
        g["criticality"] = crit_scores.get(f["name"], 0.0)
        g["security_sla"] = compute_security_requirement(f)
        g["latency_sla"] = compute_latency_sensitivity(f)
        if "measured_delay_ms" in f:
            g["latency_feasible"] = is_latency_feasible(f, f["measured_delay_ms"])
        annotated.append(g)
    return annotated


def flow_weight(flow):
    """
    weight(f) = 0.5*criticality + 0.3*security_sla + 0.2*latency_sla.

    The linear form and the 0.5/0.3/0.2 split are QSMO's own weighting
    design choice (unchanged, not attributed to any cited source).
    Only the three component terms are literature-derived -- see
    compute_criticality() (Sec. 2.1), compute_security_requirement()
    (Sec. 2.2), and compute_latency_sensitivity() (Sec. 2.3) above, or
    annotate_flow_weights() to populate all three on a flow list at
    once before calling this.
    """
    return (
        0.5 * flow["criticality"]
        + 0.3 * flow["security_sla"]
        + 0.2 * flow["latency_sla"]
    )


def flow_security(G, flow):
    """
    security(f): the weakest-link score for flow f's current path --
    1.0 only if every switch on Pf(f) is Hybrid, else 0.0. Not an
    average, not a fraction of links migrated.
    """
    path = compute_path(G, flow)
    if not path:
        return 0.0
    return min(1.0 if G.nodes[s]["state"] == "Hybrid" else 0.0 for s in path)


def compute_coverage(G, flows):
    """C = sum(weight(f) * security(f) for f in flows). The single
    number the greedy optimizer (Person B) tries to maximize."""
    return sum(flow_weight(f) * flow_security(G, f) for f in flows)


def compute_gain(G, flows, hypothetical_migrated):
    """
    Per-flow marginal coverage gain if every dpid in
    hypothetical_migrated were (hypothetically) flipped to Hybrid,
    holding everything else fixed. Does not mutate G -- works on a
    copy, then throws it away.

    Returns {flow_name: gain}, so the optimizer can score a batch of
    candidate flows in one call instead of recomputing total coverage
    once per candidate.
    """
    G2 = G.copy()
    for dpid in hypothetical_migrated:
        if dpid in G2:
            G2.nodes[dpid]["state"] = "Hybrid"

    gains = {}
    for f in flows:
        path = compute_path(G, f)  # path itself doesn't change hypothetically
        after = flow_weight(f) * (
            1.0 if all(G2.nodes[s]["state"] == "Hybrid" for s in path) else 0.0
        )
        before = flow_weight(f) * flow_security(G, f)
        gains[f["name"]] = after - before
    return gains


if __name__ == "__main__":
    # --------------------------------------------------------------
    # Self-test against a HAND-BUILT graph, not topo.py and not any
    # hardcoded PROJECT_TOPO dict. This is exactly the "test against a
    # fake graph" workflow the guide recommends in section 10, step 1:
    # coverage.py needs nothing but networkx, so this runs standalone,
    # before topology.py is ever wired to a live OS-Ken session.
    #
    # The shape mirrors topo.py's real switch-to-switch links, but
    # note this dict lives ONLY inside this __main__ test block -- it
    # is not imported by, or hardcoded into, any function above.
    # --------------------------------------------------------------
    def _fake_graph():
        G = nx.Graph()
        dpids = [f"{i:016x}" for i in range(7)]  # s0..s6 -> 0000...00 .. 0000...06
        for d in dpids:
            G.add_node(d, state="Legacy")
        s0, s1, s2, s3, s4, s5, s6 = dpids
        G.add_edge(s1, s0)
        G.add_edge(s2, s0)
        G.add_edge(s1, s2)   # redundant agg-to-agg cross-link
        G.add_edge(s3, s1)
        G.add_edge(s4, s1)
        G.add_edge(s5, s2)
        G.add_edge(s6, s2)
        return G, dict(s0=s0, s1=s1, s2=s2, s3=s3, s4=s4, s5=s5, s6=s6)

    G, name = _fake_graph()

    # core switch check: s1 and s2 tie on degree 4; lowest dpid wins -> s1
    core = core_switch(G)
    print("core switch:", core)
    assert core == name["s1"]

    flows = [
        {"name": "ctrl_s3", "destination_dpid": name["s3"],
         "criticality": 0.9, "security_sla": 0.8, "latency_sla": 0.5},
        {"name": "ctrl_s5", "destination_dpid": name["s5"],
         "criticality": 0.3, "security_sla": 0.5, "latency_sla": 0.7},
    ]

    # Pf(ctrl_s3) should be s3 -> s1 (already at core, 1 hop)
    p3 = compute_path(G, flows[0])
    print("Pf(ctrl_s3):", p3)
    assert p3 == [name["s3"], name["s1"]]

    # Pf(ctrl_s5) should be s5 -> s2 -> s1 (2 hops to reach the core)
    p5 = compute_path(G, flows[1])
    print("Pf(ctrl_s5):", p5)
    assert p5 == [name["s5"], name["s2"], name["s1"]]

    print("All-Legacy coverage:", compute_coverage(G, flows))
    assert compute_coverage(G, flows) == 0.0

    # Migrate every switch on ctrl_s3's path -> only ctrl_s3 becomes secure
    for s in compute_legacy_set(G, flows[0]):
        G.nodes[s]["state"] = "Hybrid"
    cov = compute_coverage(G, flows)
    print("After migrating ctrl_s3's path:", cov)
    assert abs(cov - flow_weight(flows[0])) < 1e-9

    # compute_gain sanity check: migrating the rest of ctrl_s5's path
    # should show a positive gain exactly equal to ctrl_s5's weight
    remaining = compute_legacy_set(G, flows[1])
    gains = compute_gain(G, flows, remaining)
    print("Hypothetical gains:", gains)
    assert abs(gains["ctrl_s5"] - flow_weight(flows[1])) < 1e-9
    assert gains["ctrl_s3"] == 0.0  # already fully migrated, no further gain

    print("All coverage.py self-checks passed (no topo.py or OS-Ken required).")

    # --------------------------------------------------------------
    # Self-test for the Sec. 2.1-2.3 pipeline (fresh, all-Legacy graph).
    # --------------------------------------------------------------
    G2, name2 = _fake_graph()

    raw_flows = [
        {
            "name": "ctrl_s3", "destination_dpid": name2["s3"],
            "impact_confidentiality": "High", "impact_integrity": "Moderate",
            "impact_availability": "Low",
            "traffic_class": "delay_sensitive", "q_delay_ms": 10.0,
            "measured_delay_ms": 8.0,
        },
        {
            "name": "ctrl_s5", "destination_dpid": name2["s5"],
            "impact_confidentiality": "Low", "impact_integrity": "Low",
            "impact_availability": "Moderate",
            "traffic_class": "loss_sensitive", "q_delay_ms": 5000.0,
            "measured_delay_ms": 6000.0,  # fails its own budget
        },
    ]

    # e*(ctrl_s3) = (s1,s3); e*(ctrl_s5) = (s1,s2) since it's the more
    # loaded of s5's two path edges (s5,s2) and (s2,s1).
    edge_utilization = {
        edge_key(name2["s1"], name2["s3"]): 0.9,
        edge_key(name2["s5"], name2["s2"]): 0.2,
        edge_key(name2["s2"], name2["s1"]): 0.6,
    }
    flow_bandwidth = {"ctrl_s3": 100.0, "ctrl_s5": 50.0}

    annotated = annotate_flow_weights(G2, raw_flows, edge_utilization, flow_bandwidth)
    by_name = {f["name"]: f for f in annotated}
    print("\nAnnotated flows:", annotated)

    # Only one flow has a nonzero raw crit score (ctrl_s3, sole
    # contributor to its bottleneck edge) -> min-max normalizes it to
    # 1.0 and the zero-contribution flow to 0.0.
    assert by_name["ctrl_s3"]["criticality"] == 1.0
    assert by_name["ctrl_s5"]["criticality"] == 0.0

    # sec_req: High/Moderate/Low -> max(1.00, 0.67, 0.33) = 1.00
    assert abs(by_name["ctrl_s3"]["security_sla"] - 1.00) < 1e-9
    # sec_req: Low/Low/Moderate -> max(0.33, 0.33, 0.67) = 0.67
    assert abs(by_name["ctrl_s5"]["security_sla"] - 0.67) < 1e-9

    # lat_sens(ctrl_s3): delay_sensitive bounds (0.25, 100) ms,
    # q_delay=10 -> (100-10)/(100-0.25) = 0.9022...
    expected_lat_s3 = (100.0 - 10.0) / (100.0 - 0.25)
    assert abs(by_name["ctrl_s3"]["latency_sla"] - expected_lat_s3) < 1e-9

    # lat_sens(ctrl_s5): loss_sensitive bounds (1600, 10000) ms,
    # q_delay=5000 -> (10000-5000)/(10000-1600) = 0.5952...
    expected_lat_s5 = (10000.0 - 5000.0) / (10000.0 - 1600.0)
    assert abs(by_name["ctrl_s5"]["latency_sla"] - expected_lat_s5) < 1e-9

    # Feasibility gate: ctrl_s3 measured 8ms <= 10ms budget -> feasible.
    # ctrl_s5 measured 6000ms > 5000ms budget -> infeasible.
    assert by_name["ctrl_s3"]["latency_feasible"] is True
    assert by_name["ctrl_s5"]["latency_feasible"] is False

    # Original input flows must be untouched (no state mutation).
    assert "criticality" not in raw_flows[0]
    assert "criticality" not in raw_flows[1]

    print("All Sec. 2.1-2.3 pipeline self-checks passed.")
