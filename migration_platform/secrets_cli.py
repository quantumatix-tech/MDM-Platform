"""Manage a LocalEncryptedFileProvider store without exposing secret values."""

from __future__ import annotations

import argparse
import getpass
import sys
import warnings
from collections.abc import Sequence
from pathlib import Path

from core.secrets.local_encrypted_file import (
    LocalEncryptedFileProvider,
    default_key_source,
    default_local_store_path,
    ensure_os_keyring,
)


def _get_keyring():
    return ensure_os_keyring()


def _default_store_path() -> Path:
    """Use the same persistent default as LocalEncryptedFileProvider."""
    return default_local_store_path()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Manage a Fernet-encrypted local secret store. 'set <name>' is the normal "
            "setup command and initializes a missing store automatically."
        )
    )
    parser.add_argument(
        "--file", default=None,
        help="Encrypted store path (default: Windows LocalAppData\\MigrationPlatform\\secrets.enc; otherwise ./secrets.enc).",
    )
    parser.add_argument(
        "--key-source", choices=("env", "keyring"), default=None,
        help="Encryption-key source (default: keyring on Windows, env elsewhere).",
    )
    parser.add_argument("--key-env-var", default="MIGRATION_SECRETS_KEY")
    parser.add_argument("--keyring-service", default="migration-platform/secrets-key")
    parser.set_defaults(_file_explicit=False, _key_source_explicit=False)
    # argparse does not expose whether a defaulted option was explicitly supplied;
    # keep that distinction so bare Windows commands can use persistent defaults
    # while explicit --key-source env remains an override.
    # Defaults are resolved after parsing in main(), so explicit options are
    # preserved exactly and remain distinguishable from platform defaults.
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "init", help="Create an empty store; keyring mode generates and saves a key."
    )
    set_command = commands.add_parser(
        "set", help="Create/update a secret; initializes a missing store and prompts securely."
    )
    set_command.add_argument("name", help="Secret reference name (not the secret value).")
    commands.add_parser("list", help="List secret reference names only.")
    del_command = commands.add_parser("delete", help="Delete a secret after confirmation.")
    del_command.add_argument("name", help="Secret reference name.")
    verify_command = commands.add_parser(
        "verify", help="Verify a secret can be decrypted without displaying it."
    )
    verify_command.add_argument("name", help="Secret reference name.")
    return parser


def _new_store(args: argparse.Namespace) -> LocalEncryptedFileProvider:
    return LocalEncryptedFileProvider(
        file_path=args.file,
        key_source=args.key_source,
        key_env_var=args.key_env_var,
        keyring_service=args.keyring_service,
    )


def _prompt_secret(prompt: str) -> str:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError(
            "Secret entry requires an interactive terminal with hidden input support."
        )
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt)


def _initialize(args: argparse.Namespace) -> None:
    path = Path(args.file)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing store: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)

    if args.key_source == "keyring":
        keyring = _get_keyring()
        key_name = "encryption-key"
        key = keyring.get_password(args.keyring_service, key_name)
        if key is None:
            from cryptography.fernet import Fernet

            key = Fernet.generate_key().decode("ascii")
            try:
                keyring.set_password(args.keyring_service, key_name, key)
                if keyring.get_password(args.keyring_service, key_name) != key:
                    raise RuntimeError("OS keyring did not confirm the saved encryption key.")
            except Exception as exc:
                raise RuntimeError(
                    f"Could not securely initialize the OS keyring ({type(exc).__name__})."
                ) from None

    LocalEncryptedFileProvider(
        file_path=str(path),
        key_source=args.key_source,
        key_env_var=args.key_env_var,
        keyring_service=args.keyring_service,
        auto_create=True,
    )
    print(f"Initialized encrypted store: {path}")
    if args.key_source == "keyring":
        print("Encryption key is stored in the configured OS keyring; it was not displayed.")
    else:
        print(f"Encryption key was read from process environment variable {args.key_env_var}.")


def _run(args: argparse.Namespace) -> None:
    if args.command == "init":
        _initialize(args)
        return

    if args.command == "set" and not Path(args.file).exists():
        _initialize(args)
    elif args.key_source == "keyring":
        _get_keyring()
    store = _new_store(args)
    if args.command == "set":
        first = _prompt_secret("Secret value: ")
        second = _prompt_secret("Confirm secret value: ")
        if not first:
            raise ValueError("Secret value must not be empty.")
        if first != second:
            raise ValueError("Secret values did not match; store was not changed.")
        store.set_secret(args.name, first)
        print(f"Saved secret reference '{args.name}'.")
    elif args.command == "list":
        names = store.list_secrets()
        if names:
            for name in names:
                print(name)
        else:
            print("(no secrets)")
    elif args.command == "delete":
        answer = input(f"Delete secret reference '{args.name}'? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Deletion cancelled.")
            return
        store.delete_secret(args.name)
        print(f"Deleted secret reference '{args.name}'.")
    elif args.command == "verify":
        store.get_secret(args.name)
        print(f"Secret reference '{args.name}' is present and decryptable.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    provided = list(sys.argv[1:] if argv is None else argv)
    has_file_option = any(value == "--file" or value.startswith("--file=") for value in provided)
    has_key_source_option = any(
        value == "--key-source" or value.startswith("--key-source=") for value in provided
    )
    has_key_configuration = has_key_source_option or any(
        value.startswith(("--key-env-var", "--keyring-service"))
        for value in provided
    )
    if args.file is None:
        args.file = str(_default_store_path())
    if args.key_source is None:
        args.key_source = "env" if has_key_configuration or has_file_option else default_key_source()
    try:
        _run(args)
    except (KeyError, FileExistsError, FileNotFoundError, ValueError, RuntimeError, OSError) as exc:
        message = exc.args[0] if exc.args and isinstance(exc.args[0], str) else type(exc).__name__
        print(f"Error: {message}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Do not emit arbitrary backend exception text: keyring drivers may include
        # implementation details, and secrets must never reach the terminal/log.
        print(f"Error: operation failed ({type(exc).__name__}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
