"""
migration/store.py -- the single persistent source of truth for QSMO.

Why this exists
---------------
Before, "which switches are Hybrid" lived only in the memory of the one
osken-manager process that also ran the migration. When that process ended,
the state, the baselines and the ledger were lost, and the stunnel processes
it had started were killed with it -- while OVS (which keeps its controller
targets in OVSDB) still pointed at tcp:127.0.0.1:2000x. The next run then
started from "everything is Legacy" on top of dead Hybrid targets.

Now every process (controller + monitor app, migration CLI, status CLI)
reads and writes the same SQLite file, runtime/qsmo.db:

    switches   dpid -> name, state (Legacy|Hybrid), hybrid port, updated
    ledger     every migration attempt / rollback (the MML, Algorithm 5)
    baselines  per-switch Legacy and Hybrid latency baselines (monitor)
    events     audit log of what each component did and why
    kv         small flags (e.g. "rebaseline requested")

plus runtime/topology.json, the live switch graph the controller publishes
(LLDP discovery only exists inside the OS-Ken process, so it exports it).

SQLite in WAL mode is safe for several processes; a fresh connection is
opened per operation, so threads never share one. Per-switch file locks
(`switch_lock`) stop a rollback and a migration touching the same bridge at
the same time.

Env overrides:
    QSMO_RUNTIME   directory holding qsmo.db / topology.json / locks
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time

try:
    import fcntl
except ImportError:  # non-POSIX: in-process locks only
    fcntl = None

import networkx as nx

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_THIS_DIR)


def runtime_dir() -> str:
    return os.environ.get("QSMO_RUNTIME", os.path.join(ROOT, "runtime"))


def _now() -> float:
    return time.time()


def _iso(ts=None) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(
        ts if ts is not None else _now(), datetime.timezone.utc).isoformat()


def norm_dpid(dpid) -> str:
    if isinstance(dpid, int):
        return "%016x" % dpid
    return str(dpid).strip().strip('"').lower().zfill(16)


def _open_perms(path: str):
    """The controller runs under sudo; keep files usable by the normal user too."""
    try:
        os.chmod(path, 0o666)
    except OSError:
        pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS switches(
    dpid TEXT PRIMARY KEY, name TEXT, state TEXT NOT NULL DEFAULT 'Legacy',
    hybrid_port INTEGER, updated REAL);
CREATE TABLE IF NOT EXISTS ledger(
    id INTEGER PRIMARY KEY AUTOINCREMENT, dpid TEXT NOT NULL, ts TEXT NOT NULL,
    outcome TEXT NOT NULL, reason TEXT, baseline TEXT, post TEXT, flows TEXT,
    recovery_latency_ms REAL);
CREATE INDEX IF NOT EXISTS ledger_dpid ON ledger(dpid);
CREATE TABLE IF NOT EXISTS baselines(
    dpid TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL, updated REAL,
    PRIMARY KEY(dpid, kind));
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, source TEXT,
    kind TEXT, dpid TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path: str | None = None, runtime: str | None = None):
        self.runtime = runtime or (os.path.dirname(os.path.abspath(path)) if path else runtime_dir())
        self.path = path or os.path.join(self.runtime, "qsmo.db")
        self.topology_path = os.path.join(self.runtime, "topology.json")
        self.lock_dir = os.path.join(self.runtime, "locks")
        os.makedirs(self.lock_dir, exist_ok=True)
        self._init_lock = threading.Lock()
        self._ensure_schema()

    # ---- plumbing -----------------------------------------------------
    @contextlib.contextmanager
    def _conn(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _ensure_schema(self):
        with self._init_lock:
            fresh = not os.path.exists(self.path)
            with self._conn() as con:
                con.execute("PRAGMA journal_mode=WAL")
                con.executescript(_SCHEMA)
            if fresh:
                _open_perms(self.path)
            for ext in ("-wal", "-shm"):
                if os.path.exists(self.path + ext):
                    _open_perms(self.path + ext)

    @contextlib.contextmanager
    def switch_lock(self, dpid, timeout: float = 90.0):
        """Exclusive per-switch lock across processes AND threads."""
        path = os.path.join(self.lock_dir, f"{norm_dpid(dpid)}.lock")
        fh = open(path, "a+")
        _open_perms(path)
        deadline = time.monotonic() + timeout
        try:
            if fcntl is not None:
                while True:
                    try:
                        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"could not lock switch {dpid} within {timeout}s")
                        time.sleep(0.1)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(fh, fcntl.LOCK_UN)
            finally:
                fh.close()

    # ---- switches -----------------------------------------------------
    def get_switch(self, dpid) -> dict | None:
        with self._conn() as con:
            row = con.execute("SELECT * FROM switches WHERE dpid=?", (norm_dpid(dpid),)).fetchone()
        return dict(row) if row else None

    def get_state(self, dpid, default: str = "Legacy") -> str:
        row = self.get_switch(dpid)
        return row["state"] if row else default

    def set_state(self, dpid, state: str, name: str | None = None, hybrid_port: int | None = None):
        assert state in ("Legacy", "Hybrid"), f"invalid state: {state!r}"
        dpid = norm_dpid(dpid)
        with self._conn() as con:
            con.execute(
                "INSERT INTO switches(dpid,name,state,hybrid_port,updated) VALUES(?,?,?,?,?) "
                "ON CONFLICT(dpid) DO UPDATE SET state=excluded.state, "
                "name=COALESCE(excluded.name, switches.name), "
                "hybrid_port=COALESCE(excluded.hybrid_port, switches.hybrid_port), "
                "updated=excluded.updated",
                (dpid, name, state, hybrid_port, _now()))

    def note_switch(self, dpid, name=None, hybrid_port=None):
        """Create a Legacy row if the switch is new; never changes an existing state."""
        dpid = norm_dpid(dpid)
        with self._conn() as con:
            con.execute(
                "INSERT INTO switches(dpid,name,state,hybrid_port,updated) VALUES(?,?,'Legacy',?,?) "
                "ON CONFLICT(dpid) DO UPDATE SET name=COALESCE(?, switches.name), "
                "hybrid_port=COALESCE(?, switches.hybrid_port)",
                (dpid, name, hybrid_port, _now(), name, hybrid_port))

    def all_switches(self) -> dict:
        with self._conn() as con:
            rows = con.execute("SELECT * FROM switches ORDER BY dpid").fetchall()
        return {r["dpid"]: dict(r) for r in rows}

    def states(self) -> dict:
        return {d: r["state"] for d, r in self.all_switches().items()}

    # ---- ledger ---------------------------------------------------------
    def ledger_add(self, dpid, baseline, post, outcome, reason=None, flows=None,
                   recovery_latency_ms=None):
        with self._conn() as con:
            con.execute(
                "INSERT INTO ledger(dpid,ts,outcome,reason,baseline,post,flows,recovery_latency_ms) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (norm_dpid(dpid), _iso(), outcome, reason,
                 json.dumps(baseline, default=str), json.dumps(post, default=str),
                 json.dumps(list(flows or [])), recovery_latency_ms))

    def ledger_rows(self, dpid=None) -> list[dict]:
        q, args = "SELECT * FROM ledger", ()
        if dpid is not None:
            q, args = q + " WHERE dpid=?", (norm_dpid(dpid),)
        with self._conn() as con:
            rows = con.execute(q + " ORDER BY id", args).fetchall()
        out = []
        for r in rows:
            out.append({
                "dpid": r["dpid"], "timestamp": r["ts"], "outcome": r["outcome"],
                "reason": r["reason"], "baseline": json.loads(r["baseline"] or "null"),
                "post": json.loads(r["post"] or "null"),
                "associated_flows": json.loads(r["flows"] or "[]"),
                "recovery_latency_ms": r["recovery_latency_ms"],
            })
        return out

    # ---- baselines ------------------------------------------------------
    def save_baseline(self, dpid, kind: str, data: dict):
        assert kind in ("legacy", "hybrid")
        with self._conn() as con:
            con.execute(
                "INSERT INTO baselines(dpid,kind,data,updated) VALUES(?,?,?,?) "
                "ON CONFLICT(dpid,kind) DO UPDATE SET data=excluded.data, updated=excluded.updated",
                (norm_dpid(dpid), kind, json.dumps(data, default=str), _now()))

    def load_baselines(self, kind: str) -> dict:
        with self._conn() as con:
            rows = con.execute("SELECT dpid,data FROM baselines WHERE kind=?", (kind,)).fetchall()
        return {r["dpid"]: json.loads(r["data"]) for r in rows}

    def delete_baseline(self, dpid, kind: str | None = None):
        with self._conn() as con:
            if kind:
                con.execute("DELETE FROM baselines WHERE dpid=? AND kind=?", (norm_dpid(dpid), kind))
            else:
                con.execute("DELETE FROM baselines WHERE dpid=?", (norm_dpid(dpid),))

    # ---- events / kv ----------------------------------------------------
    def add_event(self, source: str, kind: str, message: str, dpid=None):
        with self._conn() as con:
            con.execute("INSERT INTO events(ts,source,kind,dpid,message) VALUES(?,?,?,?,?)",
                        (_iso(), source, kind, norm_dpid(dpid) if dpid else None, message))

    def recent_events(self, n: int = 30) -> list[dict]:
        with self._conn() as con:
            rows = con.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def kv_get(self, key: str, default=None):
        with self._conn() as con:
            row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value):
        with self._conn() as con:
            con.execute("INSERT INTO kv(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value, default=str)))

    # ---- topology snapshot (written by the controller) ------------------
    def write_topology(self, graph: "nx.Graph", names: dict | None = None):
        names = names or {}
        snap = {
            "updated": _now(),
            "nodes": [{"dpid": n, "name": (graph.nodes[n].get("name") or names.get(n))}
                      for n in graph.nodes],
            "edges": [[a, b] for a, b in graph.edges],
        }
        tmp = self.topology_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(snap, fh)
        os.replace(tmp, self.topology_path)
        _open_perms(self.topology_path)

    def topology_age(self) -> "float | None":
        """Seconds since the controller last published the topology (None = never)."""
        try:
            with open(self.topology_path) as fh:
                return max(0.0, _now() - json.load(fh).get("updated", 0))
        except (OSError, ValueError):
            return None

    def read_topology(self, max_age_s: float | None = None) -> "nx.Graph | None":
        """Graph with node 'state' taken from the DB. None if there is no (fresh) snapshot."""
        try:
            with open(self.topology_path) as fh:
                snap = json.load(fh)
        except (OSError, ValueError):
            return None
        if max_age_s is not None and _now() - snap.get("updated", 0) > max_age_s:
            return None
        states = self.states()
        g = nx.Graph()
        for n in snap["nodes"]:
            d = norm_dpid(n["dpid"])
            g.add_node(d, name=n.get("name"), state=states.get(d, "Legacy"))
        for a, b in snap["edges"]:
            g.add_edge(norm_dpid(a), norm_dpid(b))
        return g

    # ---- maintenance ----------------------------------------------------
    def wipe(self):
        with self._conn() as con:
            for t in ("switches", "ledger", "baselines", "events", "kv"):
                con.execute(f"DELETE FROM {t}")


class StoreTopology:
    """Drop-in for the TopologyAdapter contract (get_graph/get_state/set_state)
    for processes that are NOT inside OS-Ken (the migration CLI)."""

    def __init__(self, store: Store, max_age_s: float | None = None):
        self.store = store
        self.max_age_s = max_age_s
        self.names: dict = {}

    def get_graph(self):
        g = self.store.read_topology(self.max_age_s)
        return g if g is not None else nx.Graph()

    def get_state(self, dpid):
        return self.store.get_state(dpid)

    def set_state(self, dpid, value):
        self.store.set_state(dpid, value, name=self.names.get(norm_dpid(dpid)))

    def set_name(self, dpid, name):
        self.names[norm_dpid(dpid)] = name


class StoreLedger:
    """MigrationLedger-compatible (log_event/get_history/compute_rollback_rate), SQLite-backed."""

    def __init__(self, store: Store):
        self.store = store

    @property
    def log(self) -> list[dict]:
        return self.store.ledger_rows()

    def log_event(self, dpid, baseline, post, outcome, reason=None,
                  associated_flows=None, recovery_latency_ms=None):
        assert outcome in ("success", "failed", "reverted"), f"unexpected outcome {outcome!r}"
        self.store.ledger_add(dpid, baseline, post, outcome, reason, associated_flows,
                              recovery_latency_ms)

    def get_history(self, dpid) -> list[dict]:
        return self.store.ledger_rows(dpid)

    def compute_rollback_rate(self) -> float:
        rel = [r for r in self.log if r["outcome"] in ("success", "reverted")]
        if not rel:
            return 0.0
        return sum(1 for r in rel if r["outcome"] == "reverted") / len(rel)

    def clear(self):
        with self.store._conn() as con:
            con.execute("DELETE FROM ledger")
