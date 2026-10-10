"""Integrated live SGBM -> BBM -> monitoring -> automatic rollback demo.

Load through osken-manager with Mininet already running. This is a
controlled live demo: latency and connectivity telemetry come from the real
OS-Ken OpenFlow Echo probe and live OVS state. No degraded metric values are
injected. The monitor runs continuously until the demo is stopped.

Safety: requires a connected live topology (any size >= 3 switches) with every switch
Legacy-only at startup. Expected size is taken from QSMO_EXPECTED_SWITCHES if set,
otherwise from the number of OVS bridges, otherwise from topology stability. It migrates a budget-selected batch, monitors every successful
Hybrid switch continuously, and applies automatic rollback only when the real
monitoring policy detects repeated measured degradation.
"""
import os
import sys
import time
import threading
import traceback
import signal

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
from network.live_wait import expected_switch_count, wait_for_stable_topology


class LiveSGBMOrchestratedDemo(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    @staticmethod
    def _find_echo_probe():
        """Find the live QSMO OpenFlow Echo probe loaded by OS-Ken."""
        for name, app in app_manager.AppManager.get_instance().applications.items():
            if (
                callable(getattr(app, "probe", None))
                and callable(getattr(app, "datapath_ids", None))
            ):
                print(f"Using live Echo probe app: {name}", flush=True)
                return app

        raise RuntimeError(
            "Live QSMO Echo probe not found. "
            "Make sure controller/qsmo_echo_probe.py is loaded."
        )

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

            # ----------------------------------------------------------
            # WAIT FOR COMPLETE LIVE TOPOLOGY DISCOVERY (any size)
            # ----------------------------------------------------------
            # OS-Ken discovers switches and LLDP links asynchronously.
            # Expected size: QSMO_EXPECTED_SWITCHES, else the OVS bridge
            # count, else just wait for a connected, unchanging graph.
            expected = expected_switch_count()
            graph = wait_for_stable_topology(topology, expected=expected, min_switches=3)
            print(
                f"Topology ready | switches={graph.number_of_nodes()} | "
                f"links={graph.number_of_edges()} | expected={expected}",
                flush=True,
            )

            nonlegacy = [d for d in graph.nodes if topology.get_state(d) != "Legacy"]
            if nonlegacy:
                raise RuntimeError(f"Safety stop: switches not Legacy at start: {nonlegacy}")

            # ----------------------------------------------------------
            # PHASE 0: LIVE OPENFLOW ECHO PROBE PRECHECK
            # ----------------------------------------------------------
            print(
                "\nPHASE 0: Connecting to live OS-Ken Echo probe",
                flush=True,
            )

            echo_probe = self._find_echo_probe()

            echo_deadline = time.monotonic() + 60.0
            successful_dpid = None
            successful_result = None

            while time.monotonic() < echo_deadline:
                connected_dpids = list(echo_probe.datapath_ids())

                if not connected_dpids:
                    print(
                        "Waiting for OpenFlow datapaths to connect to Echo probe...",
                        flush=True,
                    )
                    time.sleep(1.0)
                    continue

                print(
                    f"Echo probe currently sees "
                    f"{len(connected_dpids)} datapath(s): "
                    f"{connected_dpids}",
                    flush=True,
                )

                # A DPID can briefly appear before its datapath object is
                # fully usable. Probe every currently known DPID and accept
                # the first REAL successful Echo reply.
                for dpid in connected_dpids:
                    try:
                        result = echo_probe.probe(dpid)
                    except Exception as exc:
                        print(
                            f"Echo attempt failed for DPID={dpid}: {exc}",
                            flush=True,
                        )
                        continue

                    if result.get("ok"):
                        successful_dpid = dpid
                        successful_result = result
                        break

                    print(
                        f"Echo not ready for DPID={dpid}: "
                        f"{result.get('error')}",
                        flush=True,
                    )

                if successful_dpid is not None:
                    break

                print(
                    "No datapath has produced a successful Echo reply yet; "
                    "waiting for switch connections to stabilize...",
                    flush=True,
                )
                time.sleep(1.0)

            if successful_dpid is None:
                raise RuntimeError(
                    "No live OpenFlow datapath produced a successful Echo "
                    "reply within 60 seconds."
                )

            print(
                f"REAL ECHO TEST | DPID={successful_dpid} | "
                f"result={successful_result}",
                flush=True,
            )

            print(
                f"REAL ECHO SUCCESS | DPID={successful_dpid} | "
                f"RTT={successful_result['latency_ms']:.3f} ms",
                flush=True,
            )

            # Give the topology/controller connection set a short settling
            # period before taking the full Legacy baseline.
            time.sleep(2.0)

            # ----------------------------------------------------------
            # CREATE THE REAL MIGRATION EXECUTOR
            # ----------------------------------------------------------
            ledger = MigrationLedger()
            self.executor = MigrationExecutor(
                topology,
                ledger,
                verbose=True,
            )

            legacy_target = (
                f"ssl:{self.executor.controller_ip}:"
                f"{self.executor.legacy_port}"
            )

            print(
                f"MigrationExecutor ready | Legacy target={legacy_target}",
                flush=True,
            )

            # ----------------------------------------------------------
            # BUILD LIVE TOPOLOGY GRAPH
            # ----------------------------------------------------------
            graph = self.executor.topology.get_graph()

            if graph is None or len(graph.nodes()) == 0:
                raise RuntimeError(
                    "Live topology graph is empty; cannot perform "
                    "Legacy controller readiness check."
                )

            print(
                f"Live topology graph ready | "
                f"nodes={len(graph.nodes())} | "
                f"edges={len(graph.edges())}",
                flush=True,
            )

            # ----------------------------------------------------------
            # LEGACY CONTROLLER READINESS CHECK
            # ----------------------------------------------------------
            # Do not start the real baseline until every expected OVS
            # bridge is connected to the Legacy OS-Ken controller.
            bridge_by_dpid = {}

            for dpid in sorted(graph.nodes()):
                try:
                    bridge = self.executor._resolve_switch_name(dpid)
                    bridge_by_dpid[dpid] = bridge
                except Exception as exc:
                    print(
                        f"WARNING: Could not resolve DPID {dpid} to bridge: {exc}",
                        flush=True,
                    )

            expected_dpids = sorted(bridge_by_dpid.keys())

            print(
                "Checking Legacy controller readiness for "
                f"{len(expected_dpids)} switches...",
                flush=True,
            )

            readiness_deadline = time.time() + 30.0
            last_readiness_error = None

            while time.time() < readiness_deadline:
                not_ready = []

                for dpid, bridge in bridge_by_dpid.items():
                    try:
                        rc, stdout, stderr = self.executor.runner.run(
                            ["ovs-vsctl", "get-controller", bridge],
                            timeout=5,
                        )

                        target = stdout.strip()

                        if rc != 0:
                            not_ready.append(
                                f"{bridge}(command-failed={stderr.strip() or rc})"
                            )
                            continue

                        if target != legacy_target:
                            not_ready.append(
                                f"{bridge}(target={target or 'NONE'})"
                            )
                            continue

                        connected = self.executor._controller_target_connected(
                            bridge,
                            legacy_target,
                        )

                        if not connected:
                            not_ready.append(
                                f"{bridge}(not-connected)"
                            )

                    except Exception as exc:
                        last_readiness_error = exc
                        not_ready.append(
                            f"{bridge}(error={exc})"
                        )

                if not not_ready:
                    print(
                        "Legacy controller READY | "
                        f"all {len(expected_dpids)} switches connected to "
                        f"{legacy_target}",
                        flush=True,
                    )
                    break

                print(
                    "Waiting for Legacy controller: "
                    + ", ".join(not_ready),
                    flush=True,
                )
                time.sleep(1.0)
            else:
                raise RuntimeError(
                    "Legacy controller readiness timeout after 30 seconds. "
                    f"Expected target={legacy_target}; "
                    f"last_error={last_readiness_error}"
                )

            # ----------------------------------------------------------
            # PHASE 1/4: REAL LEGACY BASELINE
            # ----------------------------------------------------------
            monitor = MigrationMonitor(
                self.executor,
                config=MonitorConfig(
                    poll_interval=0.2,
                    baseline_samples=30,
                    warmup_samples=3,
                    rolling_window=5,
                    failure_window=10,
                    failure_threshold=2,
                    sigma_multiplier=3.0,
                    min_latency_threshold_ms=5.0,
                    consecutive_degraded_observations=3,
                    degradation_threshold_percent=20.0,
                ),
                latency_probe=echo_probe.probe,
            )

            print(
                "PHASE 1/4: Measuring REAL Legacy latency baseline "
                "(30 probes/switch, discard 3 warm-up)",
                flush=True,
            )

            legacy_baseline = monitor.capture_baseline()

            for dpid, result in legacy_baseline.items():
                print(
                    f"  Legacy {result['switch']:<5} | "
                    f"DPID={dpid} | "
                    f"median={result['median_ms']:.3f} ms | "
                    f"stddev={result['stddev_ms']:.3f} ms | "
                    f"failures={result['failures']}/{result['attempt_count']}",
                    flush=True,
                )

            flows = self._build_flows(graph)
            capability = CapabilityTracker()
            beta = 0.50
            total_cost = sum(capability.cost(dpid) for dpid in graph.nodes())
            budget = beta * total_cost

            print(
                "PHASE 2/4: Running real SGBM selection and BBM migration",
                flush=True,
            )
            schedule, sgbm_coverage = run_sgbm(
                graph, flows, budget, self.executor.migrate,
                capability=capability, verbose=True,
            )
            migrated = [r["dpid"] for r in schedule if r.get("outcome") == "success"]
            # beta=0.5 only affords a 2-switch batch once the network has >= 4 switches
            required = 2 if graph.number_of_nodes() >= 4 else 1
            if len(migrated) < required:
                raise RuntimeError(
                    f"Expected >= {required} successful migration(s); successful DPIDs={migrated}; "
                    f"schedule={schedule}"
                )
            for dpid in migrated:
                if topology.get_state(dpid) != "Hybrid":
                    raise AssertionError(f"Successful migration {dpid} is not Hybrid in topology")
            print(
                f"SGBM/BBM phase passed: migrated={migrated}, "
                f"optimizer_coverage={sgbm_coverage:.3f}", flush=True
            )

            print(
                "PHASE 2/3: Measuring real post-migration Echo latency",
                flush=True,
            )
            # IMPORTANT: keep the same MigrationMonitor instance used for
            # the Legacy baseline.  Its per-DPID Legacy baselines must remain
            # available for the Hybrid comparison and later monitoring.
            print(
                "Capturing real Hybrid post-migration latency baseline...",
                flush=True,
            )
            deadline = time.time() + 10.0
            missing = list(migrated)

            while missing and time.time() < deadline:
                connected = set(echo_probe.datapath_ids())
                missing = [
                    dpid for dpid in migrated
                    if int(str(dpid), 16) not in connected
                ]
                if missing:
                    print(
                        f"Waiting for Echo probe reconnection: {missing}",
                        flush=True,
                    )
                    time.sleep(0.2)

            if missing:
                raise RuntimeError(
                    f"Hybrid switches not visible to Echo probe after migration: {missing}"
                )

            monitor.capture_post_migration(migrated)

            print(
                "\nPHASE 4/4: Continuous live health monitoring",
                flush=True,
            )
            print(
                f"Monitoring every {monitor.config.poll_interval:.3f}s | "
                f"rolling window={monitor.config.rolling_window} | "
                f"degradation threshold={monitor.config.degradation_threshold_percent:.1f}% | "
                f"rollback after {monitor.config.consecutive_degraded_observations} "
                "consecutive degraded cycles",
                flush=True,
            )
            print(
                "Monitoring remains active until this demo process is stopped "
                "(Ctrl+C). All telemetry is real OpenFlow Echo/OVS state.",
                flush=True,
            )

            # The monitor owns the periodic polling loop. Do not manually call
            # monitor_once() here: that would turn continuous monitoring into
            # a fixed number of immediate observations and would prevent the
            # configured rolling window / poll interval from operating normally.
            monitor.start()

            def handle_shutdown(signum, frame):
                print("\n\nStopping live monitoring...", flush=True)

                try:
                    monitor.stop(timeout=5.0)

                    print("\nFinal migration report:", flush=True)
                    monitor.log_migration_report()

                    report_dir = os.path.join(ROOT, "reports")
                    os.makedirs(report_dir, exist_ok=True)

                    timestamp = time.strftime("%Y%m%d_%H%M%S")
                    report_path = os.path.join(
                        report_dir,
                        f"qsmo_migration_report_{timestamp}.txt",
                    )

                    monitor.write_text_report(report_path)

                    print(
                        f"Final migration report saved to: {report_path}",
                        flush=True,
                    )
                except Exception as exc:
                    print(
                        f"Warning: final report generation failed: {exc}",
                        flush=True,
                    )

                raise SystemExit(0)

            signal.signal(signal.SIGINT, handle_shutdown)
            signal.signal(signal.SIGTERM, handle_shutdown)

            # Keep this demo worker alive for as long as the user wants the
            # live monitoring session to run. Ctrl+C terminates the OS-Ken
            # process; the monitor itself is a daemon thread owned by the app.
            try:
                while True:
                    time.sleep(1.0)
            except KeyboardInterrupt:
                print("\n\nStopping live monitoring...", flush=True)

                monitor.stop(timeout=5.0)

                print("\nFinal migration report:", flush=True)
                monitor.log_migration_report()

                report_dir = os.path.join(ROOT, "reports")
                os.makedirs(report_dir, exist_ok=True)

                timestamp = time.strftime("%Y%m%d_%H%M%S")
                report_path = os.path.join(
                    report_dir,
                    f"qsmo_migration_report_{timestamp}.txt",
                )

                monitor.write_text_report(report_path)

                print(
                    f"Final migration report saved to: {report_path}",
                    flush=True,
                )

                should_shutdown = True

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
            # Always stop live monitoring and save the final report on shutdown.
            try:
                if "monitor" in locals() and monitor is not None:
                    monitor.stop(timeout=5.0)

                    print("\nFinal migration report:", flush=True)
                    monitor.log_migration_report()

                    report_dir = os.path.join(ROOT, "reports")
                    os.makedirs(report_dir, exist_ok=True)

                    timestamp = time.strftime("%Y%m%d_%H%M%S")
                    report_path = os.path.join(
                        report_dir,
                        f"qsmo_migration_report_{timestamp}.txt",
                    )

                    monitor.write_text_report(report_path)

                    print(
                        f"Final migration report saved to: {report_path}",
                        flush=True,
                    )
            except Exception as exc:
                print(
                    f"Warning: final report generation failed: {exc}",
                    flush=True,
                )

            if self.executor is not None and should_shutdown:
                try:
                    self.executor.shutdown()
                except Exception as exc:
                    print(
                        f"Warning: executor.shutdown() failed: {exc}",
                        flush=True,
                    )
