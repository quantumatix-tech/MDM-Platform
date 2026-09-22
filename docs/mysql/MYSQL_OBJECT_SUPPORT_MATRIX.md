# MySQL Object Support Matrix

Latest Local → Local evidence is based on the completed MySQL migration and
supporting object-level validation runs. This matrix describes actual DMS
platform support and verification status; MySQL engine capability alone does
not imply that the DMS supports the object.

## Classification keys

| Class | Meaning |
|---|---|
| **Supported / Verified** | Object migrates end-to-end and was verified on the target |
| **Partial** | Object is supported with documented constraints or environment dependencies |
| **Out of Scope** | Object is intentionally not supported by the current DMS implementation |
| **Environment Blocked** | Implementation exists, but live verification is blocked by environment, privileges, or server policy |
| **N/A** | MySQL has no native equivalent or the concept does not apply directly |

## E2E Validation Status Key

| Status | Meaning |
|---|---|
| **Local → Local** | Verified in MySQL Local → Local migration |
| **Unit tested** | Implementation behavior verified through automated tests, but not necessarily through a live E2E run |
| **Environment Blocked** | Verification depends on server privilege/configuration that is unavailable |
| **Not E2E tested** | Implementation exists or is understood, but no live E2E evidence is recorded |
| **N/A** | Native MySQL equivalent does not exist |

---

## Databases / Schema Mapping

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| MySQL database | **Supported / Verified** | Local → Local | Source and target databases are explicitly mapped by migration configuration |
| PostgreSQL-style schema | **N/A** | N/A | MySQL databases are the closest namespace concept; PostgreSQL schema semantics are not reproduced as a separate MySQL object |
| Cross-database object references | **Supported / Verified** | Local → Local | MySQL database-qualified references are handled where the referenced object is part of the migration set |
| External/unmanaged database references | **Partial** | Not exhaustively E2E tested | References to objects outside the managed migration scope are not rewritten into unrelated target objects |

---

## Tables

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| `CREATE TABLE` | **Supported / Verified** | Local → Local | Tables created and populated successfully |
| Existing target table | **Supported / Verified** | Local → Local | Existing compatible structures can be reused |
| Target schema reconciliation | **Supported / Verified** | Local → Local | Controlled by `migration.reconcile_target_schema`; incompatible partitioned structures are replaced using source-derived DDL |
| `IF NOT EXISTS` behavior | **Supported / Verified** | Unit / Local → Local | Object creation handles existing objects without uncontrolled duplicate creation |
| Table columns | **Supported / Verified** | Local → Local | Column metadata preserved |
| `NOT NULL` | **Supported / Verified** | Local → Local | Nullability preserved |
| Primary key | **Supported / Verified** | Local → Local | Primary keys verified on target |
| Foreign key | **Supported / Verified** | Local → Local | FK metadata and rejection behavior verified |
| Cross-database foreign key | **Supported / Verified** | Local → Local | MySQL cross-database FK enforcement verified with valid and invalid inserts |
| UNIQUE constraint | **Supported / Verified** | Local → Local | Duplicate-value rejection verified |
| CHECK constraint | **Supported / Verified** | Local → Local | Invalid values rejected by target |
| Defaults | **Supported / Verified** | Local → Local | Default expressions/values preserved and verified |
| `AUTO_INCREMENT` | **Supported / Verified** | Local → Local | Counter synchronization performed after data load |
| Generated columns | **Supported / Verified** | Local → Local | Generated expression and stored/generated behavior preserved |
| Table comments | **Supported / Verified** | Local → Local | Source table comments emitted and verified |
| Column comments | **Supported / Verified** | Local → Local | Source column comments emitted and verified |

---

## Table Data

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Row data migration | **Supported / Verified** | Local → Local | Latest successful run migrated 44/44 rows |
| Row count validation | **Supported / Verified** | Local → Local | Source and target row counts compared |
| Data value validation | **Supported / Verified** | Local → Local | Representative source/target values verified |
| Binary value preservation | **Supported / Verified** | Local → Local | HEX-based validation included in datatype testing |
| Full-mode target data clearing | **Supported / Verified** | Local → Local | Existing migrated target tables are cleared deterministically before reload |
| Parent-before-child data loading | **Supported / Verified** | Local → Local | Topological ordering allows FK enforcement to remain enabled |
| Object-level failure isolation | **Supported / Verified** | Unit / Local → Local | Independent object failures are recorded without unnecessarily stopping the full migration |

---

## Representative MySQL Data Types

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Integer types | **Supported / Verified** | Local → Local | `TINYINT`, `SMALLINT`, `MEDIUMINT`, `INT`, `BIGINT` including unsigned representation tested |
| Fixed/precision numeric types | **Supported / Verified** | Local → Local | `DECIMAL` / `NUMERIC` tested |
| Floating-point types | **Supported / Verified** | Local → Local | `FLOAT`, `DOUBLE` tested |
| Character types | **Supported / Verified** | Local → Local | `CHAR`, `VARCHAR`, `TEXT`, `MEDIUMTEXT`, `LONGTEXT` tested |
| Binary types | **Supported / Verified** | Local → Local | `BINARY`, `VARBINARY`, `BLOB`, `MEDIUMBLOB`, `LONGBLOB` tested |
| Date/time types | **Supported / Verified** | Local → Local | `DATE`, `TIME`, `DATETIME(6)`, `TIMESTAMP(6)`, `YEAR` tested |
| Boolean aliases | **Supported / Verified** | Local → Local | `BOOLEAN` / `BOOL` tested |
| JSON | **Supported / Verified** | Local → Local | JSON type included in representative datatype fixture |
| ENUM | **Supported / Verified** | Local → Local | Included in representative 31-column datatype fixture |
| SET | **Supported / Verified** | Local → Local | SET values normalized to declared member order before target insert |
| Representative datatype fixture | **Supported / Verified** | Local → Local | 31-column fixture; source/target metadata and values verified |

---

## Indexes

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Secondary index | **Supported / Verified** | Local → Local | Index metadata verified |
| Unique index | **Supported / Verified** | Local → Local | Unique index creation and duplicate rejection verified |
| Composite index | **Supported / Verified** | Local → Local | Composite index metadata verified |
| Index ordering / columns | **Supported / Verified** | Local → Local | Target index definitions compared with source |
| Indexes after data load | **Supported / Verified** | Local → Local | Indexes created as part of post-load constraint/index phases |

---

## Views

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Standard view | **Supported / Verified** | Local → Local | Target view created and queried successfully |
| Schema/database-qualified view | **Supported / Verified** | Local → Local | MySQL database qualification handled |
| Source → target namespace rewrite | **Supported / Verified** | Local → Local | References to migrated source objects are rewritten to target-local database |
| View functional execution | **Supported / Verified** | Local → Local | Target view executed successfully against migrated target tables |
| External/unmanaged view dependency | **Partial** | Not exhaustively E2E tested | Only migrated source dependencies are rewritten; unmanaged external dependencies remain external |

---

## Procedures

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Stored procedure | **Supported / Verified** | Local → Local | Procedure DDL migrated |
| Procedure DDL extraction | **Supported / Verified** | Local → Local | Authoritative `SHOW CREATE` definition used |
| Procedure runtime execution | **Supported / Verified** | Local → Local | Procedure executed successfully on target |
| Procedure parameter handling | **Supported / Verified** | Local → Local | Tested procedure executed with expected input |
| Procedure grants | **Partial** | Unit tested / Environment dependent | `ROUTINE_PRIVILEGES` visibility and existing target grantee are required |

---

## Functions

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Stored function | **Supported / Verified** | Local → Local | Function migrated and executed successfully after server policy remediation |
| Function DDL extraction | **Supported / Verified** | Local → Local | Authoritative `SHOW CREATE` definition used |
| Function runtime execution | **Supported / Verified** | Local → Local | `fn_get_active_customer_count()` returned expected result |
| Function under binary logging policy | **Partial / Environment dependent** | Environment dependent | With `log_bin_trust_function_creators=OFF`, MySQL may reject creation with Error 1419 |
| Function `EXECUTE` grants | **Partial** | Unit tested / Environment dependent | Requires accessible `ROUTINE_PRIVILEGES` metadata and existing target grantee |

---

## Triggers

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| `CREATE TRIGGER` | **Supported / Verified** | Local → Local | Trigger migrated and attached to target table |
| Trigger runtime behavior | **Supported / Verified** | Local → Local | Target insert/update behavior verified |
| Trigger dependency on migrated table | **Supported / Verified** | Local → Local | Trigger created after required table/object dependencies |
| Trigger under binary logging policy | **Partial / Environment dependent** | Environment dependent | Error 1419 can block creation when `log_bin_trust_function_creators=OFF` |
| Trigger enabled/disabled state | **Partial** | Local → Local | Creation state is preserved as supported by MySQL DDL; broader state-management scenarios are not separately exhaustively tested |
| Trigger connection lifecycle | **Supported / Verified** | Local → Local | Source read connections use autocommit and cleanup to avoid stale/sleeping connection issues |

---

## Events

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| `CREATE EVENT` | **Supported / Verified** | Local → Local; Azure metadata evidence | Source Event DDL is snapshotted at migration start and created on target |
| Event definition | **Supported / Verified** | Local → Local; Azure recurring evidence | Authoritative `SHOW CREATE EVENT` DDL is replayed with target definer rewriting |
| Event status / recurring schedule | **Supported / Verified** | Azure run `474f0d5157784c4d9bdf8d25f35d9523` | `ENABLED`, `EVERY`, `STARTS`, `ENDS`, and `ON COMPLETION` were retained for `evt_recurring_event_test` |
| One-time event, safely future-dated | **Supported / Unit verified** | Live Azure re-test pending configured credentials | Enabled Events are created only when outside the configured safety window at both snapshot and creation |
| One-time event due or near due | **Supported safety behavior / Unit verified** | Live Azure re-test pending configured credentials | Reported as `EVENT: BLOCKED`; DMS does not drop or replace the target Event and does not claim preservation |
| Target-only stale Events during FULL | **Not reconciled by design** | Azure run `740ba5585c394826acd9257260c3332e` | FULL creates/replaces only Events present in the source snapshot; no managed-Event ownership registry exists, so target-only Events are retained |
| Event Scheduler dependency | **Partial / Environment dependent** | Local → Local | Runtime execution depends on MySQL Event Scheduler being enabled |
| Event definer/privilege dependency | **Partial / Environment dependent** | Not exhaustively tested | Execution depends on valid target definer and required privileges |
| Recurring event scheduling | **Partial** | Not exhaustively E2E tested | One-time event verified; broader recurring scheduler scenarios are not exhaustively covered |

---

## Partitions

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Partitioned table | **Supported / Verified** | Local → Local | Native MySQL table partitioning preserved |
| RANGE partition | **Supported / Verified** | Local → Local | Verified with `RANGE(YEAR(created_at))` |
| RANGE COLUMNS | **Supported / Verified** | Implementation supported | Emitted through partition DDL generation |
| LIST partition | **Supported / Verified** | Implementation supported | Emitted through partition DDL generation |
| LIST COLUMNS | **Supported / Verified** | Implementation supported | Emitted through partition DDL generation |
| HASH partition | **Supported / Verified** | Implementation supported | Emitted through partition DDL generation |
| KEY partition | **Supported / Verified** | Implementation supported | Emitted through partition DDL generation |
| Partition names/order | **Supported / Verified** | Local → Local | Partition metadata and ordering preserved |
| `MAXVALUE` boundary | **Supported / Verified** | Local → Local | `pmax` / `MAXVALUE` verified |
| Partition metadata reporting | **Supported / Verified** | Local → Local | Partitions reported as an object category; detailed structure retained in metadata/report data |
| Existing incompatible partition structure | **Supported / Verified** | Local → Local | Controlled reconciliation can replace incompatible target partition structure |
| Unmanaged inbound FK during reconciliation | **Partial** | Local → Local | Reconciliation is blocked clearly when an inbound FK references an unmanaged object outside the migration set |

---

## Comments / Metadata

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Table comments | **Supported / Verified** | Local → Local | Exact comments verified |
| Column comments | **Supported / Verified** | Local → Local | Exact comments verified |
| View comments | **Not separately verified** | Not E2E tested | No standalone live evidence recorded |
| Routine comments | **Not separately verified** | Not E2E tested | No standalone live evidence recorded |
| Index/constraint comments | **Not supported / not enumerated** | Not E2E tested | No dedicated extraction/application path recorded |

---

## Grants

> **Task 13 update:** allowlisted user/role creation, role edges, and scoped
> grants are supported. Local → Azure run `39c9933250be4f2daadacaf44ccdc45f`
> verified the three configured principals, two role edges, and target grants.
> No global privileges/passwords are migrated; Local → Local is not executed.

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Explicit table grants | **Partial** | Unit tested / live visibility blocked | Extracted from `INFORMATION_SCHEMA.TABLE_PRIVILEGES` and applied to mapped target database |
| Table `SELECT` grant | **Partial** | Unit tested | Supported when grant metadata is visible and target grantee exists |
| Table `INSERT` grant | **Partial** | Unit tested | Same metadata/grantee requirements |
| Other supported table privileges | **Partial** | Unit tested | Applied according to discovered source table privilege metadata |
| Routine `EXECUTE` grant | **Partial** | Unit tested / environment dependent | Extracted from `ROUTINE_PRIVILEGES` when accessible |
| Target grantee existence | **Required prerequisite** | Tested | DMS does not create the target account for these grant paths |
| User creation | **Out of Scope** | N/A | Account creation is not part of current DMS support |
| Role creation | **Out of Scope** | N/A | MySQL roles are not migrated |
| Role assignment | **Out of Scope** | N/A | Role membership/assignment is not migrated |
| Global privileges | **Out of Scope** | N/A | No global privilege extraction/application |
| Database-level privileges | **Out of Scope** | N/A | No database-level privilege extraction/application |
| Password/authentication attributes | **Out of Scope** | N/A | User authentication configuration is not migrated |
| Cross-account grant metadata visibility | **Environment Blocked** | Local least-privilege environment | `mysql_test` cannot see another account's grants under the tested privilege set |

---

## Validation / Reporting

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Object existence validation | **Supported / Verified** | Local → Local | Source/target objects compared |
| Row count validation | **Supported / Verified** | Local → Local | Source and target counts compared |
| Column metadata validation | **Supported / Verified** | Local → Local | Columns/types/nullability compared |
| Constraint validation | **Supported / Verified** | Local → Local | PK, UK, FK, CHECK metadata verified |
| Index validation | **Supported / Verified** | Local → Local | Index metadata verified |
| Partition validation | **Supported / Verified** | Local → Local | Partition metadata compared |
| Routine validation | **Supported / Verified** | Local → Local | Functions/procedures verified |
| Trigger validation | **Supported / Verified** | Local → Local | Trigger definitions/behavior verified |
| Event validation | **Supported / Verified** | Local → Local | Event metadata verified |
| Object-level failure reporting | **Supported / Verified** | Unit / Local → Local | Failures are recorded independently |
| Migration status reporting | **Supported / Verified** | Local → Local | Run status, rows, errors and object outcomes reported |
| `partial_success` status | **Supported / Verified** | Unit / Local → Local | Used when independent objects fail while migration continues |
| `stop_on_error` mode | **Supported / Verified** | Unit tested | Optional fail-fast behavior |

---

## Safety / Operational Controls

| Object | Support | E2E Validation | Evidence / Notes |
|---|---|---|---|
| Concurrent migration protection | **Supported / Verified** | Unit / Local → Local | Filesystem lock keyed by effective source/target scope |
| Source connection cleanup | **Supported / Verified** | Local → Local | Autocommit and rollback/close cleanup implemented |
| Source service restart protection | **Supported / Verified** | Unit tested | DMS does not restart source MySQL service as part of the PostgreSQL WAL-specific safety mechanism |
| MySQL function/trigger server-policy handling | **Supported / Verified** | Local → Local | DMS reports Error 1419 as an explicit BLOCKED outcome and does not attempt privileged server-variable changes |
| Deterministic port handling | **Supported / Verified** | Local environment | Fixed UI port behavior; occupied port produces a clear failure rather than random fallback |

---

## Unsupported / Native MySQL Limitations

| Object / Feature | Class | Evidence / Notes |
|---|---|---|
| Materialized views | **N/A** | MySQL has no native PostgreSQL-style materialized view feature |
| PostgreSQL schemas | **N/A** | MySQL database namespace semantics differ from PostgreSQL schemas |
| PostgreSQL extensions | **N/A** | No direct general-purpose MySQL equivalent |
| PostgreSQL domains | **N/A** | No direct equivalent implemented |
| PostgreSQL composite types | **N/A** | No direct equivalent implemented |
| PostgreSQL RLS / policies | **N/A** | MySQL implementation does not provide PostgreSQL RLS/policy objects |
| Standalone PostgreSQL sequences | **N/A** | MySQL normally uses `AUTO_INCREMENT`; standalone PostgreSQL sequence semantics are not directly reproduced |
| PostgreSQL-specific object semantics | **N/A** | PostgreSQL-only constructs are outside native MySQL object migration scope |

---

## CDC

| Object | Class | E2E Validation | Evidence / Notes |
|---|---|---|---|
| MySQL CDC | **Not E2E tested** | Not E2E tested in this Local → Local object-support audit | No completed MySQL CDC E2E evidence is recorded in this matrix; therefore it is not classified as Verified |
| Continuous migration / CDC mode | **Not E2E tested** | Not E2E tested | Do not interpret successful FULL migration as CDC verification |
| Incremental INSERT / UPDATE / DELETE replication | **Not E2E tested** | Not E2E tested | Requires dedicated CDC test evidence |

---

## Final Local → Local Evidence

| Metric | Result |
|---|---:|
| Latest successful migration run | `84e6100202584c8cbaf2a52b64276fa2` |
| Migration mode | FULL |
| Tables migrated | 11 |
| Source rows | 44 |
| Target rows | 44 |
| Rows migrated | 44/44 |
| Errors | 0 |
| Migration status | SUCCESS |

### Additional verified evidence

- Representative MySQL datatype fixture: 31 columns
- Partition fixture: 3 partitions / 3 rows
- Functions and procedures: runtime verified
- Triggers: runtime verified
- Views: runtime verified with target-local namespace rewriting
- Constraints: metadata and rejection behavior verified
- Indexes: metadata verified
- Comments: table/column comments verified
- Target partition reconciliation: verified
- Grant implementation: unit tested; cross-account live visibility remains environment-dependent

## Important Interpretation

`UNSUPPORTED` / `Out of Scope` means an explicit DMS capability outcome.
It does **not** mean that MySQL itself cannot support the feature.

Likewise, `Environment Blocked` does **not** mean the implementation is
incorrect; it means the available server configuration, privileges, or
environment prevented live verification.

A successful migration run alone is not treated as proof of object support.
Object support is classified using the corresponding metadata, runtime,
constraint, data, or functional verification evidence.
