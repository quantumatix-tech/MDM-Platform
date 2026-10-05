"""MySQL fixture loader — applies ordered SQL scripts via ``mysql`` CLI.

Adapted from the PostgreSQL fixture loader (``psql`` → ``mysql``).  The script
ordering, dataclass contracts, and error semantics are preserved so that
Phase A's output structure matches the other engine pipelines.

MySQL differences:
  * ``mysql`` CLI reads the password from ``--password=`` or the
    ``MYSQL_PWD`` environment variable.  We use ``MYSQL_PWD`` to avoid the
    password appearing in the process argument list (the ``--password=``
    form exposes it in ``ps``).
MySQL CLI flags chosen for fixture loading:
  * ``-f`` (force) is intentionally NOT used; we rely on the CLI exit code
    to detect errors.  Each file is applied separately so a failure stops
    the pipeline with a clear ``ScriptResult``.
  * MySQL uses ``;`` as the statement delimiter; ``DELIMITER`` is handled
    natively by the CLI for stored routines.
  * MySQL DDL is implicitly committed (no transactional DDL wrapper needed).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field

# Scripts are applied in this exact numeric order.
# MySQL does not have standalone sequences, custom types, or RLS policies,
# so those fixture scripts are placeholders for structural parity.
FIXTURE_SCRIPTS: list[str] = [
    "01_create_schemas.sql",
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
    ``password_secret: mysql_e2e_source_pass`` resolves to the environment
    variable ``SECRET_mysql_e2e_source_pass``.

    Returns ``None`` when the variable is unset.
    """
    return os.environ.get(f"SECRET_{password_env}")


def _mysql_path() -> str:
    """Return the path to the mysql CLI executable, raising if not found."""
    path = shutil.which("mysql.exe")
    if not path:
        path = shutil.which("mysql")
    if not path:
        raise SystemExit(
            "mysql CLI not found in PATH. Install MySQL client tools "
            "(https://dev.mysql.com/downloads/mysql/)."
        )
    return path


def run_sql_file(
    sql_file: str,
    host: str,
    port: int | str,
    username: str,
    password: str | None,
    database: str,
    timeout_s: int = 60,
) -> ScriptResult:
    """Execute a single .sql file via the mysql CLI.

    The password is passed via the ``MYSQL_PWD`` environment variable so it
    never appears in the process argument list.
    """
    mysql_cli = _mysql_path()
    cmd = [
        mysql_cli,
        "-h", str(host),
        "-P", str(port),
        "-u", username,
        "-D", database,
        "--default-character-set=utf8mb4",
        "-vvv",
        "--batch",
        "--raw",
    ]

    env = os.environ.copy()
    if password is not None:
        env["MYSQL_PWD"] = password

    start = time.time()
    with open(sql_file, encoding="utf-8") as fh:
        sql_content = fh.read()
    try:
        proc = subprocess.run(
            cmd,
            input=sql_content,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            check=False,
        )
        duration = time.time() - start
        ok = proc.returncode == 0
        output = proc.stdout.strip()
        error = proc.stderr.strip() if not ok else ""
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
    finally:
        proc = None


def load_fixture(
    fixture_dir: str,
    host: str,
    port: int | str,
    username: str,
    password_env: str,
    database: str,
    reset: bool = True,
) -> LoadResult:
    """Load all fixture SQL scripts into *database* in order.

    When *reset* is True the reset script (00_reset.sql) is run first.
    Passwords are resolved from the ``SECRET_<name>`` env var.
    """
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
            break

    return result
