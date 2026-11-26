from __future__ import annotations

import ssl
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import oracledb
import pytest
import yaml

from core.connectors.oracle import OracleSourceConnector, OracleTargetConnector
from core.connectors.oracle._models import OracleConnectionError, make_connect_kwargs
from core.orchestrator import MigrationOrchestrator

BASE_CONFIG = {
    "host": "db.example.test",
    "port": 1521,
    "service_name": "APP_PDB",
    "username": "APP_USER",
    "password": "not-a-real-password",
}


def test_oracle_tls_is_opt_in_and_omission_keeps_tcp_connect_shape():
    kwargs = make_connect_kwargs(BASE_CONFIG, oracledb)

    assert kwargs == {
        "user": BASE_CONFIG["username"],
        "password": BASE_CONFIG["password"],
        "dsn": oracledb.makedsn(
            BASE_CONFIG["host"], BASE_CONFIG["port"], service_name=BASE_CONFIG["service_name"]
        ),
    }


@pytest.mark.parametrize(
    ("connector_type", "role"),
    [(OracleSourceConnector, "source"), (OracleTargetConnector, "target")],
)
def test_source_and_target_use_tcps_with_certificate_identity_verification(
    connector_type, role
):
    connector = connector_type({**BASE_CONFIG, "ssl": True})
    with (
        patch("oracledb.connect", return_value=MagicMock()) as connect,
        patch(f"core.connectors.oracle.{role}.audit_log"),
    ):
        connector.connect()

    params = connect.call_args.kwargs["params"]
    assert params.protocol == "tcps"
    assert params.ssl_server_dn_match is True
    assert params.wallet_location == "SYSTEM"
    assert "dsn" not in connect.call_args.kwargs
    assert role in {"source", "target"}


def test_ca_bundle_uses_verified_ssl_context_in_thin_mode(tmp_path):
    ca_file = tmp_path / "oracle-ca.pem"
    ca_file.write_text("test certificate bundle path", encoding="utf-8")
    context = ssl.create_default_context()
    config = {**BASE_CONFIG, "ssl": True, "ssl_ca_cert": str(ca_file)}

    with (
        patch(
            "core.connectors.oracle._models.ssl.create_default_context",
            return_value=context,
        ) as create_context,
        patch("oracledb.is_thin_mode", return_value=True),
    ):
        kwargs = make_connect_kwargs(config, oracledb)

    params = kwargs["params"]
    assert params.protocol == "tcps"
    assert params.ssl_context is context
    assert params.ssl_server_dn_match is True
    assert params.wallet_location is None
    create_context.assert_called_once()
    assert create_context.call_args.kwargs["cafile"] == str(ca_file)


def test_oracle_wallet_options_map_to_supported_driver_params(tmp_path):
    wallet = tmp_path / "wallet"
    wallet.mkdir()
    config = {
        **BASE_CONFIG,
        "ssl": True,
        "ssl_wallet_location": str(wallet),
        "_ssl_wallet_password": "resolved-wallet-secret",
        "ssl_server_cert_dn": "CN=oracle.example.test,O=Example",
        "connection_timeout": 12,
    }

    with patch("oracledb.ConnectParams", wraps=oracledb.ConnectParams) as params_factory:
        params = make_connect_kwargs(config, oracledb)["params"]

    assert params.protocol == "tcps"
    assert params.wallet_location == str(wallet)
    assert params.ssl_server_dn_match is True
    assert params.ssl_server_cert_dn == config["ssl_server_cert_dn"]
    assert params.tcp_connect_timeout == 12
    params_factory.assert_called_once()
    assert params_factory.call_args.kwargs["wallet_password"] == "resolved-wallet-secret"


@pytest.mark.parametrize(
    ("override", "exception_type"),
    [
        ({"ssl": "true"}, TypeError),
        ({"ssl": False, "ssl_ca_cert": "ca.pem"}, ValueError),
        ({"ssl": False, "ssl_server_cert_dn": "CN=db"}, ValueError),
        ({"ssl": True, "ssl_wallet_password_secret": "wallet_pw"}, ValueError),
        ({"ssl": True, "ssl_wallet_password": "pw"}, ValueError),
        ({"ssl": True, "ssl_wallet_location": "missing-wallet-dir"}, ValueError),
        ({"ssl": True, "ssl_server_cert_dn": " "}, ValueError),
    ],
)
def test_invalid_or_incomplete_tls_configuration_fails_closed(override, exception_type):
    with pytest.raises(exception_type):
        make_connect_kwargs({**BASE_CONFIG, **override}, oracledb)


def test_tls_requires_driver_connect_params_support():
    unsupported_driver = SimpleNamespace(ConnectParams=None)

    with pytest.raises(TypeError, match="ConnectParams support"):
        make_connect_kwargs({**BASE_CONFIG, "ssl": True}, unsupported_driver)


def test_ca_bundle_rejects_thick_mode_and_bad_file(tmp_path):
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("placeholder", encoding="utf-8")
    config = {**BASE_CONFIG, "ssl": True, "ssl_ca_cert": str(ca_file)}

    with (
        patch("oracledb.is_thin_mode", return_value=False),
        pytest.raises(ValueError, match="Thin mode"),
    ):
        make_connect_kwargs(config, oracledb)

    with pytest.raises(ValueError, match="CA certificate bundle"):
        make_connect_kwargs(
            {**BASE_CONFIG, "ssl": True, "ssl_ca_cert": str(tmp_path / "missing.pem")},
            oracledb,
        )


def test_wallet_secret_reference_is_resolved_for_each_oracle_endpoint():
    config = {
        "source": {
            "engine": "oracle",
            "connection": {
                **BASE_CONFIG,
                "password_secret": "db_password",
                "ssl_wallet_password_secret": "wallet_password",
            },
        },
        "target": {
            "engine": "oracle",
            "connection": {
                **BASE_CONFIG,
                "password_secret": "db_password",
                "ssl_wallet_password_secret": "wallet_password",
            },
        },
        "secrets": {"provider": "env"},
    }
    source = OracleSourceConnector(config["source"]["connection"])
    target = OracleTargetConnector(config["target"]["connection"])
    orchestrator = MigrationOrchestrator(source, target, config)

    class Resolver:
        def resolve(self, secret_name):
            return {"db_password": "db-secret", "wallet_password": "wallet-secret"}[secret_name]

    orchestrator._resolve_connector_secrets(source, config["source"], Resolver())
    orchestrator._resolve_connector_secrets(target, config["target"], Resolver())

    assert source._config["_ssl_wallet_password"] == "wallet-secret"
    assert target._config["_ssl_wallet_password"] == "wallet-secret"


@pytest.mark.parametrize(
    ("connector_type", "role"),
    [(OracleSourceConnector, "source"), (OracleTargetConnector, "target")],
)
def test_wallet_password_is_not_logged_or_returned_in_connection_errors(
    connector_type, role, tmp_path
):
    wallet = tmp_path / "wallet"
    wallet.mkdir()
    wallet_secret = "private-wallet-password"
    connector = connector_type(
        {
            **BASE_CONFIG,
            "ssl": True,
            "ssl_wallet_location": str(wallet),
            "_ssl_wallet_password": wallet_secret,
        }
    )
    error = oracledb.DatabaseError(
        SimpleNamespace(code=12514, message=f"wallet={wallet_secret}")
    )

    with (
        patch("oracledb.connect", side_effect=error),
        patch(f"core.connectors.oracle.{role}.audit_log") as audit,
        pytest.raises(OracleConnectionError) as raised,
    ):
        connector.connect()

    assert wallet_secret not in str(raised.value)
    assert wallet_secret not in repr(audit.call_args_list)
    assert wallet_secret not in "".join(traceback.format_exception(raised.value))


def test_oracle_tls_fields_are_available_for_both_endpoints():
    schema_path = Path(__file__).parents[2] / "config" / "migration_config.schema.yaml"
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))

    for endpoint in ("source", "target"):
        connection = schema[endpoint]["connection"]
        assert connection["ssl"]["optional"] is True
        for key in (
            "ssl_ca_cert",
            "ssl_wallet_location",
            "ssl_wallet_password_secret",
            "ssl_server_cert_dn",
        ):
            assert connection[key]["optional"] is True
