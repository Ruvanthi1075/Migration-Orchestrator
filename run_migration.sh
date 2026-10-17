#!/usr/bin/env python3
"""
run_migration.py -- the migration program (runs in its OWN console).

The controller + always-on monitor run separately (run_controller.sh). This
program never needs OS-Ken: it reads the live topology and the saved state
from the persistent store (runtime/qsmo.db, runtime/topology.json), does the
Legacy -> Hybrid cutovers with ovs-vsctl + stunnel, writes every result back
to the store, and exits. Switches it migrated STAY Hybrid -- the stunnel
processes are detached and the monitor keeps watching them -- until a
rollback (automatic from the monitor, or `rollback` below).

    sudo python3 run_migration.py migrate [--beta 0.5] [--switches s3,s5] [--dry-run]
    sudo python3 run_migration.py status
    sudo python3 run_migration.py rollback s3 [s5 ...] | --all
    sudo python3 run_migration.py rebaseline          # re-measure Legacy baselines
    sudo python3 run_migration.py reset               # everything back to Legacy, stunnel killed, state wiped

(use ./run_migration.sh, which handles sudo + the right python environment)

`--beta` is the TARGET FRACTION of the network (by migration cost) that should be
Hybrid. A second run therefore continues from what is already Hybrid instead of
starting over; with beta already reached it reports "nothing to do".
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from migration.migrate_link import MigrationExecutor, find_all_stunnel_pids  # noqa: E402
from migration.monitor import MigrationMonitor  # noqa: E402
from migration.store import Store, StoreLedger, StoreTopology, norm_dpid  # noqa: E402
from network.coverage import compute_coverage, compute_path  # noqa: E402
from network.live_wait import expected_switch_count  # noqa: E402
from optimizer.capability_tracker import CapabilityTracker  # noqa: E402
from optimizer.optimizer import run_sgbm  # noqa: E402

HEARTBEAT_MAX_AGE_S = 20.0


# ---------------------------------------------------------------------- helpers
def _heartbeat(store: Store):
    try:
        with open(os.path.join(store.runtime, "monitor_status.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _monitor_alive(store: Store) -> tuple[bool, str]:
    hb = _heartbeat(store)
    if not hb:
        return False, "no monitor heartbeat yet"
    age = time.time() - hb.get("heartbeat", 0)
    return age <= HEARTBEAT_MAX_AGE_S, f"last heartbeat {age:.0f}s ago (pid {hb.get('pid')})"


def _wait_topology(store: Store, topo: StoreTopology, timeout=240.0):
    age = store.topology_age()
    if age is None or age > 20.0:
        raise SystemExit("The controller is not publishing a topology (is ./run_controller.sh running "
                         "and the network up? state dir: %s)" % store.runtime)
    expected = expected_switch_count()
    print(f"Waiting for the controller's topology (OVS bridges on this host: {expected}) ...", flush=True)
    end, last_sig, since = time.monotonic() + timeout, None, time.monotonic()
    while time.monotonic() < end:
        g = topo.get_graph()
        import networkx as nx
        ok = g.number_of_nodes() > 0 and nx.is_connected(g) and (expected is None or g.number_of_nodes() == expected)
        sig = (frozenset(g.nodes), frozenset(frozenset(e) for e in g.edges))
        if ok and sig == last_sig:
            if time.monotonic() - since >= 5.0:
                return g
        else:
            last_sig, since = sig, time.monotonic()
        print(f"  topology: {g.number_of_nodes()}/{expected} switches, {g.number_of_edges()} links", flush=True)
        time.sleep(2.0)
    raise SystemExit("Topology never became complete/stable. Is ./run_controller.sh running and ./run_network.sh up?")


def _label(graph, dpid):
    return graph.nodes[dpid].get("name") or dpid[-4:]


def _resolve_selection(executor, graph, tokens):
    """Accept bridge names (s3) or dpids."""
    by_name = {}
    for d in graph.nodes:
        try:
            by_name[executor._resolve_switch_name(d)] = d
        except Exception:
            pass
    out = []
    for t in tokens:
        if t in by_name:
            out.append(by_name[t])
        elif norm_dpid(t) in graph.nodes:
            out.append(norm_dpid(t))
        else:
            raise SystemExit(f"unknown switch {t!r}; known: {sorted(by_name)}")
    return out


def _build_flows(graph):
    flows = []
    for index, dpid in enumerate(sorted(graph.nodes)):
        path = compute_path(graph, {"destination_dpid": dpid})
        if len(path) >= 2:
            flows.append({"name": f"flow_{index}", "destination_dpid": dpid, "criticality": 0.8,
                          "security_sla": 0.8, "latency_sla": 0.5})
    return flows


def _make(store):
    topo = StoreTopology(store)
    ledger = StoreLedger(store)
    ex = MigrationExecutor(topo, ledger, store=store, verbose=True)
    return topo, ledger, ex


# ---------------------------------------------------------------------- commands
def cmd_migrate(args):
    store = Store()
    topo, ledger, executor = _make(store)
    alive, why = _monitor_alive(store)
    print(f"Monitor: {'RUNNING' if alive else 'NOT RUNNING'} ({why})")
    if not alive:
        raise SystemExit("Start the controller (./run_controller.sh) and the monitor (./run_monitor.sh) "
                         "in their own terminals first.")

    graph = _wait_topology(store, topo)
    for d in graph.nodes:                                   # nice labels
        try:
            topo.set_name(d, executor._resolve_switch_name(d))
        except Exception:
            pass

    print("\nReconciling saved state with OVS / stunnel ...", flush=True)
    states = executor.reconcile(graph)
    graph = topo.get_graph()
    hybrid = [d for d, s in states.items() if s == "Hybrid"]
    print(f"Start state: Hybrid={[_label(graph, d) for d in hybrid]}  "
          f"Legacy={[_label(graph, d) for d, s in states.items() if s == 'Legacy']}")

    legacy_dpids = [d for d in graph.nodes if graph.nodes[d]["state"] == "Legacy"]
    if args.switches:
        todo = [d for d in _resolve_selection(executor, graph, args.switches.split(",")) if d in legacy_dpids]
        if not todo:
            print("Selected switches are already Hybrid -- nothing to do.")
            return cmd_status(args)
    else:
        todo = None

    # Legacy baselines are measured by the monitor (it owns the OpenFlow echo probe)
    if not args.no_wait_baseline:
        need = [d for d in (todo or legacy_dpids)]
        print("\nWaiting for the monitor's Legacy baselines ...", flush=True)
        end = time.monotonic() + args.baseline_timeout
        while time.monotonic() < end:
            have = store.load_baselines("legacy")
            missing = [d for d in need if d not in have]
            if not missing:
                break
            print(f"  baselines {len(need) - len(missing)}/{len(need)}", flush=True)
            time.sleep(2.0)
        else:
            print(f"WARNING: no Legacy baseline yet for {[_label(graph, d) for d in missing]}; "
                  f"migrating anyway (monitor will compare when it has one)")

    capability = CapabilityTracker()
    for row in ledger.log:                                  # learn from every past attempt
        if row["outcome"] in ("success", "failed"):
            capability.record(row["dpid"], row["outcome"])

    if args.dry_run:
        print("\n--dry-run: nothing changed.")
        return cmd_status(args)

    print("\nMigrating ...", flush=True)
    schedule = []
    if todo is not None:
        for d in todo:
            r = executor.migrate(d)
            capability.record(d, r["outcome"])
            schedule.append(r)
            print(f"  {_label(graph, d)} -> {r['outcome']}")
    else:
        flows = _build_flows(graph)
        if len(flows) < 1:
            raise SystemExit("No multi-hop flows can be built from this topology.")
        total = sum(capability.cost(d) for d in graph.nodes)
        spent = sum(capability.cost(d) for d in hybrid)
        budget = args.beta * total - spent
        print(f"  target: {args.beta:.0%} of network cost Hybrid | already Hybrid cost={spent:.2f} "
              f"of {total:.2f} | budget for this run={max(budget, 0):.2f}")
        cheapest = min((capability.cost(d) for d in legacy_dpids), default=None)
        frac = spent / total if total else 0.0
        print(f"  Hybrid now: {frac:.0%} of network cost (target {args.beta:.0%}) | "
              f"{len(hybrid)} Hybrid, {len(legacy_dpids)} Legacy")
        if not legacy_dpids:
            print("  Every switch is already Hybrid -- nothing to migrate.")
        elif budget <= 1e-9 or budget + 1e-9 < cheapest:
            need = min(1.0, (spent + cheapest) / total) if total else 1.0
            sug = min(1.0, math.ceil(need * 100) / 100)
            print(f"  The target is reached: the remaining budget ({max(budget, 0):.2f}) is smaller than the cheapest "
                  f"Legacy switch ({cheapest:.2f}), so SGBM stops -- the {len(legacy_dpids)} Legacy switch(es) are "
                  f"NOT blocked, the target is just met.\n"
                  f"  To migrate more:  ./run_migration.sh migrate --beta {sug:.2f}   (or --beta 1.0 for all)\n"
                  f"  Or pick switches: ./run_migration.sh migrate --switches "
                  f"{','.join(_label(graph, d) for d in legacy_dpids[:2])}")
        else:
            schedule, cov = run_sgbm(graph, flows, budget, executor.migrate,
                                     capability=capability, verbose=True)
            print(f"  coverage after this run: {compute_coverage(graph, flows):.3f}")

    ok = [r["dpid"] for r in schedule if r["outcome"] == "success"]
    bad = [r["dpid"] for r in schedule if r["outcome"] != "success"]
    print(f"\nMigrated now: {[_label(graph, d) for d in ok]}   Failed: {[_label(graph, d) for d in bad]}")

    if ok and not args.no_wait_monitor:
        print("Waiting for the monitor to measure the new Hybrid baselines ...", flush=True)
        end = time.monotonic() + 90
        while time.monotonic() < end:
            have = store.load_baselines("hybrid")
            if all(d in have for d in ok):
                break
            time.sleep(2.0)
    print()
    cmd_status(args)
    print("\nDone. Hybrid switches stay Hybrid (tunnels keep running, the monitor keeps watching).")
    print("Run again any time -- it continues from this state.")


def cmd_status(_args):
    store = Store()
    topo, ledger, executor = _make(store)
    alive, why = _monitor_alive(store)
    hb = _heartbeat(store) or {}
    graph = topo.get_graph()
    rows = store.all_switches()
    legacy_b, hybrid_b = store.load_baselines("legacy"), store.load_baselines("hybrid")
    print(f"Monitor: {'RUNNING' if alive else 'NOT RUNNING'} ({why})")
    print(f"Topology: {graph.number_of_nodes()} switches, {graph.number_of_edges()} links")
    print(f"Stunnel processes (this repo): {len(find_all_stunnel_pids(executor.hybrid_conf_dir))}")
    print()
    print(f"{'switch':<8}{'dpid':<18}{'state':<8}{'ovs target':<26}{'conn':<6}{'legacy ms':>10}{'hybrid ms':>10}  health")
    for d in sorted(set(rows) | set(graph.nodes)):
        st = rows.get(d, {}).get("state", "Legacy")
        try:
            br = executor._resolve_switch_name(d)
            tg = ",".join(executor._ovs_targets(br)) or "NONE"
            cn = "yes" if any(executor._controller_target_connected(br, t) for t in tg.split(",")) else "NO"
        except Exception:
            br, tg, cn = "-", "-", "-"
        lb = legacy_b.get(d, {}).get("median_ms")
        hb_ = hybrid_b.get(d, {}).get("median_ms")
        h = (hb.get("switches", {}).get(d, {}) or {}).get("status") or ("-" if st == "Legacy" else "pending")
        print(f"{br:<8}{d:<18}{st:<8}{tg:<26}{cn:<6}{(f'{lb:.2f}' if lb else '-'):>10}{(f'{hb_:.2f}' if hb_ else '-'):>10}  {h}")
    print(f"\nRollback rate (reverted / (success+reverted)): {ledger.compute_rollback_rate():.2f}  "
          f"| ledger entries: {len(ledger.log)}")
    ev = store.recent_events(8)
    if ev:
        print("Recent events:")
        for e in ev:
            print(f"  {e['ts'][11:19]} [{e['source']}] {e['kind']}: {e['message']}")


def cmd_rollback(args):
    store = Store()
    topo, ledger, executor = _make(store)
    graph = topo.get_graph()
    hybrid = [d for d, s in store.states().items() if s == "Hybrid"]
    if args.all:
        targets = hybrid
    else:
        targets = [d for d in _resolve_selection(executor, graph, args.switches) if d in hybrid]
    if not targets:
        print("No Hybrid switches selected.")
        return
    monitor = MigrationMonitor(executor, latency_probe=lambda d: {"ok": False, "latency_ms": None, "error": "n/a"})
    for d in targets:
        r = monitor.rollback(d, "manual rollback (run_migration.py)")
        if r["outcome"] == "failed" and "no successful migration record" in str(r.get("reason")):
            r = executor.force_rollback(d, "manual rollback (run_migration.py)")
        print(f"  {_label(graph, d)}: {r['outcome']}" + (f" ({r.get('reason')})" if r["outcome"] != "reverted" else ""))
    print()
    cmd_status(args)


def cmd_rebaseline(_args):
    Store().kv_set("rebaseline", True)
    print("Requested. The monitor will re-measure every Legacy switch's baseline within a few seconds.")


def cmd_reset(args):
    store = Store()
    topo, ledger, executor = _make(store)
    if not args.yes:
        if input("Reset EVERYTHING (all switches -> Legacy, kill stunnel, wipe saved state)? [y/N] ").lower() != "y":
            return
    graph = topo.get_graph()
    if graph.number_of_nodes() == 0:                        # no controller: use OVS bridges directly
        from migration.port_registry import discover_ovs_bridges
        import networkx as nx
        graph = nx.Graph()
        for d in discover_ovs_bridges():
            graph.add_node(d)
    executor.reset_runtime(graph)
    store.wipe()
    print("Reset done. Next run starts from a clean Legacy network "
          "(the monitor re-measures baselines automatically).")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    m = sub.add_parser("migrate", help="run SGBM + Break-Before-Make from the saved state")
    m.add_argument("--beta", type=float, default=0.5, help="target Hybrid fraction of total cost (default 0.5)")
    m.add_argument("--switches", help="migrate exactly these (comma separated bridge names or dpids), skip SGBM")
    m.add_argument("--dry-run", action="store_true")
    m.add_argument("--no-wait-baseline", action="store_true")
    m.add_argument("--no-wait-monitor", action="store_true")
    m.add_argument("--baseline-timeout", type=float, default=180.0)
    m.set_defaults(fn=cmd_migrate)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    r = sub.add_parser("rollback")
    r.add_argument("switches", nargs="*")
    r.add_argument("--all", action="store_true")
    r.set_defaults(fn=cmd_rollback)
    sub.add_parser("rebaseline").set_defaults(fn=cmd_rebaseline)
    z = sub.add_parser("reset")
    z.add_argument("--yes", action="store_true")
    z.set_defaults(fn=cmd_reset)
    args = ap.parse_args()
    if not args.cmd:
        args = ap.parse_args(["migrate"] + sys.argv[1:])
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo (ovs-vsctl and stunnel need root): ./run_migration.sh ...")
    args.fn(args)


if __name__ == "__main__":
    main()

