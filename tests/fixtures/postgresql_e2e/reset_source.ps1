# PostgreSQL E2E Audit Fixture — Reset Script (PowerShell)
# Resets migration_source by dropping all fixture objects and re-running scripts.
# Requires: psql in PATH, PostgreSQL running at 127.0.0.1:55432

param(
    [string]$PgHost = "127.0.0.1",
    [int]   $Port = 55432,
    [string]$User = "postgres",
    [string]$Db   = "migration_source",
    [switch]$SkipRole
)

$ErrorActionPreference = "Stop"
$env:PGPASSWORD = "postgres"

$scripts = @(
    "00_setup_role.sql",
    "01_drop_objects.sql",
    "02_create_types.sql",
    "03_public_tables.sql",
    "04_audit_test_tables.sql",
    "05_seed_data.sql",
    "06_create_indexes.sql",
    "07_create_views.sql",
    "08_create_functions.sql",
    "09_create_triggers.sql",
    "10_apply_comments.sql",
    "11_apply_grants.sql",
    "12_apply_rls.sql"
)

$base = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $SkipRole) {
    Write-Host "[reset] Setting up audit_user role..."
    psql -h $PgHost -p $Port -U $User -d $Db -f "$base\00_setup_role.sql" | Out-Null
}

Write-Host "[reset] Dropping existing objects..."
psql -h $PgHost -p $Port -U $User -d $Db -f "$base\01_drop_objects.sql" | Out-Null

foreach ($script in $scripts) {
    if ($script -eq "00_setup_role.sql") { continue }
    Write-Host "[reset] Running $script..."
    psql -h $PgHost -p $Port -U $User -d $Db -f "$base\$script" | Out-Null
}

Write-Host "[reset] Refreshing materialized views..."
psql -h $PgHost -p $Port -U $User -d $Db -c "REFRESH MATERIALIZED VIEW public.customer_balance_summary;" | Out-Null
psql -h $PgHost -p $Port -U $User -d $Db -c "REFRESH MATERIALIZED VIEW audit_test.audit_test_customer_summary;" | Out-Null

Write-Host "[reset] Done. Source database '$Db' is ready for migration."
