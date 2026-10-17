#!/bin/bash
# Terminal 4 -- the migration program. Examples:
#   ./run_migration.sh                      # migrate (default beta 0.5), continues from saved state
#   ./run_migration.sh migrate --beta 1.0   # migrate everything
#   ./run_migration.sh migrate --switches s3,s5
#   ./run_migration.sh status
#   ./run_migration.sh rollback s3          # or: rollback --all
#   ./run_migration.sh rebaseline
#   ./run_migration.sh reset                # back to a clean all-Legacy network
source "$(dirname "$0")/qsmo_env.sh"
case "${1:-migrate}" in
  migrate|-*) ensure_hybrid_certs || exit 1 ;;     # a migration needs the Hybrid certs of every switch
esac
exec sudo env "$PYENV" "$PY" run_migration.py "$@"
