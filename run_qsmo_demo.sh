#!/bin/bash

set -o pipefail

cd "$(dirname "$0")" || exit 1

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
controller/qsmo_echo_probe.py \
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

# ================================================================
# FINAL REPORT
# ================================================================

REPORT_DIR="$PWD/reports"
mkdir -p "$REPORT_DIR"

REPORT="$REPORT_DIR/qsmo_migration_report_$(date +%Y%m%d_%H%M%S).txt"

echo
echo "Generating final migration report..."

# The Python demo normally writes the report itself.
# If Ctrl+C causes OS-Ken to terminate before that code executes,
# recover the final report information from the evidence log.

{
    echo "================================================================"
    echo "              QSMO FINAL MIGRATION REPORT"
    echo "================================================================"
    echo
    echo "Generated : $(date)"
    echo "Evidence  : $LOG"
    echo
    echo "----------------------------------------------------------------"
    echo "MIGRATION SUMMARY"
    echo "----------------------------------------------------------------"

    grep -E \
        "Legacy .*mean=|Hybrid .*mean=|migrated to Hybrid successfully|Rolled back .*recovery_latency=" \
        "$LOG" || true

    echo
    echo "----------------------------------------------------------------"
    echo "HEALTH MONITORING"
    echo "----------------------------------------------------------------"

    grep -E \
        "Migration health (healthy|degraded)" \
        "$LOG" || true

    echo
    echo "----------------------------------------------------------------"
    echo "ROLLBACK / RECOVERY"
    echo "----------------------------------------------------------------"

    grep -E \
        "Rolled back .*recovery_latency=" \
        "$LOG" || true

    echo
    echo "----------------------------------------------------------------"
    echo "CONTROLLER EXIT"
    echo "----------------------------------------------------------------"
    echo "Exit status: $STATUS"
    echo
    echo "================================================================"

} | tee "$REPORT"

echo
echo "Final migration report saved to:"
echo "$REPORT"
echo
echo "Evidence log saved to:"
echo "$LOG"

exit "$STATUS"
