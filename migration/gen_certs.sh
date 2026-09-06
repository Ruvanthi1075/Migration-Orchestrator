#!/usr/bin/env bash
# gen_certs.sh
# Generates the CA + controller + switch certificate chain used by
# topo.py's SSL control channel (the classical/"Legacy" baseline).
# Run this on the Ubuntu VM BEFORE starting the controller or topo.py.
#
# Uses the SYSTEM openssl deliberately, not the /opt/openssl35 +
# oqs-provider build: this is the classical ECDSA baseline, which is
# what "Legacy" means in the security graph. The Hybrid PQC step
# (x25519_mlkem768 key exchange, and optionally ML-DSA signatures) is
# Feature 4's job via a per-link stunnel proxy that layers on top of
# this same trust chain -- see the bring-up guide.

set -euo pipefail

# This script lives at Migration-Orchestrator/migration/gen_certs.sh,
# so certs/ next to it resolves to Migration-Orchestrator/migration/certs
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERT_DIR="$SCRIPT_DIR/certs"
mkdir -p "$CERT_DIR"
cd "$CERT_DIR"

echo "== 1. Certificate Authority (self-signed, for this lab only) =="
openssl ecparam -name prime256v1 -genkey -noout -out ca.key
openssl req -x509 -new -key ca.key -sha256 -days 3650 \
  -subj "/CN=sdn-pqc-lab-CA" -out ca.cert

echo "== 2. Controller certificate, signed by the lab CA =="
openssl ecparam -name prime256v1 -genkey -noout -out controller.key
openssl req -new -key controller.key -subj "/CN=sdn-controller" -out controller.csr
openssl x509 -req -in controller.csr -CA ca.cert -CAkey ca.key -CAcreateserial \
  -days 825 -sha256 -out controller.cert
rm -f controller.csr

echo "== 3. Switch certificate (shared across all 7 switches for this lab) =="
# For closer-to-production realism, repeat this block per switch with a
# distinct CN (e.g. "sdn-switch-s3") and distinct key/cert filenames,
# then point each switch at its own switch-<name>.key/.cert in topo.py's
# configure_switch_ssl(). A single shared cert is fine for a testbed
# where the goal is exercising the migration logic, not access control
# between switches.
openssl ecparam -name prime256v1 -genkey -noout -out switch.key
openssl req -new -key switch.key -subj "/CN=sdn-switch" -out switch.csr
openssl x509 -req -in switch.csr -CA ca.cert -CAkey ca.key -CAcreateserial \
  -days 825 -sha256 -out switch.cert
rm -f switch.csr

chmod 600 ./*.key
echo
echo "Done. Files written to $CERT_DIR:"
ls -1 "$CERT_DIR"
echo
echo "Next: start the controller with these certs, then run topo.py."
