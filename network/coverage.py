"""
coverage.py
Feature 2: Weakest-Link Security Coverage

Turns the graph + flow list from topology.py / flows_config.json into
a single number describing how secure the network currently is.

Person B's greedy optimizer (Feature 3) calls total_coverage()
repeatedly, once per candidate link per round, so these functions are
written to be cheap and side-effect free.
"""

import json
import networkx as nx

# Legacy = classical TLS only, Hybrid = classical + PQC
SECURITY_LEVEL = {"Legacy": 0, "Hybrid": 1}

# simple, defensible weight mapping -- keep it in one place
IMPORTANCE_WEIGHT = {"High": 3, "Medium": 2, "Low": 1}


def load_flows(path):
    """Load the flow list written by Feature 1."""
    with open(path, "r") as f:
        data = json.load(f)
    return data["flows"]


def get_path_links(graph, source, destination):
    """
    Return the path between source and destination as a list of
    (a, b) edge tuples. Shortest path is a reasonable, defensible
    simplification for this beginner scope.
    """
    node_path = nx.shortest_path(graph, source, destination)
    return list(zip(node_path[:-1], node_path[1:]))


def path_security_level(graph, path_links):
    """
    The weakest-link score for one path: the MINIMUM security level
    among all links on that path, not the average and not a count of
    how many links are upgraded.
    """
    if not path_links:
        return 0
    levels = []
    for a, b in path_links:
        state = graph.edges[a, b]["state"]
        levels.append(SECURITY_LEVEL[state])
    return min(levels)


def total_coverage(graph, flows):
    """
    Sum, across every flow, of: importance_weight(flow) x
    weakest_link_score(flow's path). This is the single number
    Feature 3 tries to maximize by choosing which link to upgrade
    next.
    """
    score = 0
    for flow in flows:
        path_links = get_path_links(graph, flow["source"], flow["destination"])
        level = path_security_level(graph, path_links)
        weight = IMPORTANCE_WEIGHT[flow["importance"]]
        score += weight * level
    return score


if __name__ == "__main__":
    # self-test against the real project topology (matches topo.py)
    from topology import build_graph, PROJECT_TOPO

    graph = build_graph(PROJECT_TOPO)
    flows = load_flows("flows_config.json")

    # Sanity check: with the triangle in place, hospital_traffic's
    # shortest path (h1 -> h6) should use the s1-s2 cross-link, not
    # detour through the core switch s0.
    hospital_path = get_path_links(graph, "h1", "h6")
    print("hospital_traffic path:", hospital_path)
    assert ("s1", "s2") in hospital_path or ("s2", "s1") in hospital_path

    print("All links Legacy -> total coverage =", total_coverage(graph, flows))
    # every path's weakest link is Legacy (0), so score must be 0
    assert total_coverage(graph, flows) == 0

    # upgrade only the backbone (agg<->core, and the cross-link) -- every
    # flow crosses agg-A/agg-B, so ALL of them still fail on their
    # unmigrated edge/host-facing hop
    for a, b in [("s1", "s0"), ("s2", "s0"), ("s1", "s2")]:
        graph.edges[a, b]["state"] = "Hybrid"
    print("Backbone-only Hybrid -> total coverage =", total_coverage(graph, flows))
    assert total_coverage(graph, flows) == 0

    # now fully upgrade hospital_traffic's actual path (h1-s3-s1-s2-s5-h6)
    for a, b in [("h1", "s3"), ("s3", "s1"), ("s5", "s2"), ("s5", "h6")]:
        if graph.has_edge(a, b):
            graph.edges[a, b]["state"] = "Hybrid"
    print("hospital path fully Hybrid -> total coverage =", total_coverage(graph, flows))
    # hospital_traffic weight 3 x level 1 = 3; others still 0
    assert total_coverage(graph, flows) == 3

    print("All self-checks passed.")
