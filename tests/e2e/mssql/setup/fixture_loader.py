from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field

# Scripts are applied in this exact numeric order.
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
    "11_create_synonyms.sql",
    "12_create_partitions.sql",
    "13_create_security.sql",
    "14_apply_extended_props.sql",
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


def _resolve_password(password_env: str) -> str:
    """Read the password from the SECRET_<name> environment variable.

    Mirrors the platform's EnvSecretProvider convention: a config value of
    ``password_secret: mssql_e2e_source_pass`` resolves to the environment
    variable ``SECRET_mssql_e2e_source_pass``.
    """
    pw = os.environ.get(f"SECRET_{password_env}")
    if not pw:
        raise SystemExit(
            f"Environment variable {password_env} is not set.\n"
            "Set it before running the E2E fixture:\n"
            f"  $env:{password_env} = '<password>'  # PowerShell\n"
            f"  export {password_env}=<password>    # bash"
        )
    return pw


def _sqlcmd_path() -> str:
    """Return the path to sqlcmd, raising if not found."""
    path = shutil.which("sqlcmd")
    if not path:
        raise SystemExit(
            "sqlcmd not found in PATH. Install the Microsoft ODBC Driver and "
            "sqlcmd command-line tools."
        )
    return path


def run_sql_file(
    sql_file: str,
    host: str,
    port: int | str,
    username: str,
    password: str,
    database: str,
    trust_server_cert: bool = True,
    timeout_s: int = 30,
) -> ScriptResult:
    """Execute a single .sql file via sqlcmd.

    sqlcmd handles GO batch separators natively.
    """
    import time

    sqlcmd = _sqlcmd_path()
    cmd = [
        sqlcmd,
        "-S", f"{host},{port}",
        "-U", username,
        "-P", password,
        "-d", database,
        "-i", sql_file,
        "-b",  # batch mode — stop on error
        "-r1",  # route errors to stderr
    ]
    if trust_server_cert:
        cmd.append("-C")

    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
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


def load_fixture(
    fixture_dir: str,
    host: str,
    port: int | str,
    username: str,
    password_env: str,
    database: str,
    trust_server_cert: bool = True,
    reset: bool = True,
) -> LoadResult:
    """Load all fixture SQL scripts into *database* in order.

    When *reset* is True the reset script (00_reset.sql) is run first.
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
            trust_server_cert=trust_server_cert,
        )
        result.results.append(sr)
        if not sr.ok:
            # Stop immediately — later scripts may depend on earlier ones.
            break

    return result
