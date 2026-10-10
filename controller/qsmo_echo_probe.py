"""Live OpenFlow Echo RTT probe for QSMO.

This is intentionally topology-agnostic.  It learns connected datapaths from
OS-Ken and measures each datapath with a real OFP Echo Request/Reply exchange.
No switch names, DPIDs, counts, or latency values are hardcoded.
"""
from __future__ import annotations

import threading
import time
from itertools import count

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import DEAD_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.ofproto import ofproto_v1_3


class QSMOEchoProbe(app_manager.OSKenApp):
    """Expose a synchronous real OpenFlow Echo RTT probe to QSMO."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._datapaths = {}
        self._pending = {}
        self._xids = count(1)
        self._lock = threading.Lock()

    def datapath_ids(self):
        with self._lock:
            return tuple(self._datapaths.keys())

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        datapath = ev.datapath
        with self._lock:
            if ev.state == MAIN_DISPATCHER:
                self._datapaths[datapath.id] = datapath
            elif ev.state == DEAD_DISPATCHER:
                self._datapaths.pop(datapath.id, None)

    @set_ev_cls(ofp_event.EventOFPEchoReply, MAIN_DISPATCHER)
    def _echo_reply(self, ev):
        xid = ev.msg.xid
        with self._lock:
            waiter = self._pending.pop(xid, None)
        if waiter is not None:
            waiter.set()

    def probe(self, dpid: str, timeout: float = 1.0):
        """Return {ok, latency_ms, error} for one real datapath probe."""
        try:
            dpid_int = int(str(dpid), 16)
        except ValueError:
            return {"ok": False, "latency_ms": None, "error": f"invalid DPID: {dpid}"}

        with self._lock:
            datapath = self._datapaths.get(dpid_int)
            if datapath is None:
                return {"ok": False, "latency_ms": None, "error": "datapath not connected"}
            xid = next(self._xids)
            waiter = threading.Event()
            self._pending[xid] = waiter

        parser = datapath.ofproto_parser
        request = parser.OFPEchoRequest(datapath, data=b"qsmo")
        request.xid = xid

        started = time.perf_counter()
        try:
            datapath.send_msg(request)
            if not waiter.wait(timeout):
                return {"ok": False, "latency_ms": None, "error": "echo reply timeout"}
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            return {"ok": True, "latency_ms": elapsed_ms, "error": None}
        except Exception as exc:
            return {"ok": False, "latency_ms": None, "error": str(exc)}
        finally:
            with self._lock:
                self._pending.pop(xid, None)
