#!/usr/bin/env python3
"""
migration/port_registry.py

Dynamic, collision-free local-port allocation for the per-switch Hybrid
stunnel clients, plus auto-generation of docs/PORTS.md.

Replaces the static `20000 + last dpid byte` formula. Behaviour:

  * Backward compatible: a dpid gets 20000 + last_byte when that port is
    free (so s1..s6 stay 20001..20006 and s0 stays 20016).
  * If that port is already owned by another dpid, reserved, or bound by
    another process, the next free port in [20000, 29999] is used.
  * Allocations are persisted (migration/hybrid/port_registry.json), so a
    dpid keeps the same port across restarts.
  * docs/PORTS.md is rewritten whenever the registry changes.

Env overrides:
  QSMO_PORT_REGISTRY   path of the registry JSON
  QSMO_PORTS_DOC       path of PORTS.md ("" disables doc generation)

CLI (run on the testbed with Mininet up):
  python3 migration/port_registry.py --sync [--prune]   # allocate for every OVS bridge
  python3 migration/port_registry.py --show
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import socket
import subprocess
import tempfile
import threading

try:
    import fcntl
except ImportError:  # non-POSIX: in-process lock only
    fcntl = None

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)

PORT_BASE = 20000
PORT_MAX = 29999
LEGACY_PORT = 6653
REWRAP_PORT = 6660
HYBRID_SERVER_PORT = 16653
RESERVED = {LEGACY_PORT, REWRAP_PORT, HYBRID_SERVER_PORT}

DEFAULT_REGISTRY = os.path.join(_THIS_DIR, "hybrid", "port_registry.json")
DEFAULT_DOC = os.path.join(_ROOT, "docs", "PORTS.md")


def normalize_dpid(dpid) -> str:
    if isinstance(dpid, int):
        return "%016x" % dpid
    return str(dpid).strip().strip('"').lower().zfill(16)


def _port_is_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class PortRegistry:
    def __init__(self, path=None, doc_path=None, check_bind=True):
        self.path = path if path is not None else os.environ.get("QSMO_PORT_REGISTRY", DEFAULT_REGISTRY)
        self.doc_path = doc_path if doc_path is not None else os.environ.get("QSMO_PORTS_DOC", DEFAULT_DOC)
        self.check_bind = check_bind
        self._lock = threading.RLock()
        self._ports = {}   # dpid -> port
        self._names = {}   # dpid -> bridge name
        self._load()

    # ---- persistence ---------------------------------------------------
    def _load(self):
        try:
            with open(self.path) as fh:
                data = json.load(fh)
            self._ports = {normalize_dpid(k): int(v) for k, v in data.get("ports", {}).items()}
            self._names = {normalize_dpid(k): str(v) for k, v in data.get("names", {}).items()}
        except (OSError, ValueError):
            self._ports, self._names = {}, {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump({"ports": self._ports, "names": self._names}, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError:
            pass  # registry stays in-memory; allocation still works
        self.render_doc()

    class _FileLock:
        def __init__(self, path):
            self.path, self.fh = path + ".lock", None

        def __enter__(self):
            if fcntl is None:
                return self
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                self.fh = open(self.path, "w")
                fcntl.flock(self.fh, fcntl.LOCK_EX)
            except OSError:
                self.fh = None
            return self

        def __exit__(self, *exc):
            if self.fh:
                try:
                    fcntl.flock(self.fh, fcntl.LOCK_UN)
                finally:
                    self.fh.close()

    # ---- allocation ----------------------------------------------------
    def port_for(self, dpid, name=None) -> int:
        dpid = normalize_dpid(dpid)
        with self._lock, self._FileLock(self.path):
            self._load()  # pick up other processes' allocations
            changed = False
            if name and self._names.get(dpid) != name:
                self._names[dpid] = name
                changed = True
            if dpid not in self._ports:
                self._ports[dpid] = self._allocate(dpid)
                changed = True
            if changed:
                self._save()
            return self._ports[dpid]

    def _allocate(self, dpid) -> int:
        taken = set(self._ports.values()) | RESERVED
        preferred = PORT_BASE + int(dpid[-2:], 16)
        candidates = [preferred] + [p for p in range(PORT_BASE, PORT_MAX + 1) if p != preferred]
        for port in candidates:
            if port in taken:
                continue
            if self.check_bind and not _port_is_free(port):
                continue
            return port
        raise RuntimeError(f"no free Hybrid client port left in {PORT_BASE}-{PORT_MAX}")

    def note_name(self, dpid, name):
        """Record the OVS bridge name for docs; allocates the port too."""
        self.port_for(dpid, name=name)

    def release(self, dpid):
        dpid = normalize_dpid(dpid)
        with self._lock, self._FileLock(self.path):
            self._load()
            self._ports.pop(dpid, None)
            self._names.pop(dpid, None)
            self._save()

    def sync(self, mapping: dict, prune=False):
        """mapping: {dpid: bridge_name}. Allocate for all; optionally drop absent ones."""
        with self._lock, self._FileLock(self.path):
            self._load()
            wanted = {normalize_dpid(d): n for d, n in mapping.items()}
            for dpid, name in wanted.items():
                self._names[dpid] = name
                if dpid not in self._ports:
                    self._ports[dpid] = self._allocate(dpid)
            if prune:
                for dpid in [d for d in self._ports if d not in wanted]:
                    self._ports.pop(dpid)
                    self._names.pop(dpid, None)
            self._save()
            return dict(self._ports)

    def snapshot(self):
        with self._lock:
            return dict(self._ports), dict(self._names)

    # ---- docs/PORTS.md ---------------------------------------------------
    def render_doc(self):
        if not self.doc_path:
            return
        ports, names = self._ports, self._names

        def sort_key(item):
            return item[1]

        rows = "\n".join(
            f"| {names.get(d, '-')} | `{d}` | `{p}` | Switch-side Hybrid stunnel client |"
            for d, p in sorted(ports.items(), key=sort_key)
        ) or "| - | - | - | (no switches registered yet) |"
        text = f"""# QSMO Port Assignments

AUTO-GENERATED by `migration/port_registry.py` - do not edit by hand.
Last updated: {datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}

Each migrated switch gets a unique local loopback port in
`{PORT_BASE}-{PORT_MAX}`. The port defaults to `{PORT_BASE} + last_byte_of_dpid`;
if that port is already owned by another dpid, reserved, or in use by another
process, the next free port is assigned. Assignments are persisted in
`migration/hybrid/port_registry.json`.

## Switch-Side Hybrid Ports

| Switch | DPID | Local Hybrid Port | Purpose |
|---|---|---:|---|
{rows}

## Shared Controller-Side Ports

| Port | Address | Purpose |
|---:|---|---|
| `{HYBRID_SERVER_PORT}` | `0.0.0.0:{HYBRID_SERVER_PORT}` | Shared Hybrid stunnel server; accepts PQC TLS connections from switch-side clients |
| `{REWRAP_PORT}` | `127.0.0.1:{REWRAP_PORT}` | Internal plaintext connection from Hybrid stunnel server to legacy rewrap service |
| `{LEGACY_PORT}` | `127.0.0.1:{LEGACY_PORT}` | OS-Ken OpenFlow control endpoint / Legacy TLS rewrap destination |

## Hybrid Connection Path

```text
OVS
 | tcp:127.0.0.1:<local_port>
 v
Switch-side stunnel client
 | TLS 1.3, X25519MLKEM768
 v
Controller-side stunnel server  (:{HYBRID_SERVER_PORT})
 | plaintext loopback 127.0.0.1:{REWRAP_PORT}
 v
Legacy rewrap stunnel
 | classical TLS 127.0.0.1:{LEGACY_PORT}
 v
OS-Ken
```
"""
        try:
            os.makedirs(os.path.dirname(self.doc_path), exist_ok=True)
            with open(self.doc_path, "w") as fh:
                fh.write(text)
        except OSError:
            pass


_default = None
_default_lock = threading.Lock()


def default_registry() -> PortRegistry:
    global _default
    with _default_lock:
        if _default is None:
            _default = PortRegistry()
        return _default


def discover_ovs_bridges(timeout=10):
    """{dpid: bridge_name} for every OVS bridge currently on this host."""
    def run(cmd):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    rc, out = run(["ovs-vsctl", "list-br"])
    if rc != 0:
        raise RuntimeError("ovs-vsctl list-br failed (run with sudo, with Mininet up)")
    found = {}
    for br in out.split():
        rc, dp = run(["ovs-vsctl", "get", "bridge", br, "datapath_id"])
        if rc == 0 and dp.strip():
            found[normalize_dpid(dp)] = br
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sync", action="store_true", help="allocate ports for all live OVS bridges and rewrite PORTS.md")
    ap.add_argument("--prune", action="store_true", help="with --sync: drop switches no longer present")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    reg = PortRegistry()
    if args.sync:
        reg.sync(discover_ovs_bridges(), prune=args.prune)
    ports, names = reg.snapshot()
    for d, p in sorted(ports.items(), key=lambda kv: kv[1]):
        print(f"{names.get(d, '-'):<8} {d}  {p}")
    if args.sync:
        print(f"wrote {reg.doc_path}")


if __name__ == "__main__":
    main()
