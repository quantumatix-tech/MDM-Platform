# MSSQL Object Support Matrix

Classification of MSSQL object types based on the completed Local → Local E2E
validation on the `feature/mssql-objects` branch.

## Classification keys

| Class | Meaning |
|---|---|
| **Supported / Verified** | Object migrates end-to-end in real MSSQL E2E tests |
| **Partial** | Migrates with known constraints |
| **Out of Scope** | Intentionally not part of current implementation |
| **Not yet validated** | Direction has not been executed |

## E2E Validation Status Key

| Status | Meaning |
|---|---|
| **Local → Local** | Verified in Local → Local E2E |
| **Local → Cloud** | Verified in Local → Cloud E2E |
| **Cloud → Local** | Verified in Cloud → Local E2E |

---

## Databases / Schemas

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| `CREATE DATABASE` | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | `ensure_database_exists` creates target database if missing |
| `CREATE SCHEMA` | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | Schemas `sales`, `billing` created and populated |

---

## Tables / Columns

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| `CREATE TABLE` | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | Tables created across `sales`, `billing` schemas |
| `DROP/CREATE IF NOT EXISTS` | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | `create_object_if_missing` uses `IF NOT EXISTS` |
| Columns (all types) | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | Includes INT, VARCHAR, NVARCHAR, DECIMAL, DATETIME, BIT, UNIQUEIDENTIFIER, VARBINARY, SQL_VARIANT |
| Primary key | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | IDENTITY-based PKs on all tables |
| Foreign key | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | Cross-schema `billing.customer_addresses → sales.customers` verified |
| Cross-schema FK | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | Verified through `sys.foreign_keys` |
| UNIQUE constraint | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| Check constraint | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| Default constraints | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| Identity columns | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | IDENTITY insert/seed verified |
| Computed columns | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |

---

## Table data

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| Row data migration | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | 28 rows (L→C) / 40 rows (C→L) across 9 tables (FULL mode with MERGE/UPSERT) |
| Data type preservation | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | INT, VARCHAR, NVARCHAR, DECIMAL, DATETIME, BIT, UNIQUEIDENTIFIER, VARBINARY, SQL_VARIANT |

---

## Sequences

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| `CREATE SEQUENCE` | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| Sequence properties | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | START WITH, INCREMENT BY, MINVALUE, MAXVALUE |
| Sequence ownership | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |

---

## Indexes

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| Clustered index | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| Non-clustered index | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| Unique index | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| Index properties | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | Fill factor, padding, included columns |

---

## Views

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| Standard view | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | 1 view in `sales` schema, validated (L→C) / view batch fix validated (C→L) |
| `CREATE OR REPLACE VIEW` | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | `CREATE OR ALTER VIEW` with batch separation |
| Schema-qualified view | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |

---

## Functions / Procedures

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| Scalar function | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | 2 functions/procedures validated |
| Table-valued function | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| Stored procedure | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | 2 functions/procedures validated |
| Schema-qualified function | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| Schema-qualified procedure | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |

---

## Triggers

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| `CREATE TRIGGER` | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | 2 triggers validated |
| AFTER trigger | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| Trigger disabled state | Partial | Local → Local | Local → Cloud | Cloud → Local | 1 of 2 triggers is disabled; disabled state is preserved |
| Schema-qualified trigger | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |

---

## Synonyms

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| `CREATE SYNONYM` | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | 3 synonyms validated |
| Schema-qualified synonym | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |

---

## UDTs / Specialized data types

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| XML | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | 1 UDT validated |
| JSON | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| VARBINARY / BINARY | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| UNIQUEIDENTIFIER | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| SQL_VARIANT | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |

---

## Partitioning

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| Partition function | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | 1 partition function validated |
| Partition scheme | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| Partitioned table | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | Step 16 fix validated both directions |

---

## Users / Roles / Permissions

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| Database role creation | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | Security objects matched |
| User creation | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| Schema-level permissions | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| Table-level permissions | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| Statement-level permissions | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |

---

## Comments / Extended properties

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| `sp_addextendedproperty` on schema | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| `sp_addextendedproperty` on table | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| `sp_addextendedproperty` on column | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| `sp_addextendedproperty` on function | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |
| `sp_addextendedproperty` on procedure | Supported / Verified | Local → Local | Local → Cloud | Cloud → Local | — |

---

## Cross-schema references

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| Cross-schema FK | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | `billing.customer_addresses → sales.customers`; FK discovered, recreated, and verified through `sys.foreign_keys`. Foreign key enforcement tested with invalid insert (customer_id=999999 rejected). |
| Cross-schema object references | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |

---

## Dependency ordering

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| Schema → Table ordering | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | 11/11 dependency ordering tests passed |
| UDT → Table ordering | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| Table → Constraint ordering | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| Table → Data ordering | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| Table → View ordering | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |

---

## Error isolation

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| Per-object failure isolation | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | 6/6 error isolation tests passed |
| `stop_on_error` flag | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | Default `false`; continues eligible objects |
| Negative testing | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | 6/6 negative tests passed |

---

## Metadata validation

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| Object existence verification | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| Row count verification | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | See regression counts in `MSSQL_LOCAL_AUDIT.md` |
| Constraint verification | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | PK, UK, FK verified |
| Index verification | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | Source/target indexes matched |
| Metadata comparison | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | 54/54 metadata validation tests passed |

---

## Partitioned table data

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| Partitioned table + data | Partial | Local → Local | Local → Cloud | Not yet validated | Source/target row-count mismatch in `sales.sales_partitioned` — pre-existing target-state difference (see `MSSQL_LIMITATIONS.md`) |

---

## Reference

- Local audit report: `docs/mssql/MSSQL_LOCAL_AUDIT.md`
- Limitations: `docs/mssql/MSSQL_LIMITATIONS.md`
- Migration flow: `docs/mssql/MSSQL_MIGRATION_FLOW.md`
- E2E runbook: `docs/mssql/MSSQL_E2E_RUNBOOK.md`
- Test guide: `docs/mssql/MSSQL_TEST_GUIDE.md`