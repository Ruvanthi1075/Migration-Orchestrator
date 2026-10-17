"""controller/qsmo_monitor_app.py -- the always-on QSMO monitor.

Loaded by osken-manager together with network/topology.py and
controller/qsmo_echo_probe.py (see run_controller.sh). It starts with the
controller and keeps running no matter when -- or whether -- a migration is
run. Migration is a separate program (run_migration.py); the two only talk
through the persistent store (runtime/qsmo.db).

What it does, forever:

  1. Reconcile once the topology is up: read the saved Legacy/Hybrid state,
     adopt/restart the stunnel pair of every Hybrid switch and make OVS point
     at it. A switch that was Hybrid stays Hybrid across controller restarts.
  2. Baselines: measure (and save) the Legacy latency baseline of every
     Legacy switch that has none, and the Hybrid baseline of every switch the
     migration program has just moved to Hybrid.
  3. Tunnel supervisor: if a Hybrid switch's stunnel client / the shared
     server dies, restart it; if OVS lost the Hybrid target (testbed rebuilt)
     re-apply it; if it cannot be restored, fall back to Legacy.
  4. Health monitoring of every Hybrid switch (rolling median vs its own
     Hybrid baseline + coarse Legacy bound) and automatic rollback after
     repeated degradation -- the existing MigrationMonitor logic.
  5. Publish runtime/monitor_status.json (heartbeat + per-switch health) for
     `run_migration.py status`.

Tunables (env): QSMO_MONITOR_POLL (default 0.5 s), QSMO_HOUSEKEEP_S (5 s).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from os_ken.base import app_manager  # noqa: E402

from migration.migrate_link import MigrationExecutor, local_port_for_dpid  # noqa: E402
from migration.monitor import MigrationMonitor, MonitorConfig  # noqa: E402
from migration.store import Store, StoreLedger, norm_dpid  # noqa: E402


def _say(msg: str):
    print(f"[qsmo-monitor] {msg}", flush=True)


class QSMOMonitorApp(app_manager.OSKenApp):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.store = Store()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="qsmo-monitor", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------
    @staticmethod
    def _find(pred, what):
        while True:
            for _name, app in app_manager.AppManager.get_instance().applications.items():
                if pred(app):
                    return app
            _say(f"waiting for {what} app to load...")
            time.sleep(1.0)

    def _run(self):
        try:
            time.sleep(2.0)
            topology = self._find(
                lambda a: all(hasattr(a, x) for x in ("get_graph", "get_state", "set_state")),
                "topology")
            probe = self._find(
                lambda a: callable(getattr(a, "probe", None)) and callable(getattr(a, "datapath_ids", None)),
                "echo probe")
            self.topology, self.probe = topology, probe

            ledger = StoreLedger(self.store)
            self.executor = MigrationExecutor(topology, ledger, store=self.store, verbose=True)
            self.monitor = MigrationMonitor(
                self.executor,
                config=MonitorConfig(
                    poll_interval=float(os.environ.get("QSMO_MONITOR_POLL", "0.5")),
                    baseline_samples=30, warmup_samples=3, rolling_window=5,
                    failure_window=10, failure_threshold=2, sigma_multiplier=3.0,
                    min_latency_threshold_ms=5.0, consecutive_degraded_observations=3,
                    degradation_threshold_percent=20.0,
                ),
                latency_probe=probe.probe,
            )
            self.monitor.load_from_store(self.store)
            _say(f"started | saved baselines: legacy={len(self.monitor._baselines)} "
                 f"hybrid={len(self.monitor._post_migration)} | "
                 f"states={self.store.states() or 'none yet'}")
            self._loop()
        except Exception as exc:  # the monitor must never take the controller down
            _say(f"FATAL: {type(exc).__name__}: {exc}")
            traceback.print_exc()

    # ------------------------------------------------------------------
    def _loop(self):
        poll = self.monitor.config.poll_interval
        housekeep_every = float(os.environ.get("QSMO_HOUSEKEEP_S", "5"))
        last_house = 0.0
        last_status_print = 0.0
        last_report = 0.0
        self._reconciled_once = False
        self._topo_sig, self._topo_since = None, time.monotonic()
        self._baseline_retry_at: dict[str, float] = {}
        self._disc_since: dict[str, float] = {}
        self._reports: list[dict] = []

        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                if t0 - last_house >= housekeep_every:
                    last_house = t0
                    self._housekeeping()
                # health of every Hybrid switch (+ automatic rollback)
                self._reports = self.monitor.monitor_once()
                self._write_status()
                if t0 - last_status_print >= 15.0:
                    last_status_print = t0
                    self._print_status()
                if t0 - last_report >= 30.0:
                    last_report = t0
                    self._write_report()
            except Exception as exc:
                _say(f"cycle error: {type(exc).__name__}: {exc}")
                traceback.print_exc()
            self._stop.wait(max(0.05, poll - (time.monotonic() - t0)))

    # ------------------------------------------------------------------
    def _housekeeping(self):
        graph = self.topology.get_graph()
        nodes = sorted(graph.nodes)
        states = self.store.states()

        # names (best effort): the graph only knows dpids
        for d in nodes:
            if not graph.nodes[d].get("name"):
                try:
                    self.topology.set_name(d, self.executor._resolve_switch_name(d))
                except Exception:
                    pass

        # 1. reconcile: once the topology has been unchanged for a few seconds, then for any
        #    switch we have never seen.
        sig = tuple(nodes)
        if sig != self._topo_sig:
            self._topo_sig, self._topo_since = sig, time.monotonic()
        stable = nodes and time.monotonic() - self._topo_since >= 5.0
        if stable and not self._reconciled_once:
            _say(f"topology stable ({len(nodes)} switches): reconciling saved state with OVS")
            self.executor.reconcile(graph)
            self._reconciled_once = True
            states = self.store.states()
        elif self._reconciled_once:
            unknown = [d for d in nodes if d not in states]
            if unknown:
                _say(f"new switch(es) {unknown}: reconciling")
                self.executor.reconcile(graph.subgraph(unknown).copy())
                states = self.store.states()

        hybrid = [d for d, st in states.items() if st == "Hybrid"]

        # 3. tunnel supervisor (also for Hybrid switches not yet in the graph: they can only
        #    reconnect THROUGH their tunnel)
        if hybrid:
            restarted = self.executor.ensure_hybrid_tunnels(hybrid)
            if restarted:
                _say(f"restarted stunnel for {restarted}")
            for d in hybrid:
                self._check_hybrid_target(d, graph)

        # rebaseline request from the CLI
        if self.store.kv_get("rebaseline"):
            for d, st in states.items():
                if st == "Legacy":
                    self.store.delete_baseline(d, "legacy")
                    self.monitor._baselines.pop(d, None)
            self.store.kv_set("rebaseline", False)
            _say("re-measuring Legacy baselines on request")

        # 2. baselines
        connected = set(self.probe.datapath_ids())
        for d in nodes:
            st = states.get(d)
            if st is None or int(d, 16) not in connected:
                continue
            if time.monotonic() < self._baseline_retry_at.get(d, 0):
                continue
            try:
                if st == "Legacy" and d not in self.monitor._baselines:
                    with self.store.switch_lock(d, timeout=0.2):
                        _say(f"measuring Legacy baseline for {self.monitor.switch_label(d)}")
                        self.monitor.capture_baseline([d])
                        self.store.save_baseline(d, "legacy", self.monitor._baselines[d])
                elif st == "Hybrid" and d not in self.monitor._post_migration:
                    with self.store.switch_lock(d, timeout=0.2):
                        _say(f"measuring Hybrid baseline for {self.monitor.switch_label(d)}")
                        self.monitor.capture_post_migration([d])
                        self.store.save_baseline(d, "hybrid", self.monitor._post_migration[d])
            except TimeoutError:
                pass        # a migration/rollback owns this switch right now; retry next round
            except Exception as exc:
                _say(f"baseline for {d} failed ({exc}); retrying in 10s")
                self._baseline_retry_at[d] = time.monotonic() + 10.0

        # drop in-memory baselines the store no longer has (rollback / rebaseline from CLI)
        saved_h = self.store.load_baselines("hybrid")
        for d in list(self.monitor._post_migration):
            if d not in saved_h:
                self.monitor._post_migration.pop(d, None)

    def _check_hybrid_target(self, dpid, graph):
        """A Hybrid switch whose OVS controller target is not its Hybrid tunnel, or that has been
        disconnected for a long time, gets reconciled (restore, else fall back to Legacy)."""
        try:
            bridge = self.executor._resolve_switch_name(dpid)
        except Exception:
            return
        hyb = f"tcp:127.0.0.1:{local_port_for_dpid(dpid, name=bridge)}"
        targets = self.executor._ovs_targets(bridge)
        connected = targets == [hyb] and self.executor._controller_target_connected(bridge, hyb)
        if connected:
            self._disc_since.pop(dpid, None)
            return
        since = self._disc_since.setdefault(dpid, time.monotonic())
        if targets != [hyb] or time.monotonic() - since >= 30.0:
            _say(f"{bridge}: Hybrid path not healthy (targets={targets}); reconciling")
            import networkx as nx
            sub = nx.Graph()
            sub.add_node(dpid)
            self.executor.reconcile(sub)
            self._disc_since.pop(dpid, None)

    # ------------------------------------------------------------------
    def _write_status(self):
        states = self.store.states()
        by_dpid = {r["dpid"]: r for r in self._reports}
        sw = {}
        for d, st in states.items():
            r = by_dpid.get(d, {})
            sw[d] = {
                "state": st,
                "legacy_baseline": d in self.monitor._baselines,
                "hybrid_baseline": d in self.monitor._post_migration,
                "status": r.get("status"), "latency_ms": r.get("latency_ms"),
                "rolling_median_ms": r.get("rolling_median_ms"),
                "success_rate": r.get("success_rate"), "reasons": r.get("reasons"),
            }
        snap = {"heartbeat": time.time(), "pid": os.getpid(), "switches": sw}
        path = os.path.join(self.store.runtime, "monitor_status.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(snap, fh, default=str)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o666)
        except OSError:
            pass

    def _print_status(self):
        states = self.store.states()
        hyb = [d for d, s in states.items() if s == "Hybrid"]
        deg = [r["dpid"] for r in self._reports if r.get("status") == "degraded"]
        _say(f"alive | Hybrid={len(hyb)} Legacy={len(states) - len(hyb)} | degraded={deg or 'none'}")

    def _write_report(self):
        try:
            os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
            self.monitor.write_text_report(os.path.join(ROOT, "reports", "qsmo_monitor_latest.txt"))
        except Exception:
            pass
