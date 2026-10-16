from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from tests.e2e.postgresql.run_setup import _connection_params
from tests.e2e.postgresql.setup.database import _resolve_connection_params
from tests.e2e.postgresql.setup.fixture_loader import load_fixture


class _RecordingResolver:
    def __init__(self):
        self.names = []

    def resolve(self, name):
        self.names.append(name)
        return f"resolved:{name}"


def test_connection_params_resolves_configured_secret_through_provider():
    resolver = _RecordingResolver()

    params = _resolve_connection_params(
        host="db.example",
        port=5432,
        username="postgres",
        password_env="postgresql_e2e_source_pass",
        secret_resolver=resolver,
    )

    assert params["password"] == "resolved:postgresql_e2e_source_pass"
    assert resolver.names == ["postgresql_e2e_source_pass"]


def test_setup_resolves_source_and_target_password_secret_names():
    cfg = SimpleNamespace(
        source_connection={
            "host": "source.example",
            "port": 5432,
            "username": "source-user",
            "password_secret": "postgresql_e2e_source_pass",
        },
        target_connection={
            "host": "target.example",
            "port": 5433,
            "username": "target-user",
            "password_secret": "postgresql_e2e_target_pass",
        },
    )
    resolver = _RecordingResolver()

    source = _connection_params(cfg, "source", resolver)
    target = _connection_params(cfg, "target", resolver)

    assert source["password"] == "resolved:postgresql_e2e_source_pass"
    assert target["password"] == "resolved:postgresql_e2e_target_pass"
    assert resolver.names == [
        "postgresql_e2e_source_pass",
        "postgresql_e2e_target_pass",
    ]


def test_fixture_loader_uses_password_resolved_by_setup():
    with patch(
        "tests.e2e.postgresql.setup.fixture_loader.os.path.isfile",
        return_value=True,
    ), patch(
        "tests.e2e.postgresql.setup.fixture_loader.run_sql_file"
    ) as run_sql_file:
        run_sql_file.return_value = SimpleNamespace(ok=True)

        load_fixture(
            fixture_dir="fixtures",
            host="localhost",
            port=5432,
            username="postgres",
            password_env="source-secret",
            database="source-db",
            reset=False,
            password="resolved-password",
        )

    assert run_sql_file.call_args.args[4] == "resolved-password"
