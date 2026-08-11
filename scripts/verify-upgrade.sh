#!/usr/bin/env bash
# Boot this build against a copy of real production data before deploying it.
#
# Why this exists: the test suite builds fresh schemas, and a fresh schema
# cannot show an upgrade failure. create_all runs before the migration list and
# skips tables that already exist, so an established database keeps its old
# constraints while new tables that reference them are still created. That
# combination took the live install down while every test passed.
#
# Usage:  scripts/verify-upgrade.sh path/to/production-backup.sql
#
# Restores the backup into a THROWAWAY database, runs the app's own startup
# against it, and reports what the migrations actually did. Never touches
# production, and never touches a database whose name does not contain "test".
set -uo pipefail

BACKUP="${1:-}"
CONTAINER="${PG_CONTAINER:-pokecollector-upgrade-check}"
PORT="${PG_PORT:-55450}"
IMAGE="${PG_IMAGE:-postgres:18-alpine}"
DB="upgradetest"
PASSWORD="upgradetest"
URL="postgresql://postgres:${PASSWORD}@127.0.0.1:${PORT}/${DB}"

die() { echo "error: $*" >&2; exit 1; }

[ -n "$BACKUP" ] || die "usage: $0 <production-backup.sql>"
[ -f "$BACKUP" ] || die "no such backup: $BACKUP"
command -v docker >/dev/null || die "docker is required to run the throwaway database"

case "$DB" in *test*) ;; *) die "refusing to use a database not named as a test one" ;; esac

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  die "port $PORT is in use; set PG_PORT to a free one. Never free a port by removing whatever holds it."
fi

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> Starting a throwaway PostgreSQL on port $PORT"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
  -e POSTGRES_PASSWORD="$PASSWORD" -e POSTGRES_USER=postgres -e POSTGRES_DB="$DB" \
  -p "${PORT}:5432" "$IMAGE" >/dev/null || die "could not start the container"

until docker exec "$CONTAINER" pg_isready -U postgres -d "$DB" >/dev/null 2>&1; do
  sleep 1
done

echo "==> Restoring $BACKUP"
# The dump comes from a database with its own owner and role names, which will
# not exist here; those errors are expected and do not affect the schema.
docker exec -i -e PGPASSWORD="$PASSWORD" "$CONTAINER" \
  psql -U postgres -d "$DB" -v ON_ERROR_STOP=0 -q < "$BACKUP" >/dev/null 2>&1

before_tables=$(docker exec -e PGPASSWORD="$PASSWORD" "$CONTAINER" psql -U postgres -d "$DB" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
echo "    restored $before_tables tables"
[ "$before_tables" -gt 0 ] || die "restore produced no tables; is this a full pg_dump?"

echo "==> Running the application's own startup against it"
cd "$(dirname "$0")/../backend" || die "cannot find backend/"
PYTHON="${PYTHON:-../.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON=python3

DATABASE_URL="$URL" "$PYTHON" - <<'PY'
import sys, traceback
sys.path.insert(0, ".")
try:
    import database
    database.init_db()
    print("    startup completed")
except Exception:
    traceback.print_exc()
    print("\n    STARTUP FAILED - this build would not boot against production data")
    sys.exit(1)
PY
startup_rc=$?

echo "==> Schema after startup"
docker exec -e PGPASSWORD="$PASSWORD" "$CONTAINER" psql -U postgres -d "$DB" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" \
  | sed 's/^/    tables: /'

echo "==> Second startup, to prove it is repeatable"
DATABASE_URL="$URL" "$PYTHON" - <<'PY'
import sys, traceback
sys.path.insert(0, ".")
try:
    import database
    database.init_db()
    print("    repeat startup completed")
except Exception:
    traceback.print_exc()
    print("\n    REPEAT STARTUP FAILED - the upgrade is not idempotent")
    sys.exit(1)
PY
repeat_rc=$?

echo
if [ "$startup_rc" -eq 0 ] && [ "$repeat_rc" -eq 0 ]; then
  echo "PASS: this build starts against production data, twice."
  exit 0
fi
echo "FAIL: do not deploy this build."
exit 1
