from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from core.retry import retry_with_backoff
from core.secrets.base import SecretProvider


def ensure_os_keyring():
    """Return keyring after preferring Windows Credential Manager on Windows."""
    try:
        import keyring
    except ImportError:
        raise RuntimeError("The keyring package is required for key_source=keyring.") from None

    if os.name == "nt":
        try:
            from keyring.backends.Windows import WinVaultKeyring
        except ImportError:
            raise RuntimeError(
                "Windows Credential Manager support is unavailable in the installed "
                "keyring package."
            ) from None
        backend = keyring.get_keyring()
        if not isinstance(backend, WinVaultKeyring):
            try:
                backend = WinVaultKeyring()
                keyring.set_keyring(backend)
            except Exception as exc:
                raise RuntimeError(
                    "Could not select Windows Credential Manager "
                    f"({type(exc).__name__})."
                ) from None

    if getattr(keyring.get_keyring(), "priority", 0) <= 0:
        raise RuntimeError("No usable OS keyring backend is available.")
    return keyring


def default_local_store_path() -> Path:
    """Return the shared CLI/provider default path for the Local Store."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if _is_windows():
        if local_app_data:
            return Path(local_app_data) / "MigrationPlatform" / "secrets.enc"
        return Path.home() / "AppData" / "Local" / "MigrationPlatform" / "secrets.enc"
    return Path("secrets.enc")


def default_key_source() -> str:
    """Use the OS keyring by default on Windows and env mode elsewhere."""
    return "keyring" if _is_windows() else "env"


def _is_windows() -> bool:
    return os.name == "nt"


class LocalEncryptedFileProvider(SecretProvider):
    def __init__(
        self,
        file_path: str | None = None,
        key_source: str | None = None,
        key_env_var: str = "MIGRATION_SECRETS_KEY",
        keyring_service: str = "migration-platform/secrets-key",
        auto_create: bool = False,
    ) -> None:
        self._file_path = str(default_local_store_path() if file_path is None else file_path)
        # Supplying a custom path without a key source preserves the historical
        # env-key default. An omitted path selects the platform Local Store defaults.
        self._key_source = (
            ("env" if file_path is not None else default_key_source())
            if key_source is None
            else key_source
        )
        self._key_env_var = key_env_var
        self._keyring_service = keyring_service
        self._auto_create = auto_create
        self._key = self._load_key()
        self._secrets: dict[str, str] = self._load_secrets()

    def _load_key(self) -> bytes:
        if self._key_source == "keyring":
            try:
                keyring = ensure_os_keyring()
                key = keyring.get_password(self._keyring_service, "encryption-key")
            except Exception as exc:
                raise RuntimeError(
                    "keyring lookup failed for service "
                    f"'{self._keyring_service}' ({type(exc).__name__})."
                ) from None
            if key is None:
                raise RuntimeError(
                    f"Encryption key not found in keyring service '{self._keyring_service}'. "
                    f"Store the key first or switch key_source to 'env'."
                )
        else:
            key = os.environ.get(self._key_env_var)
            if key is None:
                raise RuntimeError(
                    f"Encryption key not found in environment variable {self._key_env_var}."
                )

        key_bytes = key.encode("utf-8")
        return key_bytes

    def _get_fernet(self) -> Any:
        from cryptography.fernet import Fernet

        return Fernet(self._key)

    def _load_secrets(self) -> dict[str, str]:
        if not os.path.exists(self._file_path):
            if self._auto_create:
                self._save_secrets({})
                return {}
            return {}
        from cryptography.fernet import InvalidToken

        try:
            with open(self._file_path, "rb") as f:
                encrypted = f.read()
            if not encrypted:
                return {}
            fernet = self._get_fernet()
            decrypted = fernet.decrypt(encrypted)
            secrets = json.loads(decrypted.decode("utf-8"))
            if not isinstance(secrets, dict) or not all(
                isinstance(name, str) and isinstance(value, str)
                for name, value in secrets.items()
            ):
                raise ValueError("Encrypted secret store has an invalid format.")
            return secrets
        except InvalidToken:
            raise ValueError(
                "Cannot decrypt the local secret store; verify the encryption key and file."
            ) from None
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_secrets(self, secrets: dict[str, str]) -> None:
        fernet = self._get_fernet()
        plaintext = json.dumps(secrets).encode("utf-8")
        encrypted = fernet.encrypt(plaintext)
        directory = os.path.dirname(os.path.abspath(self._file_path))
        os.makedirs(directory, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(prefix=".secrets-", suffix=".tmp", dir=directory)
        try:
            if os.name != "nt":
                os.chmod(temporary_path, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(encrypted)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary_path, self._file_path)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _validate_name(name: str) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Secret name must be a non-empty string.")

    def list_secrets(self) -> list[str]:
        """Return sorted secret names without exposing any secret values."""
        return sorted(self._secrets)

    def set_secret(self, name: str, value: str) -> None:
        """Create or replace a secret and persist the encrypted store atomically."""
        self._validate_name(name)
        if not isinstance(value, str):
            raise TypeError("Secret value must be a string.")
        updated = dict(self._secrets)
        updated[name] = value
        self._save_secrets(updated)
        self._secrets = updated

    def delete_secret(self, name: str) -> None:
        """Delete a named secret and persist the encrypted store atomically."""
        self._validate_name(name)
        if name not in self._secrets:
            raise KeyError(f"Secret '{name}' not found in encrypted file '{self._file_path}'")
        updated = dict(self._secrets)
        del updated[name]
        self._save_secrets(updated)
        self._secrets = updated

    @retry_with_backoff(max_retries=3, base_delay=0.1)
    def get_secret(self, name: str) -> str:
        if name not in self._secrets:
            raise KeyError(f"Secret '{name}' not found in encrypted file '{self._file_path}'")
        return self._secrets[name]
