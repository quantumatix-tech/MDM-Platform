from __future__ import annotations

import logging
from typing import Any

from core.secrets.base import SecretProvider

logger = logging.getLogger("migration_platform.secrets")


class AzureKeyVaultProvider(SecretProvider):
    def __init__(self, vault_url: str, credential: Any = None) -> None:
        if not vault_url:
            raise ValueError("Azure Key Vault URL is required")
        self._vault_url = vault_url
        self._credential = credential
        self._client: Any = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
        except ImportError:
            logger.warning(
                "azure-keyvault-secrets or azure-identity not installed; "
                "Azure Key Vault secret resolution unavailable"
            )
            return None

        credential = self._credential or DefaultAzureCredential()
        self._client = SecretClient(vault_url=self._vault_url, credential=credential)
        return self._client

    def get_secret(self, name: str) -> str:
        client = self._get_client()
        if client is None:
            raise RuntimeError(
                f"Cannot resolve secret '{name}': Azure Key Vault SDK not available"
            )
        try:
            retrieved = client.get_secret(name)
        except Exception as exc:
            error_type = type(exc).__name__
            status_code = getattr(exc, "status_code", None)
            if error_type == "ResourceNotFoundError":
                message = f"Azure Key Vault secret '{name}' was not found."
            elif error_type in {"ClientAuthenticationError", "CredentialUnavailableError"}:
                message = f"Azure authentication failed while retrieving secret '{name}'."
            elif status_code == 403 or error_type == "ForbiddenError":
                message = (
                    f"Azure Key Vault denied access to secret '{name}'; "
                    "verify the identity has secret get permission."
                )
            else:
                message = (
                    f"Failed to retrieve secret '{name}' from Azure Key Vault; "
                    "verify the vault URL, network access, and secret name."
                )
            logger.error("%s (%s)", message, error_type)
            raise RuntimeError(message) from None
        return retrieved.value
