"""
test_sgbm_live_integration.py

Live Feature 3 -> Feature 4 -> Feature 1 integration harness.

Runs inside the same OS-Ken process as TopologyAdapter and verifies:

    live TopologyAdapter
          |
          v
    topology.get_graph()
          |
          v
       run_sgbm()
          |
          v
    MigrationExecutor.migrate(dpid)
          |
          v
    successful B-B-M migration
          |
          v
    topology.set_state(dpid, "Hybrid")
          |
          v
    same live graph reports Hybrid

IMPORTANT:
- optimizer.py owns only scheduling/selection.
- migrate_link.py owns the actual migration.
- topology.py owns authoritative switch state.
- This harness does NOT manually change graph node state.

DRY-RUN MODE
------------
Set the environment variable QSMO_SGBM_DRY_RUN=1 to replace the real
MigrationExecutor.migrate with a controlled stub (_dry_run_migrate).
The stub performs no B-B-M migration; its ONLY state-changing operation
is topology.set_state(dpid, "Hybrid"), and it asserts on every call
that nothing else in the topology changed. An environment variable is
used instead of a --dry-run CLI flag because osken-manager may
interpret unknown command-line arguments itself.

    QSMO_SGBM_DRY_RUN=1 osken-manager ...
"""

import datetime
import os
import sys
import time
from pathlib import Path

from os_ken.base import app_manager
from os_ken.lib import hub


# ---------------------------------------------------------------------
# Repository imports
#
# This file lives in tests/, so the repository root is parents[1].
# OS-Ken does not necessarily put the repo root on sys.path the way
# `python3 tests/test_sgbm_live_integration.py` would, so every import
# location is added explicitly.
#
# optimizer/ is inserted LAST (i.e. ends up FIRST on sys.path) so that
# `from optimizer import ...` resolves to optimizer/optimizer.py (the
# module), not to the optimizer/ directory under ROOT, and so that
# `from capability_tracker import ...` matches how optimizer.py itself
# imports it (as a top-level module).
# ---------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]

for _p in (
    ROOT,
    ROOT / "network",
    ROOT / "migration",
):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from network.topology import TopologyAdapter
from network.live_wait import expected_switch_count, wait_for_stable_topology
from migration.migrate_link import MigrationExecutor
from optimizer import (
    run_sgbm,
    compute_coverage,
    flow_weight,
    supermodular_degree,
    sgbm_approx_ratio,
)
from optimizer.capability_tracker import CapabilityTracker


# ---------------------------------------------------------------------
# Dry-run flag (set QSMO_SGBM_DRY_RUN=1 to enable)
# ---------------------------------------------------------------------

DRY_RUN = os.environ.get("QSMO_SGBM_DRY_RUN", "0") == "1"


# ---------------------------------------------------------------------
# Small in-memory ledger for the integration harness.
#
# The real MigrationExecutor only needs log_event() from the ledger
# interface. We keep the live integration test independent of any
# persistent ledger/database side effects.
# ---------------------------------------------------------------------

class LiveTestLedger:

    def __init__(self):
        self.events = []

    def log_event(self, dpid, baseline, post, outcome):
        self.events.append(
            {
                "dpid": dpid,
                "baseline": baseline,
                "post": post,
                "outcome": outcome,
                "timestamp": datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(),
            }
        )


# ---------------------------------------------------------------------
# OS-Ken application
# ---------------------------------------------------------------------

class SGBMLiveIntegration(app_manager.OSKenApp):

    """
    OS-Ken test application.

    It locates the already-running TopologyAdapter instance from the
    OS-Ken AppManager instead of constructing a second adapter.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.topology = None
        self.executor = None
        self.ledger = None

        self.main_thread = None

        self.finished = False
        self.failed = False

    # -----------------------------------------------------------------
    # Find the REAL TopologyAdapter already loaded by OS-Ken.
    # Matched by class NAME, not isinstance(): OS-Ken may import
    # topology.py under a different module path than this file's own
    # import, which would make isinstance() fail on the same class.
    # -----------------------------------------------------------------

    def _get_live_topology(self):
        app_mgr = app_manager.AppManager.get_instance()

        for app in app_mgr.applications.values():
            if app.__class__.__name__ == "TopologyAdapter":
                return app

        raise RuntimeError(
            "Live TopologyAdapter instance was not found. "
            "Make sure network/topology.py is loaded by osken-manager "
            "before this test harness."
        )

    # -----------------------------------------------------------------
    # Controlled migration stub (dry-run only).
    #
    # Stands in for MigrationExecutor.migrate. It performs NO real
    # migration; the only state-changing operation is
    # topology.set_state(dpid, "Hybrid"), and it verifies on every call
    # that nothing else in the topology changed (i.e. that the
    # optimizer did not mutate state on its own).
    # -----------------------------------------------------------------

    def _dry_run_migrate(self, dpid):
        G = self.topology.get_graph()

        before = {
            node: G.nodes[node].get("state", "Legacy")
            for node in G.nodes
        }

        if before.get(dpid) != "Legacy":
            raise AssertionError(
                f"Dry-run migration expected {dpid} to be Legacy, "
                f"but found {before.get(dpid)}"
            )

        # The optimizer must not have changed any topology state.
        for node, state in before.items():
            if node == dpid:
                continue

            current = G.nodes[node].get("state", "Legacy")
            if current != state:
                raise AssertionError(
                    f"State changed outside migration layer: "
                    f"{node}: {state} -> {current}"
                )

        # This is the ONLY state-changing operation in dry-run mode.
        self.topology.set_state(dpid, "Hybrid")

        after = {
            node: G.nodes[node].get("state", "Legacy")
            for node in G.nodes
        }

        changed = [
            node for node in before
            if before[node] != after[node]
        ]

        if changed != [dpid]:
            raise AssertionError(
                f"Dry-run migration changed unexpected switches: {changed}"
            )

        if after[dpid] != "Hybrid":
            raise AssertionError(
                f"Migration layer failed to set {dpid} to Hybrid"
            )

        return {
            "dpid": dpid,
            "outcome": "success",
            "baseline": {
                "mode": "Legacy",
                "dry_run": True,
            },
            "post": {
                "mode": "Hybrid",
                "dry_run": True,
            },
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(),
            ),
        }

    # -----------------------------------------------------------------
    # OS-Ken starts all applications first, then this green thread runs.
    # -----------------------------------------------------------------

    def start(self):
        self.main_thread = hub.spawn(self._run_live_test)
        return self.main_thread

    # -----------------------------------------------------------------
    # Lifecycle: let OS-Ken kill the test thread and clean up.
    # -----------------------------------------------------------------

    def stop(self):
        if getattr(self, "main_thread", None) is not None:
            hub.kill(self.main_thread)
            self.main_thread = None

        if getattr(self, "executor", None) is not None:
            try:
                self.executor.shutdown()
            except Exception:
                pass

        super().stop()

    # -----------------------------------------------------------------
    # Main integration test.
    # -----------------------------------------------------------------

    def _run_live_test(self):

        try:
            print()
            print("=" * 72)
            print("SGBM -> Migration Layer -> TopologyAdapter LIVE TEST")
            print("=" * 72)

            if DRY_RUN:
                print("MODE: DRY-RUN")
                print("Real TopologyAdapter + real SGBM")
                print("MigrationExecutor.migrate replaced by controlled stub")
            else:
                print("MODE: LIVE")
                print("Real MigrationExecutor.migrate")

            # ---------------------------------------------------------
            # 1. Obtain the existing TopologyAdapter.
            # ---------------------------------------------------------

            self.topology = self._get_live_topology()

            print()
            print("[1] Found live TopologyAdapter:")
            print("    ", self.topology)

            # ---------------------------------------------------------
            # 2. Wait for topology discovery.
            #
            # EventLinkAdd/EventSwitchEnter populate the graph
            # asynchronously through topology.py.
            # ---------------------------------------------------------

            print()
            print("[2] Waiting for live topology discovery...")

            # Any topology size: wait for the full, stable graph (see network/live_wait.py).
            G = wait_for_stable_topology(
                self.topology, expected=expected_switch_count(), min_switches=1
            )

            print(
                f"    discovered nodes = {G.number_of_nodes()}"
            )
            print(
                f"    discovered edges = {G.number_of_edges()}"
            )

            # ---------------------------------------------------------
            # 3. Print authoritative initial state.
            # ---------------------------------------------------------

            legacy = [
                dpid
                for dpid in G.nodes
                if G.nodes[dpid].get("state") == "Legacy"
            ]

            hybrid = [
                dpid
                for dpid in G.nodes
                if G.nodes[dpid].get("state") == "Hybrid"
            ]

            print()
            print("[3] Initial topology state")
            print("    Legacy:", legacy)
            print("    Hybrid:", hybrid)

            if not legacy:
                raise RuntimeError(
                    "No Legacy switches are available for live SGBM test."
                )

            # ---------------------------------------------------------
            # 4. Build flow definitions from the live topology.
            #
            # We intentionally use one destination per currently
            # discovered switch. Only Legacy switches participate.
            #
            # The optimizer expects:
            #   name
            #   destination_dpid
            #   criticality
            #   security_sla
            #   latency_sla
            # ---------------------------------------------------------

            flows = []

            for index, dpid in enumerate(sorted(legacy)):

                flows.append(
                    {
                        "name": f"live_ctrl_{index}",
                        "destination_dpid": dpid,
                        "criticality": 0.8,
                        "security_sla": 0.8,
                        "latency_sla": 0.5,
                    }
                )

            print()
            print("[4] Live SGBM flows")

            for flow in flows:
                print(
                    f"    {flow['name']} -> "
                    f"{flow['destination_dpid']}"
                )

            # ---------------------------------------------------------
            # 5. Establish a small budget.
            #
            # For the first live run, do NOT migrate all switches.
            #
            # We choose approximately one switch worth of budget.
            # The capability tracker supplies the actual calibrated
            # migration cost.
            # ---------------------------------------------------------

            capability = CapabilityTracker()

            cheapest_cost = min(
                capability.cost(dpid)
                for dpid in legacy
            )

            budget = cheapest_cost + 1e-6

            print()
            print("[5] SGBM budget")
            print(f"    minimum switch cost = {cheapest_cost:.3f}")
            print(f"    test budget          = {budget:.3f}")

            # ---------------------------------------------------------
            # 6. Capture state BEFORE SGBM.
            #
            # This snapshot is only for assertions/reporting.
            # We never modify it.
            # ---------------------------------------------------------

            state_before = {
                dpid: G.nodes[dpid].get("state")
                for dpid in G.nodes
            }

            coverage_before = compute_coverage(
                G,
                flows,
            )

            d = supermodular_degree(
                G,
                flows,
            )

            ratio = sgbm_approx_ratio(
                G,
                flows,
            )

            print()
            print("[6] Initial SGBM metrics")
            print(f"    coverage = {coverage_before:.3f}")
            print(f"    supermodular degree d = {d}")
            print(
                "    structural ratio = "
                f"{ratio:.3f}"
            )

            # ---------------------------------------------------------
            # 7. Create the REAL migration executor (live mode only).
            #
            # This is the critical integration point.
            #
            # run_sgbm(..., migrate_fn=self.executor.migrate)
            #
            # Therefore the optimizer cannot fake the state change.
            #
            # In dry-run mode no executor is created: it would start
            # the real stunnel/migration machinery, which the dry run
            # deliberately avoids.
            # ---------------------------------------------------------

            self.ledger = LiveTestLedger()

            print()
            if DRY_RUN:
                print("[7] DRY-RUN: MigrationExecutor NOT created")
                print("    migrate_fn = _dry_run_migrate (controlled stub)")
            else:
                self.executor = MigrationExecutor(
                    topology=self.topology,
                    ledger=self.ledger,
                )

                print("[7] Real MigrationExecutor created")
                print("    migrate_fn = MigrationExecutor.migrate")

            # ---------------------------------------------------------
            # 8. Run SGBM against the LIVE graph.
            #
            # IMPORTANT:
            #
            #   G = topology.get_graph()
            #
            # is the actual NetworkX object maintained by
            # TopologyAdapter.
            #
            # We do not make a copy.
            #
            # The optimizer does not know how state changes happen.
            # It only receives the migration result.
            # ---------------------------------------------------------

            print()
            print("[8] Running LIVE SGBM")
            print("-" * 72)

            migration_fn = (
                self._dry_run_migrate
                if DRY_RUN
                else self.executor.migrate
            )

            schedule, coverage_after = run_sgbm(
                G,
                flows,
                budget=budget,
                migrate_fn=migration_fn,
                capability=capability,
                verbose=True,
            )

            print("-" * 72)

            # ---------------------------------------------------------
            # 9. Re-read the graph from TopologyAdapter.
            #
            # This is intentionally NOT the old local G reference.
            # It proves the authoritative topology object reports
            # the state transition.
            # ---------------------------------------------------------

            live_graph_after = self.topology.get_graph()

            if DRY_RUN:
                for dpid in live_graph_after.nodes:
                    state = live_graph_after.nodes[dpid].get(
                        "state", "Legacy"
                    )

                    # Only report nodes that actually transitioned
                    # during this run (not ones that started Hybrid).
                    if (
                        state == "Hybrid"
                        and state_before.get(dpid) == "Legacy"
                    ):
                        print(
                            f"[DRY-RUN] Verified migration-layer "
                            f"transition: {dpid}: Legacy -> Hybrid"
                        )

                print(
                    "[DRY-RUN] Verified: topology state changes occurred "
                    "through the controlled migration stub."
                )

            state_after = {
                dpid: live_graph_after.nodes[dpid].get("state")
                for dpid in live_graph_after.nodes
            }

            migrated = [
                result["dpid"]
                for result in schedule
                if result["outcome"] == "success"
            ]

            failed = [
                result["dpid"]
                for result in schedule
                if result["outcome"] == "failed"
            ]

            print()
            print("[9] Live migration results")

            print("    successful:", migrated)
            print("    failed:    ", failed)

            print()
            print("    state transitions:")

            for dpid in migrated:
                print(
                    f"      {dpid}: "
                    f"{state_before.get(dpid)} -> "
                    f"{state_after.get(dpid)}"
                )

            # ---------------------------------------------------------
            # 10. Core integration assertions.
            # ---------------------------------------------------------

            # A. SGBM actually invoked the migration layer.
            assert len(schedule) > 0, (
                "SGBM returned an empty migration schedule."
            )

            # B. Every scheduled result has the frozen migration
            #    contract fields.
            for result in schedule:

                assert "dpid" in result
                assert "outcome" in result
                assert "baseline" in result
                assert "post" in result
                assert "timestamp" in result

                assert result["outcome"] in (
                    "success",
                    "failed",
                )

            # C. Successful migrations MUST be Hybrid in the
            #    authoritative TopologyAdapter graph.
            for dpid in migrated:

                assert (
                    live_graph_after.nodes[dpid]["state"]
                    == "Hybrid"
                ), (
                    f"{dpid} reported successful migration, "
                    "but TopologyAdapter still reports "
                    f"{live_graph_after.nodes[dpid]['state']!r}."
                )

            # D. Failed migrations MUST NOT be treated as successful
            #    state transitions.
            for dpid in failed:

                assert (
                    live_graph_after.nodes[dpid]["state"]
                    != "Hybrid"
                    or dpid in migrated
                ), (
                    f"{dpid} failed migration but appears Hybrid."
                )

            # E. Optimizer must not have directly mutated a switch
            #    that was never returned by migrate_fn.
            attempted = {
                result["dpid"]
                for result in schedule
            }

            for dpid in state_before:

                if dpid not in attempted:
                    assert (
                        state_after[dpid]
                        == state_before[dpid]
                    ), (
                        f"Unattempted switch {dpid} changed state "
                        "outside the migration path."
                    )

            # F. Coverage must be recomputed from the live state.
            recomputed = compute_coverage(
                live_graph_after,
                flows,
            )

            assert abs(
                recomputed - coverage_after
            ) < 1e-9, (
                "SGBM returned coverage that does not match "
                "the authoritative live topology state."
            )

            # G. The real MigrationExecutor ledger path must have
            #    recorded successful migration events.
            #    (Live mode only: the dry-run stub bypasses the
            #    executor, so it never writes to the ledger.)
            if not DRY_RUN:
                ledger_successes = [
                    event
                    for event in self.ledger.events
                    if event["outcome"] == "success"
                ]

                assert len(ledger_successes) == len(migrated), (
                    "Ledger success count does not match "
                    "successful migration count."
                )

            # ---------------------------------------------------------
            # 11. Print final state.
            # ---------------------------------------------------------

            print()
            print("[10] Final authoritative topology state")

            for dpid in sorted(live_graph_after.nodes):
                print(
                    f"    {dpid}: "
                    f"{live_graph_after.nodes[dpid].get('state')}"
                )

            print()
            print("[11] Coverage")
            print(
                f"    before = {coverage_before:.3f}"
            )
            print(
                f"    after  = {recomputed:.3f}"
            )

            # ---------------------------------------------------------
            # 12. Success.
            # ---------------------------------------------------------

            print()
            print("=" * 72)
            if DRY_RUN:
                print("LIVE SGBM INTEGRATION TEST PASSED (DRY-RUN)")
            else:
                print("LIVE SGBM INTEGRATION TEST PASSED")
            print("=" * 72)
            print()
            print(
                "Verified chain:"
            )
            print(
                "TopologyAdapter.get_graph()"
            )
            print(
                "    -> optimizer.run_sgbm()"
            )
            if DRY_RUN:
                print(
                    "    -> _dry_run_migrate() (controlled stub)"
                )
            else:
                print(
                    "    -> MigrationExecutor.migrate()"
                )
                print(
                    "    -> successful B-B-M migration"
                )
            print(
                "    -> topology.set_state(..., 'Hybrid')"
            )
            print(
                "    -> same live graph reports Hybrid"
            )
            print()

        except Exception as exc:

            self.failed = True

            print()
            print("=" * 72)
            print("LIVE SGBM INTEGRATION TEST FAILED")
            print("=" * 72)
            print()
            print(type(exc).__name__ + ":", exc)

            import traceback
            traceback.print_exc()

        finally:

            self.finished = True

            # Do not leave the shared stunnel server/client processes
            # running after the integration test.
            if self.executor is not None:

                try:
                    self.executor.shutdown()
                except Exception as exc:
                    print(
                        "WARNING: executor.shutdown() failed:",
                        exc,
                    )

            # Give the cleanup log messages a moment to flush.
            # The thread then simply returns; OS-Ken owns shutdown of
            # the AppManager (this test must not close it itself).
            hub.sleep(1)


# ---------------------------------------------------------------------
# OS-Ken app entry point
# ---------------------------------------------------------------------

if __name__ == "__main__":
    app_manager.AppManager.get_instance().run()
