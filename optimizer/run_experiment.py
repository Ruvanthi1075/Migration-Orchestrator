"""
run_experiment.py
Budget sweep for Table I / Fig. 2 -- the script that CALLS run_sgbm().

Everything budget-related that must not live in optimizer.py lives here:

  1. wiring      annotate_flow_weights() is called BEFORE run_sgbm(), so
                 every flow carries criticality / security_sla /
                 latency_sla (otherwise flow_weight() raises KeyError).
  2. calibration calibrate_base_costs() measures, per switch, N classical
                 and N hybrid (-groups X25519MLKEM768) handshakes via
                 MigrationExecutor._probe_handshake, and turns them into
                 per-switch base costs with budget_costs().
  3. sweep       B = beta * sum(cost(s)), beta in {0.25, 0.5, 0.75, 1.0}.
                 B is a knob, not derived from anything.

Usage
-----
    python optimizer/run_experiment.py --simulate      # no OVS/OpenSSL needed

Live use (from your orchestrator_main.py, after topology discovery):

    from run_experiment import calibrate_base_costs, sweep_budgets
    base = calibrate_base_costs(executor, list(G.nodes), n=10)
    rows = sweep_budgets(lambda: topology.get_graph_copy(), raw_flows,
                         executor.migrate, base)
"""

import os
import statistics
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "network"))
sys.path.insert(0, os.path.join(_HERE, "..", "migration"))

from capability_tracker import CapabilityTracker, budget_costs  # noqa: E402
from coverage import annotate_flow_weights, compute_coverage, flow_weight  # noqa: E402
from optimizer import run_sgbm  # noqa: E402

BETAS = (0.25, 0.5, 0.75, 1.0)


# ---------------------------------------------------------------------
# 2. Calibration pass (needs the real testbed)
# ---------------------------------------------------------------------

def calibrate_base_costs(executor, dpids, n=10):
    """
    For each dpid take `n` handshakes with classical `s_client` and `n`
    with `-groups <hybrid_group>` on executor.pqc_openssl_bin, using
    MigrationExecutor._probe_handshake (it supports both). Failed probes
    are dropped from the median. Returns {dpid: cost_base(s)}.

    Caveat to state in the paper: the classical probe uses the shared
    legacy switch cert, so classical timings differ across switches only
    by noise; the per-switch spread in delta comes from each switch's
    own ML-DSA hybrid cert. The shared Hybrid stunnel server must be up;
    _ensure_stunnel_pair_up() starts it (and that switch's client,
    which migrate() would start anyway).
    """
    from migrate_link import local_port_for_dpid  # lazy: needs testbed deps

    classical_ms, hybrid_ms = {}, {}
    for dpid in dpids:
        name = executor._resolve_switch_name(dpid)
        key, cert = executor._hybrid_switch_cert_paths(name)
        ok, why = executor._ensure_stunnel_pair_up(
            dpid, name, local_port_for_dpid(dpid))
        if not ok:
            raise RuntimeError(f"calibration: Hybrid server/client not up for {dpid}: {why}")
        c, h = [], []
        for _ in range(n):
            ok_c, ms_c = executor._probe_handshake(
                executor.system_openssl_bin, executor.controller_ip,
                executor.legacy_port, executor.legacy_switch_cert,
                executor.legacy_switch_key, executor.legacy_ca_cert)
            if ok_c:
                c.append(ms_c)
            ok_h, ms_h = executor._probe_handshake(
                executor.pqc_openssl_bin, executor.hybrid_server_host,
                executor.hybrid_server_port, cert, key,
                executor.hybrid_ca_cert, groups=executor.hybrid_group)
            if ok_h:
                h.append(ms_h)
        if not c or not h:
            raise RuntimeError(f"calibration: no successful handshakes for {dpid}")
        classical_ms[dpid], hybrid_ms[dpid] = c, h
    return budget_costs(classical_ms, hybrid_ms)


# ---------------------------------------------------------------------
# 1 + 3. Wiring and sweep
# ---------------------------------------------------------------------

def prepare_flows(G, raw_flows, edge_utilization=None):
    """annotate_flow_weights() -> flows ready for run_sgbm(). b_f is taken
    from each flow's "bandwidth_mbps"; edge_utilization is U_e per
    coverage.edge_key() (empty dict if not measured)."""
    bandwidth = {f["name"]: f.get("bandwidth_mbps", 0.0) for f in raw_flows}
    return annotate_flow_weights(G, raw_flows, edge_utilization or {}, bandwidth)


def sweep_budgets(make_graph, raw_flows, migrate_fn_factory, base_costs,
                  betas=BETAS, edge_utilization=None, verbose=False):
    """
    One run_sgbm() per beta, each on a FRESH graph and FRESH tracker.
    B = beta * sum(cost(s)) over all switches (cost at zero failures).

    make_graph          : () -> networkx graph, all Legacy
    migrate_fn_factory  : () -> migrate_fn(dpid) -> result dict
    Returns list of dicts: beta, B, spent_switches, coverage, max_coverage.
    """
    rows = []
    for beta in betas:
        G = make_graph()
        flows = prepare_flows(G, raw_flows, edge_utilization)
        tracker = CapabilityTracker(base_costs=base_costs)
        B = beta * tracker.total_cost(list(G.nodes))
        schedule, cov = run_sgbm(G, flows, B, migrate_fn_factory(),
                                 capability=tracker, verbose=verbose)
        rows.append({
            "beta": beta, "B": B,
            "migrations": len(schedule),
            "coverage": cov,
            "max_coverage": sum(flow_weight(f) for f in flows),
        })
    return rows


# ---------------------------------------------------------------------
# Simulated run (no testbed)
# ---------------------------------------------------------------------

def _simulate():
    import datetime
    import random

    import networkx as nx

    dpids = [f"{i:016x}" for i in range(7)]
    s0, s1, s2, s3, s4, s5, s6 = dpids

    def make_graph():
        G = nx.Graph()
        for d in dpids:
            G.add_node(d, state="Legacy")
        for a, b in ((s1, s0), (s2, s0), (s1, s2), (s3, s1), (s4, s1), (s5, s2), (s6, s2)):
            G.add_edge(a, b)
        return G

    def flow(name, dpid, c, i, a, cls, q):
        return {"name": name, "destination_dpid": dpid, "bandwidth_mbps": 10,
                "impact_confidentiality": c, "impact_integrity": i,
                "impact_availability": a, "traffic_class": cls, "q_delay_ms": q}

    raw_flows = [
        flow("ctrl_s3", s3, "High", "Moderate", "Low", "delay_sensitive", 10),
        flow("ctrl_s4", s4, "High", "High", "Moderate", "delay_sensitive", 20),
        flow("ctrl_s5", s5, "Low", "Low", "Moderate", "loss_sensitive", 5000),
        flow("ctrl_s6", s6, "Moderate", "Moderate", "Moderate", "delay_sensitive", 40),
    ]

    # Fake calibration timings (ms) so budget_costs() is exercised.
    rng = random.Random(0)
    classical = {d: [20 + rng.random() for _ in range(10)] for d in dpids}
    hybrid = {d: [24 + 6 * i / 6 + rng.random() for _ in range(10)]
              for i, d in enumerate(dpids)}
    base = budget_costs(classical, hybrid)
    assert all(0.259 - 1e-9 <= v <= 0.5924 for v in base.values())

    def factory():
        def fn(dpid):
            return {"dpid": dpid, "outcome": "success",
                    "baseline": {"latency_ms": 20.0, "failure_rate": 0.0, "overhead_bytes": 0},
                    "post": {"latency_ms": 24.0, "failure_rate": 0.0, "overhead_bytes": 512},
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        return fn

    rows = sweep_budgets(make_graph, raw_flows, factory, base)
    print(f"{'beta':>5} {'B':>7} {'migrated':>9} {'C':>7} {'C/Cmax':>7}")
    for r in rows:
        print(f"{r['beta']:>5.2f} {r['B']:>7.3f} {r['migrations']:>9d} "
              f"{r['coverage']:>7.3f} {r['coverage'] / r['max_coverage']:>7.2%}")
    covs = [r["coverage"] for r in rows]
    assert covs == sorted(covs), "coverage must not fall as beta grows"
    assert abs(rows[-1]["coverage"] - rows[-1]["max_coverage"]) < 1e-9, "beta=1 must fully cover"
    print("\nrun_experiment.py simulated sweep OK")


if __name__ == "__main__":
    if "--simulate" in sys.argv:
        _simulate()
    else:
        print(__doc__)

