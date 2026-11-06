#!/bin/bash
# Rebuilds the local pulse_dev database from the migrations and sample data. Only ever touches this Mac's Postgres.
set -euo pipefail
cd "$(dirname "$0")/.."
unset PGHOST PGHOSTADDR PGPORT PGDATABASE PGUSER PGPASSWORD PGSERVICE PGSERVICEFILE # so these can't point it at a real database
PSQL=(psql -X -q -v ON_ERROR_STOP=1)
dropdb --if-exists --force pulse_dev # --force, because a running npm run dev holds connections
"${PSQL[@]}" -d postgres -c 'DROP ROLE IF EXISTS tracker_app'
createdb pulse_dev
for migration in migrations/*.sql; do "${PSQL[@]}" -d pulse_dev -f "$migration"; done
[ "${1:-}" = "--empty" ] || "${PSQL[@]}" -d pulse_dev -f dev/seed.sql
"${PSQL[@]}" -d postgres -c "ALTER ROLE tracker_app LOGIN PASSWORD 'local'"
[ "${1:-}" = "--empty" ] || echo "pulse_dev is ready. Run: npm run dev"
