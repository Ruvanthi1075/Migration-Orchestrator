#!/usr/bin/env python3
"""
topo.py - Quantum-Safe SDN Control-Plane Migration Orchestrator
Mininet emulated network: 3-tier tree PLUS one real redundant link.

                          +-------- s0 (core) --------+
                          |                            |
                    s1 (agg-A) ------cross-link------ s2 (agg-B)
                    /       \                        /       \
              s3(edge)   s4(edge)              s5(edge)   s6(edge)
               /  \        /  \                  /  \       /  \
             h1   h2     h3   h4               h5   h6    h7   h8

Flow groups (must match flows_config.json used by topology.py/coverage.py):
  h1, h2  -> "hospital" (edge s3)   importance = High
  h3, h4  -> "banking"  (edge s4)   importance = High
  h5, h6  -> "video"    (edge s5)   importance = Low
  h7, h8  -> "iot"      (edge s6)   importance = Medium

WHY THIS TOPOLOGY (and not a plain tree):
  - s1-s2 is a genuine extra physical link, not just more depth. Together
    with s1-s0 and s2-s0 it forms a triangle (s0-s1-s2-s0). That means
    there are TWO real, distinct routes between the agg-A side (hospital,
    banking) and the agg-B side (video, iot):
        via core:        s1 - s0 - s2   (3 hops between aggs)
        via cross-link:  s1 - s2        (1 hop between aggs, shorter!)
  - This is what makes "min path finder" in coverage.py meaningful instead
    of trivial: with only a tree, there is exactly one possible path
    between any two hosts, so a shortest-path call has nothing to choose
    between. With this triangle, networkx's shortest-path search is doing
    real work, and Feature 2/3 have to reason about which links actually
    sit on each flow's real, chosen path.
  - 15 total links (8 host-edge, 4 edge-agg, 2 agg-core, 1 agg-agg
    cross-link) across 7 switches and 8 hosts is comfortably inside the
    "5-8 switches" scope the 12-week plan calls for in Week 2, while still
    giving the greedy optimizer (Feature 3) a non-trivial set of real
    candidates every round.
  - A physical loop in a plain L2/OpenFlow network causes broadcast
    storms, so Spanning Tree Protocol (STP) is enabled on every switch
    below (stp=True). STP will keep exactly one of the three triangle
    links logically blocked during normal operation (almost certainly the
    cross-link, since it has the higher port numbers) and only activate it
    if a primary link/switch fails. That's a realistic, defensible design
    choice: the redundancy exists physically (and therefore still needs to
    be modeled and eventually migrated to Hybrid TLS by the orchestrator),
    even though it isn't the active forwarding path in day-to-day
    operation. This is NOT "resilience modeling under simulated failure"
    (which is out of scope) -- it's just an ordinary redundant physical
    design, same as any real campus/data-center network.

IMPORTANT - STP convergence: after net.start(), give the network ~30-45
seconds before running pingall so STP can finish electing the blocked
port. The script sleeps automatically; if `pingall` still drops packets
immediately after the CLI opens, just wait a bit longer and try again.

FEATURE 1 VERIFICATION: right before the CLI opens, this script also
pulls the live topology straight out of the running Mininet network
(via load_topology_from_mininet in topology.py) and diffs it against
PROJECT_TOPO, the fixture Feature 1/2 were tested against. A "MATCH"
line proves topology.py's graph model is accurate against the real
testbed, not just a hand-typed copy of it.
"""

import time

from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel, info

# topology.py lives in the same folder as this file
from topology import load_topology_from_mininet, PROJECT_TOPO


def build():
    net = Mininet(controller=RemoteController, switch=OVSKernelSwitch,
                  link=TCLink, autoSetMacs=True)

    info('*** Adding controller\n')
    net.addController('c0', controller=RemoteController,
                       ip='127.0.0.1', port=6653)

    info('*** Adding switches (core / aggregation / edge) with STP enabled\n')
    s0 = net.addSwitch('s0', cls=OVSKernelSwitch, protocols='OpenFlow13', stp=True)  # core
    s1 = net.addSwitch('s1', cls=OVSKernelSwitch, protocols='OpenFlow13', stp=True)  # agg-A
    s2 = net.addSwitch('s2', cls=OVSKernelSwitch, protocols='OpenFlow13', stp=True)  # agg-B
    s3 = net.addSwitch('s3', cls=OVSKernelSwitch, protocols='OpenFlow13', stp=True)  # edge - hospital
    s4 = net.addSwitch('s4', cls=OVSKernelSwitch, protocols='OpenFlow13', stp=True)  # edge - banking
    s5 = net.addSwitch('s5', cls=OVSKernelSwitch, protocols='OpenFlow13', stp=True)  # edge - video
    s6 = net.addSwitch('s6', cls=OVSKernelSwitch, protocols='OpenFlow13', stp=True)  # edge - iot

    info('*** Adding hosts (flat 10.0.0.0/24 subnet)\n')
    h1 = net.addHost('h1', ip='10.0.0.11/24')  # hospital
    h2 = net.addHost('h2', ip='10.0.0.12/24')  # hospital
    h3 = net.addHost('h3', ip='10.0.0.13/24')  # banking
    h4 = net.addHost('h4', ip='10.0.0.14/24')  # banking
    h5 = net.addHost('h5', ip='10.0.0.15/24')  # video
    h6 = net.addHost('h6', ip='10.0.0.16/24')  # video
    h7 = net.addHost('h7', ip='10.0.0.17/24')  # iot
    h8 = net.addHost('h8', ip='10.0.0.18/24')  # iot

    info('*** Wiring hosts to edge switches\n')
    net.addLink(h1, s3); net.addLink(h2, s3)
    net.addLink(h3, s4); net.addLink(h4, s4)
    net.addLink(h5, s5); net.addLink(h6, s5)
    net.addLink(h7, s6); net.addLink(h8, s6)

    info('*** Wiring edge switches to aggregation switches\n')
    net.addLink(s3, s1); net.addLink(s4, s1)   # agg-A: hospital + banking
    net.addLink(s5, s2); net.addLink(s6, s2)   # agg-B: video + iot

    info('*** Wiring aggregation switches to the core switch\n')
    net.addLink(s1, s0); net.addLink(s2, s0)

    info('*** Adding the redundant agg-to-agg cross-link (creates the triangle)\n')
    net.addLink(s1, s2)

    info('*** Starting network\n')
    net.start()

    info('*** Waiting for STP to converge (about 30-45s)...\n')
    time.sleep(45)

    info('*** Verifying live topology against Feature 1 fixture (PROJECT_TOPO)...\n')
    live_topo = load_topology_from_mininet(net)
    live_edges = {frozenset([l['a'], l['b']]) for l in live_topo['links']}
    expected_edges = {frozenset([l['a'], l['b']]) for l in PROJECT_TOPO['links']}

    if live_edges == expected_edges:
        print('MATCH: live Mininet topology matches PROJECT_TOPO exactly.')
    else:
        print('MISMATCH!')
        print('In live but not expected:', live_edges - expected_edges)
        print('In expected but not live:', expected_edges - live_edges)

    info('*** Ready. Try: pingall\n')
    CLI(net)
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    build()
