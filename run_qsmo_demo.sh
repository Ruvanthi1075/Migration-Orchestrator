#!/bin/bash

set -o pipefail

cd ~/Migration-Orchestrator/Migration-Orchestrator || exit 1

clear

echo "================================================================"
echo "             QSMO MIGRATION ORCHESTRATOR"
echo "             LIVE HYBRID MIGRATION SYSTEM"
echo "================================================================"
echo
echo "  PROJECT : Hybrid SDN Controller Migration"
echo "  METHOD  : SGBM + BBM"
echo "  MONITOR : Degradation Detection"
echo "  RECOVERY: Automatic Rollback"
echo
echo "================================================================"
echo "  LIVE DEMONSTRATION STARTING"
echo "================================================================"
echo

LOG="$HOME/qsmo_live_demo_$(date +%Y%m%d_%H%M%S).log"

echo "Evidence log: $LOG"
echo
echo "Starting OS-Ken controller..."
echo

sudo env PYTHONPATH="$PWD:/home/ruvan/.local/lib/python3.10/site-packages" \
osken-manager \
--observe-links \
--ctl-privkey migration/certs/controller.key \
--ctl-cert migration/certs/controller.cert \
--ca-certs migration/certs/ca.cert \
network/topology.py \
controller/simple_l2_switch.py \
tests/test_sgbm_live_orchestrated.py 2>&1 | tee "$LOG"

STATUS=${PIPESTATUS[0]}

echo
echo "================================================================"
if [ "$STATUS" -eq 0 ]; then
    echo "             CONTROLLER PROCESS EXITED"
else
    echo "             CONTROLLER EXIT STATUS: $STATUS"
fi
echo "================================================================"
echo "Evidence saved at: $LOG"

exit "$STATUS"
