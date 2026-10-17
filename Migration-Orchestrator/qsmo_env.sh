#!/bin/bash
# qsmo_env.sh -- sourced by every run_*.sh. Not meant to be run by hand.
#   * cd to the project, create runtime/ logs/ reports/ (state lives in <project>/runtime)
#   * PY / SITE / PYENV for "sudo env ..." so root sees your venv's packages
#   * ensure_certs: make sure the Legacy TLS chain exists AND is consistent
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1
mkdir -p runtime reports logs
PY="$(command -v python3)"
SITE="$($PY -c 'import site;print(":".join(site.getsitepackages()+[site.getusersitepackages()]))')"
PYENV="PYTHONPATH=$ROOT:$SITE"
CERTS="$ROOT/migration/certs"
CERTS_CHANGED=0

_pub_cert() { openssl x509 -in "$1" -noout -pubkey 2>/dev/null | sha256sum | cut -d' ' -f1; }
_pub_key()  { openssl pkey -in "$1" -pubout 2>/dev/null | sha256sum | cut -d' ' -f1; }

certs_ok() {
  local f
  for f in ca.cert controller.cert controller.key switch.cert switch.key; do
    [ -s "$CERTS/$f" ] || { CERT_WHY="missing migration/certs/$f"; return 1; }
  done
  openssl verify -CAfile "$CERTS/ca.cert" "$CERTS/controller.cert" "$CERTS/switch.cert" >/dev/null 2>&1 \
    || { CERT_WHY="controller/switch cert is not signed by ca.cert"; return 1; }
  [ "$(_pub_cert "$CERTS/controller.cert")" = "$(_pub_key "$CERTS/controller.key")" ] \
    || { CERT_WHY="controller.key does not match controller.cert"; return 1; }
  [ "$(_pub_cert "$CERTS/switch.cert")" = "$(_pub_key "$CERTS/switch.key")" ] \
    || { CERT_WHY="switch.key does not match switch.cert"; return 1; }
  return 0
}

ensure_certs() {
  if certs_ok; then return 0; fi
  echo "[certs] $CERT_WHY -> regenerating the Legacy TLS chain (migration/gen_certs.sh)"
  bash "$ROOT/migration/gen_certs.sh" >/dev/null || { echo "[certs] gen_certs.sh FAILED"; exit 1; }
  chmod 644 "$CERTS"/*.cert; chmod 600 "$CERTS"/*.key
  certs_ok || { echo "[certs] still inconsistent: $CERT_WHY"; exit 1; }
  CERTS_CHANGED=1
  echo "[certs] new chain written to $CERTS"
  echo "[certs] NOTE: Open vSwitch keeps the old CA in memory; run_network.sh restarts it for you."
}

# Hybrid (ML-DSA) certificates: controller-hybrid + one per OVS bridge. Incremental: only missing or
# invalid ones are issued. Needs the PQC OpenSSL (default /opt/openssl35/bin/openssl, PQC_OPENSSL=...).
ensure_hybrid_certs() {
  local hc="$ROOT/migration/hybrid/certs" br missing=0
  mapfile -t BR < <(sudo ovs-vsctl list-br 2>/dev/null | sort -V)
  [ "${#BR[@]}" -eq 0 ] && { echo "[hybrid-certs] no OVS bridges found: is ./run_network.sh up?"; return 1; }
  for f in hybrid-ca.cert controller-hybrid.cert controller-hybrid.key; do [ -s "$hc/$f" ] || missing=1; done
  for br in "${BR[@]}"; do
    [ -s "$hc/switch-$br-hybrid.key" ] && [ -s "$hc/switch-$br-hybrid.cert" ] || missing=1
  done
  [ "$missing" = 0 ] && return 0
  echo "[hybrid-certs] missing for some of ${#BR[@]} switches -> generating (gen_hybrid_certs.sh)"
  if ! sudo env "PQC_OPENSSL=${PQC_OPENSSL:-/opt/openssl35/bin/openssl}" bash "$ROOT/migration/hybrid/gen_hybrid_certs.sh" "${BR[@]}" >"$ROOT/logs/hybrid_certs.log" 2>&1; then
    echo "[hybrid-certs] FAILED, see $ROOT/logs/hybrid_certs.log :"; tail -6 "$ROOT/logs/hybrid_certs.log"; return 1
  fi
  echo "[hybrid-certs] done"
}
