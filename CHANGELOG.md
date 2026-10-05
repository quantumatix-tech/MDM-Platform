# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The application version is defined by the `version` field in `pyproject.toml`.
`migration_platform.__version__` reads that value from installed package metadata.

## [Unreleased]

### Changed

- **Centralized dependency management.** `pyproject.toml` is now the single
  authoritative source for Python dependencies, package metadata, and the
  application version. `requirements.txt` was removed, along with the parity
  check that existed only to keep two hand-maintained dependency lists aligned.
  `docker/Dockerfile` and `bootstrap.py` now install the project itself instead
  of a duplicated dependency list.
- **`bootstrap.py` installs the project**, not just its dependencies, and
  verifies that a real installed distribution exists before reporting success.
  Repository-local build artifacts such as a stale `migration_platform.egg-info`
  are no longer accepted as proof of installation.
- **Docker runtime installs from `pyproject.toml`** and no longer sets
  `PYTHONPATH` to work around missing package metadata.

### Added

- **Runtime and CLI version visibility.** `python -m migration_platform
  --version` reports the installed version, `requires-python` is re-validated at
  startup by `core/preflight.py`, and generated reports expose
  `platform_version` in both JSON and HTML.
- **Release and compatibility tooling.** `scripts/release_validation.py`
  validates packaging, versioning, and dependency metadata; `docs/
  compatibility_matrix.md` records evidence-based engine compatibility;
  `docs/release_process.md` documents the release procedure and checklist.

### Notes

- The Docker image build has not been verified; `docker` is unavailable in the
  development environment. Installation layout was validated by replicating the
  builder-stage prefix install locally.
- The 4 pre-existing unit test failures and the existing Ruff backlog are
  unchanged by this work.

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

- Updated `docs/RUNBOOK.md`, `docs/USER_GUIDE.md`, and
  `docs/architecture/platform_architecture.md` with Azure Key Vault and
  independent-provider-selection guidance.
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