# Compatibility Matrix and Tool Inventory

**Authoritative reference for Migration Platform version compatibility.**

This document is the single source of truth for *version* compatibility across
the application, its runtimes, drivers, and database engines. Object-level
support is documented separately in
[`architecture/platform_architecture.md`](architecture/platform_architecture.md)
§15 and the per-engine object matrices.

| Category | Authoritative document |
|---|---|
| Application version, release model | [`architecture/platform_architecture.md`](architecture/platform_architecture.md) §4.2 |
| Release history | [`../CHANGELOG.md`](../CHANGELOG.md) |
| Object-type support per engine | `docs/<engine>/<ENGINE>_OBJECT_SUPPORT_MATRIX.md` |
| **Version compatibility (this file)** | `docs/compatibility_matrix.md` |

If a version claim appears elsewhere in the repository and contradicts this
document, this document wins.

---

## 1. Status definitions

These are deliberately distinct. **"Supported" is not a claim that the
combination has been executed.**

| Status | Meaning |
|---|---|
| **TESTED** | A recorded run in this repository exercised this combination successfully. Evidence is cited. |
| **SUPPORTED** | Implemented by design with no known blocker, but **no recorded run in this repository**. Expected to work; unproven here. |
| **UNVERIFIED** | A claim exists in documentation or code, but no run in this repository substantiates it. |
| **UNSUPPORTED** | Known not to work. Cause is documented. |
| **KNOWN ISSUE** | Works with documented constraints or caveats. |

---

## 2. Application and language runtime

| Item | Required minimum | Supported | Tested | Install source | Validation command | Notes |
|---|---|---|---|---|---|---|
| Migration Platform | — | — | **0.2.0** | `pyproject.toml` `[project] version` | `python -m migration_platform --version` | Current release, tag `v0.2.0` |
| Python | **3.11** | 3.11 – 3.12 | **3.12.6** (local), **3.11** (CI, Docker) | `pyproject.toml` `requires-python = ">=3.11"` | `python --version` | Re-checked at startup by `core/preflight.py` |

Python 3.13+ is **UNVERIFIED** — no CI matrix and no recorded run.

---

## 3. Python packages

`pyproject.toml` is the **single authoritative source** of Python dependency
metadata for this repository. `[project].dependencies` holds the runtime set and
`[project.optional-dependencies` holds `dev`, `aws`, `gcp`, and `vault`. There is
no second dependency file that must be kept in sync.

Every supported installation path resolves from `pyproject.toml`:

| Environment | Command | Resolves from |
|---|---|---|
| Developer (editable) | `pip install -e ".[dev]"` | `pyproject.toml` |
| Developer (one-time) | `python bootstrap.py` → `pip install .` | `pyproject.toml` |
| CI | `pip install -e ".[dev]"` | `pyproject.toml` |
| Docker | builder stage `pip install --prefix=/install .` | `pyproject.toml` |

Consistency of the declared metadata is enforced by `scripts/release_validation.py`.

### 3.1 Database drivers

| Engine | Package | Import name | Constraint | Tested (resolved) | Connection method |
|---|---|---|---|---|---|
| MSSQL | `pyodbc` | `pyodbc` | `>=4.0` | 5.3.0 | ODBC DSN string, `pyodbc.connect` |
| PostgreSQL | `psycopg[binary]` | `psycopg` (v3) | `>=3.0` | 3.3.6 | `psycopg.connect(**kwargs)` |
| MySQL | `mysql-connector-python` | `mysql.connector` | `>=8.0` | 26.7.0 | `mysql.connector.connect(**kwargs)` |
| MySQL CDC | `mysql-replication` | `pymysqlreplication` | `>=0.44` | 1.0.17 | `BinLogStreamReader` |
| MongoDB | `pymongo` | `pymongo` | `>=4.0` | 4.18.2 | `MongoClient` |
| Cosmos DB | `pymongo` (MongoDB API) | `pymongo` | `>=4.0` | 4.18.2 | `MongoClient` |

**MSSQL note:** the driver library version is not the only requirement. The
system ODBC driver must be **ODBC Driver 18 for SQL Server**, referenced by name
in `core/connectors/mssql/{source,target,cdc}.py` and checked by `bootstrap.py`.
This is an OS-level component and is deliberately not a Python dependency;
`docker/Dockerfile` installs `msodbcsql18` **unversioned** (see §8).

Driver upgrade policy: do not change a driver to obtain a newer version without
first establishing a passing baseline on the current constraint and re-running
it on the new one. No driver has been upgraded during the 0.2.0 release work.

### 3.2 Secret providers

| Provider | Package | Constraint |
|---|---|---|
| Environment | *(stdlib)* | prefix `SECRET_` |
| AWS Secrets Manager | `boto3` | `>=1.28` (also the `aws` extra) |
| GCP Secret Manager | `google-cloud-secret-manager` | `>=2.16` (also the `gcp` extra) |
| HashiCorp Vault | `hvac` | `>=1.2` (also the `vault` extra) |
| Azure Key Vault | `azure-identity`, `azure-keyvault-secrets` | `>=1.12`, `>=4.0` |
| Local encrypted file | `cryptography` | `>=41.0` |
| OS keyring | `keyring` | `>=24.0` |

### 3.3 Other runtime dependencies

| Package | Constraint | Purpose |
|---|---|---|
| `pyyaml` | `>=6.0` | Configuration loading |
| `requests` | `>=2.0` | HTTP notifications |
| `rich` | `>=13.0` | Live terminal UI |
| `azure-storage-blob` | `>=12.0` | Report upload to blob storage |

`rich` and `azure-storage-blob` were previously **undeclared** and auto-installed
on first use via `core/driver_installer.py`. They are now declared so a clean
install is reproducible. Their graceful-degradation paths are unchanged.

### 3.4 Development dependencies (`[dev]` extra)

| Package | Constraint | Purpose |
|---|---|---|
| `ruff` | `>=0.6` | Lint and format |
| `pytest` | `>=8.0` | Test runner |
| `pytest-mock` | `>=3.14` | Mocking fixtures |

### 3.5 Declared but unused

`azure-cosmos>=4.0.0` is declared but **never imported** by connector code. The
only reference is the string `"azure.cosmos"` in `bootstrap.py`'s import table.
`core/connectors/cosmos_mongo.py` connects through `pymongo` over the MongoDB
wire protocol. The dependency is retained for bootstrap parity but is not on any
executed code path (see §8).

---

## 4. Dependency policy

| Rule | Rationale |
|---|---|
| Runtime dependencies use `>=` lower bounds anchored to a minor version | Permits security and patch updates; no recorded conflict with a newer minor. |
| Development dependencies carry an explicit floor | Prevents a floating tool from silently changing lint and test behaviour on every fresh install. |
| Transitive dependencies are **not** pinned in the repository | A committed lock file is not justified by the current single-environment deployment model, and would pin resolutions for developers, CI, and the Docker build alike. |
| `pyproject.toml` is the only dependency source | Docker, CI, and `bootstrap.py` all install the project itself, so one file defines what every environment runs. Drift is impossible by construction, and there is no duplicate list to keep in sync. |
| `requires-python` is a floor, and the resolved set is lower-bounded only | See rule 1. |
| External tools are **not** Python dependencies | Java, Terraform, and Liquibase are not used (see §7). ODBC Driver 18 is an OS component. |

To pin transitively for a reproducible build, generate a constraints file in the
build environment rather than committing one:

```bash
pip install -e . && pip freeze > constraints.txt
```

---

## 5. Database engine compatibility

Only combinations with a recorded successful run in this repository are marked
TESTED. "The code should work" is not evidence.

### 5.1 PostgreSQL

| Direction | Engine version | Status | Evidence |
|---|---|---|---|
| Local → Local | **17.4** | **TESTED** | `docs/postgresql/POSTGRESQL_LOCAL_AUDIT_PHASE_1.md:5,36,338` — 8 tables, 37 rows, 20+ non-table objects, 100% success |
| Local → Azure Flexible Server | **16** | **TESTED** | `docs/postgresql/POSTGRESQL_CLOUD_AUDIT.md:7,23,292` — all objects migrated and verified |
| Cloud → Local | 16 (Azure) | **SUPPORTED** | Flow in `docs/postgresql/POSTGRESQL_MIGRATION_FLOW.md`; no version recorded for this direction |
| 14, 15, 18, other | — | **UNVERIFIED** | No recorded run |

CI and `docker-compose.test.yml` run **PostgreSQL 15**, which is not the version
used in any audit. The CDC instructions in `docs/USER_GUIDE.md` assume **17**.
No document reconciles these three values.

### 5.2 Microsoft SQL Server

| Direction | Engine version | Status | Evidence |
|---|---|---|---|
| Local → Local | **Not recorded** | **TESTED (version unspecified)** | `docs/mssql/MSSQL_LOCAL_AUDIT.md` records `\| Engine \| Microsoft SQL Server \|` with **no version row** |
| Local → Azure SQL Database | **Not recorded** | **TESTED (version unspecified)** | `docs/mssql/MSSQL_CLOUD_AUDIT.md` |
| Azure SQL → LocalDB | **Not recorded** | **SUPPORTED** | `docs/mssql/MSSQL_E2E_RUNBOOK.md:400` |

**No SQL Server version is stated anywhere in this repository.** The only
concrete value is the test-container image
`mcr.microsoft.com/mssql/server:2022-latest` in `docker-compose.test.yml`, which
is an image tag, not an audited environment.

An implicit code floor exists: `CREATE OR ALTER` is used for idempotent
functions and triggers, which requires **SQL Server 2016 SP1 or newer**
(`core/connectors/mssql/objects/trigger.py:129`, `objects/function.py:86`).
This is a code comment, not a documented or tested support claim.

### 5.3 MySQL

| Direction | Engine version | Status | Evidence |
|---|---|---|---|
| Any | **Not recorded** | **UNVERIFIED** | No MySQL E2E run is recorded anywhere |
| Container only | 8.0 | **UNVERIFIED** | `docker-compose.test.yml` and `ci.yml` use `mysql:8.0` |

`docs/mysql/MYSQL_OBJECT_SUPPORT_MATRIX.md` states its scope is unit-suite
coverage only, and `docs/architecture/platform_architecture.md:970` records that
MySQL-to-MySQL has "no recorded live local-to-local run".

### 5.4 MongoDB and Cosmos DB

| Engine | Engine version | Status | Evidence |
|---|---|---|---|
| MongoDB | **Not recorded** | **UNVERIFIED** | `platform_architecture.md:970` — "have not been exercised against a live endpoint in this phase" |
| Cosmos DB (MongoDB API) | **Not recorded** | **UNVERIFIED** | Same |

Container images `mongo:7.0` exist in compose and CI but no run is recorded.

### 5.5 Cloud engines

| Service | Version | Status |
|---|---|---|
| Azure SQL Database | **Not recorded** | **KNOWN ISSUE** — `sp_addextendedproperty` with `@level1type = 'SEQUENCE'` fails with error 15600; platform limitation, documented in `docs/mssql/MSSQL_LIMITATIONS.md:90-93` |
| Azure Database for PostgreSQL Flexible Server | 16 | **TESTED** as target |
| Azure Database for MySQL | — | **NOT APPLICABLE** — zero references in the repository |

---

## 6. Corrections to earlier documentation claims

`docs/USER_GUIDE.md:126-131` states version floors that this repository's own
evidence does not support. Treat the following as **UNVERIFIED**, not supported:

| Documented claim | Location | Actual evidence |
|---|---|---|
| `PostgreSQL (any version, local or cloud)` | `USER_GUIDE.md:128` | Only 17.4 (local) and 16 (Azure) have recorded runs |
| `MySQL 5.7+` | `USER_GUIDE.md:129` | No MySQL E2E run recorded; CI runs 8.0 |
| `MongoDB 4.0+` | `USER_GUIDE.md:130` | No run recorded; `platform_architecture.md:970` states never exercised live |
| `SQL Server (requires ODBC Driver 18)` | `USER_GUIDE.md:131` | ODBC Driver 18 requirement is accurate; no engine version given |

These have **not** been edited in place, because `USER_GUIDE.md` is outside the
scope of this task. They are recorded here so the contradiction is documented
rather than silently propagated.

---

## 7. External tools

| Tool | Used by this project | Version requirement | Validation |
|---|---|---|---|
| **Java / JDK** | No — zero references | — | **NOT APPLICABLE** |
| **Terraform** | No — zero references | — | **NOT APPLICABLE** |
| **Liquibase** | No — zero references | — | **NOT APPLICABLE** |
| ODBC Driver 18 for SQL Server | Yes — MSSQL connectivity | 18.x, name-matched only | `bootstrap.py` checks `pyodbc.drivers()` |
| `psql` | Yes — E2E fixtures only | **Not documented** | Operator-supplied |
| `sqlcmd` | Yes — MSSQL test guide, container healthcheck | **Not documented** | Operator-supplied |
| `mysql` client | No invocation anywhere | — | **NOT APPLICABLE** |

A historical toolchain (Java 21, Terraform 1.15.5, Liquibase 5.0.3,
MySQL Connector/J 9.5.0) is associated with other projects in this workspace.
**None of it applies here** — none of those tools appears in this repository,
and none is installed or invoked by the code, CI, or Docker build.

---

## 8. Known version-specific issues

| Issue | Affected component | Detail |
|---|---|---|
| ODBC Driver 18 unversioned in image | Docker build | `docker/Dockerfile:19` installs `msodbcsql18` with no version, so image rebuilds can change driver behaviour silently |
| Floating CI tool versions | CI | `ci.yml` installs ruff unpinned via `pip install ruff`; the `[dev]` floor now bounds it, but CI does not install from `[dev]` for lint |
| Python version drift across environments | Build/test | Docs assume 3.11+; local runs 3.12.6; CI and Docker pin 3.11. No matrix job |
| Unused `azure-cosmos` dependency | Packaging | Declared but never imported; inflates the image and install time |
| `azure-storage-blob` / `rich` runtime auto-install | Runtime | Previously undeclared, causing a `pip install` during first CLI run. Now declared. |

---

## 9. Reproducibility

| Step | Command | Verified |
|---|---|---|
| Fresh environment | `python -m venv .venv` | Yes |
| Install project + dev extras | `pip install -e ".[dev]"` | Yes |
| Import and version | `python -c "import migration_platform; print(migration_platform.__version__)"` | Yes — prints `0.2.0` |
| Unit suite | `pytest tests/unit/` | Yes — 408 passed, 4 known pre-existing failures |
| Wheel build | `python -m build --wheel` | Yes |
| Clean wheel install | install wheel in a fresh venv, then import | Yes |
| Dependency metadata validation | `scripts/release_validation.py` | Yes — `pyproject.toml` is authoritative, `requirements.txt` must be absent, every runtime dependency carries a `>=` floor, duplicates are rejected, and the `dev`/`aws`/`gcp`/`vault` extras are validated |

A release candidate must clear `scripts/release_validation.py`. The release
checklist is §3 of [`../docs/release_process.md`](../docs/release_process.md).

---

## 10. Change history for this document

| Platform version | Document status |
|---|---|
| 0.2.0 | Created. First evidence-based compatibility reference. |