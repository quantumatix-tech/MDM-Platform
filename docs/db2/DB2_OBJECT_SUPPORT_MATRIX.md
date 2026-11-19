# Db2 Object Support Matrix

| Object Type | Discovery | Target DDL | Data Handling | Validation | Unit Test | Live E2E | Support Status | Evidence / Limitation |
|-------------|-----------|------------|---------------|------------|-----------|----------|----------------|----------------------|
| Schemas | ✅ `schema.py` | ✅ `schema.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Run b93dc... |
| Ordinary tables | ✅ `table.py` | ✅ `ddl.py` | ✅ `loader.py` | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Run b93dc... |
| Range-partitioned tables | ✅ `partition.py` | ✅ `ddl.py` | ✅ `loader.py` | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Run b93dc... |
| Columns / data types | ✅ `table.py` | ✅ `ddl.py` | ✅ | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Run b93dc... |
| Distinct UDTs | ✅ `udt_alias.py` | ✅ `target_udt_alias.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Run b93dc... |
| Structured UDTs | ✅ `udt_alias.py` | ✅ `target_udt_alias.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Run 33f9... (OBJTEST.ADDRESS_TYPE, METATYPE=R) |
| DEFAULT columns | ✅ `table.py:90-218` | ✅ `ddl.py:16-130` | N/A | ✅ `check_default.py` | ✅ | ✅ | ✅ | SUPPORTED | 8/8 matched |
| Generated columns | ✅ `table.py` | ✅ `ddl.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Run b93dc... |
| Identity columns | ✅ `identity.py` | ✅ `ddl.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | RESTART WITH MAX+increment |
| Identity (increment=0) | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | NOT SUPPORTED | Collision risk |
| Standalone sequences | ✅ `sequence.py` | ✅ `target_sequence.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Run b93dc... |
| Primary keys | ✅ `constraint.py` | ✅ `target_constraint.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Run b93dc... |
| Foreign keys | ✅ `foreign_key.py` | ✅ `target_foreign_key.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Run b93dc... |
| UNIQUE constraints | ✅ `constraint.py` | ✅ `target_constraint.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Run b93dc... |
| CHECK constraints | ✅ `constraint.py` | ✅ `target_constraint.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Run b93dc... |
| Indexes | ✅ `index.py` | ✅ `target_index.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Run b93dc... |
| Views | ✅ `view.py` | ✅ `target_view.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Run b93dc... |
| MQTs | ✅ `mqt.py` | ✅ `target_mqt.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Deferred refresh |
| Aliases | ✅ `udt_alias.py` | ✅ `target_udt_alias.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Run 33f9... (EMP_SYNONYM alias) |
| SQL functions | ✅ `routine.py` | ✅ `target_routine.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Run b93dc... |
| SQL procedures | ✅ `routine.py` | ✅ `target_routine.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Run b93dc... |
| Triggers | ✅ `trigger.py` | ✅ `target_trigger.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | 2/2 valid+enabled |
| Table/column comments | ✅ `comment.py` | ✅ `comment.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Run b93dc... |
| Grants | ✅ `grant.py` | ✅ `target_grant.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Ownership-generated = informational |
| Roles and role memberships | ✅ `role.py` | ✅ `target_role.py` | N/A | ✅ | ✅ | ✅ | ✅ | SUPPORTED | Run 33f9... (MIG_READ_ROLE, SECADM executor) |
| Synonyms | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | NOT IMPLEMENTED | Db2 aliases cover this use case |
| Composite/cyclic FKs | ⚠️ | ⚠️ | N/A | ⚠️ | ⚠️ | ⚠️ | ⚠️ | NOT LIVE-TESTED | Not specifically tested |
| CDC/incremental | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | OUT OF SCOPE | Intentionally excluded from this release |

### Legend
- ✅ Implemented and verified
- ❌ Not implemented
- ⚠️ Implemented but not specifically tested

### PENDING FEATURE DECISIONS

| Feature | Functional Req | Technical Feasibility | Security Impact | Effort/Risk | Recommendation |
|---------|---------------|----------------------|-----------------|-------------|----------------|
| CDC incremental | Real-time sync | Feasible (Db2 CDC) | Medium | High/High | Defer to separate phase |
| Composite/cyclic FKs | Multi-column FKs | Feasible | Low | Medium/Medium | Implement if needed |
| Increment=0 identity | Support zero-increment | Not feasible (collision) | None | N/A/N/A | Reject |
| Least-privilege setup | Narrower target privileges | Feasible | Medium | Medium/Medium | Proposed, not verified; DBADM/SECADM tested |
