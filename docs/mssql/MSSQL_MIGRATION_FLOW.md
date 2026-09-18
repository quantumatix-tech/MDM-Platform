# MSSQL E2E Migration Flow

Complete end-to-end migration flow, verified directions, and known issues for MSSQL object migration.

---

## Part 1 — Conceptual Migration Flow

This flow describes what the migration platform does, in what order, and how verification works at the end.

```
SOURCE DATABASE
    ↓
Connect to source
    ↓
Discover MSSQL objects
    ↓
Extract object definitions / metadata
    ↓
Create target objects
    ↓
Migrate data (UPSERT / MERGE)
    ↓
Apply constraints / indexes / FKs
    ↓
Apply views
    ↓
Apply functions / procedures
    ↓
Apply synonyms
    ↓
Apply triggers
    ↓
Apply comments / extended properties
    ↓
Apply users / roles / permissions
    ↓
Validate target
    ↓
E2E SUCCESS
```

### Object and dependency ordering

The migration engine creates objects in dependency order to avoid foreign key or reference failures:

1. **Schemas** — Created first so all subsequent objects have a valid namespace.
2. **UDTs / Custom types** — Created before tables that reference them.
3. **Tables** — Created with all constraints, identity columns, computed columns.
4. **Data** — Upserted into tables after table structures exist (using `MERGE` / UPSERT).
5. **Indexes** — Created after tables and data (clustered, non-clustered).
6. **Constraints** — Primary keys, foreign keys, check constraints applied after tables exist.
7. **Views** — Created after underlying tables exist.
8. **Functions** — Created after tables exist (functions may reference tables).
9. **Procedures** — Created after tables exist (procedures may reference tables).
10. **Synonyms** — Created after referenced objects exist.
11. **Triggers** — Created after tables, functions, and procedures exist.
12. **Comments / Extended properties** — Applied after all objects exist.
13. **Users / Roles / Permissions** — Applied after all objects and roles exist.

If an object creation fails, the migration records the failure and continues (error isolation via `migration.stop_on_error`).

---

## Part 2 — E2E Migration Directions

Three complete E2E migration directions are tracked:

| Direction | Description | Config |
|---|---|---|
| **Local → Local** | Local MSSQL → local MSSQL | `config/mssql_local_test.yaml` |
| **Local → Cloud** | Local MSSQL → Azure SQL | `config/mssql_local_cloud.yaml` (to be created) |
| **Cloud → Local** | Azure SQL → local MSSQL | `config/mssql_cloud_to_local.yaml` (to be created) |

### A. Local → Local — COMPLETED

**Status:** COMPLETED / VALIDATED

**Verified result available in repository history:**

- Branch: `feature/mssql-objects`
- Engine: Microsoft SQL Server
- Source: `localhost,1533` / `mssql_migration_test` → Target: `localhost,1533` / `mssql_migration_target`
- Schemas: `sales`, `billing`
- Migration mode: `full`
- Tables migrated: 9
- Rows migrated: 28
- Failed: 0
- Success rate: 100%

**Objects verified:**

- Schemas (`sales`, `billing`)
- Tables with data (9 tables across 2 schemas)
- Primary keys, foreign keys (including cross-schema `billing.customer_addresses → sales.customers`)
- Identity columns
- Computed columns
- Sequences
- Indexes
- Views (1/1 validated)
- Functions / Procedures (2/2 validated)
- Sequences (1/1 validated)
- Triggers (2/2 validated, one disabled)
- Synonyms (3/3 validated)
- UDTs (1/1 validated)
- Partition function (1/1 validated)
- Users / Roles / Permissions
- Comments / Extended Properties

Full evidence in `MSSQL_LOCAL_AUDIT.md`.

**Dependency ordering tests:** 11/11 passed.
**Cross-schema / reference tests:** 6/6 passed.
**Error isolation tests:** 6/6 passed.
**Metadata validation tests:** 54/54 passed.
**Existing MSSQL DDL tests:** 108/108 passed.

### B. Local → Cloud — NOT YET VALIDATED

> **This direction has not been executed or validated yet.**
>
> Planned: Local MSSQL → Azure SQL Database / Azure SQL Managed Instance.
>
> When completed, populate this section with:
> - Run ID
> - Environment details (source version, Azure SQL tier/version)
> - Tables migrated, rows migrated, failed objects
> - Objects verified (roles, grants, SSL, Azure-specific considerations)
> - Implementation fixes required during testing
> - Full evidence file: `MSSQL_CLOUD_AUDIT.md`

Config placeholder: `config/mssql_local_cloud.yaml`

### C. Cloud → Local — NOT YET VALIDATED

> **This direction has not been executed or validated yet.**
>
> Planned: Azure SQL → Local MSSQL.
>
> When completed, populate this section with:
> - Run ID
> - Environment details (Azure SQL source version, local target version)
> - Tables migrated, rows migrated, failed objects
> - Objects verified
> - Full evidence file: `MSSQL_CLOUD_TO_LOCAL_AUDIT.md`

Config placeholder: `config/mssql_cloud_to_local.yaml`

---

## Part 3 — E2E Issues Found and Fixed

To be populated as issues are discovered and resolved during Local→Cloud and Cloud→Local testing.

*(No issues documented for Local→Local — see `MSSQL_LOCAL_AUDIT.md` for known/pre-existing mismatches.)*

---

## Part 4 — UPSERT / MERGE Behavior

MSSQL migration uses the `MERGE` statement for data upserting. Key points:

- The target table must have a primary key or unique constraint for conflict resolution.
- `MERGE` matches source rows against target rows by primary key.
- Matching rows are updated; non-matching source rows are inserted.
- For `full` mode, the initial load is equivalent to an insert of all source rows.
- For `cdc-incremental` and `cdc-continuous` modes, `MERGE` applies individual change events.

See `MSSQL_OBJECT_SUPPORT_MATRIX.md` for object-level UPSERT validation status.

---

## Part 5 — Object Discovery Summary

The source connector discovers MSSQL objects through catalog queries in this order:

1. **Schemas** — `sys.schemas` (excluding system schemas)
2. **Tables** — `information_schema.tables` / `sys.tables`
3. **Columns** — `information_schema.columns` / `sys.columns`
4. **Constraints** — `information_schema.table_constraints` / `sys.key_constraints` / `sys.foreign_keys`
5. **Foreign keys** — `sys.foreign_keys` + `sys.foreign_key_columns`
6. **Indexes** — `sys.indexes` / `sys.index_columns`
7. **Identity columns** — `sys.identity_columns`
8. **Computed columns** — `sys.computed_columns`
9. **Sequences** — `sys.sequences`
10. **Views** — `information_schema.views` / `sys.views`
11. **Functions** — `sys.objects` (`FN`, `IF`, `TF`)
12. **Procedures** — `sys.objects` (`P`)
13. **Triggers** — `sys.triggers`
14. **Synonyms** — `sys.synonyms`
15. **UDTs** — `sys.types` / `sys.user_types`
16. **Partitioning** — `sys.partition_functions` / `sys.partition_schemes`
17. **Extended properties** — `sys.extended_properties`
18. **Users / Roles** — `sys.database_principals` / `sys.server_principals`
19. **Permissions** — `sys.database_permissions`

---

## Reference

- Object support matrix: `docs/mssql/MSSQL_OBJECT_SUPPORT_MATRIX.md`
- Limitations: `docs/mssql/MSSQL_LIMITATIONS.md`
- Local audit report: `docs/mssql/MSSQL_LOCAL_AUDIT.md`
- E2E runbook: `docs/mssql/MSSQL_E2E_RUNBOOK.md`
- Test guide: `docs/mssql/MSSQL_TEST_GUIDE.md`
- Local E2E config: `config/mssql_local_test.yaml`
