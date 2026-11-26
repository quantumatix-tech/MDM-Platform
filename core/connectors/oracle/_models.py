"""Connection argument and Oracle error helpers."""

from __future__ import annotations

import errno
import re
import socket
import ssl
from contextlib import suppress
from pathlib import Path
from typing import Any

# ORA codes associated with temporary reachability, listener, or connection loss.
# Authentication and configuration errors (for example ORA-01017/ORA-12514)
# are intentionally excluded.
TRANSIENT_ORACLE_ERROR_CODES = frozenset(
    {12170, 12537, 12541, 12543, 12547, 3113, 3114, 3135}
)
# ORA-12545 commonly indicates a bad host/address, but a temporary DNS failure
# can produce the same code. Retry it only when the underlying cause proves the
# resolution failure was transient.
AMBIGUOUS_NETWORK_ORACLE_ERROR_CODES = frozenset({12545})


def make_connect_kwargs(config: dict[str, Any], driver: Any) -> dict[str, Any]:
    """Build python-oracledb kwargs, enabling TCPS only when explicitly requested.

    Oracle deliberately treats an omitted ``ssl`` option as legacy TCP. This is
    different from connectors whose established defaults enable TLS, and
    avoids silently switching existing Oracle installations to a TCPS listener.
    """
    host = config.get("host")
    service_name = config.get("service_name")
    username = config.get("username")
    password = config.get("password")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("Oracle connection requires a non-empty host")
    if not isinstance(service_name, str) or not service_name.strip():
        raise ValueError("Oracle connection requires a non-empty service_name")
    if not isinstance(username, str) or not username.strip():
        raise ValueError("Oracle connection requires a non-empty username")
    if not isinstance(password, str) or not password:
        raise ValueError("Oracle connection requires a password (resolve password_secret first)")

    try:
        port = int(config.get("port", 1521))
    except (TypeError, ValueError) as exc:
        raise ValueError("Oracle port must be an integer between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Oracle port must be between 1 and 65535")

    ssl_enabled = config.get("ssl", False)
    if not isinstance(ssl_enabled, bool):
        raise TypeError("Oracle ssl must be a boolean")

    ca_cert = _optional_tls_path(config, "ssl_ca_cert")
    wallet_location = _optional_tls_path(config, "ssl_wallet_location")
    wallet_password = config.get("_ssl_wallet_password")
    if config.get("ssl_wallet_password") is not None:
        if wallet_location is None:
            raise ValueError("Oracle wallet password requires ssl_wallet_location")
        raise ValueError(
            "Oracle wallet password must be supplied through ssl_wallet_password_secret"
        )
    if "ssl_wallet_password_secret" in config and wallet_password is None:
        raise ValueError("Oracle ssl_wallet_password_secret was not resolved")
    cert_dn = config.get("ssl_server_cert_dn")
    if cert_dn is not None and (not isinstance(cert_dn, str) or not cert_dn.strip()):
        raise ValueError("Oracle ssl_server_cert_dn must be a non-empty string")
    if wallet_password is not None and (
        not isinstance(wallet_password, str) or not wallet_password
    ):
        raise ValueError("Oracle wallet password must be resolved to a non-empty string")
    if wallet_password is not None and wallet_location is None:
        raise ValueError("Oracle ssl_wallet_password_secret requires ssl_wallet_location")
    if not ssl_enabled and any((ca_cert, wallet_location, wallet_password, cert_dn)):
        raise ValueError("Oracle TLS options require ssl: true")

    kwargs: dict[str, Any] = {"user": username, "password": password}
    timeout = config.get("connection_timeout")
    if timeout is not None:
        try:
            timeout = int(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("Oracle connection_timeout must be a positive integer") from exc
        if timeout < 1:
            raise ValueError("Oracle connection_timeout must be a positive integer")
    if not ssl_enabled:
        kwargs["dsn"] = driver.makedsn(
            host.strip(), port, service_name=service_name.strip()
        )
        if timeout is not None:
            kwargs["tcp_connect_timeout"] = timeout
        return kwargs

    if ca_cert is not None:
        is_thin_mode = getattr(driver, "is_thin_mode", None)
        if not callable(is_thin_mode) or not is_thin_mode():
            raise ValueError("Oracle ssl_ca_cert requires python-oracledb Thin mode")
        try:
            ssl_context = ssl.create_default_context(
                purpose=ssl.Purpose.SERVER_AUTH,
                cafile=ca_cert,
            )
        except (OSError, ssl.SSLError, ValueError):
            raise ValueError("Oracle ssl_ca_cert is not a readable CA certificate bundle") from None
    else:
        ssl_context = None

    if wallet_location is not None and not Path(wallet_location).is_dir():
        raise ValueError("Oracle ssl_wallet_location must be an existing directory")

    params_kwargs: dict[str, Any] = {
        "host": host.strip(),
        "port": port,
        "service_name": service_name.strip(),
        "protocol": "tcps",
        "ssl_server_dn_match": True,
    }
    if timeout is not None:
        params_kwargs["tcp_connect_timeout"] = timeout
    if cert_dn is not None:
        params_kwargs["ssl_server_cert_dn"] = cert_dn.strip()
    if wallet_location is not None:
        params_kwargs["wallet_location"] = wallet_location
        if wallet_password is not None:
            params_kwargs["wallet_password"] = wallet_password
    elif ca_cert is None:
        # Explicitly use the OS trust store when no Oracle wallet or custom CA
        # file was provided. This keeps certificate-chain verification enabled.
        params_kwargs["wallet_location"] = "SYSTEM"
    if ssl_context is not None:
        params_kwargs["ssl_context"] = ssl_context

    connect_params = getattr(driver, "ConnectParams", None)
    if not callable(connect_params):
        raise TypeError("Oracle TLS requires python-oracledb ConnectParams support")
    try:
        kwargs["params"] = connect_params(**params_kwargs)
    except (TypeError, ValueError):
        # Driver parameter errors may include configuration strings; keep them
        # out of reports and audit logs.
        raise ValueError("Oracle TLS configuration is not supported by this driver") from None
    return kwargs


def _optional_tls_path(config: dict[str, Any], key: str) -> str | None:
    value = config.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Oracle {key} must be a non-empty path")
    return value.strip()


def validate_oracle_identifier(value: object, kind: str = "object") -> str:
    """Accept catalog-returned Oracle identifiers, including quoted-name contents."""
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 128:
        raise ValueError(f"Invalid Oracle {kind} identifier")
    return value


def validate_oracle_virtual_expression(expression: object, column_names: set[str]) -> str:
    """Accept only same-table arithmetic expressions over ordinary columns.

    This deliberately excludes function calls, string/date literals, qualified
    names, comments, and other Oracle expression features until they have an
    explicit migration contract.
    """
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("Oracle virtual column has no readable expression")
    text = expression.strip()
    tokens = re.findall(
        r'\s*("(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$#]*|'
        r'(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|[()+*/-])',
        text,
    )
    if not tokens:
        raise ValueError("Oracle virtual column expression is unsupported")
    # Tokenization must consume all non-whitespace characters.
    compact = re.sub(r"\s+", "", text)
    if re.sub(r"\s+", "", "".join(tokens)) != compact:
        raise ValueError("Oracle virtual column expression is unsupported")
    def is_identifier(token: str) -> bool:
        return token.startswith('"') or bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$#]*", token))

    exact_columns = set(column_names)
    unquoted_columns = {name.upper() for name in column_names}
    for token in tokens:
        if is_identifier(token):
            quoted = token.startswith('"')
            name = token[1:-1].replace('""', '"') if quoted else token
            supported = name in exact_columns if quoted else name.upper() in unquoted_columns
            if not supported:
                raise ValueError(
                    f"Oracle virtual column expression references unsupported column {name!r}"
                )
    # Validate the limited arithmetic grammar without evaluating SQL.
    position = 0
    def atom() -> None:
        nonlocal position
        if position < len(tokens) and tokens[position] in {"+", "-"}:
            position += 1
            atom()
        elif position < len(tokens) and tokens[position] == "(":
            position += 1
            expr()
            if position >= len(tokens) or tokens[position] != ")":
                raise ValueError("Oracle virtual column expression is unsupported")
            position += 1
        elif position < len(tokens) and (
            is_identifier(tokens[position])
            or tokens[position][0].isdigit() or tokens[position].startswith(".")
        ):
            position += 1
        else:
            raise ValueError("Oracle virtual column expression is unsupported")
    def term() -> None:
        nonlocal position
        atom()
        while position < len(tokens) and tokens[position] in {"*", "/"}:
            position += 1
            atom()
    def expr() -> None:
        nonlocal position
        term()
        while position < len(tokens) and tokens[position] in {"+", "-"}:
            position += 1
            term()
    expr()
    if position != len(tokens):
        raise ValueError("Oracle virtual column expression is unsupported")
    return text


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """Return an exception and its Python causes/contexts without looping."""
    pending = [exc]
    result: list[BaseException] = []
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        for nested in (current.__cause__, current.__context__):
            if isinstance(nested, BaseException):
                pending.append(nested)
        pending.extend(
            arg for arg in getattr(current, "args", ())
            if isinstance(arg, BaseException)
        )
    return result


def _oracle_error_codes(exc: BaseException) -> list[int]:
    """Collect ORA codes from driver arguments and the complete cause chain."""
    codes: list[int] = []
    for current in _exception_chain(exc):
        texts = [str(current)]
        for arg in getattr(current, "args", ()):
            code = getattr(arg, "code", None)
            if isinstance(code, int) and code > 0 and code not in codes:
                codes.append(code)
            message = getattr(arg, "message", None)
            if isinstance(message, str):
                texts.append(message)
            elif isinstance(arg, str):
                texts.append(arg)
        for text in texts:
            for match in re.finditer(r"ORA-(\d{5})", text, re.IGNORECASE):
                code = int(match.group(1))
                if code not in codes:
                    codes.append(code)
    return codes


def oracle_error_code(exc: BaseException) -> int | None:
    """Extract the first numeric ORA code from a driver error or its causes."""
    codes = _oracle_error_codes(exc)
    return codes[0] if codes else None


def _is_transient_network_cause(exc: BaseException) -> bool:
    """Recognize concrete OS-level reachability failures behind DPY-6005."""
    if isinstance(exc, socket.gaierror):
        return getattr(exc, "errno", None) == getattr(socket, "EAI_AGAIN", object())
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionRefusedError,
                        ConnectionResetError, ConnectionAbortedError)):
        return True
    if isinstance(exc, OSError):
        transient_errnos = {
            errno.ETIMEDOUT,
            errno.ECONNREFUSED,
            errno.ECONNRESET,
            errno.ECONNABORTED,
            errno.ENETUNREACH,
            errno.EHOSTUNREACH,
        }
        transient_winerrors = {10051, 10053, 10054, 10060, 10061, 10065}
        return (
            getattr(exc, "errno", None) in transient_errnos
            or getattr(exc, "winerror", None) in transient_winerrors
        )
    return False


def is_transient_connection_error(exc: BaseException) -> bool:
    codes = _oracle_error_codes(exc)
    if codes:
        # A permanent ORA code anywhere in a DPY wrapper's cause chain wins.
        # Treat unknown ORA codes as non-transient rather than retrying them.
        allowed_codes = (
            TRANSIENT_ORACLE_ERROR_CODES
            | AMBIGUOUS_NETWORK_ORACLE_ERROR_CODES
        )
        if not all(code in allowed_codes for code in codes):
            return False
        if any(code in AMBIGUOUS_NETWORK_ORACLE_ERROR_CODES for code in codes):
            causes = _exception_chain(exc)[1:]
            if any(isinstance(item, (ValueError, TypeError, KeyError)) for item in causes):
                return False
            return any(_is_transient_network_cause(item) for item in causes)
        return True

    chain = _exception_chain(exc)
    messages = [str(item) for item in chain]
    for item in chain:
        for arg in getattr(item, "args", ()):
            message = getattr(arg, "message", None)
            if isinstance(message, str):
                messages.append(message)
            elif isinstance(arg, str):
                messages.append(arg)
    if not any(re.search(r"\bDPY-6005\b", text, re.IGNORECASE) for text in messages):
        return False

    causes = chain[1:]
    if causes:
        # A permanent/configuration cause must not inherit retryability from
        # the outer DPY-6005 label. With causes present, require a concrete
        # transient socket/network exception.
        if any(isinstance(item, (ValueError, TypeError, KeyError)) for item in causes):
            return False
        return any(_is_transient_network_cause(item) for item in causes)

    # Some driver/platform combinations expose only a DPY message. In that
    # case require an explicit transient network reason instead of the code
    # alone; authentication, service and generic configuration errors fail closed.
    return any(
        re.search(
            r"\b(?:timed?\s*out|connection refused|actively refused|"
            r"network is unreachable|host is unreachable|connection reset|"
            r"connection aborted|temporary failure in name resolution)\b",
            text,
            re.IGNORECASE,
        )
        for text in messages
    )


def is_oracle_connection_unavailable(exc: BaseException) -> bool:
    """Identify errors after which an owned Oracle connection cannot be reused."""
    if oracle_error_code(exc) in {3113, 3114, 3135}:
        return True
    return bool(re.search(r"\bDPY-(?:1001|4011)\b", str(exc), re.IGNORECASE))


def close_connection(conn: Any) -> None:
    """Rollback best-effort and close a connection, even if rollback fails."""
    if conn is None:
        return
    with suppress(Exception):
        conn.rollback()
    conn.close()


class OracleOperationsNotImplemented(NotImplementedError):
    """Raised for Oracle migration operations outside connection scope."""


class OracleConnectionError(RuntimeError):
    """Credential-safe connection failure suitable for logs and run reports."""



def safe_connection_error_message(exc: BaseException) -> str:
    code = oracle_error_code(exc)

    if code in {1017, 28000}:
        if code == 1017:
            return (
                "Oracle authentication failed (ORA-01017). Verify the configured account exists, "
                "is unlocked, and its password secret and service are correct. Standard FULL "
                "migration does not create schema owners; use the explicit Oracle E2E onboarding "
                "workflow after DBA-approved provisioning configuration."
            )
        return f"Oracle authentication failed (ORA-{code:05d}); verify the configured account is unlocked"

    if code == 12514:
        return "Oracle service is unavailable or unknown (ORA-12514)"

    if code is not None:
        return f"Oracle connection failed (ORA-{code:05d})"

    if re.search(r"\bDPY-6005\b", str(exc), re.IGNORECASE):
        return "Oracle network connection failed (DPY-6005)"

    return "Oracle connection failed (driver error)"
