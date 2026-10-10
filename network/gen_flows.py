#!/usr/bin/env python3
"""
network/gen_flows.py

Generates network/flows_config.json from the LIVE topology instead of a
hand-edited file. One flow per switch (c0 -> switch), same schema that
coverage.load_flows() / annotate_flow_weights() already read.

Discovery (run as root with Mininet up):
  * bridges + dpids : ovs-vsctl list-br / get bridge <br> datapath_id
  * switch<->switch links : `ip -o link` veth peers (s1-eth1@s3-eth3)

Tiering (by degree in the discovered graph):
  * leaf switches (degree 1)     -> service profiles, assigned round-robin
                                    in dpid order (hospital, banking, video, iot)
  * everything else (agg / core) -> "infrastructure" profile
  * no link info found           -> every switch gets "infrastructure"

For the original 7-switch tree this reproduces the existing file.

Usage:
  sudo python3 network/gen_flows.py                       # writes network/flows_config.json
  sudo python3 network/gen_flows.py --out /tmp/f.json --overrides network/flow_overrides.json

--overrides is an optional JSON object keyed by bridge name or dpid, e.g.
  {"s3": {"impact_availability": "High", "q_delay_ms": 10}}

From inside the controller (live graph, no OVS calls):
  from network.gen_flows import flows_from_graph
  flows = flows_from_graph(topology.get_graph())
"""
import argparse
import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(_HERE, "flows_config.json")

INFRA = dict(bandwidth_mbps=10, impact_confidentiality="Moderate", impact_integrity="Moderate",
             impact_availability="Moderate", traffic_class="delay_sensitive",
             q_delay_ms=40, measured_delay_ms=35)

EDGE_PROFILES = [  # hospital, banking, video, iot (same order as topo.py's comments)
    dict(bandwidth_mbps=25, impact_confidentiality="High", impact_integrity="High",
         impact_availability="Moderate", traffic_class="delay_sensitive", q_delay_ms=15, measured_delay_ms=12),
    dict(bandwidth_mbps=25, impact_confidentiality="High", impact_integrity="High",
         impact_availability="Moderate", traffic_class="delay_sensitive", q_delay_ms=15, measured_delay_ms=12),
    dict(bandwidth_mbps=5, impact_confidentiality="Low", impact_integrity="Low",
         impact_availability="Low", traffic_class="delay_sensitive", q_delay_ms=60, measured_delay_ms=55),
    dict(bandwidth_mbps=8, impact_confidentiality="Low", impact_integrity="Moderate",
         impact_availability="Low", traffic_class="delay_sensitive", q_delay_ms=45, measured_delay_ms=40),
]


def _dpid(x):
    return str(x).strip().strip('"').lower().zfill(16)


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return r.returncode, r.stdout


def discover_ovs():
    """-> ({dpid: bridge_name}, [(dpid_a, dpid_b), ...])"""
    rc, out = _run(["ovs-vsctl", "list-br"])
    if rc != 0:
        raise SystemExit("ovs-vsctl list-br failed - run with sudo and Mininet up")
    by_name = {}
    for br in out.split():
        rc, dp = _run(["ovs-vsctl", "get", "bridge", br, "datapath_id"])
        if rc == 0 and dp.strip():
            by_name[br] = _dpid(dp)
    links = set()
    rc, out = _run(["ip", "-o", "link", "show"])
    if rc == 0:
        for m in re.finditer(r"^\d+:\s+([^@:\s]+)@([^:\s]+):", out, re.M):
            a, b = (x.rsplit("-eth", 1)[0] for x in m.groups())
            if a in by_name and b in by_name and a != b:
                links.add(frozenset((by_name[a], by_name[b])))
    return {d: n for n, d in by_name.items()}, [tuple(sorted(l)) for l in links]


def _core(degree):
    return max(degree, key=lambda n: (degree[n], -int(n, 16))) if degree else None


def build_flows(names, links, overrides=None):
    """names: {dpid: bridge_name}; links: [(dpid, dpid)]. Returns the flow list."""
    overrides = overrides or {}
    degree = {d: 0 for d in names}
    for a, b in links:
        degree[a] += 1
        degree[b] += 1
    have_links = bool(links)
    flows, edge_i = [], 0
    for dpid in sorted(names, key=lambda d: int(d, 16)):
        name = names[dpid]
        if have_links and degree[dpid] == 1:
            profile = EDGE_PROFILES[edge_i % len(EDGE_PROFILES)]
            edge_i += 1
        else:
            profile = INFRA
        flow = {"name": f"ctrl_{name}", "source": "c0", "destination": name,
                "destination_dpid": dpid, **profile}
        flow.update(overrides.get(name, {}))
        flow.update(overrides.get(dpid, {}))
        flows.append(flow)
    # natural order by bridge name (s0, s1, ... s10) to keep the file diff-friendly
    flows.sort(key=lambda f: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", f["destination"])])
    return flows


def flows_from_graph(G, overrides=None):
    """Live-graph variant (topology.get_graph()). Node attr 'name' used if set."""
    names = {n: (G.nodes[n].get("name") or "s" + n.lstrip("0")[-4:] or "s0") for n in G.nodes}
    links = [tuple(sorted(e)) for e in G.edges]
    return build_flows(names, links, overrides)


def write_flows(flows, path=DEFAULT_OUT):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"flows": flows}, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--overrides", help="JSON file of per-switch field overrides")
    args = ap.parse_args()
    overrides = json.load(open(args.overrides)) if args.overrides else {}
    names, links = discover_ovs()
    if not names:
        sys.exit("no OVS bridges found - start topo.py first")
    flows = build_flows(names, links, overrides)
    write_flows(flows, args.out)
    print(f"wrote {len(flows)} flows ({len(links)} links seen) -> {args.out}")


if __name__ == "__main__":
    main()
