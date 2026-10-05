from __future__ import annotations

import logging
from typing import Any

from core.secrets.base import SecretResolver
from core.secrets.env import EnvSecretProvider

logger = logging.getLogger(__name__)


def create_secret_provider(
    config: dict[str, Any], provider_override: str | None = None
) -> SecretResolver | None:
    secrets_config = config.get("secrets", {})
    provider_type = (
        provider_override
        if provider_override is not None
        else secrets_config.get("provider", "env")
    )

    if provider_type == "env":
        return SecretResolver(EnvSecretProvider())

    if provider_type == "azure_keyvault":
        try:
            from core.secrets.azure_keyvault import AzureKeyVaultProvider

            vault_url = secrets_config.get("azure_keyvault", {}).get("url", "")
            if not vault_url:
                raise ValueError("secrets.azure_keyvault.url is required")
            return SecretResolver(AzureKeyVaultProvider(vault_url))
        except Exception as exc:
            logger.error("Failed to construct AzureKeyVaultProvider: %s", exc)
            raise RuntimeError(f"Azure Key Vault provider failed to initialize: {exc}") from exc

    if provider_type == "aws_secrets_manager":
        try:
            from core.secrets.aws_secrets_manager import AWSSecretsManagerProvider

            region = secrets_config.get("aws_secrets_manager", {}).get("region", "us-east-1")
            return SecretResolver(AWSSecretsManagerProvider(region=region))
        except Exception as exc:
            logger.error("Failed to construct AWSSecretsManagerProvider: %s", exc)
            raise RuntimeError(f"AWS Secrets Manager provider failed to initialize: {exc}") from exc

    if provider_type == "gcp_secret_manager":
        try:
            from core.secrets.gcp_secret_manager import GCPSecretManagerProvider

            project_id = secrets_config.get("gcp_secret_manager", {}).get("project_id", "")
            if not project_id:
                raise ValueError("gcp_secret_manager.project_id is required")
            return SecretResolver(GCPSecretManagerProvider(project_id=project_id))
        except Exception as exc:
            logger.error("Failed to construct GCPSecretManagerProvider: %s", exc)
            raise RuntimeError(f"GCP Secret Manager provider failed to initialize: {exc}") from exc

    if provider_type == "hashicorp_vault":
        try:
            from core.secrets.hashicorp_vault import HashiCorpVaultProvider

            vault_config = secrets_config.get("hashicorp_vault", {})
            url = vault_config.get("url", "")
            token = vault_config.get("token", "")
            mount_point = vault_config.get("mount_point", "secret")
            if not url or not token:
                raise ValueError("hashicorp_vault.url and token are required")
            return SecretResolver(HashiCorpVaultProvider(url=url, token=token, mount_point=mount_point))
        except Exception as exc:
            logger.error("Failed to construct HashiCorpVaultProvider: %s", exc)
            raise RuntimeError(f"HashiCorp Vault provider failed to initialize: {exc}") from exc

    if provider_type == "local_encrypted_file":
        try:
            from core.secrets.local_encrypted_file import LocalEncryptedFileProvider

            file_config = secrets_config.get("local_encrypted_file", {})
            return SecretResolver(
                LocalEncryptedFileProvider(
                    file_path=file_config.get("file"),
                    key_source=file_config.get("key_source"),
                    key_env_var=file_config.get("key_env_var", "MIGRATION_SECRETS_KEY"),
                    keyring_service=file_config.get("keyring_service", "migration-platform/secrets-key"),
                    auto_create=file_config.get("auto_create", False),
                )
            )
        except Exception as exc:
            logger.error("Failed to construct LocalEncryptedFileProvider: %s", exc)
            raise RuntimeError(f"Local encrypted file provider failed to initialize: {exc}") from exc

    raise ValueError(f"Unknown secret provider: {provider_type!r}")
