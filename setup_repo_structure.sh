#!/usr/bin/env bash
# Run this from inside ~/sdn-project (your existing repo root).
# Reorganizes the current flat folder (topo.py, simple_l2_switch.py,
# key.pem, cert.pem) into the full team structure, and creates empty
# folders for everyone else's features so the repo shape is right
# from day one.

set -e  # stop on first error, so you notice if a `mv` fails

echo "Creating folder skeleton..."
mkdir -p docs
mkdir -p setup
mkdir -p network                      # Person A -- Feature 1 + 2
mkdir -p controller
mkdir -p optimizer                    # Person B -- Feature 3 + 5
mkdir -p migration/stunnel_configs    # Person C -- Feature 4
mkdir -p migration/certs              # TLS keys/certs live here, NOT at repo root
mkdir -p monitoring/docker            # Person D -- Feature 6
mkdir -p orchestrator
mkdir -p tests
mkdir -p results/logs
mkdir -p results/plots
mkdir -p report/IEEE_report
mkdir -p report/presentation
mkdir -p scripts

echo "Moving existing files into their new homes..."
[ -f topo.py ]             && mv topo.py network/topo.py
[ -f simple_l2_switch.py ] && mv simple_l2_switch.py controller/simple_l2_switch.py
[ -f key.pem ]             && mv key.pem migration/certs/key.pem
[ -f cert.pem ]            && mv cert.pem migration/certs/cert.pem

echo "Creating placeholder top-level files..."
touch README.md requirements.txt

# IMPORTANT: keeps private keys, certs, and Python junk out of git
cat > .gitignore << 'EOF'
# secrets -- never commit these
*.pem
*.key
migration/certs/

# python
__pycache__/
*.pyc
.venv/
sdn-env/

# results you regenerate, not hand-authored
results/logs/*.csv
results/logs/*.json
results/plots/*.png
EOF

echo ""
echo "Done. New layout:"
find . -maxdepth 2 -not -path '*/\.*' | sort
echo ""
echo "Next: drop topology.py, flows_config.json, and coverage.py into network/"
echo "      (alongside the topo.py that just moved there)."
