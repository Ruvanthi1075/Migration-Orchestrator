"""Read-only live observation test for Person D's migration monitor."""

import os
import sys
import time
import threading

from os_ken.base import app_manager
from os_ken.ofproto import ofproto_v1_3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from migration.ledger import MigrationLedger
from migration.migrate_link import MigrationExecutor
from migration.monitor import MigrationMonitor
from network.live_wait import expected_switch_count, wait_for_stable_topology


class LiveMonitorObservation(app_manager.OSKenApp):
    """Observe the existing live topology without migrating or rolling back."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._test_thread = threading.Thread(
            target=self._run_test,
            name="live-monitor-observation",
            daemon=True,
        )
        self._test_thread.start()

    def _find_topology_adapter(self):
        apps = app_manager.AppManager.get_instance().applications

        for name, app in apps.items():
            if (
                hasattr(app, "get_graph")
                and hasattr(app, "get_state")
                and hasattr(app, "set_state")
            ):
                print(f"Found live topology application: {name}")
                return app

        raise RuntimeError("Live TopologyAdapter not found.")

    def _run_test(self):
        executor = None

        try:
            topology = self._find_topology_adapter()

            graph = wait_for_stable_topology(topology, expected=expected_switch_count())

            executor = MigrationExecutor(
                topology,
                MigrationLedger(),
                verbose=False,
            )

            monitor = MigrationMonitor(executor)

            print("\n=== LIVE MONITOR READ-ONLY OBSERVATION ===")
            print(f"Discovered switches: {graph.number_of_nodes()}")
            print(
                "Hybrid switches: "
                f"{sum(topology.get_state(d) == 'Hybrid' for d in graph.nodes)}"
            )

            observations = monitor.observe_hybrid_switches()

            if not observations:
                print("No Hybrid switches to observe.")
            else:
                for observation in observations:
                    print(observation)

            print("\nObservation completed.")

        except Exception as exc:
            print(f"Monitor observation failed: {exc}")
            raise

        finally:
            if executor is not None:
                executor.shutdown()


if __name__ == "__main__":
    raise RuntimeError(
        "Load this file through osken-manager with the live topology."
    )
