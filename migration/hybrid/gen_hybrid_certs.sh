#!/usr/bin/env bash
# gen_hybrid_certs.sh
# Generates/updates the CA + controller + per-switch certificate chain used by
# the Hybrid (PQC) stunnel pair that migrate_link.py drives during Stage 1/2.
#
# DYNAMIC: the switch list is no longer hardcoded. Resolution order:
#   1. command-line args:        ./gen_hybrid_certs.sh s0 s1 s2
#   2. SWITCHES env (space-sep): SWITCHES="s0 s1 s2" ./gen_hybrid_certs.sh
#   3. live OVS bridges:         ovs-vsctl list-br   (Mininet must be running)
#
# INCREMENTAL: existing CA / controller / switch certs that still verify
# against the CA are kept; only missing or invalid ones are issued. So adding
# a switch to topo.py and re-running only creates that switch's cert.
#   FORCE=1  regenerate everything (new CA too)
#   PRUNE=1  delete switch-*-hybrid.* for switches not in the current list
#
# This is a PURE ML-DSA signature chain, not a composite classical+PQC
# certificate. The hybridity lives in the KEY EXCHANGE (X25519MLKEM768).
#
# Uses the PQC-enabled OpenSSL build (/opt/openssl35), NOT the system openssl
# gen_certs.sh uses for the Legacy baseline. Never mix the two chains.
# Output: migration/hybrid/certs/ (separate from gen_certs.sh's migration/certs/).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERT_DIR="$SCRIPT_DIR/certs"
mkdir -p "$CERT_DIR"
cd "$CERT_DIR"

PQC_OPENSSL="${PQC_OPENSSL:-/opt/openssl35/bin/openssl}"
MLDSA_ALG="${MLDSA_ALG:-mldsa65}"
FORCE="${FORCE:-0}"
PRUNE="${PRUNE:-0}"

# ---- switch list (dynamic) -------------------------------------------------
if [[ $# -gt 0 ]]; then
    SWITCH_LIST=("$@")
elif [[ -n "${SWITCHES:-}" ]]; then
    read -ra SWITCH_LIST <<< "$SWITCHES"
else
    OVS_OUT="$(ovs-vsctl list-br 2>/dev/null || sudo -n ovs-vsctl list-br 2>/dev/null || true)"
    SWITCH_LIST=()
    while IFS= read -r line; do
        [[ -n "$line" ]] && SWITCH_LIST+=("$line")
    done < <(printf '%s\n' "$OVS_OUT" | sort -V)
fi

if [[ ${#SWITCH_LIST[@]} -eq 0 ]]; then
    echo "ERROR: no switches found. Start topo.py (OVS bridges must exist), or pass names:" >&2
    echo "       ./gen_hybrid_certs.sh s0 s1 s2    |    SWITCHES=\"s0 s1\" ./gen_hybrid_certs.sh" >&2
    exit 1
fi
for name in "${SWITCH_LIST[@]}"; do
    if [[ ! "$name" =~ ^[A-Za-z0-9_.-]+$ ]]; then
        echo "ERROR: invalid switch name '$name' (allowed: letters, digits, _ . -)" >&2
        exit 1
    fi
done
echo "== Switches: ${SWITCH_LIST[*]} =="

if [[ ! -x "$PQC_OPENSSL" ]]; then
    echo "ERROR: PQC OpenSSL build not found/executable at $PQC_OPENSSL" >&2
    echo "       (must be the /opt/openssl35 build with native ML-DSA support," >&2
    echo "       NOT the system openssl gen_certs.sh uses)" >&2
    exit 1
fi

echo "== Confirming ML-DSA support in $PQC_OPENSSL =="
if ! "$PQC_OPENSSL" list -signature-algorithms | grep -qi "$MLDSA_ALG"; then
    echo "ERROR: '$MLDSA_ALG' not found in '$PQC_OPENSSL list -signature-algorithms'." >&2
    echo "       Set MLDSA_ALG to the exact name (mldsa44 / mldsa65 / mldsa87)." >&2
    exit 1
fi
echo "  OK: $MLDSA_ALG available"

valid_cert() {  # $1 = cert file, $2 = key file; true if both exist and cert verifies against the CA
    [[ "$FORCE" != "1" && -s "$1" && -s "$2" && -s hybrid-ca.cert ]] \
        && "$PQC_OPENSSL" verify -CAfile hybrid-ca.cert "$1" >/dev/null 2>&1
}

issue() {  # $1 = base name (file prefix), $2 = CN
    "$PQC_OPENSSL" genpkey -algorithm "$MLDSA_ALG" -out "$1.key"
    "$PQC_OPENSSL" req -new -key "$1.key" -subj "/CN=$2" -out "$1.csr"
    "$PQC_OPENSSL" x509 -req -in "$1.csr" -CA hybrid-ca.cert -CAkey hybrid-ca.key \
        -CAcreateserial -days 825 -out "$1.cert"
    rm -f "$1.csr"
}

echo "== 1. Hybrid CA =="
if [[ "$FORCE" != "1" && -s hybrid-ca.cert && -s hybrid-ca.key ]]; then
    echo "  keeping existing hybrid-ca.cert"
else
    "$PQC_OPENSSL" genpkey -algorithm "$MLDSA_ALG" -out hybrid-ca.key
    "$PQC_OPENSSL" req -x509 -new -key hybrid-ca.key -days 3650 \
        -subj "/CN=sdn-pqc-lab-hybrid-CA" -out hybrid-ca.cert
    echo "  generated new CA"
fi

echo "== 2. Controller Hybrid certificate =="
if valid_cert controller-hybrid.cert controller-hybrid.key; then
    echo "  keeping existing controller-hybrid.cert"
else
    issue controller-hybrid "sdn-controller-hybrid"
    echo "  issued controller-hybrid.cert"
fi

echo "== 3. Per-switch Hybrid certificates (migrate_link.py expects switch-<name>-hybrid.{key,cert}) =="
for name in "${SWITCH_LIST[@]}"; do
    if valid_cert "switch-${name}-hybrid.cert" "switch-${name}-hybrid.key"; then
        echo "  -- $name: exists, kept"
    else
        issue "switch-${name}-hybrid" "sdn-switch-${name}-hybrid"
        echo "  -- $name: issued"
    fi
done

if [[ "$PRUNE" == "1" ]]; then
    echo "== 4. Pruning certs for switches no longer in the list =="
    for f in switch-*-hybrid.cert; do
        [[ -e "$f" ]] || continue
        n="${f#switch-}"; n="${n%-hybrid.cert}"
        keep=0
        for name in "${SWITCH_LIST[@]}"; do [[ "$name" == "$n" ]] && keep=1; done
        if [[ $keep -eq 0 ]]; then
            rm -f "switch-${n}-hybrid.cert" "switch-${n}-hybrid.key"
            echo "  removed $n"
        fi
    done
fi

chmod 600 ./*.key
echo
echo "Done. Files in $CERT_DIR:"
ls -1 "$CERT_DIR"
echo
echo "Next steps:"
echo "  1. migration/hybrid/stunnel/server.conf must point at controller-hybrid.{cert,key} and hybrid-ca.cert."
echo "  2. Do NOT run gen_certs.sh's Legacy chain and this script against the same cert dir."
