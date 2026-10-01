#!/usr/bin/env bash
# check_chain.sh -- verify the chained Hybrid stunnel path layer by layer,
# BEFORE running MigrationExecutor.migrate() against a real switch.
#
#   switch --PQC--> :16653 --plain--> :6660 --classical TLS--> :6653 OS-Ken
#
# Prereqs (all already running):
#   1. osken-manager, started exactly as for Legacy (Step 4 -- no extra flags)
#   2. the shared server:  stunnel migration/hybrid/stunnel/server.conf
#
# Usage (from the repo root):   bash migration/hybrid/check_chain.sh [switch-name]
# Default switch-name is s3. Nothing here touches OVS or the Legacy sessions.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SW="${1:-s3}"
PQC_OPENSSL="${PQC_OPENSSL:-/opt/openssl35/bin/openssl}"
HC="$REPO/migration/hybrid/certs"
pass=0; fail=0
ok()  { echo "  PASS: $*"; pass=$((pass+1)); }
bad() { echo "  FAIL: $*"; fail=$((fail+1)); }

echo "== 1. All three listeners are up =="
for port in 6653 6660 16653; do
    if command -v ss >/dev/null 2>&1; then
        ss -tln | awk '{print $4}' | grep -qE "[:.]${port}\$" && up=1 || up=0
    else   # no iproute2: probe with a bare TCP connect instead
        (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null && up=1 || up=0
    fi
    if [[ $up -eq 1 ]]; then ok "port $port listening"
    else bad "nothing listening on $port"; fi
done
echo "   (6653 = OS-Ken SSL, 6660 = rewrap plaintext, 16653 = Hybrid PQC front door)"

echo "== 2. Hybrid front door: PQC handshake with $SW's ML-DSA cert =="
out=$("$PQC_OPENSSL" s_client -connect 127.0.0.1:16653 -tls1_3 -groups X25519MLKEM768 \
      -cert "$HC/switch-${SW}-hybrid.cert" -key "$HC/switch-${SW}-hybrid.key" \
      -CAfile "$HC/hybrid-ca.cert" </dev/null 2>&1)
if echo "$out" | grep -qE "Verification: OK|Verify return code: 0"; then ok "handshake + cert chain verified"
else bad "handshake/verify failed -- see: tail -30 migration/hybrid/stunnel/logs/server.log"; fi
echo "$out" | grep -E "Negotiated TLS1.3 group|Peer signature type|Protocol  *:" | sed 's/^/   /'

echo "== 3. Rewrap hop -> OS-Ken: plaintext OpenFlow HELLO on 6660 must get a HELLO back =="
reply=$(python3 - <<'PY'
import socket, struct
try:
    s = socket.create_connection(("127.0.0.1", 6660), timeout=5)
    s.sendall(struct.pack("!BBHI", 4, 0, 8, 1))      # OFPT_HELLO, OF1.3
    s.settimeout(5)
    d = s.recv(64)
    print(d.hex() if d else "CLOSED")
except Exception as e:
    print("ERR " + type(e).__name__)
PY
)
# byte 0 = version (>=0x04), byte 1 = type 0 (HELLO)
if [[ "$reply" =~ ^0[4-9]00 ]]; then ok "OS-Ken answered with an OpenFlow HELLO ($reply)"
else bad "no HELLO back (got: $reply) -- rewrap->OS-Ken TLS leg is broken; see server.log"; fi

echo
echo "Result: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
