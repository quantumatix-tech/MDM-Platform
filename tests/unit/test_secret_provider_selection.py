from __future__ import annotations

import logging

import pytest

from core.orchestrator import MigrationOrchestrator
from core.secrets.azure_keyvault import AzureKeyVaultProvider
from core.secrets.factory import create_secret_provider


def _orchestrator(config):
    return MigrationOrchestrator(object(), object(), config)


def test_global_env_provider_remains_default_for_source_and_target(monkeypatch):
    monkeypatch.setenv("SECRET_src", "source-value")
    monkeypatch.setenv("SECRET_dst", "target-value")
    orchestrator = _orchestrator({"secrets": {"provider": "env"}})

    assert orchestrator._source_secret_resolver is orchestrator._target_secret_resolver
    assert orchestrator._source_secret_resolver.resolve("src") == "source-value"
    assert orchestrator._target_secret_resolver.resolve("dst") == "target-value"


def test_global_azure_provider_remains_default_for_source_and_target(monkeypatch):
    class FakeAzureProvider:
        def __init__(self, vault_url):
            pass

        def get_secret(self, name):
            return f"azure:{name}"

    monkeypatch.setattr("core.secrets.azure_keyvault.AzureKeyVaultProvider", FakeAzureProvider)
    orchestrator = _orchestrator(
        {"secrets": {"provider": "azure_keyvault", "azure_keyvault": {"url": "https://vault/"}}}
    )

    assert orchestrator._source_secret_resolver is orchestrator._target_secret_resolver
    assert orchestrator._source_secret_resolver.resolve("src") == "azure:src"
    assert orchestrator._target_secret_resolver.resolve("dst") == "azure:dst"


def test_local_store_source_and_azure_target_use_independent_resolvers(monkeypatch):
    class FakeLocalProvider:
        def __init__(self, **kwargs):
            pass

        def get_secret(self, name):
            assert name == "src"
            return "local-source-value"

    class FakeAzureProvider:
        def __init__(self, vault_url):
            assert vault_url == "https://vault.example/"

        def get_secret(self, name):
            assert name == "dst"
            return "azure-target-value"

    monkeypatch.setattr(
        "core.secrets.local_encrypted_file.LocalEncryptedFileProvider", FakeLocalProvider
    )
    monkeypatch.setattr("core.secrets.azure_keyvault.AzureKeyVaultProvider", FakeAzureProvider)
    config = {
        "secrets": {
            "provider": "env",
            "local_encrypted_file": {},
            "azure_keyvault": {"url": "https://vault.example/"},
        },
        "source": {"secret_provider": "local_encrypted_file"},
        "target": {"secret_provider": "azure_keyvault"},
    }

    orchestrator = _orchestrator(config)

    assert orchestrator._secret_resolver is None
    assert orchestrator._source_secret_resolver is not orchestrator._target_secret_resolver
    source = type("Connector", (), {"_config": {}})()
    target = type("Connector", (), {"_config": {}})()
    orchestrator._resolve_connector_secrets(
        source, {"connection": {"password_secret": "src"}}, orchestrator._source_secret_resolver
    )
    orchestrator._resolve_connector_secrets(
        target, {"connection": {"password_secret": "dst"}}, orchestrator._target_secret_resolver
    )
    assert source._config["password"] == "local-source-value"
    assert target._config["password"] == "azure-target-value"


def test_env_source_can_override_global_azure_default(monkeypatch):
    class FakeAzureProvider:
        def __init__(self, vault_url):
            pass

        def get_secret(self, name):
            return f"azure:{name}"

    monkeypatch.setattr("core.secrets.azure_keyvault.AzureKeyVaultProvider", FakeAzureProvider)
    monkeypatch.setenv("SECRET_src", "env-source-value")
    config = {
        "secrets": {"provider": "azure_keyvault", "azure_keyvault": {"url": "https://vault/"}},
        "source": {"secret_provider": "env"},
    }
    orchestrator = _orchestrator(config)

    assert orchestrator._source_secret_resolver.resolve("src") == "env-source-value"
    assert orchestrator._target_secret_resolver.resolve("dst") == "azure:dst"


def test_azure_missing_url_is_actionable_and_does_not_construct_provider():
    with pytest.raises(RuntimeError, match="secrets.azure_keyvault.url is required"):
        create_secret_provider({"secrets": {"provider": "azure_keyvault"}})


def test_unknown_endpoint_provider_is_rejected():
    with pytest.raises(ValueError, match="Unknown secret provider"):
        create_secret_provider({"secrets": {}}, "not-a-provider")


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (type("ResourceNotFoundError", (Exception,), {"__str__": lambda self: "SECRET_VALUE"})(), "was not found"),
        (type("ClientAuthenticationError", (Exception,), {"__str__": lambda self: "SECRET_VALUE"})(), "authentication failed"),
        (type("ForbiddenError", (Exception,), {"status_code": 403, "__str__": lambda self: "SECRET_VALUE"})(), "denied access"),
        (Exception("SECRET_VALUE"), "Failed to retrieve secret"),
    ],
)
def test_azure_failures_are_sanitized(exception, expected, caplog):
    class FakeClient:
        def get_secret(self, name):
            raise exception

    provider = AzureKeyVaultProvider("https://vault.example/")
    provider._client = FakeClient()
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError) as raised:
        provider.get_secret("db-password")

    assert expected in str(raised.value)
    assert "SECRET_VALUE" not in str(raised.value)
    assert "SECRET_VALUE" not in caplog.text
    assert raised.value.__cause__ is None
