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