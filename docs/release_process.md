# Release Process

Step-by-step procedure for cutting a Migration Platform release.

The *model* (what each artifact means) is documented in
[`architecture/platform_architecture.md`](architecture/platform_architecture.md)
§4.2. This document covers the *procedure*.

Authoritative documents, one per category:

| Category | Authoritative document |
|---|---|
| Application version source of truth | `pyproject.toml` `[project] version` |
| Release model | `architecture/platform_architecture.md` §4.2 |
| Release history | `../CHANGELOG.md` |
| Version compatibility | [`compatibility_matrix.md`](compatibility_matrix.md) |
| Release validation procedure | this document |
| Dependency policy | [`compatibility_matrix.md`](compatibility_matrix.md) §4 |
| Object support per engine | `<engine>/<ENGINE>_OBJECT_SUPPORT_MATRIX.md` |

---

## 1. Versioning scheme

[Semantic Versioning](https://semver.org/):

| Change | Increment |
|---|---|
| Incompatible API or configuration change | MAJOR |
| Backwards-compatible feature | MINOR |
| Backwards-compatible bug fix | PATCH |

Object-level versioning is deliberately **not** implemented. There is no
`MSSQLViewDefV1`; the object DTOs carry no version field. The report format
carries its own independent `report_version` (`"2.0"`), which is unrelated to
the application version and changes only when the report schema changes.

---

## 2. Release procedure

### Step 1 — Prepare on a release branch

```bash
git switch main
git pull --ff-only
git switch -c version/X.Y.Z
```

### Step 2 — Bump the version

Edit the single `version` field in `pyproject.toml`. Do **not** add a second
version constant anywhere. `migration_platform.__version__` reads installed
metadata and must never be hardcoded.

Runtime dependencies are declared only in `pyproject.toml`. Adding, changing, or
removing one is a single-file edit — no secondary dependency file exists to keep
in sync. `scripts/release_validation.py` verifies the metadata is internally
consistent.

### Step 3 — Write the changelog entry

Add a dated section to `CHANGELOG.md` describing only what actually changed.
Keep `## [Unreleased]` above it for work not yet released. Do not claim CI
green, full test passage, or production readiness unless verified.

### Step 4 — Validate

```bash
pip install -e ".[dev]"
python scripts/release_validation.py
```

Every check must pass. The script compares the unit suite against a recorded
baseline of known pre-existing failures, so it will report a **new** failure if
one appears. Do not delete entries from `KNOWN_BASELINE_FAILURES` to make a run
pass — fix the underlying assertion instead.

### Step 5 — Review and commit

Stage **only** intended files, by explicit path. Never `git add .` or
`git add -A`; the working tree contains untracked personal files that must never
enter a release commit.

```bash
git status --short
git diff --check
git add pyproject.toml CHANGELOG.md <other intended files>
git commit -m "release: vX.Y.Z"
```

### Step 6 — Fast-forward main

```bash
git switch main
git merge --ff-only version/X.Y.Z
```

If `--ff-only` fails, stop. Do not force, do not create a merge commit.

### Step 7 — Tag

Only after `main` points at the release commit:

```bash
git tag -a vX.Y.Z -m "Release vX.Y.Z"
```

Use an **annotated** tag. Verify:

```bash
git show --no-patch --format=fuller vX.Y.Z
git rev-parse main vX.Y.Z^{}    # must resolve to the same commit
```

Tags are immutable. A mistake is corrected by cutting a new patch version, never
by moving or deleting a tag.

### Step 8 — Push

```bash
git push origin main
git push origin vX.Y.Z
```

Never force-push. Never rewrite published release history.

---

## 3. Release validation checklist

`scripts/release_validation.py` automates every item below.

### Packaging

- [ ] `pip install -e ".[dev]"` succeeds
- [ ] `python -c "import migration_platform; print(migration_platform.__version__)"` prints the release version
- [ ] `python -m migration_platform --version` prints `migration-platform <version>`
- [ ] `python -m build --wheel` produces `migration_platform-<version>-*.whl`
- [ ] Wheel contains `core/`, `migration_platform/`, `<dist-info>/`
- [ ] Wheel does **not** contain `config/`, `docker/`, `logs/`, `reports/`
- [ ] Clean-venv wheel install imports all connector packages

### Version consistency

- [ ] `pyproject.toml` version is valid `X.Y.Z`
- [ ] Installed metadata matches `pyproject.toml`
- [ ] `__version__` resolves from metadata, not a hardcoded literal
- [ ] `CHANGELOG.md` has `## [Unreleased]` and `## [<version>]`
- [ ] Tag (after tagging) peels to the release commit

### Runtime prerequisites

- [ ] Startup preflight accepts the running interpreter
- [ ] `requires-python` floor enforced at runtime, not only at install time

### Dependency consistency

- [ ] Every runtime dependency is declared in `pyproject.toml`; no undeclared runtime import
- [ ] No dependency used at runtime is left undeclared
- [ ] `[dev]` extras carry explicit version floors

### Tests

- [ ] Unit suite result matches the recorded baseline
- [ ] No **new** failures versus the baseline
- [ ] Migration validation for affected engines re-run where credentials exist

### Report integrity

- [ ] Generated JSON report carries `platform_version`
- [ ] HTML report footer shows the platform version

---

## 4. Version-specific upgrade procedure

When changing a runtime, dependency, driver, or database engine version, record
the outcome in [`compatibility_matrix.md`](compatibility_matrix.md).

| Change | Procedure |
|---|---|
| **Python version** | Confirm `requires-python` still matches. Run the unit suite on the new interpreter and compare to baseline. Update the Tested column. |
| **Dependency / driver upgrade** | Establish a passing baseline on the current constraint first. Then raise the constraint, reinstall into a clean environment, and re-run the same suite. If results differ, revert and record why. |
| **Database engine minor/major upgrade** | Requires a live instance. Re-run the engine's documented E2E flow and update the engine matrix from the recorded result — never from expectation. |
| **External tool upgrade** | Java, Terraform, and Liquibase are not used by this project. ODBC Driver 18 is checked by `bootstrap.py`; verify with `pyodbc.drivers()` after upgrading. |

**Downgrade testing** is not meaningful for the Python runtime or the
`pyproject.toml` dependencies: the recorded baseline is the newest supported
configuration, and moving below it tests configurations the project does not
claim to support. For drivers and engines, downgrade testing is meaningful only
where a lower version is recorded as TESTED in the compatibility matrix. No
production dependency should be downgraded to "see if it breaks".

---

## 5. Known pre-existing failures at the 0.2.0 baseline

These are recorded so a release is not blocked by unrelated debt, and so a new
failure is still detected. They are **not** to be skipped, weakened, or deleted.

| Test | Cause | Related to versioning? |
|---|---|---|
| `test_cross_engine_type_safety.py::...[MySQLTargetConnector-mysql-INT]` | Asserts unquoted identifiers; connector emits backticks | No |
| `test_cross_engine_type_safety.py::...[MSSQLTargetConnector-mssql-BIGINT]` | Asserts unqualified table name; connector emits `"public"."orders"` | No |
| `test_mysql_datatypes.py::test_mysql_target_has_no_role_capabilities_and_classifies_denials` | Asserts `create_role_if_not_exists` absent; provided by the base class | No |
| `test_mysql_datatypes.py::test_mysql_common_orchestrator_has_no_role_migration_path` | Expects `partial_success`, receives `failed` | No |

Baseline: **408 passed, 4 failed**.

Two further pre-existing conditions are documented but not blocking a version
bump:

- **Lint debt** — `ruff check core/ tests/` reports 444 errors and
  `ruff format --check` would reformat 83 files. Unrelated to versioning.
- **Integration tests cannot pass in CI as written** — all connection settings
  hardcode `host: localhost`, while GitHub Actions service containers are
  reachable at service-label hostnames (`postgres`, `mysql`, `mongodb`). CI
  also defines no MSSQL service, though `tests/integration/test_connectors.py`
  contains `TestMSSQLFullMigration`. Unrelated to versioning.

Because of these, **a green CI run is not currently a release gate**. Use
`scripts/release_validation.py` as the gate instead, and treat CI as advisory
until the integration host configuration is corrected.

---

## 6. Version Management

Version Management adds runtime version capture, report embedding, compatibility
matrix validation, and release validation automation. All Version Management
tasks are complete and validated.

### 6.1 Targeted Version Management test files

Run these four test files when validating Version Management changes or before a
release that modifies version-related code:

| Test file | Purpose | When to run |
|---|---|---|
| `tests/unit/test_runtime_manifest.py` | Validates runtime manifest schema, captured metadata (app version, Python version, package versions, Git branch, DB engine versions), secret protection, and non-fatal DB version failures | Before release; after changes to `core/runtime_manifest.py` or report builder manifest integration |
| `tests/unit/test_report_manifest.py` | Validates manifest is embedded in JSON (`runtime_manifest`, `platform_version`) and HTML reports (footer, accordion), and schema version consistency | Before release; after changes to `core/reporting/report_builder.py` |
| `tests/unit/test_compatibility_matrix.py` | Validates `compatibility_matrix.yaml` structure, required fields, version consistency with `pyproject.toml`, and Markdown generation | Before release; after changes to `compatibility_matrix.yaml` or `scripts/generate_compatibility_matrix.py` |
| `tests/unit/test_release_validation.py` | Validates version format, installed metadata match, CLI `--version`, wheel build, Git tag matching, and KNOWN_BASELINE_FAILURES enforcement | Before release; after changes to `scripts/release_validation.py` |

Run all four together with:
```bash
pytest tests/unit/test_runtime_manifest.py tests/unit/test_report_manifest.py tests/unit/test_compatibility_matrix.py tests/unit/test_release_validation.py -v
```

### 6.2 Release validation: `python scripts/release_validation.py`

This script is the **release gate** (replaces CI). It performs 22+ automated checks:

- **Packaging**: editable install, version import, CLI `--version`, wheel build, wheel contents, clean-venv connector imports
- **Version consistency**: `pyproject.toml` format, installed metadata match, `__version__` from metadata, `CHANGELOG.md` structure, Git tag peel
- **Runtime prerequisites**: preflight interpreter check, `requires-python` enforcement
- **Dependency consistency**: all runtime deps declared, no undeclared imports, dev extras have version floors
- **Tests**: unit suite matches recorded baseline (408 passed, 4 known failures), no new failures, migration validation where credentials exist
- **Report integrity**: JSON carries `platform_version`, HTML footer shows platform version

Run after `pip install -e ".[dev]"` and before committing the release. Every check
must pass. The script compares the unit suite against `KNOWN_BASELINE_FAILURES` —
do not delete entries to force a pass; fix the underlying assertion instead.

### 6.3 Runtime Manifest

**Purpose**: Captures a complete snapshot of the execution environment at
migration runtime for troubleshooting, audit, and reproducibility.

**Captured metadata** (schema version `"1.0"`):
- `application`: `name`, `version` (from installed metadata), `git_branch`
- `python`: `version` (e.g., `"3.12.6"`), `executable` path
- `packages`: key dependency versions (e.g., `pyodbc`, `psycopg2`, `mysql-connector-python`, `sqlalchemy`, `alembic`, `pydantic`, `pyyaml`, `jinja2`)
- `database`: `source` and `target` engine versions (queried from connectors; optional, non-fatal on failure)

**Report integration**:
- JSON report: `runtime_manifest` object + top-level `platform_version`
- HTML report: footer shows platform version; accordion with full manifest (hidden by default)

**Secret protection**: Manifest captures **only versions and identifiers** — no
passwords, connection strings, hostnames, usernames, or secrets. The secrets
resolver in `core/secrets/env.py` is never invoked during manifest collection.

**Troubleshooting use**: When a migration behaves unexpectedly, the manifest in
the report answers: Which platform version ran? Which Python? Which driver
versions? Which database engine versions? What Git commit/branch? This replaces
guesswork with evidence.

### 6.4 Git pre-commit commands for release commits

Use these commands to verify the release commit contains **only intended
changes**. Run in order before `git commit`:

```bash
# 1. Show a concise summary of all working-tree changes (staged + unstaged)
git status --short
```
**What it checks**: Lists modified (`M`), added (`A`), deleted (`D`), renamed
(`R`), untracked (`??`) files with short codes. Verifies no personal/untracked
files will be accidentally staged.

```bash
# 2. Check for whitespace errors in unstaged changes
git diff --check
```
**What it checks**: Detects trailing whitespace, spaces before tabs, and other
whitespace issues in the working tree. Run before staging to catch formatting
problems early.

```bash
# 3. Stage ONLY the intended files by explicit path
git add pyproject.toml CHANGELOG.md <other intended files>
```
**What it checks**: Adds specific files to the index. Never use `git add .` or
`git add -A` — the working tree contains untracked personal files that must
never enter a release commit.

```bash
# 4. Check for whitespace errors in STAGED changes
git diff --cached --check
```
**What it checks**: Same whitespace check as above, but on the **staged** diff
(the exact commit content). Run after `git add` to ensure the commit itself is
clean.

```bash
# 5. Show a summary of staged changes (insertions/deletions per file)
git diff --cached --stat
```
**What it checks**: Confirms the staged changes match expectations — only the
intended files, with reasonable line counts.

```bash
# 6. List ONLY the names of staged files
git diff --cached --name-only
```
**What it checks**: Final verification of exactly which files are staged. Cross-
reference with the intended file set.

```bash
# 7. Show the full staged diff for manual review
git diff --cached
```
**What it checks**: Complete content review of the commit. Verify no secrets,
no accidental deletions, no unrelated changes. This is the last gate before
`git commit`.

**When a developer should run these**: Before every release commit (Step 5 in
the release procedure). For non-release commits, `git status --short` and
`git diff --cached --name-only` are sufficient as a quick sanity check.