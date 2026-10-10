#!/usr/bin/env python3
"""
tests/test_any_topology.py - offline proof that the pipeline is topology-agnostic.

No Mininet / OVS / OpenSSL / OS-Ken needed. For many topology shapes it builds the
link graph exactly as topo.py would wire it, generates flows (network/gen_flows.py),
annotates weights (coverage.py), runs the real SGBM optimizer, and checks:

  * every flow path starts at its switch and ends at the core
  * paths do not depend on link-discovery (insertion) order
  * beta=1 reaches full coverage; coverage never falls as the budget grows
  * the optimizer terminates and stays sane when some switches always fail
  * disconnected / partially discovered graphs do not crash path computation
  * port allocation is collision-free for every dpid

Usage:
    python3 tests/test_any_topology.py                      # built-in matrix + current topo.py
    python3 tests/test_any_topology.py --topology ring:9    # one specific topology
"""
import argparse
import os
import random
import sys
import tempfile

import networkx as nx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "migration"))

from network import topo  # noqa: E402
from network.gen_flows import build_flows  # noqa: E402
from network.coverage import (annotate_flow_weights, compute_coverage, compute_path, core_switch,  # noqa: E402
                              flow_weight)
from optimizer.capability_tracker import CapabilityTracker  # noqa: E402
from optimizer.optimizer import run_sgbm, sgbm_approx_ratio, supermodular_degree  # noqa: E402
from port_registry import PortRegistry  # noqa: E402

MATRIX = ["default", "line:2", "line:3", "line:8", "ring:3", "ring:6", "star:5", "mesh:5",
          "tree:2:2", "tree:3:2", "tree:2:3"]
FAILS = []


def random_spec(seed, n):
    g = nx.gnp_random_graph(n, 0.25, seed=seed)
    while not nx.is_connected(g):
        seed += 1000
        g = nx.gnp_random_graph(n, 0.25, seed=seed)
    names = ["s%d" % i for i in range(n)]
    return {"switches": {nm: "%016x" % (i + 1) for i, nm in enumerate(names)}, "hosts": {},
            "links": [(names[a], names[b]) for a, b in g.edges]}


def build(spec):
    plan = topo.plan_topology(spec)
    G = nx.Graph()
    for name, dpid in plan["switches"].items():
        G.add_node(dpid, state="Legacy", name=name)
    for a, b in plan["links"]:
        G.add_edge(plan["switches"][a], plan["switches"][b])
    names = {d: n for n, d in plan["switches"].items()}
    links = [(plan["switches"][a], plan["switches"][b]) for a, b in plan["links"]]
    return plan, G, names, links


def annotate(G, names, links):
    raw = build_flows(names, links)
    return annotate_flow_weights(G, raw, {}, {f["name"]: f["bandwidth_mbps"] for f in raw})


def ok_migrate(dpid):
    return {"dpid": dpid, "outcome": "success"}


def check_topology(label, spec):
    plan, G, names, links = build(spec)
    n = len(G)
    core = core_switch(G)
    flows = annotate(G, names, links)
    assert len(flows) == n, "one flow per switch"
    assert all(f["destination_dpid"] in G for f in flows)

    # 1. paths start at the switch, end at the core, use real edges
    for f in flows:
        p = compute_path(G, f)
        assert p[0] == f["destination_dpid"] and p[-1] == core, (f["name"], p)
        assert all(G.has_edge(a, b) for a, b in zip(p, p[1:])), p

    # 2. deterministic regardless of discovery order
    ref = {f["name"]: compute_path(G, f) for f in flows}
    rng = random.Random(1)
    for _ in range(5):
        edges, nodes = list(G.edges), list(G.nodes)
        rng.shuffle(edges); rng.shuffle(nodes)
        G2 = nx.Graph()
        for d in nodes:
            G2.add_node(d, state="Legacy", name=names[d])
        G2.add_edges_from(edges)
        assert core_switch(G2) == core
        assert {f["name"]: compute_path(G2, f) for f in flows} == ref, "paths depend on discovery order"

    # 3. full budget -> full coverage; monotone in the budget
    max_cov = sum(flow_weight(f) for f in flows)
    cov_by_beta = []
    for beta in (0.25, 0.5, 0.75, 1.0):
        Gb = G.copy()
        tracker = CapabilityTracker()
        B = beta * tracker.total_cost(list(Gb.nodes))
        _s, cov = run_sgbm(Gb, flows, B, ok_migrate, capability=tracker, verbose=False)
        cov_by_beta.append(cov)
        if beta == 1.0:
            assert abs(cov - max_cov) < 1e-9, f"beta=1 coverage {cov} != {max_cov}"
            assert all(Gb.nodes[d]["state"] == "Hybrid" for d in Gb.nodes)
    assert cov_by_beta == sorted(cov_by_beta), cov_by_beta

    # 4. unreliable switches: terminates, never exceeds max, never raises
    bad = set(sorted(G.nodes)[::3])
    Gf = G.copy()
    tracker = CapabilityTracker()
    _s, cov = run_sgbm(Gf, flows, 1.0 * tracker.total_cost(list(Gf.nodes)),
                       lambda d: {"dpid": d, "outcome": "failed" if d in bad else "success"},
                       capability=tracker, verbose=False)
    assert 0 <= cov <= max_cov + 1e-9
    assert all(Gf.nodes[d]["state"] == "Legacy" for d in bad)

    # 5. theory helpers stay valid
    d = supermodular_degree(G, flows)
    rho = sgbm_approx_ratio(G, flows)
    assert 0 <= d <= n - 1 and 0 < rho <= 1

    # 6. collision-free ports for every dpid of this topology
    with tempfile.TemporaryDirectory() as tmp:
        reg = PortRegistry(path=os.path.join(tmp, "r.json"), doc_path="", check_bind=False)
        ports = [reg.port_for(dpid, name=names[dpid]) for dpid in G.nodes]
        assert len(set(ports)) == n

    # 7. disconnected / partially-discovered graph must not crash
    if n >= 4:
        Gd = G.copy()
        Gd.remove_edges_from(list(Gd.edges(core)))
        for f in flows:
            assert compute_path(Gd, f)
        compute_coverage(Gd, flows)

    print(f"PASS {label:<10} switches={n:<3} links={len(links):<3} core={names[core]:<4} "
          f"flows={len(flows)} d={d:<2} beta-coverage={['%.2f' % c for c in cov_by_beta]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topology", help="check only this topology (e.g. ring:9)")
    args = ap.parse_args()
    cases = []
    if args.topology:
        cases.append((args.topology, topo.from_name(args.topology)))
    else:
        cases.append(("topo.py", topo.TOPOLOGY))
        cases += [(m, topo.from_name(m)) for m in MATRIX]
        cases += [(f"random{n}", random_spec(n, n)) for n in (6, 9, 14, 25, 40)]
    for label, spec in cases:
        try:
            check_topology(label, spec)
        except Exception as exc:  # noqa: BLE001
            FAILS.append(label)
            import traceback
            print(f"FAIL {label}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{len(cases) - len(FAILS)}/{len(cases)} topologies OK" + (f"; FAILED: {FAILS}" if FAILS else ""))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
