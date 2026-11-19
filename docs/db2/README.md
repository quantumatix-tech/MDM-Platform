# Db2 Migration Documentation

## Start Here

1. **New to Db2 migration?** Read [DB2_SETUP_AND_MIGRATION_GUIDE.md](DB2_SETUP_AND_MIGRATION_GUIDE.md)
2. **What objects are supported?** See [DB2_OBJECT_SUPPORT_MATRIX.md](DB2_OBJECT_SUPPORT_MATRIX.md)
3. **Technical details and evidence?** See [DB2_TECHNICAL_REFERENCE.md](DB2_TECHNICAL_REFERENCE.md)

## Quick Start

```bash
# 1. Copy config template
cp config/db2.example.yaml config/db2_client.yaml

# 2. Set secrets (PowerShell)
$env:SECRET_db2_source_pass = "your_source_password"
$env:SECRET_db2_target_pass = "your_target_password"

# 3. Optional: security executor for role self-grants (avoids SQL0554N)
$env:SECRET_db2_admin_pass = "your_admin_password"

# 4. Run preflight (read-only)
python scripts/db2/verify_db2_e2e_preflight.py --config config/db2_client.yaml

# 5. Run FULL migration
python -m migration_platform --config config/db2_client.yaml --mode full --no-live-ui

# 6. Run acceptance verification
python scripts/db2/verify_db2_acceptance.py --config config/db2_client.yaml
```

## Document Index

| Document | Purpose |
|----------|---------|
| [DB2_SETUP_AND_MIGRATION_GUIDE.md](DB2_SETUP_AND_MIGRATION_GUIDE.md) | Complete client onboarding and migration guide |
| [DB2_OBJECT_SUPPORT_MATRIX.md](DB2_OBJECT_SUPPORT_MATRIX.md) | Supported objects, evidence and client actions |
| [DB2_TECHNICAL_REFERENCE.md](DB2_TECHNICAL_REFERENCE.md) | Tested versions, architecture, technical notes, E2E evidence |

## Configuration

| Config File | Status | Purpose |
|-------------|--------|---------|
| `config/db2.example.yaml` | **Tracked** | Reusable public example template |
| `config/db2_objects_objtg08.yaml` | **Tracked** | Successful local acceptance configuration (OBJSC01→OBJTG08) |
| `config/db2_objects_template.yaml` | **Tracked** | Reusable Db2 template with security executor |
| `config/db2_client.yaml` | Local-only | Copy from `db2.example.yaml`; never commit |
| `config/db2_e2e*.yaml` | Local-only | E2E test configs; gitignored |
| `config/db2_test*.yaml` | Local-only | Test configs; gitignored |
| `config/db2_connection_test.yaml` | Local-only | Connection test config; gitignored |

> All `db2_objects_objtg*.yaml` configs except `objtg08` are obsolete local-only test configs and are **not tracked**. `config/db2_client.yaml` is never committed (gitignored).

## Verification Scripts

- `scripts/db2/verify_db2_acceptance.py` — Full acceptance verification (read-only catalog comparison)
- `scripts/db2/verify_db2_e2e_preflight.py` — Read-only preflight check
- `scripts/db2_e2e/` — Source fixture SQL and target setup scripts

## Test Status

- Unit tests: **1269 passed, 0 failed** (includes Db2 role transaction tests)
- Ruff: clean on all changed files
- Live E2E acceptance: OBJSC01 → OBJTG08, 1 table, 2/2 rows, 100% success (run `33f9fadbeeb149bab34dc13980d0613c`)
