"""
visualize.py
Visual representation of Feature 1 (Network/Flow Model) + Feature 2
(Weakest-Link Security Coverage), for the demo / report.

Draws the topology as a graph -- switches as squares, hosts as
circles, links colored red (Legacy) or green (Hybrid) -- and prints
each flow's weakest-link score plus the total coverage number
underneath. Run this at different "migration stages" (by editing
which edges are Hybrid below) to show the score changing as links
get upgraded, which is exactly what Feature 3 will do automatically.
"""

import matplotlib.pyplot as plt
import networkx as nx

from topology import build_graph, PROJECT_TOPO
from coverage import (
    load_flows,
    get_path_links,
    path_security_level,
    total_coverage,
    IMPORTANCE_WEIGHT,
)

# Fixed layout coordinates mirroring the real topo.py: core at the top,
# aggregation below it, edge switches below that, hosts at the bottom.
# agg-A (s1: hospital+banking) on the left, agg-B (s2: video+iot) on
# the right, so the s1-s2 cross-link reads as a clean horizontal line.
POS = {
    "s0": (2, 3),
    "s1": (0, 2), "s2": (4, 2),
    "s3": (-1, 1), "s4": (1, 1), "s5": (3, 1), "s6": (5, 1),
    "h1": (-1.5, 0), "h2": (-0.5, 0),
    "h3": (0.5, 0), "h4": (1.5, 0),
    "h5": (2.5, 0), "h6": (3.5, 0),
    "h7": (4.5, 0), "h8": (5.5, 0),
}


def draw(graph, flows, stage_label, ax):
    node_colors = [
        "#f4a261" if graph.nodes[n]["node_type"] == "switch" else "#8ecae6"
        for n in graph.nodes
    ]
    node_shapes_switch = [n for n in graph.nodes if graph.nodes[n]["node_type"] == "switch"]
    node_shapes_host = [n for n in graph.nodes if graph.nodes[n]["node_type"] == "host"]

    edge_colors = [
        "#2a9d8f" if graph.edges[e]["state"] == "Hybrid" else "#e63946"
        for e in graph.edges
    ]

    nx.draw_networkx_edges(graph, POS, ax=ax, edge_color=edge_colors, width=2.5)
    nx.draw_networkx_nodes(graph, POS, ax=ax, nodelist=node_shapes_switch,
                            node_shape="s", node_color="#f4a261", node_size=550,
                            edgecolors="black")
    nx.draw_networkx_nodes(graph, POS, ax=ax, nodelist=node_shapes_host,
                            node_shape="o", node_color="#8ecae6", node_size=400,
                            edgecolors="black")
    nx.draw_networkx_labels(graph, POS, ax=ax, font_size=7, font_weight="bold")

    score = total_coverage(graph, flows)
    ax.set_title(f"{stage_label}\nTotal coverage = {score}", fontsize=10, fontweight="bold")
    ax.axis("off")


def print_flow_breakdown(graph, flows, stage_label):
    print(f"\n=== {stage_label} ===")
    for flow in flows:
        links = get_path_links(graph, flow["source"], flow["destination"])
        level = path_security_level(graph, links)
        weight = IMPORTANCE_WEIGHT[flow["importance"]]
        state_word = "Hybrid (protected end-to-end)" if level == 1 else "Legacy (weakest link unprotected)"
        print(f"  {flow['name']:<18} weight={weight}  weakest-link={state_word}  "
              f"contribution={weight * level}")
    print(f"  {'TOTAL COVERAGE':<18} = {total_coverage(graph, flows)}")


if __name__ == "__main__":
    flows = load_flows("flows_config.json")

    fig, axes = plt.subplots(1, 3, figsize=(16, 5.5))

    # Stage 0: nothing migrated yet
    g0 = build_graph(PROJECT_TOPO)
    print_flow_breakdown(g0, flows, "Stage 0: All links Legacy")
    draw(g0, flows, "Stage 0: Before migration", axes[0])

    # Stage 1: only the backbone migrated (core<->agg, and the
    # cross-link). Every flow crosses agg-A/agg-B, so the backbone
    # matters for all four flows at once -- but each flow STILL has an
    # un-migrated host-facing hop, so coverage is still 0. This is the
    # "4 out of 15 links upgraded, but 0% real benefit" case Feature 2
    # exists to catch.
    g1 = build_graph(PROJECT_TOPO)
    for a, b in [("s1", "s0"), ("s2", "s0"), ("s1", "s2")]:
        g1.edges[a, b]["state"] = "Hybrid"
    print_flow_breakdown(g1, flows, "Stage 1: Backbone-only migrated")
    draw(g1, flows, "Stage 1: Backbone Hybrid\n(0% real benefit yet)", axes[1])

    # Stage 2: fully migrated
    g2 = build_graph(PROJECT_TOPO)
    for a, b in g2.edges:
        g2.edges[a, b]["state"] = "Hybrid"
    print_flow_breakdown(g2, flows, "Stage 2: All links migrated")
    draw(g2, flows, "Stage 2: Fully migrated", axes[2])

    fig.suptitle("Feature 1 + Feature 2: Network Model & Weakest-Link Coverage",
                 fontsize=13, fontweight="bold")
    plt.tight_layout()
    plt.savefig("coverage_visualization.png", dpi=150, bbox_inches="tight")
    print("\nSaved coverage_visualization.png")
