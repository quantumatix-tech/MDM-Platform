from __future__ import annotations

import socket
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import oracledb
import pytest
import yaml

from core.connectors.oracle import OracleSourceConnector, OracleTargetConnector
from core.connectors.oracle._models import OracleConnectionError
from core.orchestrator import MigrationOrchestrator
from migration_platform.__main__ import SOURCE_CONNECTORS, TARGET_CONNECTORS
from tests.unit.oracle_test_settings import resolve_oracle_test_connection


def _driver_error_with_cause(message, cause):
    error = oracledb.OperationalError(SimpleNamespace(code=None, message=message))
    try:
        raise error from cause
    except oracledb.Error as wrapped:
        return wrapped


def test_oracle_connectors_are_registered_as_source_and_target_only():
    assert SOURCE_CONNECTORS["oracle"] is OracleSourceConnector
    assert TARGET_CONNECTORS["oracle"] is OracleTargetConnector


def test_oracle_engine_and_service_options_are_in_configuration_schema():
    schema_path = Path(__file__).parents[2] / "config" / "migration_config.schema.yaml"
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))

    assert "oracle" in schema["source"]["engine"]["enum"]
    assert "oracle" in schema["target"]["engine"]["enum"]
    for endpoint in ("source", "target"):
        connection = schema[endpoint]["connection"]
        assert connection["service_name"]["optional"] is True
        assert "password_secret" in connection


@pytest.mark.parametrize(
    ("connector_type", "role"),
    [(OracleSourceConnector, "source"), (OracleTargetConnector, "target")],
)
def test_connect_uses_configured_host_service_and_credentials(
    connector_type, role, oracle_test_config, oracle_test_secrets
):
    conn = MagicMock()
    config = resolve_oracle_test_connection(oracle_test_config, role)
    connector = connector_type(config)
    dsn = oracledb.makedsn(
        config["host"], config["port"], service_name=config["service_name"]
    )

    with patch("oracledb.connect", return_value=conn) as connect, patch(
        f"core.connectors.oracle.{role}.audit_log"
    ) as audit:
        connector.connect()

    connect.assert_called_once_with(
        user=config["username"], password=oracle_test_secrets[role], dsn=dsn
    )
    assert connector._conn is conn
    assert oracle_test_secrets[role] not in repr(audit.call_args_list)


@pytest.mark.parametrize(
    ("connector_type", "role"),
    [(OracleSourceConnector, "source"), (OracleTargetConnector, "target")],
)
def test_invalid_credentials_propagate_without_retry_or_secret_logging(
    connector_type, role, oracle_test_config, oracle_test_secrets
):
    secret = oracle_test_secrets[role]
    error = oracledb.DatabaseError(
        SimpleNamespace(code=1017, message=f"ORA-01017: invalid login; {secret}")
    )
    config = {**resolve_oracle_test_connection(oracle_test_config, role),
              "connection_retry_delay": 0}
    connector = connector_type(config)

    with (
        patch("oracledb.connect", side_effect=error) as connect,
        patch(f"core.connectors.oracle.{role}.audit_log") as audit,
        patch(f"core.connectors.oracle.{role}.time.sleep") as sleep,
        pytest.raises(OracleConnectionError) as raised,
    ):
        connector.connect()

    assert "Oracle authentication failed (ORA-01017)" in str(raised.value)
    assert "Standard FULL migration does not create schema owners" in str(raised.value)
    assert raised.value.__cause__ is None
    connect.assert_called_once()
    sleep.assert_not_called()
    assert secret not in repr(audit.call_args_list)
    assert secret not in "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize(
    ("connector_type", "role"),
    [(OracleSourceConnector, "source"), (OracleTargetConnector, "target")],
)
def test_unreachable_database_retries_transient_errors_then_propagates(
    connector_type, role, oracle_test_config, oracle_test_secrets
):
    error = oracledb.DatabaseError(
        SimpleNamespace(code=12541, message="ORA-12541: no listener")
    )
    connector = connector_type(
        {
            **resolve_oracle_test_connection(oracle_test_config, role),
            "connection_max_retries": 2,
            "connection_retry_delay": 0,
        }
    )

    with (
        patch("oracledb.connect", side_effect=error) as connect,
        patch(f"core.connectors.oracle.{role}.audit_log") as audit,
        patch(f"core.connectors.oracle.{role}.time.sleep") as sleep,
        pytest.raises(OracleConnectionError, match="ORA-12541") as raised,
    ):
        connector.connect()

    assert raised.value.__cause__ is None
    assert connect.call_count == 3
    assert sleep.call_count == 2
    assert [call.kwargs["status"] for call in audit.call_args_list] == [
        "retrying",
        "retrying",
        "failed",
    ]


def test_oracle_source_and_target_resolve_secrets_independently(
    oracle_test_config, oracle_test_secrets
):
    config = oracle_test_config
    source = OracleSourceConnector(config["source"]["connection"])
    target = OracleTargetConnector(config["target"]["connection"])
    orchestrator = MigrationOrchestrator(source, target, config)
    assert orchestrator._source_secret_resolver is not orchestrator._target_secret_resolver

    orchestrator._resolve_connector_secrets(
        source, config["source"], orchestrator._source_secret_resolver
    )
    orchestrator._resolve_connector_secrets(
        target, config["target"], orchestrator._target_secret_resolver
    )

    assert source._config["password"] == oracle_test_secrets["source"]
    assert target._config["password"] == oracle_test_secrets["target"]
    assert source._config["password"] != target._config["password"]


def test_secret_resolution_noops_for_connector_without_private_config():
    orchestrator = object.__new__(MigrationOrchestrator)
    orchestrator._secret_resolver = None
    orchestrator._redaction_secrets = set()
    connector = SimpleNamespace()

    orchestrator._resolve_connector_secrets(
        connector,
        {"engine": "mysql", "connection": {}},
        resolver=MagicMock(),
    )

    assert orchestrator._redaction_secrets == set()


def test_secret_resolution_fails_clearly_when_connector_cannot_receive_secret():
    orchestrator = object.__new__(MigrationOrchestrator)
    orchestrator._secret_resolver = None
    orchestrator._redaction_secrets = set()

    with pytest.raises(TypeError, match="cannot receive configured secret"):
        orchestrator._resolve_connector_secrets(
            SimpleNamespace(),
            {"engine": "mysql", "connection": {"password_secret": "db_password"}},
            resolver=MagicMock(),
        )


@pytest.mark.parametrize(
    ("connector_type", "role"),
    [(OracleSourceConnector, "source"), (OracleTargetConnector, "target")],
)
@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"host": ""}, "non-empty host"),
        ({"service_name": ""}, "non-empty service_name"),
        ({"username": ""}, "non-empty username"),
        ({"password": ""}, "requires a password"),
        ({"port": "not-a-port"}, "port must be an integer"),
        ({"port": 70000}, "port must be between"),
        ({"connection_timeout": "bad"}, "connection_timeout must be a positive integer"),
    ],
)
def test_invalid_connection_configuration_fails_before_driver_call(
    connector_type, role, override, message, oracle_test_config, oracle_test_secrets
):
    config = {**resolve_oracle_test_connection(oracle_test_config, role), **override}
    connector = connector_type(config)
    with patch("oracledb.connect") as connect, pytest.raises(ValueError, match=message):
        connector.connect()
    connect.assert_not_called()


@pytest.mark.parametrize(
    ("connector_type", "role"),
    [(OracleSourceConnector, "source"), (OracleTargetConnector, "target")],
)
@pytest.mark.parametrize(
    "cause_factory",
    [lambda: TimeoutError("timed out"), lambda: ConnectionRefusedError("connection refused")],
    ids=["tcp-timeout", "connection-refused"],
)
def test_dpy6005_with_transient_socket_cause_retries_exactly_as_configured(
    connector_type, role, cause_factory, oracle_test_config, oracle_test_secrets
):
    secret = oracle_test_secrets[role]
    error = _driver_error_with_cause(
        f"DPY-6005: cannot connect; password={secret}", cause_factory()
    )
    config = {
        **resolve_oracle_test_connection(oracle_test_config, role),
        "connection_max_retries": 2,
        "connection_retry_delay": 0,
    }
    connector = connector_type(config)
    with (
        patch("oracledb.connect", side_effect=error) as connect,
        patch(f"core.connectors.oracle.{role}.audit_log") as audit,
        patch(f"core.connectors.oracle.{role}.time.sleep") as sleep,
        pytest.raises(OracleConnectionError, match="DPY-6005") as raised,
    ):
        connector.connect()

    assert connect.call_count == 3
    assert sleep.call_count == 2
    assert [call.kwargs["status"] for call in audit.call_args_list] == [
        "retrying", "retrying", "failed",
    ]
    assert str(raised.value) == "Oracle network connection failed (DPY-6005)"
    assert secret not in str(raised.value)
    assert secret not in repr(audit.call_args_list)


@pytest.mark.parametrize(
    ("connector_type", "role"),
    [(OracleSourceConnector, "source"), (OracleTargetConnector, "target")],
)
def test_dpy6005_with_zero_configured_retries_attempts_once(
    connector_type, role, oracle_test_config, oracle_test_secrets
):
    error = _driver_error_with_cause("DPY-6005: timed out", TimeoutError("timed out"))
    config = {
        **resolve_oracle_test_connection(oracle_test_config, role),
        "connection_max_retries": 0,
        "connection_retry_delay": 0,
    }
    connector = connector_type(config)
    with (
        patch("oracledb.connect", side_effect=error) as connect,
        patch(f"core.connectors.oracle.{role}.audit_log") as audit,
        patch(f"core.connectors.oracle.{role}.time.sleep") as sleep,
        pytest.raises(OracleConnectionError),
    ):
        connector.connect()

    connect.assert_called_once()
    sleep.assert_not_called()
    assert [call.kwargs["status"] for call in audit.call_args_list] == ["failed"]


@pytest.mark.parametrize(
    ("connector_type", "role"),
    [(OracleSourceConnector, "source"), (OracleTargetConnector, "target")],
)
def test_dpy6005_retries_then_recovers_successfully(
    connector_type, role, oracle_test_config, oracle_test_secrets
):
    error = _driver_error_with_cause("DPY-6005: timed out", TimeoutError("timed out"))
    conn = MagicMock()
    config = {
        **resolve_oracle_test_connection(oracle_test_config, role),
        "connection_max_retries": 2,
        "connection_retry_delay": 0,
    }
    connector = connector_type(config)
    with (
        patch("oracledb.connect", side_effect=[error, conn]) as connect,
        patch(f"core.connectors.oracle.{role}.audit_log") as audit,
        patch(f"core.connectors.oracle.{role}.time.sleep") as sleep,
    ):
        connector.connect()

    assert connector._conn is conn
    assert connect.call_count == 2
    sleep.assert_called_once_with(0)
    assert [call.kwargs["status"] for call in audit.call_args_list] == [
        "retrying", "success",
    ]


@pytest.mark.parametrize(
    ("connector_type", "role"),
    [(OracleSourceConnector, "source"), (OracleTargetConnector, "target")],
)
@pytest.mark.parametrize(
    ("code", "message", "safe_message"),
    [
        (
            1017,
            "ORA-01017: invalid login",
            "Oracle authentication failed (ORA-01017). Verify the configured account exists, is unlocked, and its password secret and service are correct. Standard FULL migration does not create schema owners; use the explicit Oracle E2E onboarding workflow after DBA-approved provisioning configuration.",
        ),
        (
            28000,
            "ORA-28000: account locked",
            "Oracle authentication failed (ORA-28000); verify the configured account is unlocked",
        ),
        (
            12514,
            "ORA-12514: listener does not know service",
            "Oracle service is unavailable or unknown (ORA-12514)",
        ),
        (12545, "ORA-12545: invalid target address", "Oracle connection failed (ORA-12545)"),
    ],
)
def test_nontransient_ora_errors_do_not_retry(
    connector_type, role, code, message, safe_message, oracle_test_config,
    oracle_test_secrets,
):
    error = oracledb.DatabaseError(SimpleNamespace(code=code, message=message))
    config = {
        **resolve_oracle_test_connection(oracle_test_config, role),
        "connection_max_retries": 2,
        "connection_retry_delay": 0,
    }
    connector = connector_type(config)
    with (
        patch("oracledb.connect", side_effect=error) as connect,
        patch(f"core.connectors.oracle.{role}.audit_log") as audit,
        patch(f"core.connectors.oracle.{role}.time.sleep") as sleep,
        pytest.raises(OracleConnectionError, match="ORA-") as raised,
    ):
        connector.connect()

    connect.assert_called_once()
    sleep.assert_not_called()
    assert [call.kwargs["status"] for call in audit.call_args_list] == ["failed"]
    assert str(raised.value) == safe_message


@pytest.mark.parametrize(
    ("connector_type", "role"),
    [(OracleSourceConnector, "source"), (OracleTargetConnector, "target")],
)
@pytest.mark.parametrize(
    ("code", "message", "safe_message"),
    [
        (
            1017,
            "ORA-01017: invalid login; password=hidden",
            "Oracle authentication failed (ORA-01017). Verify the configured account exists, is unlocked, and its password secret and service are correct. Standard FULL migration does not create schema owners; use the explicit Oracle E2E onboarding workflow after DBA-approved provisioning configuration.",
        ),
        (
            28000,
            "ORA-28000: account locked; password=hidden",
            "Oracle authentication failed (ORA-28000); verify the configured account is unlocked",
        ),
        (
            12514,
            "ORA-12514: unknown service; password=hidden",
            "Oracle service is unavailable or unknown (ORA-12514)",
        ),
    ],
)
def test_dpy6005_with_permanent_ora_cause_fails_closed_without_retry(
    connector_type, role, code, message, safe_message, oracle_test_config,
    oracle_test_secrets,
):
    cause = oracledb.DatabaseError(SimpleNamespace(code=code, message=message))
    error = _driver_error_with_cause(
        "DPY-6005: cannot connect; timed out", cause
    )
    config = {
        **resolve_oracle_test_connection(oracle_test_config, role),
        "connection_max_retries": 2,
        "connection_retry_delay": 0,
    }
    connector = connector_type(config)
    with (
        patch("oracledb.connect", side_effect=error) as connect,
        patch(f"core.connectors.oracle.{role}.audit_log") as audit,
        patch(f"core.connectors.oracle.{role}.time.sleep") as sleep,
        pytest.raises(OracleConnectionError) as raised,
    ):
        connector.connect()

    connect.assert_called_once()
    sleep.assert_not_called()
    assert [call.kwargs["status"] for call in audit.call_args_list] == ["failed"]
    assert str(raised.value) == safe_message
    assert "hidden" not in repr(audit.call_args_list)


@pytest.mark.parametrize(
    ("connector_type", "role"),
    [(OracleSourceConnector, "source"), (OracleTargetConnector, "target")],
)
def test_dpy6005_with_configuration_cause_does_not_retry(
    connector_type, role, oracle_test_config, oracle_test_secrets
):
    error = _driver_error_with_cause(
        "DPY-6005: timed out", ValueError("invalid endpoint configuration")
    )
    config = {
        **resolve_oracle_test_connection(oracle_test_config, role),
        "connection_max_retries": 2,
        "connection_retry_delay": 0,
    }
    connector = connector_type(config)
    with (
        patch("oracledb.connect", side_effect=error) as connect,
        patch(f"core.connectors.oracle.{role}.audit_log") as audit,
        patch(f"core.connectors.oracle.{role}.time.sleep") as sleep,
        pytest.raises(OracleConnectionError),
    ):
        connector.connect()

    connect.assert_called_once()
    sleep.assert_not_called()
    assert [call.kwargs["status"] for call in audit.call_args_list] == ["failed"]


@pytest.mark.parametrize(
    ("connector_type", "role"),
    [(OracleSourceConnector, "source"), (OracleTargetConnector, "target")],
)
@pytest.mark.parametrize(
    ("message", "expected_calls", "expected_retries"),
    [
        ("DPY-6005: cannot connect; timed out", 3, 2),
        ("DPY-6005: connection could not be established", 1, 0),
    ],
    ids=["explicit-timeout", "unknown-cause-fails-closed"],
)
def test_dpy6005_without_cause_requires_explicit_transient_reason(
    connector_type, role, message, expected_calls, expected_retries,
    oracle_test_config, oracle_test_secrets,
):
    error = _driver_error_with_cause(message, None)
    config = {
        **resolve_oracle_test_connection(oracle_test_config, role),
        "connection_max_retries": 2,
        "connection_retry_delay": 0,
    }
    connector = connector_type(config)
    with (
        patch("oracledb.connect", side_effect=error) as connect,
        patch(f"core.connectors.oracle.{role}.audit_log") as audit,
        patch(f"core.connectors.oracle.{role}.time.sleep") as sleep,
        pytest.raises(OracleConnectionError),
    ):
        connector.connect()

    assert connect.call_count == expected_calls
    assert sleep.call_count == expected_retries
    assert (
        sum(call.kwargs["status"] == "retrying" for call in audit.call_args_list)
        == expected_retries
    )
    assert all(oracle_test_secrets[role] not in repr(call) for call in audit.call_args_list)


@pytest.mark.parametrize(
    ("connector_type", "role"),
    [(OracleSourceConnector, "source"), (OracleTargetConnector, "target")],
)
@pytest.mark.parametrize(
    ("cause_factory", "expected_calls", "expected_retries"),
    [
        (
            lambda: socket.gaierror(socket.EAI_AGAIN, "temporary name resolution failure"),
            3,
            2,
        ),
        (
            lambda: socket.gaierror(socket.EAI_NONAME, "name or service not known"),
            1,
            0,
        ),
    ],
    ids=["temporary-dns", "invalid-hostname"],
)
def test_ora12545_retries_only_with_proven_temporary_dns_cause(
    connector_type, role, cause_factory, expected_calls, expected_retries,
    oracle_test_config, oracle_test_secrets,
):
    error = _driver_error_with_cause(
        "DPY-6005: ORA-12545: cannot resolve configured target",
        cause_factory(),
    )
    config = {
        **resolve_oracle_test_connection(oracle_test_config, role),
        "connection_max_retries": 2,
        "connection_retry_delay": 0,
    }
    connector = connector_type(config)
    with (
        patch("oracledb.connect", side_effect=error) as connect,
        patch(f"core.connectors.oracle.{role}.audit_log") as audit,
        patch(f"core.connectors.oracle.{role}.time.sleep") as sleep,
        pytest.raises(OracleConnectionError, match="ORA-12545"),
    ):
        connector.connect()

    assert connect.call_count == expected_calls
    assert sleep.call_count == expected_retries
    assert (
        sum(call.kwargs["status"] == "retrying" for call in audit.call_args_list)
        == expected_retries
    )
    assert all(oracle_test_secrets[role] not in repr(call) for call in audit.call_args_list)


def test_orchestrator_error_report_and_audit_do_not_contain_secret(
    oracle_test_config, oracle_test_secrets, capsys
):
    config = oracle_test_config
    source = OracleSourceConnector(config["source"]["connection"])
    target = OracleTargetConnector(config["target"]["connection"])
    orchestrator = MigrationOrchestrator(source, target, config)
    error = oracledb.DatabaseError(
        SimpleNamespace(
            code=1017,
            message=f"ORA-01017 authentication failed {oracle_test_secrets['source']}",
        )
    )

    with (
        patch("oracledb.connect", side_effect=error),
        patch("core.connectors.oracle.source.audit_log") as source_audit,
        patch("core.connectors.oracle.target.audit_log") as target_audit,
        patch("core.orchestrator.audit_log") as run_audit,
    ):
        result = orchestrator.run_full()

    assert result["status"] == "failed"
    assert "Oracle authentication failed (ORA-01017)" in result["error"]
    assert "Standard FULL migration does not create schema owners" in result["error"]
    audit_records = repr(
        source_audit.call_args_list
        + target_audit.call_args_list
        + run_audit.call_args_list
    )
    captured = capsys.readouterr()
    output = captured.out + captured.err
    serialized_result = repr(result)
    for secret in oracle_test_secrets.values():
        assert secret not in audit_records
        assert secret not in serialized_result
        assert secret not in output


@pytest.mark.parametrize("connector_type", [OracleSourceConnector, OracleTargetConnector])
def test_close_rolls_back_closes_and_clears_connection_even_if_rollback_fails(
    connector_type, oracle_test_config
):
    connector = connector_type(oracle_test_config["source"]["connection"])
    conn = MagicMock()
    conn.rollback.side_effect = RuntimeError("rollback failed")
    connector._conn = conn

    connector.close()
    connector.close()

    conn.rollback.assert_called_once_with()
    conn.close.assert_called_once_with()
    assert connector._conn is None


@pytest.mark.parametrize("connector_type", [OracleSourceConnector, OracleTargetConnector])
def test_close_without_connection_is_safe(connector_type, oracle_test_config):
    connector = connector_type(oracle_test_config["source"]["connection"])
    connector.close()
    assert connector._conn is None
