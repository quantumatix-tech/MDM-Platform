# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The application version is defined by the `version` field in `pyproject.toml`.
`migration_platform.__version__` reads that value from installed package metadata.

## [Unreleased]

### Added

- **Version Management implementation (Stages 1-9).** The repository now has a
  complete, evidence-based version-management system:
  - `scripts/release_validation.py` validates packaging, versioning, and
    dependency metadata before any release tag.
  - `docs/compatibility_matrix.yaml` records engine compatibility and a
    `runtime` section for Python and dependency versions, with statuses
    TESTED, SUPPORTED, or UNVERIFIED.
  - `core/runtime_manifest.py` captures application version, Git metadata,
    Python runtime, package/driver versions, and database engine versions for
    every migration execution.
  - `core/reporting/report_builder.py` includes the runtime manifest in both
    JSON and HTML migration reports.
  - `docker/Dockerfile` and `docker/entrypoint.sh` provide a reproducible
    runtime image with a pinned Python 3.11.9 base and MSODBC Driver 18.
  - `.github/workflows/ci.yml` runs a dedicated `version-validation` job that
    executes release validation, compatibility matrix validation, and the
    relevant unit tests on every push and pull request.
- **Docker reproducibility.** The Docker image is built from a pinned
  `python:3.11.9-slim` base, installs dependencies from `pyproject.toml`, and
  verifies the runtime manifest inside the container. Secrets are supplied at
  runtime via environment variables and are never baked into the image.

### Changed

- **Compatibility matrix now records runtime components.** The
  `runtime` section in `docs/compatibility_matrix.yaml` lists the important
  Python packages and drivers with their tested versions and evidence.
  `scripts/generate_compatibility_matrix.py` validates this section and renders
  it in the generated Markdown.

### Notes

- The Docker image build and container run have been verified locally with
  Podman 5.4.2. The runtime manifest inside the container correctly reports
  application version 0.2.3, Python 3.11.9, and installed package versions.
- The 4 pre-existing unit test failures (`test_cross_engine_type_safety.py` ×2,
  `test_mysql_datatypes.py` ×2) are unchanged by this work.

## [0.3.0] - 2026-10-23

### Added

- **MySQL local E2E finalized (Local Python).** FULL migration completed successfully
  (run `fc54500c696f4cd792844858e7662284`): 7 tables, 33 rows, 0 errors,
  duration 2.8s (config `mysql_local_01.yaml`, root user, no SSL). Podman
  FULL E2E (run `d82f18ae5bd34164a8409a2c74911d22`): 7 tables, 33 rows,
  0 errors, status success.
- **PostgreSQL Podman E2E finalized.** FULL migration completed successfully
  (run `3dc4381335974ff3a727f59d806421d0`): 7 tables, 33 rows, 0 errors,
  100% success rate (config `psql_local_02.yaml`, host 172.24.48.1:55432,
  ssl: false). Root cause: pg_hba.conf target database name case mismatch
  and SSL disabled on server.
- **PostgreSQL local E2E finalized.** FULL migration completed successfully
  (run `76e7c82824e741adba322dfae9625124`): 7 tables, 33 rows, 0 errors.
  All objects including functions, procedures, triggers, views, materialized
- **MSSQL E2E verified (local + Podman).** Local FULL E2E (run
  `905ce6fa6fc743e7b3e86c508448c1c7`): 9 tables, 28 rows, 0 errors,
  status success (local Python, platform 0.2.3). Podman FULL E2E (run
  `cfe7efdb6b424786910402dd69f62eea`): 7 tables, 33 rows, 0 errors,
  status success (Podman container, platform 0.2.3). The two runs used
  different source fixtures (9 tables/28 rows vs 7 tables/33 rows).

### Changed

- **MySQL security allowlist fix.** `migration.security_users` is now merged
  from the `migration:` config section into the source connection config
  (`migration_platform/__main__.py`) and `security_user_allowlist()` falls back
  to `config.migration.security_users` (`core/connectors/mysql/objects/security.py`).
  This avoids unnecessary `mysql.user` metadata queries when an allowlist is
  configured.
- **MySQL routine/trigger NULL DDL guards.** `_show_create()` in both source
  and target connectors now raises an actionable error when `SHOW CREATE`
  returns a NULL definition, naming the missing `SHOW_ROUTINE` privilege.
- **MySQL orchestrator error isolation.** Definer-prerequisite discovery
  (`list_events`, `list_functions`, `get_all_triggers`) is wrapped in
  try/except so errors are recorded without aborting before the phases that
  report them.
- **MySQL error messages improved.** All NULL-DDL errors now include the
  exact `GRANT SHOW_ROUTINE` SQL for the DBA.

### Fixed

- **MySQL trigger creation.** Resolved by `SET PERSIST
  log_bin_trust_function_creators = ON` on the target server (DBA-approved).
- **PostgreSQL E2E.** Verified working with `config/psql_local_01.yaml`
  (host 127.0.0.1, port 55432, ssl: false). No code changes were required.

### Documentation

- MySQL E2E status updated from BLOCKED to VERIFIED across all E2E docs.
- PostgreSQL E2E run ID updated to verified run; Podman path now VERIFIED.
- MSSQL E2E evidence documented (local + Podman both VERIFIED).

## [0.2.3] - 2026-10-05

### Added

- **FK-aware/dependency-ordered data loading.** Tables are now loaded in
  topological order based on foreign key relationships, ensuring parent tables
  are loaded before child tables across PostgreSQL, MySQL, and MSSQL.
- **Topological ordering of FK dependencies.** New `core/dependency_order.py`
  module with `build_dependency_graph()`, `topological_sort()`,
  `order_data_load_objects()`, and `DependencyCycleError`.
- **Deterministic ordering for independent objects.** Alphabetical tiebreaker
  ensures consistent load order for unrelated tables.
- **Cycle detection before data modification.** `DependencyCycleError` is
  raised before `clear_objects_for_full_sync` is called, preventing partial
  data loads in cyclic schemas.
- **Support through existing `Schema.foreign_keys` metadata.** Works with
  PostgreSQL, MySQL, and MSSQL connectors without schema changes.
- **CDC initial-sync ordering.** CDC migrations now use the same dependency
  ordering for initial data sync phase.

### Notes

- The 4 pre-existing unit test failures (`test_cross_engine_type_safety.py` ×2,
  `test_mysql_datatypes.py` ×2) are unchanged by this work.

## [0.2.2] - 2026-10-05

### Added

- **Azure Key Vault secret provider.** Azure Key Vault can now be used as a
  secret provider via `secrets.provider: azure_keyvault` and
  `secrets.azure_keyvault.url`. Uses `DefaultAzureCredential` for
  authentication (no credential values are stored in config). Requires the
  `azure-identity` and `azure-keyvault-secrets` packages.
- **Independent endpoint secret-provider selection.** `source.secret_provider`
  and `target.secret_provider` override the global `secrets.provider` for their
  endpoint only, enabling e.g. a local encrypted-file store for the source and
  Azure Key Vault for the target. When omitted, the existing global provider
  remains the backward-compatible default.
- **Local Encrypted File provider enhancements.** Platform-aware defaults
  (OS keyring on Windows, environment key elsewhere), atomic file writes with
  `fsync`, `list_secrets`/`set_secret`/`delete_secret` CLI operations, and
  sanitized error messages that never expose secret values.
- **`migration_platform.secrets_cli`** — interactive CLI for managing the local
  encrypted-file store (`set`, `list`, `verify`, `delete`, `init`).
- **`docs/azure_keyvault.md`** — comprehensive Azure Key Vault setup,
  configuration, troubleshooting, and security guidance.
- **`tests/unit/test_secret_provider_selection.py`** — 7 tests covering
  provider selection, backward compatibility, and error sanitization.
- **`tests/unit/test_local_encrypted_file_cli.py`** — 10 tests covering local
  store CRUD, encryption-key management, and CLI security.

### Changed

- Updated `docs/USER_GUIDE.md` and
  `docs/architecture/platform_architecture.md` with Azure Key Vault and
  independent-provider-selection guidance (chaos/resilience procedures moved from `docs/RUNBOOK.md`).
- `config/mysql_local_test.yaml` updated to demonstrate mixed local-store +
  Azure Key Vault provider usage.
- `config/migration_config.schema.yaml` — added optional `secret_provider`
  field to `source` and `target` sections.

### Notes

- The 4 pre-existing unit test failures (`test_cross_engine_type_safety.py` ×2,
  `test_mysql_datatypes.py` ×2) are unchanged by this work.

## [0.2.1] - 2026-10-04

### Added

- **Acceptance E2E workflow (Mode 2).** New setup → external migration → validation
  flow for real product/acceptance testing, coexisting with the existing automated
  regression E2E (Mode 1). Supports MSSQL, PostgreSQL, and MySQL.
  - `tests/e2e/acceptance/` — shared utilities (`E2EConfig`, config loader,
    report writer, phase/count formatters).
  - Per-engine `run_setup.py` and `run_validation.py` runners under
    `tests/e2e/{mssql,postgresql,mysql}/`.
  - `tests/e2e/acceptance_clean.py` — shared cleanup runner for all engines.
  - Demo configs: `config/{mssql,postgresql,mysql}_e2e_acceptance.yaml`.
  - `docs/e2e_validation/ACCEPTANCE_RUNBOOK.md` — full workflow documentation.
  - `tests/unit/test_e2e_acceptance.py` — 28 unit tests for the shared module.

### Changed

- Replaced duplicate `_fmt_phase` helpers in per-engine validation runners with the
  shared `fmt_phase` from `tests/e2e/acceptance/`.

## [0.2.0] - 2026-10-01

First tagged release. Contains the completed platform architecture transition.

### Added

- **Modular connector architecture** — connectors moved from single monolithic modules
  to per-database packages under `core/connectors/<engine>/`, each split into
  `source.py`, `target.py`, `cdc.py`, models, and per-object modules.
- **MSSQL connector modularization** — split into table, view, sequence, function,
  trigger, partition, synonym, type, comment, and security object modules.
- **PostgreSQL connector modularization** — split into per-object modules.
- **MySQL connector modularization** — split into per-object modules, with a MySQL
  object support matrix.
- **MSSQL object migration coverage** — partitioning, specialized data types,
  users/roles/permissions, comments and extended properties, and triggers.
- **PostgreSQL object migration coverage** — non-public schema migration, views and
  materialized views in non-public schemas, functions and procedures, triggers,
  comments, grants and privileges, row-level security policies, cross-schema foreign
  keys, and end-to-end object migration validation.
- **Primary-key-name preservation** — source primary key constraint names are read
  from catalog metadata and preserved on the target for PostgreSQL and MSSQL,
  including composite key column ordering.
- **Identity sequence preservation** — identity and sequence state is migrated, with
  view creation failures now reported instead of silently ignored.
- **Unique constraint and standalone index preservation** for PostgreSQL.
- **MySQL phases wired into the orchestrator and configuration.**
- **Packaging configuration** — explicit setuptools package discovery for the
  `core` and `migration_platform` packages, fixing project installation and wheel
  builds, which previously failed on automatic flat-layout discovery.
- **Version reporting** — `migration_platform.__version__` resolves from installed
  package metadata, with `pyproject.toml` as the single source of truth.

### Changed

- Metadata discovery is scoped by schema for PostgreSQL, and MSSQL source metadata
  queries order composite primary key columns by ordinal position.
- Migration safety and reliability improvements in the orchestrator and validation
  layer.
- Reusable MSSQL cloud-to-local configuration.
- Documentation reorganized around the modular architecture, including
  `docs/architecture/platform_architecture.md`.

### Validation status

Validated against live MSSQL and PostgreSQL instances for local-to-local migration,
including migration runs that completed row-for-row with all validations passing.
The unit suite reports 408 passing tests with 4 pre-existing failures unrelated to
this release.