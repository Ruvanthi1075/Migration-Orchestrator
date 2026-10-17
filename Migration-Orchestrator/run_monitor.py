#!/usr/bin/env python3
"""run_monitor.py -- terminal 3: the always-on QSMO monitor (use ./run_monitor.sh)."""
import os
import signal
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from migration.monitor_service import MonitorService  # noqa: E402


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo (ovs-vsctl and stunnel need root): ./run_monitor.sh")
    svc = MonitorService()
    signal.signal(signal.SIGINT, lambda *_: svc.stop())
    signal.signal(signal.SIGTERM, lambda *_: svc.stop())
    # Ctrl+C stops ONLY the monitor. Hybrid tunnels run in their own session and keep running;
    # the next ./run_monitor.sh (or ./run_migration.sh) re-adopts them.
    svc.run()
    print("[qsmo-monitor] stopped (Hybrid tunnels keep running)")


if __name__ == "__main__":
    main()
