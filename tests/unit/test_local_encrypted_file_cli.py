from __future__ import annotations

import logging

import pytest
from cryptography.fernet import Fernet

from core.secrets.local_encrypted_file import LocalEncryptedFileProvider
from core.secrets.factory import create_secret_provider
from migration_platform import secrets_cli


def test_local_store_crud_persists_across_reopen(tmp_path, monkeypatch):
    path = tmp_path / "secrets.enc"
    monkeypatch.setenv("TEST_LOCAL_STORE_KEY", Fernet.generate_key().decode("ascii"))
    store = LocalEncryptedFileProvider(
        file_path=str(path), key_env_var="TEST_LOCAL_STORE_KEY", auto_create=True
    )

    store.set_secret("mysql-source-password", "sentinel-source-password")
    store.set_secret("other-secret", "sentinel-other-value")
    assert store.list_secrets() == ["mysql-source-password", "other-secret"]
    assert LocalEncryptedFileProvider(
        file_path=str(path), key_env_var="TEST_LOCAL_STORE_KEY"
    ).get_secret("mysql-source-password") == "sentinel-source-password"

    store.set_secret("mysql-source-password", "sentinel-updated-password")
    reopened = LocalEncryptedFileProvider(file_path=str(path), key_env_var="TEST_LOCAL_STORE_KEY")
    assert reopened.get_secret("mysql-source-password") == "sentinel-updated-password"

    reopened.delete_secret("other-secret")
    final = LocalEncryptedFileProvider(file_path=str(path), key_env_var="TEST_LOCAL_STORE_KEY")
    assert final.list_secrets() == ["mysql-source-password"]
    with pytest.raises(KeyError, match="other-secret"):
        final.get_secret("other-secret")


def test_local_store_rejects_incorrect_encryption_key(tmp_path, monkeypatch):
    path = tmp_path / "secrets.enc"
    correct_key = Fernet.generate_key().decode("ascii")
    wrong_key = Fernet.generate_key().decode("ascii")
    path.write_bytes(Fernet(correct_key.encode("ascii")).encrypt(b'{"db":"password-value"}'))
    monkeypatch.setenv("TEST_WRONG_LOCAL_STORE_KEY", wrong_key)
    with pytest.raises(ValueError, match="verify the encryption key") as raised:
        LocalEncryptedFileProvider(file_path=str(path), key_env_var="TEST_WRONG_LOCAL_STORE_KEY")
    assert "password-value" not in str(raised.value)
    assert wrong_key not in str(raised.value)


def test_existing_fernet_json_store_remains_readable(tmp_path, monkeypatch):
    key = Fernet.generate_key()
    path = tmp_path / "existing.enc"
    path.write_bytes(Fernet(key).encrypt(b'{"existing-name":"existing-value"}'))
    monkeypatch.setenv("EXISTING_LOCAL_STORE_KEY", key.decode("ascii"))

    store = LocalEncryptedFileProvider(
        file_path=str(path), key_env_var="EXISTING_LOCAL_STORE_KEY"
    )

    assert store.list_secrets() == ["existing-name"]
    assert store.get_secret("existing-name") == "existing-value"


def test_cli_keyring_init_set_list_verify_delete_and_reopen(tmp_path, monkeypatch, capsys):
    import keyring

    keyring_values: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        keyring,
        "get_password",
        lambda service, username: keyring_values.get((service, username)),
    )
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda service, username, value: keyring_values.__setitem__((service, username), value),
    )
    monkeypatch.setattr(secrets_cli, "_get_keyring", lambda: keyring)
    path = tmp_path / "nested" / "secrets.enc"

    answers = iter(["sentinel-cli-password", "sentinel-cli-password"])
    monkeypatch.setattr(secrets_cli, "_prompt_secret", lambda _prompt: next(answers))
    assert secrets_cli.main(
        ["--file", str(path), "--key-source", "keyring", "set", "mysql-source-password"]
    ) == 0
    set_output = capsys.readouterr().out
    key = keyring_values[("migration-platform/secrets-key", "encryption-key")]
    assert "sentinel-cli-password" not in set_output
    assert key not in set_output

    assert secrets_cli.main(
        ["--file", str(path), "--key-source", "keyring", "list"]
    ) == 0
    assert capsys.readouterr().out.strip() == "mysql-source-password"
    assert secrets_cli.main(
        ["--file", str(path), "--key-source", "keyring", "verify", "mysql-source-password"]
    ) == 0
    assert "present and decryptable" in capsys.readouterr().out

    # A fresh provider instance exercises the same keyring lookup used after
    # closing and reopening PowerShell.
    assert LocalEncryptedFileProvider(
        file_path=str(path), key_source="keyring"
    ).get_secret("mysql-source-password") == "sentinel-cli-password"

    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert secrets_cli.main(
        ["--file", str(path), "--key-source", "keyring", "delete", "mysql-source-password"]
    ) == 0
    assert "Deleted secret reference" in capsys.readouterr().out
    assert LocalEncryptedFileProvider(
        file_path=str(path), key_source="keyring"
    ).list_secrets() == []


def test_cli_no_options_uses_windows_local_store_and_keyring(tmp_path, monkeypatch, capsys):
    import keyring
    import core.secrets.local_encrypted_file as local_store

    keyring_values: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keyring, "get_password", lambda service, username: keyring_values.get((service, username)))
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda service, username, value: keyring_values.__setitem__((service, username), value),
    )
    monkeypatch.setattr(secrets_cli, "_get_keyring", lambda: keyring)
    monkeypatch.setattr(local_store, "_is_windows", lambda: True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(secrets_cli, "_prompt_secret", lambda _prompt: "windows-default-value")

    assert secrets_cli.main(["set", "mysql-source-password"]) == 0
    output = capsys.readouterr().out
    store_path = tmp_path / "MigrationPlatform" / "secrets.enc"
    assert store_path.exists()
    assert "windows-default-value" not in output
    assert keyring_values[("migration-platform/secrets-key", "encryption-key")]
    assert LocalEncryptedFileProvider(
        file_path=str(store_path), key_source="keyring"
    ).get_secret("mysql-source-password") == "windows-default-value"
    migration_provider = create_secret_provider({
        "secrets": {"provider": "local_encrypted_file", "local_encrypted_file": {}}
    })
    assert migration_provider.resolve("mysql-source-password") == "windows-default-value"


def test_local_store_windows_default_path_and_explicit_custom_file(tmp_path, monkeypatch):
    import core.secrets.local_encrypted_file as local_store

    monkeypatch.setattr(local_store, "_is_windows", lambda: True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    expected = tmp_path / "profile" / "MigrationPlatform" / "secrets.enc"
    assert local_store.default_local_store_path() == expected

    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("CUSTOM_STORE_KEY", key)
    custom = tmp_path / "custom" / "chosen.enc"
    provider = LocalEncryptedFileProvider(
        file_path=str(custom), key_source="env", key_env_var="CUSTOM_STORE_KEY", auto_create=True
    )
    assert provider._file_path == str(custom)
    provider.set_secret("custom-name", "test-only-value")
    assert LocalEncryptedFileProvider(
        file_path=str(custom), key_source="env", key_env_var="CUSTOM_STORE_KEY"
    ).get_secret("custom-name") == "test-only-value"


def test_cli_set_initializes_env_store_and_updates_existing_secret(tmp_path, monkeypatch, capsys):
    path = tmp_path / "auto-created.enc"
    monkeypatch.setenv("TEST_AUTO_STORE_KEY", Fernet.generate_key().decode("ascii"))
    answers = iter(["first-password", "first-password", "updated-password", "updated-password"])
    monkeypatch.setattr(secrets_cli, "_prompt_secret", lambda _prompt: next(answers))

    options = ["--file", str(path), "--key-env-var", "TEST_AUTO_STORE_KEY"]
    assert secrets_cli.main([*options, "set", "db-password"]) == 0
    assert path.exists()
    assert "first-password" not in capsys.readouterr().out
    assert secrets_cli.main([*options, "set", "db-password"]) == 0
    assert "updated-password" not in capsys.readouterr().out
    assert LocalEncryptedFileProvider(
        file_path=str(path), key_env_var="TEST_AUTO_STORE_KEY"
    ).get_secret("db-password") == "updated-password"


def test_cli_set_rejects_noninteractive_input_before_creating_store(tmp_path, monkeypatch, capsys):
    path = tmp_path / "noninteractive.enc"
    monkeypatch.setenv("TEST_AUTO_STORE_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(secrets_cli.sys.stdin, "isatty", lambda: False)

    assert secrets_cli.main([
        "--file", str(path), "--key-env-var", "TEST_AUTO_STORE_KEY", "set", "db"
    ]) == 1
    assert "interactive terminal" in capsys.readouterr().err
    assert path.exists()  # Initialized, but no value could be entered or written.


def test_cli_mismatched_secret_confirmation_does_not_write_value(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TEST_LOCAL_STORE_KEY", Fernet.generate_key().decode("ascii"))
    path = tmp_path / "secrets.enc"
    assert secrets_cli.main(
        ["--file", str(path), "--key-env-var", "TEST_LOCAL_STORE_KEY", "init"]
    ) == 0
    capsys.readouterr()
    answers = iter(["sentinel-first-password", "sentinel-second-password"])
    monkeypatch.setattr(secrets_cli, "_prompt_secret", lambda _prompt: next(answers))

    assert secrets_cli.main(
        ["--file", str(path), "--key-env-var", "TEST_LOCAL_STORE_KEY", "set", "db"]
    ) == 1
    output = capsys.readouterr()
    assert "did not match" in output.err
    assert "sentinel-first-password" not in output.out + output.err
    assert "sentinel-second-password" not in output.out + output.err
    assert LocalEncryptedFileProvider(
        file_path=str(path), key_env_var="TEST_LOCAL_STORE_KEY"
    ).list_secrets() == []


def test_cli_output_and_encrypted_file_do_not_contain_secret_or_key(
    tmp_path, monkeypatch, capsys, caplog
):
    secret = "sentinel-never-print-this-password"
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("TEST_LOCAL_STORE_KEY", key)
    path = tmp_path / "secrets.enc"
    assert secrets_cli.main(
        ["--file", str(path), "--key-env-var", "TEST_LOCAL_STORE_KEY", "init"]
    ) == 0
    capsys.readouterr()
    monkeypatch.setattr(secrets_cli, "_prompt_secret", lambda _prompt: secret)

    with caplog.at_level(logging.DEBUG):
        assert secrets_cli.main(
            ["--file", str(path), "--key-env-var", "TEST_LOCAL_STORE_KEY", "set", "db"]
        ) == 0
        output = capsys.readouterr().out + caplog.text

    ciphertext = path.read_bytes()
    assert secret not in output
    assert key not in output
    assert secret.encode() not in ciphertext
    assert key.encode() not in ciphertext
