"""Live OpenFlow Echo RTT probe for QSMO.

Topology-agnostic: it learns the connected datapaths from OS-Ken and measures each one
with a real OFP Echo Request/Reply exchange. Nothing (names, DPIDs, counts, latencies)
is hardcoded.

The monitor runs in its OWN process/terminal (run_monitor.sh), but only the controller
owns the OpenFlow connections. So this app also serves a tiny loopback-only JSON-lines
service (default 127.0.0.1:6691, env QSMO_PROBE_PORT) that the monitor calls:

    {"op":"ping"}                         -> {"ok":true, "pid":..}
    {"op":"dpids"}                        -> {"ok":true, "dpids":["0000000000000003", ...]}
    {"op":"probe","dpid":"..","timeout":1} -> {"ok":bool, "latency_ms":float|null, "error":str|null}
"""
from __future__ import annotations

import json
import os
import threading
import time
from itertools import count

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import DEAD_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.lib import hub
from os_ken.ofproto import ofproto_v1_3

PROBE_HOST = os.environ.get("QSMO_PROBE_HOST", "127.0.0.1")
PROBE_PORT = int(os.environ.get("QSMO_PROBE_PORT", "6691"))


class QSMOEchoProbe(app_manager.OSKenApp):
    """Real OpenFlow Echo RTT probe, usable in-process and over the loopback service."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._datapaths = {}
        self._pending = {}
        self._xids = count(1)
        try:
            self._server = hub.StreamServer((PROBE_HOST, PROBE_PORT), self._serve)
            hub.spawn(self._server.serve_forever)
            self.logger.info("QSMO probe service listening on %s:%s", PROBE_HOST, PROBE_PORT)
        except Exception as exc:   # never take the controller down over this
            self._server = None
            self.logger.error("QSMO probe service could NOT start on %s:%s (%s). "
                              "Another controller still running? The monitor cannot work without it.",
                              PROBE_HOST, PROBE_PORT, exc)

    # ---- state -----------------------------------------------------------
    def datapath_ids(self):
        return tuple(self._datapaths.keys())

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self._datapaths[datapath.id] = datapath
        elif ev.state == DEAD_DISPATCHER:
            # only forget it if this is still the registered connection (a switch that moved to
            # its Hybrid tunnel reconnects with the same dpid before the old socket dies)
            if self._datapaths.get(datapath.id) is datapath:
                self._datapaths.pop(datapath.id, None)

    @set_ev_cls(ofp_event.EventOFPEchoReply, MAIN_DISPATCHER)
    def _echo_reply(self, ev):
        waiter = self._pending.pop(ev.msg.xid, None)
        if waiter is not None:
            waiter.set()

    # ---- the probe ---------------------------------------------------------
    def probe(self, dpid: str, timeout: float = 1.0):
        """Return {ok, latency_ms, error} for one real datapath probe (call from a greenthread)."""
        try:
            dpid_int = int(str(dpid), 16)
        except ValueError:
            return {"ok": False, "latency_ms": None, "error": f"invalid DPID: {dpid}"}

        datapath = self._datapaths.get(dpid_int)
        if datapath is None:
            return {"ok": False, "latency_ms": None, "error": "datapath not connected"}
        xid = next(self._xids)
        # greenthread (loopback service) -> hub.Event; plain OS thread (in-process callers such
        # as the live tests) -> threading.Event. _echo_reply only calls .set() on either.
        in_green = threading.current_thread() is threading.main_thread()
        waiter = hub.Event() if in_green else threading.Event()
        self._pending[xid] = waiter

        request = datapath.ofproto_parser.OFPEchoRequest(datapath, data=b"qsmo")
        request.xid = xid
        started = time.perf_counter()
        try:
            datapath.send_msg(request)
            if not waiter.wait(timeout):
                return {"ok": False, "latency_ms": None, "error": "echo reply timeout"}
            return {"ok": True, "latency_ms": (time.perf_counter() - started) * 1000.0, "error": None}
        except Exception as exc:
            return {"ok": False, "latency_ms": None, "error": str(exc)}
        finally:
            self._pending.pop(xid, None)

    # ---- loopback service ----------------------------------------------------
    def _serve(self, sock, addr):
        try:
            fh = sock.makefile("rwb")
            line = fh.readline()
            if not line:
                return
            try:
                req = json.loads(line.decode())
                op = req.get("op")
                if op == "ping":
                    resp = {"ok": True, "pid": os.getpid()}
                elif op == "dpids":
                    resp = {"ok": True, "dpids": ["%016x" % d for d in self.datapath_ids()]}
                elif op == "probe":
                    resp = self.probe(req["dpid"], float(req.get("timeout", 1.0)))
                else:
                    resp = {"ok": False, "error": f"unknown op {op!r}"}
            except Exception as exc:
                resp = {"ok": False, "latency_ms": None, "error": f"bad request: {exc}"}
            fh.write((json.dumps(resp) + "\n").encode())
            fh.flush()
        except Exception:
            pass
        finally:
            try:
                sock.close()
            except Exception:
                pass
