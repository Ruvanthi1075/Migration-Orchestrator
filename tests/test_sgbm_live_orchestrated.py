"""Integrated live SGBM -> BBM -> monitoring -> automatic rollback demo.

Load through osken-manager with Mininet already running. This is a controlled
DEMO: metric values are explicitly injected to exercise the configured
three-consecutive-observation rollback policy. OVS connectivity and migration /
rollback execution remain live and are verified against OVS and TopologyAdapter.

Safety: requires a connected seven-switch topology with every switch Legacy-only
at startup. It migrates a budget-selected batch, monitors every successful
Hybrid switch, then exercises automatic rollback for each using injected degraded
metrics. On success all switches return to Legacy before executor shutdown.
"""
import os
import sys
import time
import threading
import traceback

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
from network.coverage import compute_coverage, compute_path


class LiveSGBMOrchestratedDemo(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executor = None
        self._thread = threading.Thread(
            target=self._run_demo,
            name="live-sgbm-monitor-rollback-demo",
            daemon=True,
        )
        self._thread.start()

    @staticmethod
    def _find_topology():
        for name, app in app_manager.AppManager.get_instance().applications.items():
            if all(hasattr(app, attr) for attr in ("get_graph", "get_state", "set_state")):
                print(f"Using authoritative topology app: {name}", flush=True)
                return app
        raise RuntimeError("Live TopologyAdapter not found")

    @staticmethod
    def _build_flows(graph):
        flows = []
        for index, dpid in enumerate(sorted(graph.nodes)):
            path = compute_path(graph, {"destination_dpid": dpid})
            if len(path) >= 2:
                flows.append({
                    "name": f"orchestrated_flow_{index}",
                    "destination_dpid": dpid,
                    "criticality": 0.8,
                    "security_sla": 0.8,
                    "latency_sla": 0.5,
                })
        if len(flows) < 2:
            raise RuntimeError(f"Need at least two multi-hop flows; found {len(flows)}")
        return flows

    def _run_demo(self):
        migrated = []
        should_shutdown = True
        try:
            print("\n" + "=" * 68 + "\n  QSMO | HYBRID CONTROLLER MIGRATION\n  SGBM + BBM  |  HEALTH MONITORING  |  AUTOMATIC RECOVERY\n" + "=" * 68, flush=True)
            topology = self._find_topology()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                graph = topology.get_graph()
                if graph.number_of_nodes() == 7 and graph.number_of_edges() >= 6:
                    break
                print(
                    f"Waiting for topology: {graph.number_of_nodes()}/7 switches, "
                    f"{graph.number_of_edges()} links", flush=True
                )
                time.sleep(1)

            graph = topology.get_graph()
            if graph.number_of_nodes() != 7 or not nx.is_connected(graph):
                raise RuntimeError(
                    f"Preflight requires connected 7-switch graph; got "
                    f"{graph.number_of_nodes()} nodes / {graph.number_of_edges()} edges"
                )
            nonlegacy = [d for d in graph.nodes if topology.get_state(d) != "Legacy"]
            if nonlegacy:
                raise RuntimeError(
                    f"Safety stop: all switches must start Legacy; non-Legacy={nonlegacy}"
                )

            ledger = MigrationLedger()
            self.executor = MigrationExecutor(topology, ledger, verbose=True)
            legacy_target = f"ssl:{self.executor.controller_ip}:{self.executor.legacy_port}"

            # OVS may report the full LLDP topology before every TLS/OpenFlow
            # controller session has completed reconnecting. Wait boundedly for
            # every bridge to be Legacy-only AND connected; never bypass the
            # connection safety check.
            bridge_by_dpid = {
                dpid: self.executor._resolve_switch_name(dpid)
                for dpid in graph.nodes
            }
            readiness_deadline = time.monotonic() + 60.0
            last_readiness = {}
            last_print = 0.0
            while True:
                all_ready = True
                last_readiness = {}
                for dpid, bridge in bridge_by_dpid.items():
                    rc, targets_out, err = self.executor.runner.run(
                        ["ovs-vsctl", "get-controller", bridge], timeout=5
                    )
                    targets = targets_out.split()
                    connected = (
                        rc == 0
                        and targets == [legacy_target]
                        and self.executor._controller_target_connected(bridge, legacy_target)
                    )
                    last_readiness[bridge] = {
                        "rc": rc,
                        "targets": targets,
                        "connected": connected,
                        "error": err.strip(),
                    }
                    if not connected:
                        all_ready = False

                if all_ready:
                    print(
                        "Preflight passed: all 7 bridges are Legacy-only and controller-connected.",
                        flush=True,
                    )
                    break

                now = time.monotonic()
                if now >= readiness_deadline:
                    raise RuntimeError(
                        "Safety stop: Legacy controller readiness timed out after 60s; "
                        f"per-bridge status={last_readiness}"
                    )
                if now - last_print >= 5.0:
                    waiting = [
                        bridge for bridge, status in last_readiness.items()
                        if not status["connected"]
                    ]
                    print(
                        "Waiting for Legacy controller connections on: "
                        f"{waiting} (bounded timeout 60s)",
                        flush=True,
                    )
                    last_print = now
                time.sleep(0.5)

            flows = self._build_flows(graph)
            capability = CapabilityTracker()
            beta = 0.50
            total_cost = sum(capability.cost(d) for d in graph.nodes)
            budget = beta * total_cost
            initial_coverage = compute_coverage(graph, flows)
            print(
                f"Preflight passed: switches=7, flows={len(flows)}, beta={beta:.2f}, "
                f"budget={budget:.3f}, initial_coverage={initial_coverage:.3f}",
                flush=True,
            )
            print("PHASE 1/3: Running real SGBM selection and BBM migration", flush=True)
            schedule, sgbm_coverage = run_sgbm(
                graph, flows, budget, self.executor.migrate,
                capability=capability, verbose=True,
            )
            migrated = [r["dpid"] for r in schedule if r.get("outcome") == "success"]
            if len(migrated) < 2:
                raise RuntimeError(
                    f"Expected a multi-switch successful batch; successful DPIDs={migrated}; "
                    f"schedule={schedule}"
                )
            for dpid in migrated:
                if topology.get_state(dpid) != "Hybrid":
                    raise AssertionError(f"Successful migration {dpid} is not Hybrid in topology")
            print(
                f"SGBM/BBM phase passed: migrated={migrated}, "
                f"optimizer_coverage={sgbm_coverage:.3f}", flush=True
            )

            # Explicitly labeled injected metrics exercise the policy. They are not
            # claimed to be measured production telemetry.
            print(
                "PHASE 2/3: Live OVS connectivity observations; controlled DEMO metric "
                "injection enabled (latency=1000ms, failure_rate=0.50)", flush=True
            )
            monitor = MigrationMonitor(
                self.executor,
                config=MonitorConfig(
                    poll_interval=0.2,
                    latency_threshold_ms=100.0,
                    failure_rate_threshold=0.10,
                    consecutive_degraded_observations=3,
                ),
                metric_provider=lambda _dpid: {
                    "latency_ms": 1000.0,
                    "failure_rate": 0.50,
                },
            )

            rollback_results = {}
            for observation_no in range(1, 4):
                reports = monitor.monitor_once()
                by_dpid = {r.get("dpid"): r for r in reports}
                for dpid in migrated:
                    report = by_dpid.get(dpid)
                    if report is None:
                        # A switch already rolled back must not reappear as Hybrid.
                        if observation_no < 3 or topology.get_state(dpid) != "Legacy":
                            raise AssertionError(
                                f"Monitor omitted still-Hybrid switch {dpid}: {reports}"
                            )
                        continue
                    print(
                        f"  {self.executor._resolve_switch_name(dpid):<5} | "
                        f"Sample {observation_no}/3 | "
                        f"Health: {report['status'].upper():<9} | "
                        f"Latency: {report.get('latency_ms')} ms | "
                        f"Failure: {report.get('failure_rate', 0) * 100:.0f}% | "
                        f"Recovery: {report.get('recovery', {}).get('outcome', 'Monitoring').upper()}", flush=True
                    )
                    if report.get("recovery"):
                        rollback_results[dpid] = report["recovery"]
                if observation_no < 3:
                    for dpid in migrated:
                        if topology.get_state(dpid) != "Hybrid":
                            raise AssertionError(
                                f"{dpid} rolled back before third degraded observation"
                            )

            print("PHASE 3/3: Verifying actual Legacy rollback and shared ledger", flush=True)
            for dpid in migrated:
                if topology.get_state(dpid) != "Legacy":
                    raise AssertionError(f"Rollback did not restore topology state for {dpid}")
                bridge = self.executor._resolve_switch_name(dpid)
                if not self.executor._controller_target_connected(bridge, legacy_target):
                    raise AssertionError(f"Legacy SSL connection not verified for {bridge}")
                outcomes = [event["outcome"] for event in ledger.get_history(dpid)]
                if outcomes != ["success", "reverted"]:
                    raise AssertionError(f"Unexpected ledger outcomes for {dpid}: {outcomes}")
                if rollback_results.get(dpid, {}).get("outcome") != "reverted":
                    raise AssertionError(f"No successful automatic rollback report for {dpid}")
                print(
                    f"Verified {bridge}: OVS Legacy SSL connected, topology=Legacy, "
                    f"ledger={outcomes}", flush=True
                )

            final_coverage = compute_coverage(topology.get_graph(), flows)
            print("\nINTEGRATED LIVE SGBM + MONITOR + AUTOMATIC ROLLBACK PASSED", flush=True)
            print(
                f"migrated_then_reverted={len(migrated)}; initial_coverage="
                f"{initial_coverage:.3f}; migration_coverage={sgbm_coverage:.3f}; "
                f"final_coverage={final_coverage:.3f}; rollback_rate="
                f"{ledger.compute_rollback_rate():.3f}", flush=True
            )
            print(
                "Metric trigger was controlled synthetic demo input; controller connectivity, "
                "BBM cutovers, rollback targets, topology states, and ledger outcomes were live-verified.",
                flush=True,
            )
            print("Run `pingall` in Mininet for final data-plane validation.", flush=True)

        except Exception as exc:
            print(
                f"\nINTEGRATED LIVE DEMO FAILED: {type(exc).__name__}: {exc}",
                flush=True,
            )
            traceback.print_exc()
            # Never terminate stunnel endpoints while any switch remains Hybrid;
            # doing so would leave OVS pointing at dead local listeners.
            if self.executor is not None:
                try:
                    graph = self.executor.topology.get_graph()
                    stranded = [
                        d for d in graph.nodes
                        if self.executor.topology.get_state(d) == "Hybrid"
                    ]
                    if stranded:
                        should_shutdown = False
                        print(
                            "Safety preservation: leaving executor/tunnel processes alive "
                            f"for Hybrid switches {stranded}; do not reset blindly.",
                            flush=True,
                        )
                except Exception:
                    should_shutdown = False
        finally:
            if self.executor is not None and should_shutdown:
                try:
                    self.executor.shutdown()
                except Exception as exc:
                    print(f"Warning: executor.shutdown() failed: {exc}", flush=True)
