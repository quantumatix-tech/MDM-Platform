from typing import ClassVar

from core import orchestrator as orchestrator_module
from core.orchestrator import MigrationOrchestrator


class _Target:
    _config: ClassVar[dict[str, str]] = {
        "username": "TARGET_OWNER", "password": "target-password"
    }


def test_executor_password_falls_back_to_local_store_when_global_provider_is_env(monkeypatch):
    monkeypatch.delenv("SECRET_oracle_permission_executor_test", raising=False)

    class Resolver:
        def resolve(self, name):
            if name == "oracle_permission_executor_test":
                return "executor-password-from-local-store"
            raise KeyError(name)

    original = orchestrator_module.create_secret_provider

    def provider(config, override=None):
        if override == "local_encrypted_file":
            return Resolver()
        return original(config, override)

    monkeypatch.setattr(orchestrator_module, "create_secret_provider", provider)
    orchestrator = MigrationOrchestrator(
        object(), _Target(),
        {
            "secrets": {"provider": "env"},
            "target": {"connection": {"username": "TARGET_OWNER"}},
            "oracle_permission_migration": {
                "executor": {
                    "username": "ORACLE_PERMISSION_EXECUTOR",
                    "password_secret": "oracle_permission_executor_test",
                }
            },
        },
    )
    connection = orchestrator._resolve_oracle_permission_executor_connection()
    assert connection["password"] == "executor-password-from-local-store"
    assert connection["username"] == "ORACLE_PERMISSION_EXECUTOR"


def test_executor_local_store_fallback_does_not_replace_non_env_provider(monkeypatch):
    monkeypatch.setattr(orchestrator_module, "create_secret_provider", lambda *_args: None)
    orchestrator = MigrationOrchestrator(
        object(), _Target(),
        {
            "secrets": {"provider": "azure_keyvault"},
            "target": {"connection": {"username": "TARGET_OWNER"}},
            "oracle_permission_migration": {
                "executor": {
                    "username": "ORACLE_PERMISSION_EXECUTOR",
                    "password_secret": "oracle_permission_executor_test",
                }
            },
        },
    )
    try:
        orchestrator._resolve_oracle_permission_executor_connection()
    except RuntimeError as exc:
        assert "secret provider is not configured" in str(exc)
    else:
        raise AssertionError("missing Azure provider should not fall back to local store")
