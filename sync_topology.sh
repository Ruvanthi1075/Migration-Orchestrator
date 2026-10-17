#!/bin/bash
# sync_topology.sh -- run after topo.py is up (Mininet running), whenever the topology changes.
# (network/topo.py runs it automatically; you can also run it by hand: bash sync_topology.sh)
# Regenerates everything that used to be hand-maintained:
#   network/flows_config.json, docs/PORTS.md (+ port registry), Hybrid per-switch certs.
# Files written as root are handed back to the user who started the sudo command, so you never
# need a manual `chown` afterwards.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"

SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
# root's python must see the packages of the user's environment (networkx, ...)
USITE=""
if [ -n "${SUDO_USER:-}" ]; then
  USITE="$(sudo -u "$SUDO_USER" python3 -c 'import site;print(":".join(site.getsitepackages()+[site.getusersitepackages()]))' 2>/dev/null || true)"
fi
export PYTHONPATH="$ROOT:${PYTHONPATH:-}:$USITE"
PYRUN() { $SUDO env "PYTHONPATH=$PYTHONPATH" python3 "$@"; }

fix_owner() {
  local u="${SUDO_USER:-}"
  [ -z "$u" ] && u="$(stat -c %U "$ROOT")"
  [ "$u" = root ] && return 0
  $SUDO chown -R "$u:$u" migration docs network reports runtime logs 2>/dev/null || true
}
trap fix_owner EXIT

echo "[sync] flows"
PYRUN network/gen_flows.py
echo "[sync] port registry"
PYRUN migration/port_registry.py --sync --prune
echo "[sync] Hybrid certificates"
mapfile -t SW < <($SUDO ovs-vsctl list-br | sort -V)
PQC_OPENSSL="${PQC_OPENSSL:-/opt/openssl35/bin/openssl}" PRUNE="${PRUNE:-0}" \
  bash migration/hybrid/gen_hybrid_certs.sh "${SW[@]}"
echo "[sync] done"

