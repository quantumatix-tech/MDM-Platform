# Phase 0 — Stabilize & Safety Baseline

## 1. Phase Overview

Phase 0 establishes the Unified DMS Platform safety and reliability baseline
before deeper architectural work begins. The work focuses on preventing silent
cross-engine DDL errors, keeping independent migration objects moving when one
object fails, and ensuring source PostgreSQL service restarts require an
explicit operator decision.

Together, these changes create a stabilization layer around the existing
migration pipeline. The phase improves type safety, failure visibility, and
operational safety without redesigning CDC or expanding the migration feature
set beyond the approved Phase 0 scope.

## 2. Starting State — Before Phase 0

### 2.1 Cross-engine Type Handling

Live MySQL and MSSQL target DDL used `col.target_type or col.source_type`.
When a cross-engine mapping was unavailable, target DDL could silently fall
back to the source-native type. This risked generating invalid or inappropriate
DDL in the target engine instead of requiring an explicit mapping decision.

### 2.2 Full Migration Error Handling

The full-migration object-creation and data-load loops were not isolated per
object. An individual exception could escape to the outer migration handler and
prevent independent later objects from being created or loaded. This reduced
both migration resilience and the usefulness of the final result.

### 2.3 PostgreSQL WAL Preflight Safety

PostgreSQL WAL preflight could automatically locate and patch
`postgresql.conf`, then restart the source PostgreSQL service when logical WAL
was required. A migration platform must not unexpectedly restart a user's
source database service because that action can interrupt dependent workloads
and requires explicit operator authorization.

## 3. Phase 0 Goals

- Never silently pass an unmapped cross-engine type into target DDL.
- Isolate independent migration failures while allowing controlled fail-fast
  behavior when requested.
- Never automatically restart a PostgreSQL source service without explicit
  operator authorization.
- Preserve existing valid same-engine behavior.
- Preserve existing project functionality outside the Phase 0 scope.

## 4. Phase 0 Changes — System-Level View

The three changes work together to make failures explicit at the point where
they occur and controllable by the operator. Type incompatibilities stop before
unsafe DDL is issued, object-level migration failures are retained in the
result while unrelated objects can continue, and source-service disruption is
blocked unless explicitly enabled.

| Area | Before | After | Safety / Benefit |
| --- | --- | --- | --- |
| Type handling | MySQL/MSSQL target DDL could fall back from `target_type` to `source_type`. | Missing cross-engine mappings raise `UnmappedTypeError`; mapped types use `target_type`. | Prevents silent source-type passthrough into incompatible target DDL. |
| Error handling | A creation or data-load exception could stop the full migration. | Per-object failures are reported; default behavior continues eligible objects, with optional fail-fast. | Preserves progress for independent objects and makes partial outcomes visible. |
| WAL / service restart | WAL preflight could patch source configuration and restart PostgreSQL automatically. | Restart path requires explicit CDC configuration opt-in. | Prevents unapproved source-service restarts. |

The resulting migration pipeline surfaces type, object, and operational
failures with actionable context rather than silently proceeding or
unconditionally interrupting a source service.

## 5. Detailed Implementation

### 5.1 Cross-engine Type Safety

`UnmappedTypeError` was added to provide table, column, source type,
source-engine (when available), and target-engine context with guidance to
configure a target type mapping. MySQL and MSSQL target connectors now use
`target_type` when a mapping is available. For cross-engine or unknown-source
contexts, a missing mapping raises the error before `CREATE TABLE` is executed.

Explicit same-engine MySQL/MSSQL migrations retain source-type behavior.
PostgreSQL-to-PostgreSQL target DDL remains unchanged: if no target type is
provided, it emits the source type. The CLI supplies source-engine metadata to
target connector construction so target connectors can make this distinction.

Implementation files: `core/connectors/base.py`, `core/connectors/mysql.py`,
`core/connectors/mssql.py`, and `migration_platform/__main__.py`.

### 5.2 Error Isolation

`migration.stop_on_error` was added to full-migration orchestration with a
default of `false`. With the default, object-creation and data-load failures are
recorded individually, audit logged, and retained in per-object results while
later eligible objects continue. Setting `migration.stop_on_error: true`
records the first object failure and then ends the migration with failed status.

When one or more objects fail while at least one independent object completes,
the final migration status is `partial_success`. Failures are exposed through
`phases.object_failures`, including phase, object name, and error details.
Known failed objects are excluded from validation so successful independent
objects can still be validated.

Implementation files: `core/orchestrator.py` and
`config/migration_config.schema.yaml`.

### 5.3 WAL Restart Safety

`cdc.allow_source_service_restart` was added with a default of `false`. When
the source is already configured with `wal_level = logical`, WAL preflight
remains a no-op.

When logical WAL is required but restart is not allowed, preflight stops with
an actionable error explaining the logical-replication requirement, that
automatic restart is disabled, and that the operator may set
`cdc.allow_source_service_restart: true` only when explicitly authorizing the
platform to restart the source service. In this disabled path, the platform
does not locate or modify `postgresql.conf` and does not restart PostgreSQL.

With explicit `true`, the pre-existing configuration-patch, restart, reconnect,
and verification path is retained. The safety guarantee is that an automatic
PostgreSQL source-service restart requires this explicit opt-in.

Implementation files: `core/orchestrator.py` and
`config/migration_config.schema.yaml`.

## 6. Configuration Changes

| Configuration | Default | Behavior |
| --- | --- | --- |
| `migration.stop_on_error` | `false` | Continues eligible full-migration objects after a recorded creation or data-load failure; `true` enables fail-fast behavior. |
| `cdc.allow_source_service_restart` | `false` | Blocks automatic PostgreSQL source-service restart during WAL preflight; `true` explicitly enables the existing restart path. |

Both defaults are deliberate safety defaults: the platform continues
independent migration work by default while it never restarts a source service
without direct operator authorization.

## 7. Verification & Test Evidence

Phase 0 verification combines focused behavior tests with the complete unit
suite:

- Task 1 focused tests: `5 passed`. They verify generated mapped MySQL/MSSQL
  DDL, unmapped cross-engine failure behavior, and unchanged
  PostgreSQL-to-PostgreSQL DDL behavior.
- Task 2 focused tests: `3 passed`. They verify isolated creation and data-load
  failures, continued processing of good objects, failure reporting,
  `partial_success`, and fail-fast behavior.
- Task 3 focused tests: `3 passed`. They verify restart denial when the flag is
  omitted or false, explicit restart opt-in, and logical-WAL no-op behavior.
- Final unit suite: `46 passed in 3.26s` using
  `python -m pytest tests/unit -q`.

## 8. Integration Testing Status

### Unit Tests

**PASS — 46 passed.**

### Integration Tests

**BLOCKED — not passed.** PostgreSQL at `localhost:5432` is reachable, but the
configured integration-test role is rejected during authentication before any
migration, CDC, or WAL code executes. This is an environment and credential
prerequisite, not evidence of a migration-code failure. No infrastructure was
changed to bypass it, and no credentials or secrets are documented here.

## 9. Files Changed

### Implementation

- `core/connectors/base.py`
- `core/connectors/mysql.py`
- `core/connectors/mssql.py`
- `core/orchestrator.py`
- `migration_platform/__main__.py`
- `config/migration_config.schema.yaml`

### Tests

- `tests/unit/test_cross_engine_type_safety.py`
- `tests/unit/test_error_isolation.py`
- `tests/unit/test_wal_restart_safety.py`

### Final Documentation

- `docs/PHASE_0.md`

## 10. Scope Boundaries

Phase 0 did not redesign CDC or implement CDC checkpoints in this phase. It did
not implement rollback or recovery, a dependency planner, a canonical metadata
model, a capability matrix, or broad cross-engine object transformation. These
boundaries prevent Phase 0 completion from being interpreted as completion of
later architectural roadmap work.

The phase was limited to Tasks 1, 2, and 3. Unrelated existing uncommitted
changes were preserved. No commit was created, nothing was pushed, and no
branch was created or changed.

## 11. Final System State After Phase 0

Before Phase 0, unsafe or silent behavior was possible: unmapped types could
reach target DDL, one object failure could stop independent work, and WAL
preflight could restart a source PostgreSQL service automatically.

After Phase 0:

- unmapped cross-engine types fail explicitly;
- independent full-migration failures can be isolated;
- fail-fast can be explicitly enabled;
- partial success is represented and failed objects are reported; and
- PostgreSQL source-service restart requires explicit opt-in.

Phase 0 materially improves the DMS safety baseline, but it does not make the
entire platform production-ready.

## 12. Phase 0 Acceptance Criteria

- [x] Cross-engine unmapped types fail explicitly
- [x] Same-engine behavior preserved
- [x] Object creation failures isolated
- [x] Data-load failures isolated
- [x] Fail-fast option available
- [x] Partial success represented
- [x] Failed objects reported
- [x] PostgreSQL source restart disabled by default
- [x] Explicit restart opt-in available
- [x] Unit test suite passes
- [ ] Integration suite — blocked by PostgreSQL authentication prerequisite

## 13. Phase 0 Final Status

**IMPLEMENTATION STATUS: COMPLETE**

**UNIT TEST STATUS: PASS — 46 passed**

**INTEGRATION STATUS: BLOCKED — environment authentication prerequisite**

**PRODUCTION READINESS: NOT CLAIMED**

Phase 0 is complete from an implementation and unit-test perspective. The
project can proceed to Phase 1 while the integration-environment authentication
prerequisite remains separately tracked.

## 14. Next Phase

Phase 0 is complete. The project can proceed to the next approved roadmap
phase after final review.
