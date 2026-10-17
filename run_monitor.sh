#!/bin/bash
# Terminal 3 -- the always-on monitor. Start it after ./run_controller.sh. Ctrl+C stops only the
# monitor; Hybrid switches stay Hybrid and their tunnels keep running.
source "$(dirname "$0")/qsmo_env.sh"
exec sudo env "$PYENV" "$PY" run_monitor.py "$@"
