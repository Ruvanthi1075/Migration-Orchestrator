#!/usr/bin/env bash
# gen_hybrid_certs.sh
# Generates the CA + controller + per-switch certificate chain used by
# the Hybrid (PQC) stunnel pair that migrate_link.py drives during
# Stage 1/2 (guide QSMO_Implementation_Guide.docx, section 8.4).
#
# This is a PURE ML-DSA signature chain, not a composite classical+PQC
# certificate -- composite formats are not finalized in the IETF as of
# this writing (guide §8.4/§12). The hybridity in this project lives in
# the KEY EXCHANGE (X25519 + ML-KEM-768, negotiated by stunnel's
# `groups = X25519MLKEM768` directive), not in the certificate itself.
#
# Deliberately uses the PQC-enabled OpenSSL build (/opt/openssl35), NOT
# the system openssl gen_certs.sh uses for the Legacy ECDSA baseline
# (guide §1.1 / §8.4). Mixing the two chains would make "Legacy" and
# "Hybrid" indistinguishable by construction -- never point this script
# at the system openssl, and never point gen_certs.sh at this one.
#
# Run this on the Ubuntu VM BEFORE running any migration that touches
# the switches listed below. Output lands in migration/hybrid/certs/,
# kept entirely separate from gen_certs.sh's migration/certs/ output
# (see that script's own comment on this point).

set -euo pipefail

# This script lives at Migration-Orchestrator-optimizer/migration/hybrid/gen_hybrid_certs.sh,
# so certs/ next to it resolves to .../migration/hybrid/certs
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERT_DIR="$SCRIPT_DIR/certs"
mkdir -p "$CERT_DIR"
cd "$CERT_DIR"

# Path to the PQC-enabled OpenSSL build. Override with:
#   PQC_OPENSSL=/some/other/path ./gen_hybrid_certs.sh
PQC_OPENSSL="${PQC_OPENSSL:-/opt/openssl35/bin/openssl}"

# ML-DSA parameter set. mldsa65 is the guide's default (roughly
# NIST-Level-3-equivalent) -- confirm the exact string your OpenSSL
# build exposes before running this, per guide §8.4:
#   $PQC_OPENSSL list -signature-algorithms | grep -i mldsa
MLDSA_ALG="${MLDSA_ALG:-mldsa65}"

# Every switch in the current topo.py tree (network/topo.py §5.2 dpid
# table). Edit this list if the topology changes -- this is the one
# place a topology-shape fact is allowed to be literal, since it's a
# one-off provisioning script, not a module topology.py/coverage.py/
# migrate_link.py import from (the no-hardcode rule, guide §6.1,
# applies to those modules, not to this cert-provisioning script).
SWITCHES=(s0 s1 s2 s3 s4 s5 s6)

if [[ ! -x "$PQC_OPENSSL" ]]; then
    echo "ERROR: PQC OpenSSL build not found/executable at $PQC_OPENSSL" >&2
    echo "       (this must be the /opt/openssl35 build with native ML-DSA support," >&2
    echo "       NOT the system openssl gen_certs.sh uses -- see this script's header)" >&2
    exit 1
fi

echo "== Confirming ML-DSA support in $PQC_OPENSSL =="
if ! "$PQC_OPENSSL" list -signature-algorithms | grep -qi "$MLDSA_ALG"; then
    echo "ERROR: '$MLDSA_ALG' not found in '$PQC_OPENSSL list -signature-algorithms'." >&2
    echo "       Run that command yourself and set MLDSA_ALG to the exact name string" >&2
    echo "       (mldsa44 / mldsa65 / mldsa87) before re-running this script." >&2
    exit 1
fi
echo "  OK: $MLDSA_ALG available"

echo "== 1. Hybrid CA (self-signed, ML-DSA, for this lab only) =="
"$PQC_OPENSSL" genpkey -algorithm "$MLDSA_ALG" -out hybrid-ca.key
"$PQC_OPENSSL" req -x509 -new -key hybrid-ca.key -days 3650 \
    -subj "/CN=sdn-pqc-lab-hybrid-CA" -out hybrid-ca.cert

echo "== 2. Controller Hybrid certificate, signed by the hybrid CA =="
"$PQC_OPENSSL" genpkey -algorithm "$MLDSA_ALG" -out controller-hybrid.key
"$PQC_OPENSSL" req -new -key controller-hybrid.key \
    -subj "/CN=sdn-controller-hybrid" -out controller-hybrid.csr
"$PQC_OPENSSL" x509 -req -in controller-hybrid.csr -CA hybrid-ca.cert \
    -CAkey hybrid-ca.key -CAcreateserial -days 825 -out controller-hybrid.cert
rm -f controller-hybrid.csr

echo "== 3. Per-switch Hybrid certificates (distinct CN each, unlike the shared Legacy cert) =="
# migrate_link.py's _hybrid_switch_cert_paths() expects exactly this
# naming convention: switch-<name>-hybrid.{key,cert}
for name in "${SWITCHES[@]}"; do
    echo "  -- $name --"
    "$PQC_OPENSSL" genpkey -algorithm "$MLDSA_ALG" -out "switch-${name}-hybrid.key"
    "$PQC_OPENSSL" req -new -key "switch-${name}-hybrid.key" \
        -subj "/CN=sdn-switch-${name}-hybrid" -out "switch-${name}-hybrid.csr"
    "$PQC_OPENSSL" x509 -req -in "switch-${name}-hybrid.csr" -CA hybrid-ca.cert \
        -CAkey hybrid-ca.key -CAcreateserial -days 825 -out "switch-${name}-hybrid.cert"
    rm -f "switch-${name}-hybrid.csr"
done

chmod 600 ./*.key
echo
echo "Done. Files written to $CERT_DIR:"
ls -1 "$CERT_DIR"
echo
echo "Next steps:"
echo "  1. Point migration/hybrid/stunnel/server.conf's cert/key at controller-hybrid.{cert,key}"
echo "     and CAfile at hybrid-ca.cert (see server.conf.template in this directory)."
echo "  2. Rebuild/confirm the hybrid_stunnel_bin binary at /opt/stunnel-pqc/bin/stunnel"
echo "     (system stunnel does not support the 'groups' directive -- see migrate_link.py's"
echo "     D2 design-decision note for why a separate binary path is required)."
echo "  3. Do NOT run gen_certs.sh's Legacy chain and this script against the same cert dir --"
echo "     they must stay in migration/certs/ and migration/hybrid/certs/ respectively."
