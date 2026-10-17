"""migration/monitor_service.py -- the always-on QSMO monitor, as its OWN program.

Run it with ./run_monitor.sh (terminal 3). It no longer lives inside osken-manager:
it reads the topology the controller publishes (runtime/topology.json), asks the controller's
loopback probe service for real OpenFlow echo RTTs, and shares all state through
runtime/qsmo.db. Restarting it (or the controller, or the migration program) never loses
anything: a switch that was Hybrid stays Hybrid.

What it does, forever:
  1. Reconcile once the topology is up: read the saved Legacy/Hybrid state, adopt/restart the
     stunnel pair of every Hybrid switch and make OVS point at it.
  2. Baselines: measure (and save) the Legacy baseline of every Legacy switch that has none
     and the Hybrid baseline of every switch the migration program has just moved to Hybrid.
  3. Tunnel supervisor: dead stunnel -> restart; OVS lost the Hybrid target -> re-apply;
     cannot be restored -> fall back to Legacy.
  4. Health monitoring of every Hybrid switch + automatic rollback after repeated degradation.
  5. Publish runtime/monitor_status.json (heartbeat + per-switch health).

Because the migration program writes new states to the same database, the monitor picks them up
on its next cycle: after a migration/rollback it is monitoring the UPDATED network.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import traceback

from migration.migrate_link import MigrationExecutor, local_port_for_dpid
from migration.monitor import MigrationMonitor, MonitorConfig
from migration.store import Store, StoreLedger, StoreTopology

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE_HOST = os.environ.get("QSMO_PROBE_HOST", "127.0.0.1")
PROBE_PORT = int(os.environ.get("QSMO_PROBE_PORT", "6691"))
TOPOLOGY_MAX_AGE_S = 20.0     # controller republishes every ~5 s; older == controller is not running


def say(msg: str):
    print(f"[qsmo-monitor] {msg}", flush=True)


class RemoteProbe:
    """Same interface as QSMOEchoProbe (probe / datapath_ids) but talks to the controller."""

    def __init__(self, host=PROBE_HOST, port=PROBE_PORT):
        self.host, self.port = host, port

    def _call(self, req: dict, timeout: float) -> dict:
        with socket.create_connection((self.host, self.port), timeout=2.0) as s:
            s.settimeout(timeout)
            s.sendall((json.dumps(req) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf.decode())

    def alive(self) -> bool:
        try:
            return bool(self._call({"op": "ping"}, 2.0).get("ok"))
        except Exception:
            return False

    def datapath_ids(self):
        try:
            return tuple(int(d, 16) for d in self._call({"op": "dpids"}, 3.0).get("dpids", []))
        except Exception:
            return ()

    def probe(self, dpid: str, timeout: float = 1.0):
        try:
            return self._call({"op": "probe", "dpid": dpid, "timeout": timeout}, timeout + 2.0)
        except Exception as exc:
            return {"ok": False, "latency_ms": None, "error": f"controller probe service unreachable: {exc}"}


class MonitorService:
    def __init__(self, store: Store | None = None):
        self.store = store or Store()
        self.topology = StoreTopology(self.store)
        self.probe = RemoteProbe()
        self._stop = threading.Event()
        self.executor = MigrationExecutor(self.topology, StoreLedger(self.store), store=self.store, verbose=True)
        self.monitor = MigrationMonitor(
            self.executor,
            config=MonitorConfig(
                poll_interval=float(os.environ.get("QSMO_MONITOR_POLL", "0.5")),
                baseline_samples=30, warmup_samples=3, rolling_window=5,
                failure_window=10, failure_threshold=2, sigma_multiplier=3.0,
                min_latency_threshold_ms=5.0, consecutive_degraded_observations=3,
                degradation_threshold_percent=20.0,
            ),
            latency_probe=self.probe.probe,
        )
        self.monitor.load_from_store(self.store)
        self._reports: list[dict] = []
        self._reconciled_once = False
        self._topo_sig, self._topo_since = None, time.monotonic()
        self._baseline_retry_at: dict[str, float] = {}
        self._disc_since: dict[str, float] = {}

    # ------------------------------------------------------------------
    def stop(self):
        self._stop.set()

    def controller_up(self) -> tuple[bool, str]:
        age = self.store.topology_age()
        if age is None:
            return False, "no topology snapshot yet (is ./run_controller.sh running?)"
        if age > TOPOLOGY_MAX_AGE_S:
            return False, f"topology snapshot is {age:.0f}s old (controller stopped?)"
        if not self.probe.alive():
            return False, f"controller probe service {PROBE_HOST}:{PROBE_PORT} not answering"
        return True, "ok"

    def run(self):
        say(f"started | state dir: {self.store.runtime} | saved baselines: "
            f"legacy={len(self.monitor._baselines)} hybrid={len(self.monitor._post_migration)} | "
            f"states={self.store.states() or 'none yet'}")
        poll = self.monitor.config.poll_interval
        housekeep_every = float(os.environ.get("QSMO_HOUSEKEEP_S", "5"))
        last_house = last_print = last_report = last_wait = 0.0
        was_up = False
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                up, why = self.controller_up()
                if not up:
                    if was_up or t0 - last_wait >= 10.0:
                        say(f"waiting for the controller: {why}")
                        last_wait = t0
                    if was_up:                      # controller went away: re-reconcile when it is back
                        self._reconciled_once = False
                        self._topo_sig = None
                    was_up = False
                    self._reports = []
                    self._write_status(controller=False)
                    self._stop.wait(2.0)
                    continue
                if not was_up:
                    say("controller is up")
                    was_up = True
                if t0 - last_house >= housekeep_every:
                    last_house = t0
                    self._housekeeping()
                self._reports = self.monitor.monitor_once()
                self._write_status(controller=True)
                if t0 - last_print >= 15.0:
                    last_print = t0
                    self._print_status()
                if t0 - last_report >= 30.0:
                    last_report = t0
                    self._write_report()
            except Exception as exc:       # the monitor must keep running
                say(f"cycle error: {type(exc).__name__}: {exc}")
                traceback.print_exc()
                self._stop.wait(2.0)
            self._stop.wait(max(0.05, poll - (time.monotonic() - t0)))

    # ------------------------------------------------------------------
    def _housekeeping(self):
        graph = self.topology.get_graph()
        nodes = sorted(graph.nodes)
        states = self.store.states()

        # names (best effort): the snapshot only knows dpids unless the controller learned them
        for d in nodes:
            if not graph.nodes[d].get("name"):
                try:
                    self.topology.set_name(d, self.executor._resolve_switch_name(d))
                except Exception:
                    pass

        # 1. reconcile once the topology has been unchanged for a few seconds, then for new switches
        sig = tuple(nodes)
        if sig != self._topo_sig:
            self._topo_sig, self._topo_since = sig, time.monotonic()
        stable = nodes and time.monotonic() - self._topo_since >= 5.0
        if stable and not self._reconciled_once:
            say(f"topology stable ({len(nodes)} switches): reconciling saved state with OVS")
            self.executor.reconcile(graph)
            self._reconciled_once = True
            states = self.store.states()
        elif self._reconciled_once:
            unknown = [d for d in nodes if d not in states]
            if unknown:
                say(f"new switch(es) {unknown}: reconciling")
                self.executor.reconcile(graph.subgraph(unknown).copy())
                for d in unknown:                      # no OVS bridge found: still record it (Legacy)
                    self.store.note_switch(d)
                states = self.store.states()

        hybrid = [d for d, st in states.items() if st == "Hybrid"]

        # 3. tunnel supervisor
        if hybrid and self._reconciled_once:
            restarted = self.executor.ensure_hybrid_tunnels(hybrid)
            if restarted:
                say(f"restarted stunnel for {restarted}")
            for d in hybrid:
                self._check_hybrid_target(d)

        # rebaseline request from the CLI
        if self.store.kv_get("rebaseline"):
            for d, st in states.items():
                if st == "Legacy":
                    self.store.delete_baseline(d, "legacy")
                    self.monitor._baselines.pop(d, None)
            self.store.kv_set("rebaseline", False)
            say("re-measuring Legacy baselines on request")

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
                        say(f"measuring Legacy baseline for {self.monitor.switch_label(d)}")
                        self.monitor.capture_baseline([d])
                        self.store.save_baseline(d, "legacy", self.monitor._baselines[d])
                elif st == "Hybrid" and d not in self.monitor._post_migration:
                    with self.store.switch_lock(d, timeout=0.2):
                        say(f"measuring Hybrid baseline for {self.monitor.switch_label(d)}")
                        self.monitor.capture_post_migration([d])
                        self.store.save_baseline(d, "hybrid", self.monitor._post_migration[d])
            except TimeoutError:
                pass        # a migration/rollback owns this switch right now; retry next round
            except Exception as exc:
                say(f"baseline for {d} failed ({exc}); retrying in 10s")
                self._baseline_retry_at[d] = time.monotonic() + 10.0

        # drop in-memory baselines the store no longer has (rollback / rebaseline / reset from CLI)
        saved_h = self.store.load_baselines("hybrid")
        for d in list(self.monitor._post_migration):
            if d not in saved_h:
                self.monitor._post_migration.pop(d, None)
        saved_l = self.store.load_baselines("legacy")
        for d in list(self.monitor._baselines):
            if d not in saved_l and d not in self.monitor._post_migration:
                self.monitor._baselines.pop(d, None)

    def _check_hybrid_target(self, dpid):
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
            say(f"{bridge}: Hybrid path not healthy (targets={targets}); reconciling")
            import networkx as nx
            sub = nx.Graph()
            sub.add_node(dpid)
            self.executor.reconcile(sub)
            self._disc_since.pop(dpid, None)

    # ------------------------------------------------------------------
    def _write_status(self, controller: bool):
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
        snap = {"heartbeat": time.time(), "pid": os.getpid(), "controller_up": controller, "switches": sw}
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
        say(f"alive | Hybrid={len(hyb)} Legacy={len(states) - len(hyb)} | degraded={deg or 'none'}")

    def _write_report(self):
        try:
            os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
            self.monitor.write_text_report(os.path.join(ROOT, "reports", "qsmo_monitor_latest.txt"))
        except Exception:
            pass
