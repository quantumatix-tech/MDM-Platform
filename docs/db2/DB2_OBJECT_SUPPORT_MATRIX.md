# Db2 Object Support Matrix

## A. Verified / Supported Object Capabilities

| Object | Implementation | Unit Tests | Live E2E | Status | Evidence |
|--------|---------------|------------|----------|--------|----------|
| Schemas | `schema.py` | ✅ | ✅ | SUPPORTED | Run `b93dc7dbc29b479ab1561953b82b07fe` (9 tables, 22 rows, 0 errors) |
| Ordinary tables | `table.py`, `ddl.py`, `loader.py` | ✅ | ✅ | SUPPORTED | Run `b93dc7dbc29b479ab1561953b82b07fe` |
| Range-partitioned tables | `partition.py`, `ddl.py`, `loader.py` | ✅ | ✅ | SUPPORTED | Run `b93dc7dbc29b479ab1561953b82b07fe` |
| Columns / data types | `table.py`, `ddl.py` | ✅ | ✅ | SUPPORTED | Run `b93dc7dbc29b479ab1561953b82b07fe` |
| Distinct UDTs | `udt_alias.py`, `target_udt_alias.py` | ✅ | ✅ | SUPPORTED | Run `b93dc7dbc29b479ab1561953b82b07fe` |
| Structured UDTs | `udt_alias.py`, `target_udt_alias.py` | ✅ | ✅ | SUPPORTED | Run `33f9fadbeeb149bab34dc13980d0613c` (OBJTEST.ADDRESS_TYPE, METATYPE=R) |
| DEFAULT columns | `table.py`, `ddl.py`, `check_default.py` | ✅ | ✅ | SUPPORTED | 8/8 matched |
| Generated columns | `table.py`, `ddl.py` | ✅ | ✅ | SUPPORTED | Run `b93dc7dbc29b479ab1561953b82b07fe` |
| Identity columns (normal increment) | `identity.py`, `ddl.py` | ✅ | ✅ | SUPPORTED | RESTART WITH MAX+increment |
| Standalone sequences | `sequence.py`, `target_sequence.py` | ✅ | ✅ | SUPPORTED | Run `b93dc7dbc29b479ab1561953b82b07fe` |
| Primary keys | `constraint.py`, `target_constraint.py` | ✅ | ✅ | SUPPORTED | Run `b93dc7dbc29b479ab1561953b82b07fe` |
| Foreign keys (single-column) | `foreign_key.py`, `target_foreign_key.py` | ✅ | ✅ | SUPPORTED | Run `b93dc7dbc29b479ab1561953b82b07fe` |
| UNIQUE constraints | `constraint.py`, `target_constraint.py` | ✅ | ✅ | SUPPORTED | Run `b93dc7dbc29b479ab1561953b82b07fe` |
| CHECK constraints | `constraint.py`, `target_constraint.py` | ✅ | ✅ | SUPPORTED | Run `b93dc7dbc29b479ab1561953b82b07fe` |
| Indexes | `index.py`, `target_index.py` | ✅ | ✅ | SUPPORTED | Run `b93dc7dbc29b479ab1561953b82b07fe` |
| Views | `view.py`, `target_view.py` | ✅ | ✅ | SUPPORTED | Run `b93dc7dbc29b479ab1561953b82b07fe` |
| MQTs | `mqt.py`, `target_mqt.py` | ✅ | ✅ | SUPPORTED | Deferred refresh |
| Db2 Aliases (synonym use case) | `udt_alias.py`, `target_udt_alias.py` | ✅ | ✅ | SUPPORTED | Run `33f9fadbeeb149bab34dc13980d0613c` (EMP_SYNONYM alias) |
| SQL functions | `routine.py`, `target_routine.py` | ✅ | ✅ | SUPPORTED | Run `b93dc7dbc29b479ab1561953b82b07fe` |
| SQL procedures | `routine.py`, `target_routine.py` | ✅ | ✅ | SUPPORTED | Run `b93dc7dbc29b479ab1561953b82b07fe` |
| Triggers | `trigger.py`, `target_trigger.py` | ✅ | ✅ | SUPPORTED | 2/2 valid+enabled |
| Table/column comments | `comment.py` | ✅ | ✅ | SUPPORTED | Run `b93dc7dbc29b479ab1561953b82b07fe` |
| Grants | `grant.py`, `target_grant.py` | ✅ | ✅ | SUPPORTED | Ownership-generated = informational |
| Roles and role memberships | `role.py`, `target_role.py` | ✅ | ✅ | SUPPORTED | Run `33f9fadbeeb149bab34dc13980d0613c` (MIG_READ_ROLE) |

## B. Implemented But Acceptance Pending

| Object | Implementation | Unit Tests | Live E2E | Status | Evidence / Limitation |
|--------|---------------|------------|----------|--------|----------------------|
| Foreign keys (composite / multi-column) | `foreign_key.py`, `target_foreign_key.py` | ✅ | NOT VERIFIED | CODE PRESENT / LIVE E2E NOT VERIFIED | Multi-column FK discovery and DDL generation exist via `SYSCAT.KEYCOLUSE`; FK ordering unit-tested. No specific live multi-column FK E2E evidence in report JSONs. |

## C. Known Limitations / Future Scope

| Item | Category | Evidence / Limitation |
|------|----------|----------------------|
| Foreign keys (cyclic) | NOT SUPPORTED | `db2_fk_order.py` detects dependency cycles and raises `ValueError("Foreign key dependency cycle detected")`. Cyclic FKs are rejected — not migrated. Future implementation plan documented in `DB2_TECHNICAL_REFERENCE.md` section 8a (deferred constraint creation after data load; not implemented in 0.4.0). |
| Identity increment=0 | NOT SUPPORTED BY CODE | Code computes `nxt = MAX + increment`; increment=0 yields `nxt = MAX` causing collision (verified by unit test `test_increment_zero_rejected`). Code does not explicitly reject increment=0 at discovery/DDL time. Db2 LUW `INCREMENT BY 0` rejection claim is unverified — do not state it as fact. |
| CDC / incremental migration | OUT OF SCOPE | Intentionally excluded from this Db2 release. Requires Db2 CDC infrastructure. |
| Least-privilege permissions | NOT LIVE VERIFIED | Tested setup used DBADM/SECADM. Proposed narrower privileges (CREATEIN, SELECT/INSERT/UPDATE/DELETE, USAGE, EXECUTE) are not verified. Security deployment limitation, not an object type. |

### Legend

| Symbol | Meaning |
|--------|---------|
| ✅ | Implemented and verified by unit test and/or live E2E evidence |
| ❌ | Not implemented / not supported |
| NOT VERIFIED | Code exists but no specific live E2E evidence |

### Evidence References

- **Run `33f9fadbeeb149bab34dc13980d0613c`** (OBJSC01 → OBJTG08): FULL migration SUCCESS, 1 table, 2/2 rows, 0 migration errors. Verified: OBJTEST.ADDRESS_TYPE structured UDT (METATYPE=R), OBJTEST.EMP_SYNONYM alias, MIG_READ_ROLE role membership.
- **Run `b93dc7dbc29b479ab1561953b82b07fe`** (E2ESRC02 → E2ETGT07): 9 tables, 22 rows, 0 mismatches, 0 errors, report JSON status="success".
- **Run `96980b310bca4d88ab6bc04b101b1f2c`** (E2ESRC02 → E2ETGT07): 9 tables, 22 rows, 0 mismatches, 0 errors, report JSON status="success".
- **Run `30f998eb2f9e4827b539d76dfdc30f49`** (E2ESRC02 → E2ETGT07): NO report file found in `reports/`; status cannot be verified.
- **Unit tests**: 1269 passed, 0 failed (includes Db2 role transaction tests and FK edge-case tests).
