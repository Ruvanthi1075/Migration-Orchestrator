#!/bin/bash
# Diagnose "pingall fails / SSLV3_ALERT_CERTIFICATE_UNKNOWN / switches never connect".
# Run it with the controller (terminal 2) and network (terminal 1) up.
source "$(dirname "$0")/qsmo_env.sh"
ok(){ echo "  [ok]   $*"; }; bad(){ echo "  [FAIL] $*"; }
echo "1. certificate files"
if certs_ok; then ok "chain in migration/certs is consistent"; else bad "$CERT_WHY  (run ./run_network.sh or ./run_controller.sh: they regenerate it)"; fi
echo "2. controller listener :6653 accepts the switch cert"
out="$(echo | timeout 8 openssl s_client -connect 127.0.0.1:6653 -cert "$CERTS/switch.cert" -key "$CERTS/switch.key" \
      -CAfile "$CERTS/ca.cert" -verify_return_error 2>&1)"
if echo "$out" | grep -q "Verification: OK"; then ok "TLS handshake + controller cert verified"; else bad "handshake failed:"; echo "$out" | grep -iE "error|alert|refused" | head -3; fi
echo "3. what Open vSwitch is configured with"
sudo ovs-vsctl get-ssl 2>&1 | sed 's/^/        /'
SUM="$(sha256sum "$CERTS/ca.cert" | cut -d' ' -f1)"
[ "$(cat runtime/ovs_ca.sha 2>/dev/null)" = "$SUM" ] && ok "OVS was (re)started with this CA" || bad "OVS may hold an older CA: QSMO_RESTART_OVS=1 ./run_network.sh"
u="$(ps -o user= -C ovs-vswitchd 2>/dev/null | head -1)"
if [ -n "$u" ] && [ "$u" != root ]; then
  sudo -u "$u" test -r "$CERTS/switch.key" && ok "ovs-vswitchd ($u) can read switch.key" \
    || bad "ovs-vswitchd runs as '$u' and cannot read switch.key -> sudo chmod 644 $CERTS/switch.key (and every parent dir must be x for others)"
fi
echo "4. switch -> controller connections"
sudo ovs-vsctl --columns=name,is_connected list controller 2>&1 | paste -sd' ' | sed 's/name/\n  name/g' | head -40
echo "5. last SSL complaints from Open vSwitch"
sudo grep -iE "ssl|tls|certificate" /var/log/openvswitch/ovs-vswitchd.log 2>/dev/null | tail -5 | sed 's/^/        /'
