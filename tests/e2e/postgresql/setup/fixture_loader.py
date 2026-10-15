"""PostgreSQL fixture loader — applies ordered SQL scripts via ``psql``.

Adapted from the MSSQL fixture loader (``sqlcmd`` → ``psql``).  The script
ordering, dataclass contracts, and error semantics are preserved so that
Phase A's output structure matches the MSSQL E2E experience.

PostgreSQL differences:
  * ``psql`` reads the password from the ``PGPASSWORD`` environment variable
    (or ``.pgpass``).  We inject it into the subprocess environment rather than
    passing it on the command line, so it never appears in the process
    argument list.
  * ``-v ON_ERROR_STOP=1`` makes psql stop on the first error and return a
    non-zero exit code — the PostgreSQL equivalent of sqlcmd's ``-b`` flag.
  * PostgreSQL has no ``GO`` batch separator; statements are delimited by
    ``;`` and may appear on the same line or across lines within a file.
  * ``-1`` wraps each file in a transaction so a partial script is rolled back
    atomically.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field

_IS_WINDOWS = os.name == "nt"

# Scripts are applied in this exact numeric order.
# Mirrors the MSSQL fixture loader so the two pipelines stay structurally
# aligned; PostgreSQL substitutes native equivalents where the dialect
# differs (e.g. 02 = enum types, 08 = functions, 09 = procedures,
# 12 = range partitioned tables, 14 = comments instead of extended props).
FIXTURE_SCRIPTS: list[str] = [
    "01_create_schemas.sql",
    "02_create_types.sql",
    "03_create_tables.sql",
    "04_create_foreign_keys.sql",
    "05_create_indexes.sql",
    "06_create_sequences.sql",
    "07_create_views.sql",
    "08_create_functions.sql",
    "09_create_procedures.sql",
    "10_create_triggers.sql",
    "12_create_partitions.sql",
    "13_create_security.sql",
    "14_create_comments.sql",
    "15_seed_data.sql",
]

RESET_SCRIPT = "00_reset.sql"


@dataclass
class ScriptResult:
    script: str
    ok: bool
    output: str = ""
    error: str = ""
    duration_s: float = 0.0


@dataclass
class LoadResult:
    fixture_dir: str
    database: str
    results: list[ScriptResult] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def failed_scripts(self) -> list[str]:
        return [r.script for r in self.results if not r.ok]


def _resolve_password(password_env: str) -> str | None:
    """Read the password from the ``SECRET_<name>`` environment variable.

    Mirrors the platform's EnvSecretProvider convention: a config value of
    ``password_secret: postgresql_e2e_source_pass`` resolves to the environment
    variable ``SECRET_postgresql_e2e_source_pass``.

    Returns ``None`` when the variable is unset, so that PostgreSQL instances
    using ``trust`` authentication can connect without a password.
    """
    return os.environ.get(f"SECRET_{password_env}")


def _psql_path() -> str:
    """Resolve a native psql executable from PSQL_PATH or PATH.

    Wrapper scripts are deliberately rejected: they can redirect connections
    or hide the real psql process exit code.
    """
    configured_path = os.environ.get("PSQL_PATH", "").strip()
    if configured_path:
        if not _is_native_psql(configured_path):
            raise SystemExit(
                f"PSQL_PATH must point to an existing, launchable native psql "
                f"executable; got {configured_path!r}. Windows .cmd/.bat "
                "wrappers are not supported."
            )
        return os.path.abspath(configured_path)

    command = "psql.exe" if _IS_WINDOWS else "psql"
    path = shutil.which(command)
    if path and _is_native_psql(path):
        return path

    detail = f"PATH resolved {path!r}, which is not a native executable" if path else ""
    raise SystemExit(
        "PostgreSQL client executable psql was not found. "
        "Install PostgreSQL client tools and add psql to PATH, or set PSQL_PATH "
        "to the native psql executable. "
        f"{detail}"
    )


def _is_native_psql(path: str) -> bool:
    """Return whether *path* is a launchable psql binary, not a wrapper."""
    suffix = os.path.splitext(path)[1].lower()
    if suffix in {".cmd", ".bat"}:
        return False
    if _IS_WINDOWS and suffix != ".exe":
        return False
    return os.path.isfile(path) and os.access(path, os.X_OK)


def run_sql_file(
    sql_file: str,
    host: str,
    port: int | str,
    username: str,
    password: str | None,
    database: str,
    timeout_s: int = 60,
) -> ScriptResult:
    """Execute a single .sql file via psql.

    ``ON_ERROR_STOP=1`` causes psql to abort on the first SQL error and
    return a non-zero exit code.  ``-1`` wraps the file in a transaction.
    """
    psql = _psql_path()
    cmd = [
        psql,
        "-X",  # ignore system/user startup files
        "-w",  # never prompt interactively for a password
        "-h", str(host),
        "-p", str(port),
        "-U", username,
        "-d", database,
        "-v", "ON_ERROR_STOP=1",
        "-1",  # single transaction per file
        "-f", sql_file,
        "--quiet",  # suppress startup messages
    ]

    env = os.environ.copy()
    if password is not None:
        env["PGPASSWORD"] = password

    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            check=False,
        )
        duration = time.time() - start
        ok = proc.returncode == 0
        output = proc.stdout.strip()
        error = proc.stderr.strip()
        return ScriptResult(
            script=os.path.basename(sql_file),
            ok=ok,
            output=output,
            error=error,
            duration_s=duration,
        )
    except subprocess.TimeoutExpired:
        return ScriptResult(
            script=os.path.basename(sql_file),
            ok=False,
            output="",
            error=f"Timed out after {timeout_s}s",
            duration_s=timeout_s,
        )


def load_fixture(
    fixture_dir: str,
    host: str,
    port: int | str,
    username: str,
    password_env: str,
    database: str,
    reset: bool = True,
    password: str | None = None,
) -> LoadResult:
    """Load all fixture SQL scripts into *database* in order.

    When *reset* is True the reset script (00_reset.sql) is run first.
    Passwords are resolved from the ``SECRET_<name>`` env var.
    """
    if password is None:
        password = _resolve_password(password_env)
    result = LoadResult(fixture_dir=fixture_dir, database=database)

    scripts = FIXTURE_SCRIPTS
    if reset and os.path.isfile(os.path.join(fixture_dir, RESET_SCRIPT)):
        scripts = [RESET_SCRIPT] + scripts

    for script_name in scripts:
        script_path = os.path.join(fixture_dir, script_name)
        if not os.path.isfile(script_path):
            result.results.append(ScriptResult(
                script=script_name,
                ok=False,
                error=f"File not found: {script_path}",
            ))
            continue
        sr = run_sql_file(
            script_path, host, port, username, password, database,
        )
        result.results.append(sr)
        if not sr.ok:
            # Stop immediately — later scripts may depend on earlier ones.
            break

    return result
