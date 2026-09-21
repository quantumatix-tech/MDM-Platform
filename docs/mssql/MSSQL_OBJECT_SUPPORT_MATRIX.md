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
| **Cloud → Local** | Not yet validated |

---

## Databases / Schemas

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| `CREATE DATABASE` | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | `ensure_database_exists` creates target database if missing |
| `CREATE SCHEMA` | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | Schemas `sales`, `billing` created and populated |

---

## Tables / Columns

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| `CREATE TABLE` | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | Tables created across `sales`, `billing` schemas |
| `DROP/CREATE IF NOT EXISTS` | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | `create_object_if_missing` uses `IF NOT EXISTS` |
| Columns (all types) | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | Includes INT, VARCHAR, NVARCHAR, DECIMAL, DATETIME, BIT, UNIQUEIDENTIFIER, VARBINARY, SQL_VARIANT |
| Primary key | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | IDENTITY-based PKs on all tables |
| Foreign key | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | Cross-schema `billing.customer_addresses → sales.customers` verified |
| Cross-schema FK | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | Verified through `sys.foreign_keys` |
| UNIQUE constraint | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| Check constraint | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| Default constraints | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| Identity columns | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | IDENTITY insert/seed verified |
| Computed columns | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |

---

## Table data

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| Row data migration | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | 28 rows across 9 tables (FULL mode with MERGE/UPSERT) |
| Data type preservation | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | INT, VARCHAR, NVARCHAR, DECIMAL, DATETIME, BIT, UNIQUEIDENTIFIER, VARBINARY, SQL_VARIANT |

---

## Sequences

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| `CREATE SEQUENCE` | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| Sequence properties | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | START WITH, INCREMENT BY, MINVALUE, MAXVALUE |
| Sequence ownership | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |

---

## Indexes

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| Clustered index | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| Non-clustered index | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| Unique index | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| Index properties | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | Fill factor, padding, included columns |

---

## Views

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| Standard view | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | 1 view in `sales` schema, validated |
| `CREATE OR REPLACE VIEW` | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| Schema-qualified view | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |

---

## Functions / Procedures

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| Scalar function | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | 2 functions/procedures validated |
| Table-valued function | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| Stored procedure | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | 2 functions/procedures validated |
| Schema-qualified function | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| Schema-qualified procedure | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |

---

## Triggers

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| `CREATE TRIGGER` | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | 2 triggers validated |
| AFTER trigger | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| Trigger disabled state | Partial | Local → Local | Local → Cloud | Not yet validated | 1 of 2 triggers is disabled; disabled state is preserved |
| Schema-qualified trigger | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |

---

## Synonyms

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| `CREATE SYNONYM` | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | 3 synonyms validated |
| Schema-qualified synonym | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |

---

## UDTs / Specialized data types

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| XML | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | 1 UDT validated |
| JSON | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| VARBINARY / BINARY | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| UNIQUEIDENTIFIER | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| SQL_VARIANT | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |

---

## Partitioning

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| Partition function | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | 1 partition function validated |
| Partition scheme | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| Partitioned table | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |

---

## Users / Roles / Permissions

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| Database role creation | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | Security objects matched |
| User creation | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| Schema-level permissions | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| Table-level permissions | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| Statement-level permissions | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |

---

## Comments / Extended properties

| Object | Support | Local → Local | Local → Cloud | Cloud → Local | Notes / Limitations |
|---|---|---|---|---|---|
| `sp_addextendedproperty` on schema | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| `sp_addextendedproperty` on table | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| `sp_addextendedproperty` on column | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| `sp_addextendedproperty` on function | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |
| `sp_addextendedproperty` on procedure | Supported / Verified | Local → Local | Local → Cloud | Not yet validated | — |

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