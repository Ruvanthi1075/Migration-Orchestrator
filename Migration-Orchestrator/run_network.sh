#!/bin/bash
# Terminal 1 -- the Mininet network (topology comes from network/topo.py).
# Start it first (or after the controller; switches keep retrying), leave it open, run `pingall` here.
#   ./run_network.sh                       # topology from network/topo.py
#   ./run_network.sh --topology ring:6     # or any spec topo.py understands
#   QSMO_RESTART_OVS=1 ./run_network.sh    # force an Open vSwitch restart
source "$(dirname "$0")/qsmo_env.sh"

ensure_certs

# Open vSwitch loads the CA / switch cert once and keeps them. Restart it whenever the CA it
# knows about is not the CA on disk (first run, regenerated certs, project moved).
CA_SUM="$(sha256sum "$CERTS/ca.cert" | cut -d' ' -f1)"
MARK="$ROOT/runtime/ovs_ca.sha"
if [ "$CERTS_CHANGED" = 1 ] || [ "${QSMO_RESTART_OVS:-0}" = 1 ] || [ "$(cat "$MARK" 2>/dev/null)" != "$CA_SUM" ]; then
  echo "[ovs] restarting Open vSwitch so it loads the current certificates"
  sudo systemctl restart openvswitch-switch 2>/dev/null \
    || sudo service openvswitch-switch restart 2>/dev/null \
    || sudo /usr/share/openvswitch/scripts/ovs-ctl restart >/dev/null 2>&1 \
    || echo "[ovs] could not restart Open vSwitch automatically: run 'sudo systemctl restart openvswitch-switch'"
  echo "$CA_SUM" > "$MARK"; chmod 666 "$MARK" 2>/dev/null
fi

echo "[net] cleaning any previous Mininet run (mn -c)"
sudo mn -c >/dev/null 2>&1

if ! sudo ss -ltn 2>/dev/null | grep -q ':6653 '; then
  echo "[net] WARNING: nothing listens on :6653 yet -> start ./run_controller.sh (terminal 2)."
  echo "[net]          Switches are fail-secure: pingall cannot work until the controller is up."
fi

# network/topo.py runs sync_topology.sh itself once the switches are up
sudo env "$PYENV" "$PY" network/topo.py "$@"
