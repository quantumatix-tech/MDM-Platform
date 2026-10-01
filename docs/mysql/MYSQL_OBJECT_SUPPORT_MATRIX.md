# MySQL Object Support Matrix

Scope: what the modular MySQL connector (`core/connectors/mysql/`) actually implements
and what the unit suite actually exercises. This document deliberately lists only
implemented behaviour. Features that exist in code but are not yet wired into the
orchestrator are called out explicitly under **Implemented but not orchestrated**.

## Supported

Reported as `supported: true` by `MySQLSourceConnector.get_capabilities()` /
`MySQLTargetConnector.get_capabilities()`.

| Object / feature | Source discovery | Target creation | Notes |
|---|---|---|---|
| Tables | yes | yes | `CREATE TABLE` with mapped column types |
| Columns | yes | yes | incl. nullability, size, defaults, comments |
| Column auto-increment | yes | yes | presence round-tripped; see AUTO_INCREMENT below |
| Generated columns | yes | yes | `GENERATED ALWAYS AS (...)` with `VIRTUAL`/`STORED` mode |
| Primary keys | yes | yes | via `INFORMATION_SCHEMA.KEY_COLUMN_USAGE` |
| Indexes | yes | yes | incl. `FULLTEXT` and `SPATIAL` index types |
| Unique constraints | yes | yes | |
| Check constraints | yes | yes | MySQL `CHECK_CLAUSE` unescaping handled |
| Foreign keys | yes | yes | applied after data load; children ordered before parents |
| Inline partitions | yes | yes | partition method, expression and ordered partition list verified |
| Views | yes | yes | |
| Functions | yes | yes | `DEFINER` rewritten to the target definer |
| Stored procedures | yes | yes | `DEFINER` rewritten to the target definer |
| Triggers | yes | yes | suspended around data load, recreated after |
| Events | yes | yes | MySQL/MariaDB scheduled events; one-time events near their due time are blocked rather than silently re-timed |
| Table and column comments | yes | yes | applied via `ALTER TABLE` |
| Grants | yes | yes | direct privileges for selected accounts |
| Users | yes | yes | created ahead of object DDL when referenced by a `DEFINER` |
| Binlog CDC | yes | n/a | requires `server_id` and binlog configuration |

## Not supported

Reported as `supported: false`, with the connector's own reason.

| Object / feature | Reason |
|---|---|
| Materialized views | MySQL has no native materialized views |
| Row-level security | MySQL has no row-level security policies |
| Extensions | MySQL has no PostgreSQL extension model |
| Custom types | MySQL has no PostgreSQL domain/type model |
| Standalone sequences | `AUTO_INCREMENT` is table-bound |
| Schemas | MySQL databases are namespaces, not PostgreSQL schemas |
| Security principals | MySQL exposes accounts and grants, not a separate principal object model |
| Database roles | MySQL has no database-role object model; the shared role path is not driven for a MySQL source |

## AUTO_INCREMENT

MySQL `AUTO_INCREMENT` is table-bound, so it is not modelled as a standalone
sequence object. Two independent mechanisms exist:

1. **DDL round-trip** — source `INFORMATION_SCHEMA.COLUMNS.EXTRA` is captured and
   re-emitted in the target `CREATE TABLE`, so a migrated table keeps the attribute.
2. **Counter synchronisation** — after explicit source IDs are loaded, the counter
   is advanced to `max(column) + 1` via the `auto_increment` orchestration phase.

## DEFINER handling

Migrated routines, triggers, and events carry a `DEFINER` clause. The connector
rewrites it to the target definer account. Configure it with
`target.connection.routine_definer` (`user@host`), or set
`target.connection.preserve_source_definer: true` to keep the source account —
this applies only when that account exists on the target.

Referenced source accounts are created **before** any object DDL in the
`security_users_pre_objects` phase, so objects never fail with an orphan
`DEFINER` reference.

## Full-mode data semantics

Full mode is replacement synchronisation, not an incremental upsert:

- Target triggers are suspended before the load and recreated afterwards
  (`trigger_data_load_handling`).
- Every migrated target table is cleared before loading, so target-only rows —
  including rows from past trigger side effects — cannot survive a successful run
  (`full_target_sync`). `TRUNCATE` is deliberately avoided because it is
  incompatible with foreign-key-referenced tables; foreign-key checks are
  disabled only for the duration of the delete and always restored.

## Implemented but not orchestrated

These are implemented in the connector and covered by unit tests, but the
orchestrator does not yet drive them. Do not rely on them without verifying the
wiring first.

| Feature | Status |
|---|---|
| Staged target schema reconciliation (`reconcile_mysql_table`) | Implemented, not wired; no config key exposed |
| Reconciliation backup cleanup (`finalize_schema_reconciliations`) | Implemented, not wired |
| Scheduled-event snapshot phase | Implemented, not wired as a separate phase |

## Configuration

`config/mysql_local_test.yaml` is a runnable local-to-local example. MySQL-specific
keys are declared under `target.connection`:

| Key | Default | Purpose |
|---|---|---|
| `routine_definer` | unset | `user@host` DEFINER override for migrated objects |
| `preserve_source_definer` | `false` | keep the source `DEFINER` account instead of rewriting |
| `security_users` | unset | account allowlist; global direct permissions are discovered only for listed users |

Secrets are never stored in the config file. Each account references a secret name
via `password_secret`, resolved through the configured `secrets.provider`.
