#!/bin/bash
# Terminal 2 -- the SDN controller (OS-Ken): topology adapter + L2 switch + OpenFlow echo probe.
# Leave it running. The monitor is a SEPARATE program: ./run_monitor.sh (terminal 3).
# Order:  1 ./run_network.sh   2 ./run_controller.sh   3 ./run_monitor.sh   4 ./run_migration.sh
set -o pipefail
source "$(dirname "$0")/qsmo_env.sh"

OSKEN="$(command -v osken-manager)"
[ -z "$OSKEN" ] && { echo "osken-manager not found (activate your .venv first)"; exit 1; }

ensure_certs
if [ "$CERTS_CHANGED" = 1 ]; then
  echo "[certs] certificates were regenerated -> (re)start ./run_network.sh so Open vSwitch loads them"
fi

# a leftover controller would make the new one fail to bind -> switches talk to the OLD one
for port in 6653 6691; do
  who="$(sudo ss -ltnp "sport = :$port" 2>/dev/null | tail -n +2)"
  if [ -n "$who" ]; then
    echo "Port $port is already in use:"; echo "$who"
    echo "Stop the old controller first (Ctrl+C in its terminal, or: sudo pkill -f osken-manager)"; exit 1
  fi
done

LOG="logs/controller_$(date +%Y%m%d_%H%M%S).log"
echo "Controller log : $ROOT/$LOG"
echo "State (SQLite) : $ROOT/runtime/qsmo.db   (survives restarts; 'ls -a runtime')"

sudo env "$PYENV" "$OSKEN" \
  --observe-links \
  --ctl-privkey migration/certs/controller.key \
  --ctl-cert migration/certs/controller.cert \
  --ca-certs migration/certs/ca.cert \
  network/topology.py \
  controller/simple_l2_switch.py \
  controller/qsmo_echo_probe.py 2>&1 | tee "$LOG"
