"""
Live SGBM multi-switch integration test.

Requires:
    - Mininet/OVS topology already running
    - OS-Ken with network/topology.py and controller/simple_l2_switch.py
    - Legacy control-plane connectivity healthy
    - Hybrid stunnel/OpenSSL environment configured

This test deliberately uses leaf-switch destinations so that each flow's
shortest path to the core contains multiple switches.

It verifies:
    1. Live topology discovery.
    2. Initial all-Legacy state.
    3. Candidate Legacy sets Lf(f) for every test flow.
    4. Documented SGBM budget formulation:
           B = beta * sum(cost(s) for s in Legacy)
    5. Real MigrationExecutor migration.
    6. At least one actual SGBM batch contains >= 2 switches.
    7. Successful migrations become Hybrid in the authoritative topology.
    8. Ledger records successful migrations.
"""

import ast
import contextlib
import io
import os
import re
import sys
import time

from os_ken.base import app_manager
from os_ken.ofproto import ofproto_v1_3

# Make project modules importable when this file is executed by osken-manager.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from migration.migrate_link import MigrationExecutor
from migration.ledger import MigrationLedger
from optimizer.optimizer import run_sgbm
from optimizer.capability_tracker import CapabilityTracker
from network.coverage import compute_coverage, compute_legacy_set, compute_path


# The Mininet topology under test has 7 switches.
MIN_SWITCHES = 7

# Require at least two leaf destinations so that the test exercises
# multi-switch paths and shared path dependencies.
MIN_LEAF_FLOWS = 2

DISCOVERY_TIMEOUT = 60.0


class LiveSGBMMultiIntegration(app_manager.OSKenApp):
    """
    OS-Ken test application.

    Loaded after network/topology.py and controller/simple_l2_switch.py,
    then obtains the live TopologyAdapter instance from OS-Ken's app manager.
    """

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.topology = None
        self.executor = None

        self._test_thread = self._spawn_test_thread()

    def _spawn_test_thread(self):
        import threading

        thread = threading.Thread(
            target=self._run_test,
            name="sgbm-live-multi-test",
            daemon=True,
        )
        thread.start()
        return thread

    def _find_topology_adapter(self):
        """
        Find the real TopologyAdapter loaded by network/topology.py.

        Do not construct a second topology object. The live OS-Ken graph
        is the authoritative graph.
        """

        apps = app_manager.AppManager.get_instance().applications

        print("\n=== Searching for live TopologyAdapter ===")

        for name, app in apps.items():
            if (
                hasattr(app, "get_graph")
                and hasattr(app, "get_state")
                and hasattr(app, "set_state")
            ):
                print(
                    f"Found live topology application: "
                    f"{name} ({type(app).__name__})"
                )
                return app

        raise RuntimeError(
            "Could not find the live TopologyAdapter application. "
            "Make sure network/topology.py is loaded."
        )

    def _wait_for_topology(
        self,
        min_switches=MIN_SWITCHES,
        timeout=DISCOVERY_TIMEOUT,
    ):
        """
        Wait until the live topology contains at least `min_switches`
        switches and at least one link.
        """

        deadline = time.time() + timeout
        last_report = 0.0

        while time.time() < deadline:
            graph = self.topology.get_graph()

            if (
                graph.number_of_nodes() >= min_switches
                and graph.number_of_edges() > 0
            ):
                return graph

            now = time.time()

            if now - last_report >= 5.0:
                print(
                    f"Waiting for topology discovery: "
                    f"{graph.number_of_nodes()}/{min_switches} switches, "
                    f"{graph.number_of_edges()} edges"
                )
                last_report = now

            time.sleep(1.0)

        graph = self.topology.get_graph()

        raise RuntimeError(
            f"Expected at least {min_switches} switches within "
            f"{timeout:.1f}s, but discovered only "
            f"{graph.number_of_nodes()} nodes "
            f"({sorted(graph.nodes)}) and "
            f"{graph.number_of_edges()} edges."
        )

    @staticmethod
    def _build_live_flows(graph):
        """
        Build flows whose SGBM paths contain multiple switches.

        We deliberately select destinations based on the actual
        compute_path() implementation used by the optimizer rather than
        assuming that leaf switches have degree == 1.
        """

        candidates = []

        for dpid in sorted(graph.nodes):
            flow = {
                "name": f"multihop_candidate_{dpid}",
                "destination_dpid": dpid,
            }

            path = compute_path(graph, flow)

            # A path of >= 2 switches is enough to create a genuine
            # multi-switch Legacy set.
            if len(path) >= 2:
                candidates.append(
                    {
                        "dpid": dpid,
                        "path": path,
                    }
                )

        print("\nMulti-switch destinations selected for live flows:")

        for candidate in candidates:
            print(
                f"  {candidate['dpid']}: "
                f"path={candidate['path']} "
                f"switch_count={len(candidate['path'])}"
            )

        if len(candidates) < MIN_LEAF_FLOWS:
            raise RuntimeError(
                f"Expected at least {MIN_LEAF_FLOWS} destinations "
                f"with multi-switch paths, but found only "
                f"{len(candidates)}."
            )

        flows = []

        for index, candidate in enumerate(candidates):
            flows.append(
                {
                    "name": f"multihop_ctrl_{index}",
                    "destination_dpid": candidate["dpid"],
                    "criticality": 0.8,
                    "security_sla": 0.8,
                    "latency_sla": 0.5,
                }
            )

        return flows

    @staticmethod
    def _log_candidate_batches(graph, flows, capability, budget):
        """
        Print the initial Legacy set Lf(f), path, and cost for every flow.

        This is intentionally performed before SGBM modifies graph state.
        """

        print("\n" + "-" * 72)
        print("INITIAL SGBM CANDIDATE LEGACY SETS")
        print("-" * 72)

        candidates = []

        for flow in flows:
            path = compute_path(graph, flow)
            legacy_set = compute_legacy_set(graph, flow)

            cost = sum(
                capability.cost(dpid)
                for dpid in legacy_set
            )

            fits = cost <= budget

            print(
                f"{flow['name']}:"
            )
            print(
                f"  destination = {flow['destination_dpid']}"
            )
            print(
                f"  path        = {path}"
            )
            print(
                f"  Lf          = {sorted(legacy_set)}"
            )
            print(
                f"  batch size  = {len(legacy_set)}"
            )
            print(
                f"  cost        = {cost:.6f}"
            )
            print(
                f"  fits budget = {fits}"
            )

            candidates.append(
                {
                    "flow": flow,
                    "path": path,
                    "legacy_set": set(legacy_set),
                    "cost": cost,
                }
            )

        multi_switch_candidates = [
            candidate
            for candidate in candidates
            if len(candidate["legacy_set"]) >= 2
            and candidate["cost"] <= budget
        ]

        print(
            f"\nAffordable multi-switch candidates: "
            f"{len(multi_switch_candidates)}"
        )

        if not multi_switch_candidates:
            raise AssertionError(
                "No affordable multi-switch SGBM candidate exists "
                "before migration. The live test cannot prove batch "
                "selection unless at least one flow has an affordable "
                "Legacy set containing >= 2 switches."
            )

        return candidates

    def _run_test(self):
        try:
            time.sleep(3)

            print("\n" + "=" * 72)
            print("LIVE SGBM MULTI-SWITCH BATCH INTEGRATION TEST")
            print("=" * 72)

            # ----------------------------------------------------------
            # 1. Find authoritative live topology and wait for discovery
            # ----------------------------------------------------------

            self.topology = self._find_topology_adapter()
            graph = self._wait_for_topology()

            time.sleep(2)
            graph = self.topology.get_graph()

            if graph.number_of_nodes() < MIN_SWITCHES:
                raise RuntimeError(
                    f"Expected at least {MIN_SWITCHES} switches, "
                    f"but discovered only {graph.number_of_nodes()}: "
                    f"{sorted(graph.nodes)}"
                )

            print(
                f"Live topology ready: "
                f"{len(graph.nodes)} nodes, {len(graph.edges)} edges"
            )
            print(
                f"Discovered DPIDs: {sorted(graph.nodes)}"
            )

            # ----------------------------------------------------------
            # 2. Verify clean Legacy starting state
            # ----------------------------------------------------------

            legacy = [
                dpid
                for dpid in graph.nodes
                if self.topology.get_state(dpid) == "Legacy"
            ]

            hybrid = [
                dpid
                for dpid in graph.nodes
                if self.topology.get_state(dpid) == "Hybrid"
            ]

            print(f"Legacy switches: {len(legacy)}")
            print(f"Hybrid switches: {len(hybrid)}")

            assert len(legacy) == len(graph.nodes), (
                "Test requires a clean all-Legacy starting state. "
                f"Found Hybrid switches: {hybrid}"
            )

            assert len(legacy) >= 4, (
                "Multi-switch test requires at least 4 Legacy switches."
            )

            # ----------------------------------------------------------
            # 3. Build deliberately multi-hop live flows
            # ----------------------------------------------------------

            flows = self._build_live_flows(graph)

            print(
                f"\nConstructed {len(flows)} multi-hop "
                f"live SGBM flow(s)."
            )

            # ----------------------------------------------------------
            # 4. Capability tracker + documented beta budget
            # ----------------------------------------------------------

            capability = CapabilityTracker()

            beta = 0.50

            total_capability_cost = sum(
                capability.cost(dpid)
                for dpid in legacy
            )

            budget = beta * total_capability_cost

            print("\nSGBM budget calculation:")
            print(f"  beta                    = {beta:.2f}")
            print(
                f"  total Legacy cost       = "
                f"{total_capability_cost:.3f}"
            )
            print(
                f"  migration budget B      = "
                f"{budget:.3f}"
            )

            # ----------------------------------------------------------
            # 5. Log and validate initial candidate Legacy sets
            # ----------------------------------------------------------

            initial_candidates = self._log_candidate_batches(
                graph,
                flows,
                capability,
                budget,
            )

            # At least one multi-switch candidate must be affordable.
            affordable_multi_candidates = [
                candidate
                for candidate in initial_candidates
                if len(candidate["legacy_set"]) >= 2
                and candidate["cost"] <= budget
            ]

            assert affordable_multi_candidates, (
                "No affordable candidate batch contains >= 2 switches."
            )

            # ----------------------------------------------------------
            # 6. Real MigrationExecutor
            # ----------------------------------------------------------

            ledger = MigrationLedger()

            self.executor = MigrationExecutor(
                self.topology,
                ledger,
            )

            print("\nUsing real MigrationExecutor.migrate()")

            # ----------------------------------------------------------
            # 7. Wrap migrate_fn to record actual SGBM batch boundaries
            # ----------------------------------------------------------

            actual_batches = []
            current_batch = []
            last_dpid = None

            def tracked_migrate(dpid):
                nonlocal current_batch, last_dpid

                # run_sgbm calls migrate_fn once for each DPID in a batch.
                #
                # The optimizer sorts each selected Lf before iterating.
                # Therefore a batch is identified by the consecutive
                # migration calls associated with one SGBM round.
                #
                # We record every DPID here; the SGBM verbose output and
                # post-run grouping below are used to verify the selected
                # batch structure.
                result = self.executor.migrate(dpid)

                current_batch.append(dpid)
                last_dpid = dpid

                return result

            initial_coverage = compute_coverage(graph, flows)
            print(f"\nInitial coverage       : {initial_coverage:.3f}")
            print("\nStarting run_sgbm() with beta=0.50...\n")

            # ----------------------------------------------------------
            # 8. Run actual SGBM and capture its explicit round decisions
            # ----------------------------------------------------------

            optimizer_output = io.StringIO()
            with contextlib.redirect_stdout(optimizer_output):
                schedule, final_coverage = run_sgbm(
                    graph,
                    flows,
                    budget=budget,
                    migrate_fn=tracked_migrate,
                    capability=capability,
                    verbose=True,
                )
            optimizer_log = optimizer_output.getvalue()
            print(optimizer_log, end="")

            # Parse only the optimizer's own batch-decision log lines.
            # This distinguishes a selected batch from an incidental
            # contiguous sequence of successful migrations.
            selected_batches = []
            for line in optimizer_log.splitlines():
                match = re.search(r"Round: completed flow=.*? batch=(\[[^\]]*\])", line)
                if match:
                    batch = ast.literal_eval(match.group(1))
                    if isinstance(batch, list) and batch:
                        selected_batches.append(batch)

            # The optimizer processes each selected Lf consecutively.
            # Recover the actual batches deterministically by replaying
            # the schedule against a copy of the initial Legacy state.
            #
            # Since successful migrations become Hybrid in G, reconstruct
            # the batches from the order in which successful DPIDs were
            # returned and the initial candidate sets.

            successful = [
                result
                for result in schedule
                if result["outcome"] == "success"
            ]

            failed = [
                result
                for result in schedule
                if result["outcome"] == "failed"
            ]

            # ----------------------------------------------------------
            # 9. Use the actual batches emitted by SGBM
            # ----------------------------------------------------------

            successful_dpids = [
                result["dpid"]
                for result in successful
            ]
            selected_multi_batches = [
                batch for batch in selected_batches if len(batch) >= 2
            ]

            # ----------------------------------------------------------
            # 10. Result summary
            # ----------------------------------------------------------

            print("\n" + "-" * 72)
            print("SGBM RESULT")
            print("-" * 72)

            print(
                f"Schedule entries      : {len(schedule)}"
            )
            print(
                f"Successful migrations : {len(successful)}"
            )
            print(
                f"Failed migrations     : {len(failed)}"
            )
            print(
                f"Final coverage        : {final_coverage:.3f}"
            )

            print("\nSuccessful DPIDs:")

            for dpid in successful_dpids:
                print(f"  {dpid}")

            print("\nDetected multi-switch selected batch(es):")

            for batch in selected_multi_batches:
                print(
                    f"  batch={batch} "
                    f"size={len(batch)}"
                )

            # ----------------------------------------------------------
            # 11. Core assertions
            # ----------------------------------------------------------

            assert successful, (
                "SGBM did not produce any successful migrations."
            )

            assert selected_multi_batches, (
                "SGBM did not select any multi-switch batch. "
                "At least one selected batch must contain >= 2 switches."
            )

            assert any(
                len(batch) >= 2
                for batch in selected_multi_batches
            ), (
                "No selected SGBM batch contained two or more switches."
            )

            # A DPID should not be successfully migrated more than once
            # during this clean run.
            assert len(set(successful_dpids)) == len(successful_dpids), (
                "Duplicate successful migration detected."
            )

            # ----------------------------------------------------------
            # 12. Verify authoritative Hybrid state
            # ----------------------------------------------------------

            final_graph = self.topology.get_graph()

            for dpid in successful_dpids:
                state = self.topology.get_state(dpid)

                print(
                    f"State check: {dpid} -> {state}"
                )

                assert state == "Hybrid", (
                    f"Successful migration for {dpid} did not result "
                    f"in authoritative Hybrid state."
                )

                assert final_graph.nodes[dpid]["state"] == "Hybrid", (
                    f"Graph state for {dpid} is not Hybrid."
                )

            # ----------------------------------------------------------
            # 13. Failed migrations must remain Legacy
            # ----------------------------------------------------------

            failed_dpids = {
                result["dpid"]
                for result in failed
            }

            for dpid in failed_dpids:
                assert self.topology.get_state(dpid) == "Legacy", (
                    f"Failed migration for {dpid} incorrectly changed "
                    f"authoritative state to Hybrid."
                )

            # ----------------------------------------------------------
            # 14. Coverage must increase
            # ----------------------------------------------------------

            assert final_coverage > initial_coverage, (
                f"Coverage did not improve: initial={initial_coverage:.3f}, "
                f"final={final_coverage:.3f}."
            )

            # ----------------------------------------------------------
            # 15. Ledger consistency
            # ----------------------------------------------------------

            ledger_successes = 0

            for dpid in successful_dpids:
                history = ledger.get_history(dpid)

                assert history, (
                    f"No ledger history recorded for successful "
                    f"migration {dpid}."
                )

                assert any(
                    record["outcome"] == "success"
                    for record in history
                ), (
                    f"Ledger contains no successful event for {dpid}."
                )

                ledger_successes += sum(
                    1
                    for record in history
                    if record["outcome"] == "success"
                )

            assert ledger_successes == len(successful_dpids), (
                "Ledger success count does not match successful "
                "migration count."
            )

            # ----------------------------------------------------------
            # 16. Final summary
            # ----------------------------------------------------------

            print("\n" + "=" * 72)
            print("LIVE SGBM MULTI-SWITCH BATCH INTEGRATION TEST PASSED")
            print("=" * 72)

            print(
                f"beta={beta:.2f}, "
                f"budget={budget:.3f}, "
                f"successful={len(successful)}, "
                f"coverage={final_coverage:.3f}"
            )

            print(
                f"multi-switch batch(es) detected="
                f"{len(selected_multi_batches)}"
            )

            print(
                "Verified that SGBM selected at least one batch "
                "containing two or more switches and that successful "
                "migrations were reflected as Hybrid in the "
                "authoritative live TopologyAdapter."
            )

        except Exception as exc:
            print("\n" + "=" * 72)
            print("LIVE SGBM MULTI-SWITCH BATCH INTEGRATION TEST FAILED")
            print("=" * 72)
            print(f"{type(exc).__name__}: {exc}")
            raise

        finally:
            if self.executor is not None:
                try:
                    self.executor.shutdown()
                except Exception as exc:
                    print(
                        f"Warning: executor.shutdown() failed: {exc}"
                    )
