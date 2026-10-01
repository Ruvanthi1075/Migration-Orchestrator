"""
topology.py
Feature 1 -- Control-Plane Topology Adapter (Person A)

CORRECTED MODEL (see QSMO_Implementation_Guide.docx, section 2 and 6.1)
------------------------------------------------------------------------
This is the LINK graph: switch <-> switch control-plane adjacency,
discovered live from OS-Ken's own topology events (LLDP-based), for
the switches that Mininet/OVS brings up via topo.py. It is NOT built
from topo.py, NOT built from a hand-written dict, and does not import
anything from topo.py. The orchestrator side and the Mininet side only
agree on the dpid naming convention (16-hex-char dpid strings), per
the guide's section 4 and 5.1.

NO-HARDCODE RULE (guide section 6.1)
-------------------------------------
There are zero switch names, dpids, or switch counts written literally
anywhere below. Every node and edge comes from topo_api.get_all_switch()
/ get_all_link() at the moment the graph is rebuilt. If the physical
topology changes (topo.py is swapped for a different shape, a switch
is added/removed, a link flaps), this module reacts to the resulting
OS-Ken event and rebuilds G from scratch -- nothing here needs to be
edited by hand. This is what makes topology.py "not static."

Controller (c0) is deliberately NOT a node in this graph. LLDP-based
topology discovery only sees switch<->switch links; the controller's
own control channel isn't part of that discovery. This matches the
guide's corrected model: Pf for a flow is measured over the discovered
switch graph only (section 2.2).

Note on STP interaction (learned during live verification): the
discovered graph reflects the ACTIVE spanning tree, not the full
physical wiring -- a port that STP has put into blocking state does
not forward LLDP probes either, so a redundant/backup link (e.g. the
s1-s2-s0 triangle's blocked leg) will not appear as an edge here while
it is blocked. This is correct behavior for this module (it only
reports what LLDP can currently see), but means core_dpid and every
Pf(flow) are computed over whichever spanning tree STP has currently
converged to, which can differ between runs. Verified live against two
different converged topologies (7 switches/7 links, and 7 switches/6
links with a different elected core switch) -- compute_path()/
compute_coverage() in coverage.py produced correct results in both
cases with zero code changes, confirming the topology-agnostic
property this module is designed for.

Data contract this module implements (guide section 5.1):
    get_graph()          -> networkx.Graph
        node id       : dpid, 16-hex-char string, e.g. "0000000000000003"
        node['name']  : human label if known (best-effort, may be None)
        node['state'] : "Legacy" | "Hybrid"  (default "Legacy" for a new switch)
        edge          : undirected, one per discovered switch<->switch LLDP link
    get_state(dpid)   -> "Legacy" | "Hybrid"
    set_state(dpid, value) -> None

State ownership rule (guide section 2.3): this module never decides
WHEN a switch becomes Hybrid or reverts to Legacy -- that is
migrate_link.py (Person C) and monitor.py (Person D)'s job
respectively. topology.py only stores whatever state it is told and
answers questions about the graph. coverage.py (also Person A) only
reads this graph; it never calls set_state.
"""

from os_ken.base import app_manager
from os_ken.controller.handler import set_ev_cls
from os_ken.topology import event, api as topo_api

import networkx as nx


class TopologyAdapter(app_manager.OSKenApp):
    """
    OS-Ken app that maintains the single authoritative NetworkX graph
    of the SDN control plane's switch-to-switch adjacency, and the
    per-dpid Legacy/Hybrid state.

    Run this alongside the rest of the orchestrator apps under
    osken-manager. The SSL flags are required for the switch<->
    controller handshake to succeed at all -- topo.py only configures
    the switch side of that handshake; OS-Ken's own SSL listener needs
    these to present its own certificate:

        sudo osken-manager --observe-links \\
            --ctl-privkey migration/certs/controller.key \\
            --ctl-cert migration/certs/controller.cert \\
            --ca-certs migration/certs/ca.cert \\
            network/topology.py \\
            controller/simple_l2_switch.py

    --observe-links is required -- without it, OS-Ken never emits
    EventLinkAdd / EventLinkDelete and this graph will only ever have
    isolated nodes.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.G = nx.Graph()
        # dpid (str) -> "Legacy" | "Hybrid". Kept separate from G so a
        # state survives a _rebuild() even for a switch that
        # momentarily drops out of the discovered graph (e.g. a link
        # flap that doesn't actually take the switch itself down).
        self.states = {}
        # dpid (str) -> human label, best-effort only, never relied on
        # for any graph logic (see docstring above).
        self.names = {}

    # ---- OS-Ken event hooks -------------------------------------------------
    # Every one of these just triggers a full rebuild from OS-Ken's own
    # live topology API. Nothing here special-cases a particular
    # switch, dpid, or count.

    @set_ev_cls(event.EventSwitchEnter)
    def on_switch_enter(self, ev):
        self._rebuild()

    @set_ev_cls(event.EventSwitchLeave)
    def on_switch_leave(self, ev):
        self._rebuild()

    @set_ev_cls(event.EventLinkAdd)
    def on_link_add(self, ev):
        self._rebuild()

    @set_ev_cls(event.EventLinkDelete)
    def on_link_delete(self, ev):
        self._rebuild()

    # ---- Core rebuild logic ---------------------------------------------

    def _rebuild(self):
        """
        Throw the old graph away and reconstruct it entirely from what
        OS-Ken currently reports. This is intentionally simple (no
        incremental diffing) so there is no way for stale state to
        linger after a topology change -- correctness over micro-
        efficiency, since this only runs on switch/link churn, not on
        a hot path.
        """
        switches = topo_api.get_all_switch(self)
        links = topo_api.get_all_link(self)

        G = nx.Graph()
        for sw in switches:
            dpid = self._dpid_str(sw.dp.id)
            G.add_node(
                dpid,
                name=self.names.get(dpid),
                state=self.states.get(dpid, "Legacy"),
            )
        for lk in links:
            src = self._dpid_str(lk.src.dpid)
            dst = self._dpid_str(lk.dst.dpid)
            if src in G and dst in G:
                G.add_edge(src, dst)

        self.G = G

    @staticmethod
    def _dpid_str(dpid_int):
        """Canonical 16-hex-char dpid string, per the guide's 5.1 contract."""
        return "%016x" % dpid_int

    # ---- Public contract (guide section 5.1) -----------------------------

    def get_graph(self):
        """Return the current live switch<->switch control-plane graph."""
        return self.G

    def get_state(self, dpid):
        """"Legacy" | "Hybrid" for a given dpid; unseen switches default to Legacy."""
        return self.states.get(dpid, "Legacy")

    def set_state(self, dpid, value):
        """
        Record a state transition for dpid. Per the ownership rule
        (guide 2.3), topology.py itself never calls this on its own
        initiative -- migrate_link.py calls it after a verified
        successful Hybrid cutover, monitor.py calls it after a
        rollback. This method only enforces the invariant and keeps
        the graph node's 'state' attribute in sync.
        """
        assert value in ("Legacy", "Hybrid"), f"invalid state: {value!r}"
        self.states[dpid] = value
        if dpid in self.G:
            self.G.nodes[dpid]["state"] = value

    def set_name(self, dpid, name):
        """Optional best-effort human label; never used for graph logic."""
        self.names[dpid] = name
        if dpid in self.G:
            self.G.nodes[dpid]["name"] = name
