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


def flow_weight(flow):
    """weight(f) = 0.5*criticality + 0.3*security_sla + 0.2*latency_sla."""
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
