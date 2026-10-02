import sys
from pathlib import Path

import networkx as nx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "network"))
sys.path.insert(0, str(ROOT / "optimizer"))

from topology import TopologyAdapter
from optimizer import run_sgbm
from capability_tracker import CapabilityTracker


def build_test_topology():
    topo = TopologyAdapter()

    dpids = [
        "0000000000000001",
        "0000000000000002",
        "0000000000000003",
    ]

    topo.G = nx.Graph()

    for dpid in dpids:
        topo.G.add_node(
            dpid,
            dpid=dpid,
            name=f"s{int(dpid[-1])}",
            state="Legacy",
        )
        topo.states[dpid] = "Legacy"

    topo.G.add_edge(dpids[0], dpids[1])
    topo.G.add_edge(dpids[1], dpids[2])

    return topo, dpids


def fake_migrate_factory(topo):
    """
    Simulates the real migrate_link.migrate() ownership boundary.

    optimizer.py calls this function, but this function is responsible
    for updating topology state.
    """

    def migrate(dpid):
        current_state = topo.get_state(dpid)

        assert current_state == "Legacy", (
            f"{dpid} was expected to be Legacy before migration, "
            f"but is {current_state}"
        )

        topo.set_state(dpid, "Hybrid")

        return {
            "dpid": dpid,
            "outcome": "success",
            "baseline": {
                "latency_ms": 20.0,
                "failure_rate": 0.0,
                "overhead_bytes": 0,
            },
            "post": {
                "latency_ms": 24.0,
                "failure_rate": 0.0,
                "overhead_bytes": 512,
            },
            "timestamp": "integration-test",
        }

    return migrate


def test_topology_graph_is_same_object_seen_by_optimizer():
    topo, dpids = build_test_topology()

    # This is the graph that production orchestrator_main.py
    # would pass into run_sgbm().
    G = topo.get_graph()

    # ------------------------------------------------------------
    # 1. Verify get_graph() returns TopologyAdapter's actual graph.
    # ------------------------------------------------------------
    assert G is topo.G

    for dpid in dpids:
        assert topo.get_state(dpid) == "Legacy"
        assert G.nodes[dpid]["state"] == "Legacy"

    # ------------------------------------------------------------
    # 2. Verify topology.set_state() updates that SAME graph.
    # ------------------------------------------------------------
    topo.set_state(dpids[0], "Hybrid")

    assert topo.get_state(dpids[0]) == "Hybrid"
    assert G.nodes[dpids[0]]["state"] == "Hybrid"

    # Reset it for the SGBM test.
    topo.set_state(dpids[0], "Legacy")

    # ------------------------------------------------------------
    # 3. Define flows.
    #
    #    For this test, the highest-degree node is dpid 2.
    #
    #       s1 ---- s2 ---- s3
    #                 ^
    #                core
    # ------------------------------------------------------------
    flows = [
        {
            "name": "ctrl_s3",
            "destination_dpid": dpids[2],
            "criticality": 1.0,
            "security_sla": 1.0,
            "latency_sla": 1.0,
        }
    ]

    # ------------------------------------------------------------
    # 4. Use the REAL TopologyAdapter graph with run_sgbm().
    # ------------------------------------------------------------
    migrate_fn = fake_migrate_factory(topo)
    tracker = CapabilityTracker()

    schedule, coverage = run_sgbm(
        G,
        flows,
        budget=100.0,
        migrate_fn=migrate_fn,
        capability=tracker,
        verbose=True,
    )

    # ------------------------------------------------------------
    # 5. Verify migration happened.
    # ------------------------------------------------------------
    assert schedule
    assert all(result["outcome"] == "success" for result in schedule)

    # ------------------------------------------------------------
    # 6. MOST IMPORTANT ASSERTION:
    #
    #    topology.set_state() changed the same graph object
    #    that was passed to run_sgbm().
    # ------------------------------------------------------------
    for dpid in dpids:
        assert topo.get_state(dpid) == "Hybrid"
        assert G.nodes[dpid]["state"] == "Hybrid"
        assert topo.G.nodes[dpid]["state"] == "Hybrid"

    # Identity must still be preserved.
    assert G is topo.G

    # ------------------------------------------------------------
    # 7. Coverage should now be non-zero/full for this flow.
    # ------------------------------------------------------------
    assert coverage == 1.0

    print("\nIntegration test passed.")
    print("Graph identity preserved:", G is topo.G)

    for dpid in dpids:
        print(
            dpid,
            "topology.state =", topo.get_state(dpid),
            "graph.state =", G.nodes[dpid]["state"],
        )
