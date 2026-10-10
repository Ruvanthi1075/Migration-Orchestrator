#!/bin/bash
# sync_topology.sh -- run after topo.py is up (Mininet running), whenever the topology changes.
# Regenerates everything that used to be hand-maintained:
#   network/flows_config.json, docs/PORTS.md (+ port registry), Hybrid per-switch certs.
set -euo pipefail
cd "$(dirname "$0")"

sudo python3 network/gen_flows.py
sudo python3 migration/port_registry.py --sync --prune
mapfile -t SW < <(sudo ovs-vsctl list-br | sort -V)
PRUNE="${PRUNE:-0}" bash migration/hybrid/gen_hybrid_certs.sh "${SW[@]}"
