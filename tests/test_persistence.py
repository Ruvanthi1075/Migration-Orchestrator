"""Offline tests (no Mininet/OVS/stunnel): persistence, reconcile, rollback bookkeeping.

Run:  python3 -m pytest tests/test_persistence.py -q     (or: python3 tests/test_persistence.py)
"""
import os
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import networkx as nx

from migration import migrate_link
from migration.migrate_link import CommandRunner, MigrationExecutor
from migration.monitor import MigrationMonitor
from migration.store import Store, StoreLedger, StoreTopology

LEGACY = "ssl:127.0.0.1:6653"


class FakeOVS(CommandRunner):
    """Models bridges, their controller sets and is_connected. A tcp: target is 'connected'
    only while self.tunnel_up is True; the Legacy target is always connected."""

    def __init__(self, bridges):
        self.bridges = bridges                     # name -> dpid
        self.targets = {n: [LEGACY] for n in bridges}
        self.ids = {}                              # (bridge, target) -> uuid
        self.tunnel_up = True
        self.calls = []

    def _id(self, b, t):
        return self.ids.setdefault((b, t), str(uuid.uuid4()))

    def run(self, args, timeout=None, input_bytes=b""):
        self.calls.append(args)
        a = list(args)
        if a[:2] == ["ovs-vsctl", "list-br"]:
            return 0, "\n".join(self.bridges) + "\n", ""
        if a[1:3] == ["get", "bridge"]:
            return 0, f'"{self.bridges[a[3]]}"\n', ""
        if a[1] == "get-controller":
            return 0, "\n".join(self.targets[a[2]]) + "\n", ""
        if a[1] == "set-controller":
            self.targets[a[2]] = a[3:]
            return 0, "", ""
        if a[:4] == ["ovs-vsctl", "--bare", "get", "Bridge"]:
            return 0, " ".join(self._id(a[4], t) for t in self.targets[a[4]]), ""
        if a[:4] == ["ovs-vsctl", "--bare", "get", "Controller"]:
            cid, col = a[4], a[5]
            for (b, t), i in self.ids.items():
                if i == cid:
                    if col == "target":
                        return 0, t + "\n", ""
                    if col == "is_connected":
                        up = t == LEGACY or self.tunnel_up
                        return 0, ("true" if up else "false") + "\n", ""
        return 1, "", "unhandled " + " ".join(a)


def _executor(tmp, ovs, store, stunnel_ok=True):
    topo = StoreTopology(store)
    ex = MigrationExecutor(topo, StoreLedger(store), runner=ovs, store=store, verbose=False,
                           hybrid_conf_dir=os.path.join(tmp, "stunnel"),
                           hybrid_log_dir=os.path.join(tmp, "stunnel", "logs"))
    ex._ensure_stunnel_pair_up = lambda d, b, p: ((True, None) if ovs.tunnel_up and stunnel_ok else (False, "stunnel dead"))
    return topo, ex


def _setup(tmp):
    os.environ["QSMO_PORT_REGISTRY"] = os.path.join(tmp, "ports.json")
    os.environ["QSMO_PORTS_DOC"] = ""
    migrate_link._port_registry.__globals__["_default"] = None   # fresh registry per test
    store = Store(runtime=tmp)
    g = nx.Graph()
    g.add_edge("0000000000000001", "0000000000000002")
    for n in g.nodes:
        g.nodes[n]["state"] = "Legacy"
    store.write_topology(g)
    ovs = FakeOVS({"s1": "0000000000000001", "s2": "0000000000000002"})
    return store, ovs


def test_state_baselines_ledger_survive_restart():
    with tempfile.TemporaryDirectory() as tmp:
        store, ovs = _setup(tmp)
        store.set_state("0000000000000001", "Hybrid", name="s1", hybrid_port=20001)
        store.save_baseline("0000000000000001", "legacy", {"mean_ms": 1.5})
        StoreLedger(store).log_event("0000000000000001", {"a": 1}, {"b": 2}, "success", associated_flows=["f"])
        # "restart": brand-new objects over the same files
        s2 = Store(runtime=tmp)
        assert s2.get_state("0000000000000001") == "Hybrid"
        assert s2.load_baselines("legacy")["0000000000000001"]["mean_ms"] == 1.5
        led = StoreLedger(s2)
        assert led.get_history("0000000000000001")[0]["outcome"] == "success"
        g = StoreTopology(s2).get_graph()
        assert g.nodes["0000000000000001"]["state"] == "Hybrid" and g.nodes["0000000000000002"]["state"] == "Legacy"
        assert g.number_of_edges() == 1


def test_reconcile_keeps_hybrid_after_testbed_rebuilt():
    """Store says Hybrid, but OVS was rebuilt and points at Legacy again -> Hybrid re-applied."""
    with tempfile.TemporaryDirectory() as tmp:
        store, ovs = _setup(tmp)
        store.set_state("0000000000000001", "Hybrid", name="s1")
        topo, ex = _executor(tmp, ovs, store)
        out = ex.reconcile(topo.get_graph(), restore_timeout=1)
        assert out["0000000000000001"] == "Hybrid"
        hyb = ovs.targets["s1"]
        assert len(hyb) == 1 and hyb[0].startswith("tcp:127.0.0.1:2000"), hyb
        assert out["0000000000000002"] == "Legacy" and ovs.targets["s2"] == [LEGACY]
        assert store.get_state("0000000000000001") == "Hybrid"


def test_reconcile_falls_back_to_legacy_when_tunnel_cannot_be_restored():
    with tempfile.TemporaryDirectory() as tmp:
        store, ovs = _setup(tmp)
        store.set_state("0000000000000001", "Hybrid", name="s1")
        store.save_baseline("0000000000000001", "hybrid", {"median_ms": 2})
        ovs.tunnel_up = False
        topo, ex = _executor(tmp, ovs, store)
        out = ex.reconcile(topo.get_graph(), restore_timeout=1)
        assert out["0000000000000001"] == "Legacy"
        assert ovs.targets["s1"] == [LEGACY]            # never left pointing at a dead tunnel
        assert "0000000000000001" not in store.load_baselines("hybrid")


def test_reconcile_adopts_connected_hybrid_and_cleans_dead_one_when_store_is_empty():
    """The user's real situation after upgrading: OVS has leftovers, store has no rows."""
    with tempfile.TemporaryDirectory() as tmp:
        store, ovs = _setup(tmp)
        port1 = migrate_link.local_port_for_dpid("0000000000000001", name="s1")
        port2 = migrate_link.local_port_for_dpid("0000000000000002", name="s2")
        ovs.targets["s1"] = [f"tcp:127.0.0.1:{port1}"]
        ovs.targets["s2"] = [f"tcp:127.0.0.1:{port2}"]

        class Half(FakeOVS):
            pass
        # s1's tunnel is alive, s2's is dead
        orig = ovs.run

        def run(args, timeout=None, input_bytes=b""):
            a = list(args)
            if a[:4] == ["ovs-vsctl", "--bare", "get", "Controller"] and a[5] == "is_connected":
                for (b, t), i in ovs.ids.items():
                    if i == a[4] and t.startswith("tcp:"):
                        return 0, ("true" if b == "s1" else "false") + "\n", ""
            return orig(args, timeout, input_bytes)
        ovs.run = run
        topo, ex = _executor(tmp, ovs, store)
        out = ex.reconcile(topo.get_graph(), restore_timeout=1)
        assert out == {"0000000000000001": "Hybrid", "0000000000000002": "Legacy"}, out
        assert ovs.targets["s2"] == [LEGACY]


def test_rollback_updates_store_and_stops_client():
    with tempfile.TemporaryDirectory() as tmp:
        store, ovs = _setup(tmp)
        topo, ex = _executor(tmp, ovs, store)
        d = "0000000000000001"
        ex.ledger.log_event(d, {"latency_ms": 1}, {"latency_ms": 2}, "success")
        store.set_state(d, "Hybrid", name="s1")
        store.save_baseline(d, "hybrid", {"median_ms": 2})
        ovs.targets["s1"] = ["tcp:127.0.0.1:20001"]
        stopped = []
        ex.stop_client = lambda dpid: stopped.append(dpid)
        mon = MigrationMonitor(ex, latency_probe=lambda x: {"ok": False, "latency_ms": None, "error": "n/a"})
        r = mon.rollback(d, "test")
        assert r["outcome"] == "reverted", r
        assert store.get_state(d) == "Legacy" and stopped == [d]
        assert d not in store.load_baselines("hybrid")
        assert [x["outcome"] for x in ex.ledger.get_history(d)] == ["success", "reverted"]
        assert ovs.targets["s1"] == [LEGACY]


def test_force_rollback_without_ledger_record():
    with tempfile.TemporaryDirectory() as tmp:
        store, ovs = _setup(tmp)
        topo, ex = _executor(tmp, ovs, store)
        d = "0000000000000002"
        store.set_state(d, "Hybrid", name="s2")
        ovs.targets["s2"] = ["tcp:127.0.0.1:20002"]
        ex.stop_client = lambda dpid: None
        r = ex.force_rollback(d)
        assert r["outcome"] == "reverted" and store.get_state(d) == "Legacy"


def test_switch_lock_is_exclusive_across_threads():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(runtime=tmp)
        order = []

        def worker(tag, hold):
            with store.switch_lock("0000000000000001", timeout=5):
                order.append(f"{tag}-in")
                time.sleep(hold)
                order.append(f"{tag}-out")
        t1 = threading.Thread(target=worker, args=("a", 0.4))
        t2 = threading.Thread(target=worker, args=("b", 0.0))
        t1.start(); time.sleep(0.1); t2.start(); t1.join(); t2.join()
        assert order == ["a-in", "a-out", "b-in", "b-out"], order


def test_server_conf_rendered_with_real_paths():
    with tempfile.TemporaryDirectory() as tmp:
        store, ovs = _setup(tmp)
        topo = StoreTopology(store)
        ex = MigrationExecutor(topo, StoreLedger(store), runner=ovs, store=store, verbose=False)
        ex.hybrid_conf_dir = os.path.join(tmp, "stunnel")
        os.makedirs(ex.hybrid_conf_dir)
        src = os.path.join(os.path.dirname(migrate_link.__file__), "hybrid", "stunnel", "server.conf")
        open(ex.server_conf_template, "w").write(open(src).read())
        text = open(ex.render_server_conf()).read()
        root = os.path.dirname(os.path.dirname(os.path.abspath(migrate_link.__file__)))
        assert "@ROOT@" not in text and f"{root}/migration/hybrid/certs/hybrid-ca.cert" in text
        assert "krithika" not in text


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print("all persistence tests passed")
