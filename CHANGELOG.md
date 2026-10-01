# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The application version is defined by the `version` field in `pyproject.toml`.
`migration_platform.__version__` reads that value from installed package metadata.

## [Unreleased]

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