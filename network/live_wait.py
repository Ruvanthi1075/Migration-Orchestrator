"""
network/live_wait.py

Topology-size-agnostic preflight helpers shared by the live tests, so no
test hardcodes a switch count. The expected size comes from, in order:
  1. QSMO_EXPECTED_SWITCHES env var
  2. the number of OVS bridges on this host (whatever topo.py created)
  3. nothing -> just wait for a connected graph that stops changing
"""
import os
import subprocess
import time

import networkx as nx


def ovs_bridge_count():
    try:
        r = subprocess.run(["ovs-vsctl", "list-br"], capture_output=True,
                           text=True, timeout=10)
        if r.returncode == 0:
            return len(r.stdout.split())
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def expected_switch_count():
    env = os.environ.get("QSMO_EXPECTED_SWITCHES")
    if env:
        return int(env)
    return ovs_bridge_count()


def wait_for_stable_topology(topology, expected=None, timeout=None,
                             stable_for=None, min_switches=2):
    """
    Block until the live graph is connected, has `expected` switches (when
    known), and nodes/edges have not changed for `stable_for` seconds.
    Returns the graph; raises RuntimeError on timeout.
    """
    # Loopy topologies (grid/mesh/ring) take longer to settle: STP blocks
    # ports and LLDP links flap while it converges. Tunable via env.
    if timeout is None:
        # scales with size: 15 switches -> 375 s, never below 240 s
        default_to = max(240.0, 25.0 * (expected or 0))
        timeout = float(os.environ.get("QSMO_TOPO_TIMEOUT", default_to))
    if stable_for is None:
        stable_for = float(os.environ.get("QSMO_TOPO_STABLE_S", "10"))
    deadline = time.monotonic() + timeout
    last_sig, stable_since = None, None
    while time.monotonic() < deadline:
        g = topology.get_graph()
        n, e = g.number_of_nodes(), g.number_of_edges()
        connected = n > 0 and nx.is_connected(g)
        ok = connected and n >= min_switches and (expected is None or n == expected)
        sig = (frozenset(g.nodes), frozenset(frozenset(x) for x in g.edges))
        if ok and sig == last_sig:
            if time.monotonic() - stable_since >= stable_for:
                return g
        else:
            last_sig, stable_since = sig, time.monotonic()
        print(f"Waiting for topology: {n}/{expected if expected is not None else '?'} "
              f"switches, {e} links (connected={connected})", flush=True)
        time.sleep(1.0)
    g = topology.get_graph()
    raise RuntimeError(
        "Preflight requires a stable connected topology; got "
        f"{g.number_of_nodes()} nodes / {g.number_of_edges()} edges (expected={expected})"
    )
