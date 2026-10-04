"""Live SGBM -> actual BBM migration -> 3-strike monitor -> actual Legacy rollback.

Load as an OS-Ken app alongside network/topology.py and controller/simple_l2_switch.py.
Requires an already-running Mininet topology and configured OQS/stunnel environment.
This test intentionally performs one real switch migration and then one real rollback.
"""
import os
import sys
import time
import threading
import networkx as nx
from os_ken.base import app_manager
from os_ken.ofproto import ofproto_v1_3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from migration.ledger import MigrationLedger
from migration.migrate_link import MigrationExecutor
from migration.monitor import MigrationMonitor, MonitorConfig
from optimizer.optimizer import run_sgbm
from optimizer.capability_tracker import CapabilityTracker
from network.coverage import core_switch

class LiveBBMRollbackTest(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        threading.Thread(target=self._run, name="live-bbm-rollback", daemon=True).start()

    def _find_topology(self):
        for app in app_manager.AppManager.get_instance().applications.values():
            if hasattr(app, "get_graph") and hasattr(app, "get_state") and hasattr(app, "set_state"):
                return app
        raise RuntimeError("Live TopologyAdapter not found")

    def _run(self):
        executor = None
        try:
            print("\n=== LIVE SGBM + BBM + AUTOMATIC ROLLBACK ===", flush=True)
            topology = self._find_topology()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                G = topology.get_graph()
                if G.number_of_nodes() >= 7 and G.number_of_edges() >= 1:
                    break
                time.sleep(1)
            G = topology.get_graph()
            if G.number_of_nodes() != 7 or not nx.is_connected(G):
                raise RuntimeError(f"Preflight requires connected 7-switch graph; got {G.number_of_nodes()} nodes / {G.number_of_edges()} edges")
            nonlegacy = [d for d in G.nodes if topology.get_state(d) != "Legacy"]
            if nonlegacy:
                raise RuntimeError(f"Safety stop: switches not Legacy at test start: {nonlegacy}")

            ledger = MigrationLedger()
            executor = MigrationExecutor(topology, ledger, verbose=True)
            expected_legacy = f"ssl:{executor.controller_ip}:{executor.legacy_port}"
            for dpid in G.nodes:
                bridge = executor._resolve_switch_name(dpid)
                rc, current_targets, err = executor.runner.run(
                    ["ovs-vsctl", "get-controller", bridge]
                )
                targets = current_targets.split()
                if rc != 0 or targets != [expected_legacy]:
                    raise RuntimeError(
                        f"Safety stop: {bridge} must start Legacy-only; "
                        f"got rc={rc}, targets={targets}, error={err.strip()}"
                    )
            core = core_switch(G)
            flow = [{"name": "bbm_rollback_probe", "destination_dpid": core,
                     "criticality": 0.9, "security_sla": 0.9, "latency_sla": 0.8}]
            capability = CapabilityTracker()
            budget = capability.cost(core) + 1e-6
            print(f"Preflight OK: nodes={G.number_of_nodes()} edges={G.number_of_edges()} core={core}", flush=True)
            print("SGBM will use a one-switch budget; actual cutover is BREAK-BEFORE-MAKE (BBM).", flush=True)

            schedule, _coverage = run_sgbm(G, flow, budget, executor.migrate,
                                           capability=capability, verbose=True)
            successes = [r for r in schedule if r.get("outcome") == "success"]
            if len(successes) != 1:
                raise RuntimeError(f"Expected exactly one successful live SGBM migration; schedule={schedule}")
            dpid = successes[0]["dpid"]
            if topology.get_state(dpid) != "Hybrid":
                raise AssertionError(f"SGBM result did not leave {dpid} Hybrid")
            print(f"REAL BBM MIGRATION PASSED for {dpid}; starting 3-strike degradation test", flush=True)

            monitor = MigrationMonitor(
                executor,
                config=MonitorConfig(poll_interval=0.1, latency_threshold_ms=100.0,
                                     failure_rate_threshold=0.10,
                                     consecutive_degraded_observations=3),
                metric_provider=lambda _dpid: {"latency_ms": 1000.0, "failure_rate": 0.5},
            )
            reports = []
            for n in range(1, 4):
                reports = monitor.monitor_once()
                report = next((x for x in reports if x.get("dpid") == dpid), None)
                if report is None:
                    raise AssertionError(f"Monitor did not observe migrated switch {dpid}: {reports}")
                recovery = report.get("recovery")
                print(f"Degraded observation {n}/3: status={report['status']} recovery={recovery}", flush=True)
                if n < 3 and topology.get_state(dpid) != "Hybrid":
                    raise AssertionError("Rollback happened before the configured third degraded observation")

            if not recovery or recovery.get("outcome") != "reverted":
                raise AssertionError(f"Third degraded observation did not complete rollback: {reports}")
            if topology.get_state(dpid) != "Legacy":
                raise AssertionError("Topology state did not return to Legacy")
            legacy_target = f"ssl:{executor.controller_ip}:{executor.legacy_port}"
            if not executor._controller_target_connected(executor._resolve_switch_name(dpid), legacy_target):
                raise AssertionError("OVS did not verify the Legacy SSL target connected")
            history = ledger.get_history(dpid)
            if [e["outcome"] for e in history] != ["success", "reverted"]:
                raise AssertionError(f"Unexpected shared ledger outcomes: {history}")
            print("\nLIVE BBM AUTOMATIC ROLLBACK PASSED", flush=True)
            print(f"Verified: DPID={dpid}; Legacy SSL connected; topology=Legacy; ledger=success,reverted", flush=True)
            print("Now run `pingall` in the Mininet CLI to complete the data-plane check.", flush=True)
        except Exception as exc:
            print(f"\nLIVE BBM ROLLBACK TEST FAILED: {type(exc).__name__}: {exc}", flush=True)
            import traceback
            traceback.print_exc()
        finally:
            if executor is not None:
                executor.shutdown()
