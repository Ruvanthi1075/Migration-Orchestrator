#!/bin/bash
# sync_topology.sh -- run after topo.py is up (Mininet running), whenever the topology changes.
# Regenerates everything that used to be hand-maintained:
#   network/flows_config.json, docs/PORTS.md (+ port registry), Hybrid per-switch certs.
set -euo pipefail
cd "$(dirname "$0")"
# topo.py already runs us as root with the right PYTHONPATH; only sudo when we are not root
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
PYRUN() { $SUDO env "PYTHONPATH=${PYTHONPATH:-$PWD}" python3 "$@"; }

PYRUN network/gen_flows.py
PYRUN migration/port_registry.py --sync --prune
mapfile -t SW < <($SUDO ovs-vsctl list-br | sort -V)
PRUNE="${PRUNE:-0}" bash migration/hybrid/gen_hybrid_certs.sh "${SW[@]}"
