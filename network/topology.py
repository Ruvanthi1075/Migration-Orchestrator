"""
topology.py
Feature 1 (part 1): Network and Flow Model

Builds a networkx graph representing the SDN testbed topology and
exposes a single clean function, build_graph(), for other modules
to import.
"""

import json
import networkx as nx


def build_graph(topo_description):
    """
    Build a networkx Graph from a simple topology description.

    topo_description is a dict of the form:
        {
            "switches": ["s1", "s2", "s3", ...],
            "hosts":    ["h1", "h2", "h3", ...],
            "links": [
                {"a": "s1", "b": "s2"},
                {"a": "s1", "b": "h1"},
                ...
            ]
        }

    In a real deployment this description would come from querying
    the running Mininet topology (see load_topology_from_mininet
    below); for development/testing it can be loaded from a small
    JSON/dict fixture instead, since the graph logic itself doesn't
    care where the description came from.

    Returns:
        networkx.Graph -- nodes are switches/hosts, edges are links.
        Every edge starts in the "Legacy" security state.
    """
    graph = nx.Graph()

    for switch in topo_description.get("switches", []):
        graph.add_node(switch, node_type="switch")

    for host in topo_description.get("hosts", []):
        graph.add_node(host, node_type="host")

    for link in topo_description.get("links", []):
        a, b = link["a"], link["b"]
        if a not in graph or b not in graph:
            raise ValueError(f"Link references unknown node: {a} - {b}")
        # every link starts as Legacy (classical TLS only) until
        # Feature 4 migrates it to Hybrid
        graph.add_edge(a, b, state="Legacy")

    return graph


def load_topology_description(path):
    """Load a topology description from a JSON file on disk."""
    with open(path, "r") as f:
        return json.load(f)


def load_topology_from_mininet(net):
    """
    OPTIONAL bridge for once you're running this against your real
    Mininet topology instead of the JSON fixture.

    Call this from a script that mininet itself imports, e.g.:

        from mininet.net import Mininet
        from topology import load_topology_from_mininet, build_graph

        net = Mininet(topo=my_topo)
        net.start()
        topo_description = load_topology_from_mininet(net)
        graph = build_graph(topo_description)

    net is a running mininet.net.Mininet instance. This walks net.switches
    and net.hosts and net.links to produce the same dict shape build_graph()
    already expects, so nothing else in Feature 1/2 has to change.
    """
    switches = [s.name for s in net.switches]
    hosts = [h.name for h in net.hosts]
    links = []
    for link in net.links:
        a = link.intf1.node.name
        b = link.intf2.node.name
        links.append({"a": a, "b": b})
    return {"switches": switches, "hosts": hosts, "links": links}


def summarize(graph):
    """Small human-readable summary, useful for a self-test / demo."""
    print(f"Nodes: {graph.number_of_nodes()}  Edges: {graph.number_of_edges()}")
    for node, data in graph.nodes(data=True):
        print(f"  {node:>4}  ({data['node_type']})")
    for a, b, data in graph.edges(data=True):
        print(f"  {a} -- {b}   state={data['state']}")


# This mirrors topo.py exactly: 7 switches (s0 core, s1/s2 agg, s3-s6
# edge), 8 hosts in 4 groups, plus the s1-s2 cross-link that creates
# the triangle s0-s1-s2-s0. Kept here so topology.py/coverage.py can be
# demoed and unit-tested without needing Mininet running.
PROJECT_TOPO = {
    "switches": ["s0", "s1", "s2", "s3", "s4", "s5", "s6"],
    "hosts": ["h1", "h2", "h3", "h4", "h5", "h6", "h7", "h8"],
    "links": [
        # hosts -> edge switches
        {"a": "h1", "b": "s3"}, {"a": "h2", "b": "s3"},   # hospital
        {"a": "h3", "b": "s4"}, {"a": "h4", "b": "s4"},   # banking
        {"a": "h5", "b": "s5"}, {"a": "h6", "b": "s5"},   # video
        {"a": "h7", "b": "s6"}, {"a": "h8", "b": "s6"},   # iot
        # edge -> aggregation
        {"a": "s3", "b": "s1"}, {"a": "s4", "b": "s1"},   # agg-A
        {"a": "s5", "b": "s2"}, {"a": "s6", "b": "s2"},   # agg-B
        # aggregation -> core
        {"a": "s1", "b": "s0"}, {"a": "s2", "b": "s0"},
        # redundant agg-to-agg cross-link (creates the triangle)
        {"a": "s1", "b": "s2"},
    ],
}

if __name__ == "__main__":
    # self-test: build the actual project topology (matching topo.py)
    # and print it
    g = build_graph(PROJECT_TOPO)
    summarize(g)
