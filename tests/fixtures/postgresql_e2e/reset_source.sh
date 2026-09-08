#!/usr/bin/env bash
# PostgreSQL E2E Audit Fixture — Reset Script (Bash)
# Resets migration_source by dropping all fixture objects and re-running scripts.
# Requires: psql in PATH, PostgreSQL running at 127.0.0.1:55432

set -euo pipefail

HOST="${1:-127.0.0.1}"
PORT="${2:-55432}"
USER="${3:-postgres}"
DB="${4:-migration_source}"
SKIP_ROLE="${5:-false}"

export PGPASSWORD=postgres
BASE="$(cd "$(dirname "$0")" && pwd)"

SCRIPTS=(
    "00_setup_role.sql"
    "01_drop_objects.sql"
    "02_create_types.sql"
    "03_public_tables.sql"
    "04_audit_test_tables.sql"
    "05_seed_data.sql"
    "06_create_indexes.sql"
    "07_create_views.sql"
    "08_create_functions.sql"
    "09_create_triggers.sql"
    "10_apply_comments.sql"
    "11_apply_grants.sql"
    "12_apply_rls.sql"
)

if [ "$SKIP_ROLE" != "true" ]; then
    echo "[reset] Setting up audit_user role..."
    psql -h "$HOST" -p "$PORT" -U "$USER" -d "$DB" -f "$BASE/00_setup_role.sql" >/dev/null
fi

echo "[reset] Dropping existing objects..."
psql -h "$HOST" -p "$PORT" -U "$USER" -d "$DB" -f "$BASE/01_drop_objects.sql" >/dev/null

for script in "${SCRIPTS[@]}"; do
    if [ "$script" = "00_setup_role.sql" ]; then
        continue
    fi
    echo "[reset] Running $script..."
    psql -h "$HOST" -p "$PORT" -U "$USER" -d "$DB" -f "$BASE/$script" >/dev/null
done

echo "[reset] Refreshing materialized views..."
psql -h "$HOST" -p "$PORT" -U "$USER" -d "$DB" -c "REFRESH MATERIALIZED VIEW public.customer_balance_summary;" >/dev/null
psql -h "$HOST" -p "$PORT" -U "$USER" -d "$DB" -c "REFRESH MATERIALIZED VIEW audit_test.audit_test_customer_summary;" >/dev/null

echo "[reset] Done. Source database '$DB' is ready for migration."
