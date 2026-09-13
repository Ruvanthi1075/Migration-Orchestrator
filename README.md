QSMO — Automated Quantum-Safe Migration Orchestrator
An orchestrator that automatically migrates an SDN control plane from classical TLS ("Legacy") to hybrid post-quantum TLS ("Hybrid"), one switch at a time, prioritized by which control links carry the most critical traffic — with automatic monitoring and rollback if a migrated link degrades.

Built on Mininet + Open vSwitch (the emulated network) and OS-Ken (the SDN controller framework, a maintained fork of Ryu).

What problem this solves
The SDN controller talks to every switch over a TLS-encrypted control channel. Classical TLS is vulnerable to future quantum computers. NIST-standardized post-quantum algorithms (ML-KEM, ML-DSA) fix that, but migrating every switch at once isn't realistic — devices vary, PQC handshakes cost more, and not every link is equally important. This project automates the decision of which switch to migrate next, verifies each migration actually worked, and rolls it back automatically if it doesn't hold up under monitoring.

Repo layout
network/
  topology.py          Feature 1 — live control-plane graph (Person A)
  coverage.py           Feature 2 — weakest-link security coverage math (Person A)
  flows_config.json     per-switch flow definitions (criticality/security/latency weights)
  topo.py                Mininet topology definition (Person C)
  visualize.py            demo/report visualization, not part of the frozen interface

controller/
  simple_l2_switch.py    shared L2 learning-switch app (must ignore LLDP — see Known Issues)

optimizer/
  optimizer.py            Algorithm 1 / SGBM greedy migration selection (Person B)
  capability_tracker.py    per-device capability tracking (Person B)

migration/
  gen_certs.sh             generates the CA/controller/switch cert chain for SSL
  stunnel_configs/         stunnel client/server configs used for the two-stage PQC cutover
  migrate_link.py           [not yet started] Guarded Migration Execution (Person C)

(not yet present)
  monitor.py                [not yet started] TDM + BRD — degradation monitoring & rollback (Person D)
  ledger.py                  [not yet started] Migration Metadata Ledger (Person C/D)
The corrected model (read before touching any file)
The original paper's Algorithm 1 conflates the migration target with the graph flow paths are measured over — that collapses once you're actually on a real SDN control channel, which is always 1 hop from controller to switch. This project splits it into two separate graphs:

LINK graph (topology.py) — switch↔switch adjacency, discovered live via OS-Ken/LLDP. This is the control plane. The controller itself is never a node here.
Every place the original paper says "path Pf," read: the switches that must be Hybrid before flow f is fully protected, computed over the LINK graph.
core_dpid   = switch with highest degree in the LINK graph (ties -> lowest dpid)
Pf(flow)    = shortest path, over LINK graph, from flow's destination_dpid to core_dpid
Lf(flow)    = { s in Pf(flow) : state(s) == Legacy }
w(f)        = 0.5*criticality + 0.3*security_sla + 0.2*latency_sla
security(f) = min(state(s) for s in Pf(f))     # Legacy=0, Hybrid=1 — weakest link, not an average
C           = sum(w(f) * security(f) for f in F)
State ownership (nobody but the designated owner writes state):

optimizer.py — reads state, never writes it.
migrate_link.py — the only module that sets a switch to "Hybrid", after a verified handshake.
monitor.py — the only module that reverts a switch to "Legacy" on rollback.
coverage.py — read-only, never mutates state.
No-hardcode rule: topology.py contains zero switch names, dpids, or counts written literally — everything comes from topo_api.get_all_switch()/get_all_link() live.

Setup
sudo apt install mininet openvswitch-switch python3-os-ken
python3 -m pip install networkx matplotlib

cd migration && ./gen_certs.sh   # generates the cert chain topo.py and osken-manager both need
Running it
Terminal 1 — controller, leave running:

cd Migration-Orchestrator
sudo osken-manager --observe-links \
  --ctl-privkey migration/certs/controller.key \
  --ctl-cert migration/certs/controller.cert \
  --ca-certs migration/certs/ca.cert \
  network/topology.py \
  controller/simple_l2_switch.py
Terminal 2 — Mininet:

cd Migration-Orchestrator/network
sudo python3 topo.py
# exit cleanly with `exit`, never Ctrl+C — leftover state needs `sudo mn -c` to clean up
Terminal 3 — verification:

sudo ovs-vsctl show                                       # look for is_connected: true
for s in s0 s1 s2 s3 s4 s5 s6; do echo -n "$s: "; sudo ovs-vsctl get bridge $s datapath_id; done
Standalone coverage.py test (no Mininet/OS-Ken needed):

cd network && python3 coverage.py
Known issues / open items
optimizer.py imports dead function names. It currently does from coverage import total_coverage, get_path_links, flow_weight — but coverage.py only exposes compute_coverage, compute_path, compute_legacy_set, compute_gain, flow_weight. This will ImportError immediately. Needs Person B to update to the frozen §5.3 names before optimizer/coverage integration can be tested end-to-end.
s0's dpid must be pinned explicitly in topo.py. OVS treats an all-zero datapath-id as "unset" and silently self-assigns a MAC-derived one instead, which isn't guaranteed stable across reboots. Fix: dpid='0000000000000010' on s0's net.addSwitch() call (any nonzero value works — just not ...0000). s1–s6 are fine as-is since their trailing-digit-derived dpids are already nonzero.
simple_l2_switch.py must ignore LLDP frames. Without an early if eth.ethertype == ether_types.ETH_TYPE_LLDP: return in the packet-in handler, the learning switch floods OS-Ken's topology-discovery probes like ordinary traffic, causing topology.py to discover a near-full-mesh of phantom links instead of the real ~7-link topology. Confirmed live: link count climbed toward C(7,2)=21 before the fix.
migrate_link.py, monitor.py, ledger.py not started yet — Person C/D's work.
STP means the discovered graph can legitimately vary between runs (7 links vs. 6, depending on which redundant leg STP blocks) — this is expected behavior, not a bug; coverage.py's functions are topology-agnostic by design and were verified correct against both converged shapes.
Team / ownership
Area	Owner	File(s)
Control-plane graph + weakest-link coverage math	Person A	network/topology.py, network/coverage.py
Greedy migration selection (SGBM)	Person B	optimizer/optimizer.py, optimizer/capability_tracker.py
Mininet topology, Guarded Migration Execution	Person C	network/topo.py, migration/migrate_link.py
Degradation monitoring, rollback, audit ledger	Person D	monitor.py, ledger.py
See QSMO_Implementation_Guide.docx for the full spec this repo implements against.****
