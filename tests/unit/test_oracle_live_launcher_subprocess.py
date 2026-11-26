from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
CONFIG_PATH = REPO_ROOT / "config" / "oracle_e2e_acceptance.yaml"


def _discovery_records() -> str:
    import yaml

    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    profile = raw["e2e"]["onboarding_profile"]
    records = ["CDB|FREE"]
    for side in ("source", "target"):
        container = raw["e2e"]["owner_provisioning"][side]["expected_container_name"]
        service = raw[side]["connection"]["service_name"]
        admin = profile[side]["admin_connection"]["username"]
        owner = raw["e2e"]["resources"][side]["name"]
        tablespace = profile[side]["permanent_tablespace"]
        records.extend(
            [
                f"PDB|{side}|{container}|READ WRITE|{service}",
                f"CTX|{side}|{container}",
                f"ADMIN|{side}|{admin}|OPEN|NO",
                f"OWNER|{side}|{owner}|MISSING|NO|0|",
                f"QUOTA|{side}|{owner}|{tablespace}|MISSING",
                f"OMF|{side}|",
                f"TS|{side}|{tablespace}|PERMANENT|ONLINE|1",
                f"DF|{side}|{tablespace}|offline-datafile-{side}.dbf|AVAILABLE",
            ]
        )
    permission_policy = raw.get("oracle_permission_migration", {})
    executor = permission_policy.get("executor", {}).get("username")
    if permission_policy.get("enabled") is True and executor:
        records.append(f"EXECUTOR|target|{executor}|MISSING|NO")
    records.append("ORACLE_READ_ONLY_DISCOVERY_COMPLETE")
    return "\r\n".join(records) + "\r\n"


def _run_launcher_subprocess(
    tmp_path: Path,
    *,
    failure: bool,
    failure_section: str = "source.admin",
    oracle_error: str = "ORA-00942: table or view does not exist",
) -> subprocess.CompletedProcess[str]:
    output_file = tmp_path / "sqlplus-output.txt"
    fake_sqlplus = tmp_path / ("fake_sqlplus.exe" if os.name == "nt" else "fake_sqlplus")
    fake_sqlplus.write_text("fake executable for injected subprocess boundary", encoding="utf-8")
    if failure:
        stdout = (
            f"ORACLE_INVENTORY_SECTION|{failure_section}\r\n"
            f"{oracle_error}\r\n"
        )
        stderr = "SP2-0734: unknown command beginning\r\n"
        exit_code = oracle_error.split(":", 1)[0].removeprefix("ORA-")
    else:
        stdout = _discovery_records()
        stderr = ""
        exit_code = "0"
    output_file.write_text(stdout + "\n-- STDERR --\n" + stderr, encoding="utf-8")

    harness = f'''
import os, sys
from pathlib import Path
from types import SimpleNamespace

class Cursor:
    def __init__(self, user, service): self.user, self.service = user, service
    def __enter__(self): return self
    def __exit__(self, *_args): return None
    def execute(self, _sql): return None
    def fetchone(self): return (self.user, self.service, self.service, "FREE")

class Connection:
    def __init__(self, user, service): self._cursor = Cursor(user, service)
    def cursor(self): return self._cursor
    def close(self): return None

class Driver:
    @staticmethod
    def makedsn(host, port, service_name): return f"{{host}}:{{port}}/{{service_name}}"
    @staticmethod
    def connect(**kwargs):
        service = kwargs["dsn"].rsplit("/", 1)[-1]
        return Connection(kwargs["user"], service)

sys.modules["oracledb"] = Driver
from tests.e2e.oracle import live_launcher

class Resolver:
    def resolve(self, reference): return "DUMMY_SECRET_SHOULD_NOT_APPEAR"
live_launcher.create_secret_provider = lambda *_args, **_kwargs: Resolver()

result_file = Path({json.dumps(str(output_file))})
contents = result_file.read_text(encoding="utf-8")
stdout, _, stderr = contents.partition("\\n-- STDERR --\\n")
def fake_sqlplus(args, **kwargs):
    assert args[-1] == "/ as sysdba"
    assert kwargs["env"]["ORACLE_SID"] == "FREE"
    assert "ORACLE_READ_ONLY_DISCOVERY_COMPLETE" in kwargs["input"]
    print("FAKE_SQLPLUS_RECEIVED_INPUT")
    return SimpleNamespace(returncode=int(os.environ["FAKE_SQLPLUS_EXIT"]), stdout=stdout, stderr=stderr)

live_launcher.subprocess.run = fake_sqlplus
sys.argv = ["live_launcher.py", "--config", {json.dumps(str(CONFIG_PATH))}, "--sqlplus", {json.dumps(str(fake_sqlplus))}, "--preflight-only"]
try:
    raise SystemExit(live_launcher.main())
except Exception as exc:
    print(f"Oracle LIVE E2E STOPPED: {{exc}}", file=sys.stderr)
    raise SystemExit(1)
'''
    env = os.environ.copy()
    env["ORACLE_SID"] = "FREE"
    env["FAKE_SQLPLUS_EXIT"] = exit_code
    return subprocess.run(
        [sys.executable, "-c", harness],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_exact_acceptance_cli_preflight_passes_through_fake_sqlplus_without_ddl(tmp_path):
    result = _run_launcher_subprocess(tmp_path, failure=False)
    assert result.returncode == 0, result.stderr
    assert "FAKE_SQLPLUS_RECEIVED_INPUT" in result.stdout
    assert "READ-ONLY PREFLIGHT PASSED" in result.stdout
    assert "create_missing_owner" in result.stdout
    assert "DUMMY_SECRET_SHOULD_NOT_APPEAR" not in result.stdout + result.stderr


def test_exact_acceptance_cli_reports_sqlplus_section_error_and_never_approves_or_runs(tmp_path):
    result = _run_launcher_subprocess(tmp_path, failure=True)
    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert "section=source.admin" in output
    assert "SQL*Plus exit=942" in output
    assert "ORA-00942: table or view does not exist" in output
    assert "SP2-0734" in output
    assert "SELECT 'ADMIN|source|'" in output
    assert "DUMMY_SECRET_SHOULD_NOT_APPEAR" not in output
    assert "Approve this exact plan" not in output


def test_exact_acceptance_cli_reports_source_owner_ora_00937_and_generated_scalar_sql(tmp_path):
    result = _run_launcher_subprocess(
        tmp_path,
        failure=True,
        failure_section="source.owner",
        oracle_error="ORA-00937: not a single-group group function",
    )
    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert "section=source.owner" in output
    assert "SQL*Plus exit=937" in output
    assert "ORA-00937: not a single-group group function" in output
    assert "SELECT 'OWNER|source|'" in output
    assert "NVL((SELECT u.account_status" in output
    assert "MAX(" not in output
    assert "DUMMY_SECRET_SHOULD_NOT_APPEAR" not in output
    assert "Approve this exact plan" not in output
