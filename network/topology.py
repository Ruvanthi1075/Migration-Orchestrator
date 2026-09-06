"""
topology.py
Feature 1 (part 1): Network and Flow Model -- CONTROL-PLANE VERSION

Builds a networkx graph representing the SDN control plane: the
controller and the OVS switches, per Section IV-A of the paper
("V represents the SDN controller and Open vSwitch instances, and E
represents the control-plane communication links"). Hosts are NOT
nodes in this graph -- they exist only in topo.py's Mininet script,
to generate realistic background traffic. This is the fix for the
earlier version, which modeled host-to-host data-plane traffic
instead of controller-to-switch control links.

MODELING NOTE -- read this before treating "paths" here as literal
packet paths: Mininet's controller channel normally rides the VM's
loopback (127.0.0.1:6653), bypassing the emulated switch links
entirely, regardless of topology (see topo.py's docstring). So the
multi-hop "paths" below are a documented PRIORITIZATION MODEL, not a
claim about how OpenFlow/SSL packets physically travel: they encode
"which upstream links have to be trustworthy for this switch's
control channel to be considered secure," based on the network's
management hierarchy (core -> agg -> edge). This is an intentional,
stated simplification -- say so in the paper -- rather than the
previous version's silent conflation of data-plane and control-plane
security.

Controller redundancy: c0 is attached directly to THREE switches
(s0, s1, s2) instead of one, modeling a realistic redundant
control-plane design (the controller is reachable via more than one
upstream point, common in real deployments for resilience). This is
also what "more controller-to-switch links" gives you functionally:
s0/s1/s2's control sessions are single-hop and migrate trivially;
s3-s6 (the switches actually carrying the labeled hospital/banking/
video/iot traffic) still require a 2-hop path through their
aggregation switch, which is where weakest-link scoring and batch
greedy migration (Algorithm 1) do real work.
"""

import json
import networkx as nx


def build_graph(topo_description):
    """
    Build a networkx Graph from a simple topology description.

    topo_description is a dict of the form:
        {
            "switches": ["c0", "s0", "s1", ...],
            "links": [
                {"a": "c0", "b": "s0"},
                ...
            ]
        }

    Every edge starts in the "Legacy" security state. "switches" here
    includes the controller node (c0) -- it's kept in the same list
    (rather than a separate "controllers" key) because build_graph
    doesn't need to distinguish it structurally; only node naming
    convention (c0 vs s<N>) tells them apart, same as the original
    Feature 1 didn't need to distinguish host vs switch structurally.
    """
    graph = nx.Graph()

    for switch in topo_description.get("switches", []):
        graph.add_node(switch, node_type="controller" if switch == "c0" else "switch")

    for link in topo_description.get("links", []):
        a, b = link["a"], link["b"]
        if a not in graph or b not in graph:
            raise ValueError(f"Link references unknown node: {a} - {b}")
        graph.add_edge(a, b, state="Legacy")

    return graph


def load_topology_description(path):
    """Load a topology description from a JSON file on disk."""
    with open(path, "r") as f:
        return json.load(f)


def summarize(graph):
    """Small human-readable summary, useful for a self-test / demo."""
    print(f"Nodes: {graph.number_of_nodes()}  Edges: {graph.number_of_edges()}")
    for node, data in graph.nodes(data=True):
        print(f"  {node:>4}  ({data['node_type']})")
    for a, b, data in graph.edges(data=True):
        print(f"  {a} -- {b}   state={data['state']}")


# Matches topo.py's data-plane wiring for s0-s6 (so the switch tier
# labels in capability_tracker.py still line up), plus the controller
# c0 attached redundantly to s0 (core), s1 (agg-A), and s2 (agg-B).
PROJECT_TOPO = {
    "switches": ["c0", "s0", "s1", "s2", "s3", "s4", "s5", "s6"],
    "links": [
        # controller's redundant direct attachments
        {"a": "c0", "b": "s0"},
        {"a": "c0", "b": "s1"},
        {"a": "c0", "b": "s2"},
        # aggregation -> core
        {"a": "s1", "b": "s0"}, {"a": "s2", "b": "s0"},
        # redundant agg-to-agg cross-link (creates the triangle)
        {"a": "s1", "b": "s2"},
        # edge -> aggregation
        {"a": "s3", "b": "s1"}, {"a": "s4", "b": "s1"},   # agg-A: hospital + banking
        {"a": "s5", "b": "s2"}, {"a": "s6", "b": "s2"},   # agg-B: video + iot
    ],
}

if __name__ == "__main__":
    g = build_graph(PROJECT_TOPO)
    summarize(g)

    # Sanity check: with c0 attached to both s0 and s1/s2, an edge
    # switch's control session should be short (2 hops), not routed
    # the long way around through every tier.
    path = nx.shortest_path(g, "c0", "s3")
    print("\nc0 -> s3 control path:", path)
    assert len(path) - 1 == 2  # c0-s1-s3, since c0 is directly on s1
