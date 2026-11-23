"""Oracle table DDL, constraints, full-sync loading, and count validation."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterator
from typing import Any

from core.audit_logger import audit_log
from core.connectors.base import (
    Schema,
    TablePartition,
    TargetConnector,
    TypeDef,
    UnmappedTypeError,
    UpsertResult,
    quote_identifier,
)
from core.connectors.oracle import objects as oracle_objects
from core.connectors.oracle import permissions as oracle_permissions
from core.connectors.oracle import precision as oracle_precision
from core.connectors.oracle import replacement as oracle_replacement
from core.connectors.oracle._models import (
    OracleConnectionError,
    close_connection,
    is_oracle_connection_unavailable,
    is_transient_connection_error,
    make_connect_kwargs,
    oracle_error_code,
    safe_connection_error_message,
    validate_oracle_identifier,
    validate_oracle_virtual_expression,
)
from core.connectors.oracle.indexes import normalize_index_rows
from core.connectors.oracle.objects.type import hydrate_object, serialize_object
from core.dependency_order import order_data_load_objects


def _is_oracle_object_payload(value: Any) -> bool:
    return isinstance(value, dict) and (
        set(value) == {"__oracle_object__", "attributes"}
        or set(value) == {"__oracle_collection__", "kind", "items"}
    )


class OracleTargetConnector(TargetConnector):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._conn: Any = None
        self._managed_schemas: dict[str, Schema] = {}
        self._suspended_trigger_names: list[str] = []
        self._managed_object_dependencies: set[tuple[str, str]] = set()
        self._precision_safe_full_load = False
        self._permission_executor_conn: Any = None
        self._oracle_collection_kinds: dict[str, str] = {}

    def enable_precision_safe_full_load(self) -> None:
        """Use lossless Oracle NUMBER and temporal transport for Oracle FULL."""
        self._precision_safe_full_load = True

    def connect(self) -> None:
        import oracledb

        kwargs = make_connect_kwargs(self._config, oracledb)
        retries = int(self._config.get("connection_max_retries", 3))
        if retries < 0:
            raise ValueError("Oracle connection_max_retries cannot be negative")
        delay = float(self._config.get("connection_retry_delay", 1.0))
        if delay < 0:
            raise ValueError("Oracle connection_retry_delay cannot be negative")

        for attempt in range(retries + 1):
            original_fetch_decimals = oracledb.defaults.fetch_decimals
            try:
                oracledb.defaults.fetch_decimals = True
                try:
                    self._conn = oracledb.connect(**kwargs)
                finally:
                    oracledb.defaults.fetch_decimals = original_fetch_decimals
                audit_log(
                    phase="connect",
                    status="success",
                    details={"engine": "oracle", "role": "target"},
                )
                return
            except oracledb.Error as exc:
                if not is_transient_connection_error(exc) or attempt >= retries:
                    audit_log(
                        phase="connect",
                        status="failed",
                        details={
                            "engine": "oracle",
                            "role": "target",
                            "error_code": oracle_error_code(exc),
                        },
                    )
                    raise OracleConnectionError(safe_connection_error_message(exc)) from None
                audit_log(
                    phase="connect",
                    status="retrying",
                    details={
                        "engine": "oracle",
                        "role": "target",
                        "attempt": attempt + 1,
                        "error_code": oracle_error_code(exc),
                    },
                )
                time.sleep(delay * (2**attempt))

    def close(self) -> None:
        conn, self._conn = self._conn, None
        close_connection(conn)
        executor_conn, self._permission_executor_conn = self._permission_executor_conn, None
        close_connection(executor_conn)

    def _permission_executor(self) -> Any:
        if self._permission_executor_conn is None:
            raise RuntimeError(
                "Oracle permission executor is not connected; permission migration must pass preflight"
            )
        return self._permission_executor_conn

    def create_sequence(self, seq: Any) -> None:
        return oracle_objects.create_sequence(self._conn, seq)

    def advance_sequence(
        self, seq_name: str, table: str, column: str, owned_by: str | None = None
    ) -> None:
        return oracle_objects.advance_sequence(self._conn, seq_name, table, column)

    def restore_standalone_sequence_state(self, seq: Any) -> None:
        return oracle_objects.restore_standalone_sequence_state(self._conn, seq)

    def create_type(self, type_def: TypeDef) -> None:
        result = oracle_objects.create_type(
            self._conn, type_def, target_owner=str(self._config.get("username", ""))
        )
        if type_def.kind.casefold() in {"varray", "nested_table"}:
            self._oracle_collection_kinds[str(type_def.name).casefold()] = type_def.kind.casefold()
        return result

    def validate_object_privileges(self, **definitions: Any) -> dict[str, Any]:
        return oracle_objects.validate_privileges(
            self._conn, target_owner=self._config.get("username"), **definitions
        )

    def preflight_permission_migration(
        self, policy: dict[str, Any], permission_inventory: dict[str, Any],
        source_inventory: dict[str, Any], *, source_owner: str,
        executor_connection: dict[str, Any] | None = None,
        onboarding_admin_connection: dict[str, Any] | None = None,
        approved_legacy_keys: set[str] | None = None,
        permit_legacy_candidates: bool = False,
        source_scope_identity: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Connect the scoped executor and preflight permission work before cleanup."""
        import oracledb

        target_owner = str(self._config.get("username", "")).strip()
        if not isinstance(executor_connection, dict):
            raise RuntimeError(
                "Oracle permission migration requires a separate executor username and password_secret"
            )
        executor_user = str(executor_connection.get("username", "")).strip()
        if not executor_user or executor_user.casefold() == target_owner.casefold():
            raise RuntimeError(
                "Oracle permission executor must be a separate configured target identity"
            )
        if "password" not in executor_connection or "password_secret" in executor_connection:
            raise RuntimeError("Oracle permission executor password must be resolved from a secret reference")
        try:
            self._permission_executor_conn = oracledb.connect(
                **make_connect_kwargs(executor_connection, oracledb)
            )
        except Exception as exc:
            error_code = oracle_error_code(exc)
            code = f" (ORA-{error_code})" if error_code else ""
            raise RuntimeError(
                f"Oracle permission executor connection failed{code}; verify the configured secret and target service"
            ) from None
        with self._conn.cursor() as cur:
            cur.execute("SELECT USER FROM dual")
            actual_owner = cur.fetchone()
            cur.execute("SELECT SYS_CONTEXT('USERENV', 'CON_NAME') FROM dual")
            current_container = cur.fetchone()
        if not actual_owner or str(actual_owner[0]).casefold() != target_owner.casefold():
            raise RuntimeError(
                f"Oracle permission preflight requires a direct target-owner session as {target_owner}"
            )
        if current_container and str(current_container[0]).upper() in {"CDB$ROOT", "PDB$SEED"}:
            raise RuntimeError(
                "Oracle permission principal provisioning requires a PDB-local target session; "
                f"current container is {current_container[0]}"
            )
        owner_identity = self._database_identity(self._conn)
        executor_identity = self._database_identity(self._permission_executor())
        if executor_identity["user"].casefold() != executor_user.casefold():
            raise RuntimeError("Oracle permission executor session identity does not match configured username")
        if any(
            owner_identity[field].casefold() != executor_identity[field].casefold()
            for field in ("db_unique_name", "container_name", "service_name")
        ):
            raise RuntimeError(
                "Oracle permission executor is connected to a different database, PDB, or service"
            )
        if source_scope_identity is None:
            raise RuntimeError("Oracle permission migration requires verified SOURCE database/PDB identity")
        scope_id = oracle_permissions.permission_scope_id(source_scope_identity, owner_identity)
        onboarding = policy.get("onboarding", {})
        if isinstance(onboarding, dict) and onboarding.get("enabled") is True:
            if not isinstance(onboarding_admin_connection, dict):
                raise RuntimeError(
                    "Oracle permission onboarding is enabled but its separate admin secret was not resolved"
                )
            admin_conn = None
            try:
                admin_conn = oracledb.connect(
                    **make_connect_kwargs(onboarding_admin_connection, oracledb)
                )
                oracle_permissions.bootstrap_permission_executor(
                    admin_conn, self._permission_executor(),
                    admin_user=str(onboarding_admin_connection.get("username", "")),
                    executor_user=executor_user,
                    target_identity=owner_identity,
                    inventory=permission_inventory,
                    raw_policy=policy,
                )
            except RuntimeError:
                raise
            except Exception as exc:
                error_code = oracle_error_code(exc)
                code = f" (ORA-{error_code})" if error_code else ""
                raise RuntimeError(
                    f"Oracle permission one-time onboarding failed{code}; no broad fallback was attempted"
                ) from None
            finally:
                close_connection(admin_conn)
        authority_gaps = oracle_permissions.executor_authority_gaps(
            self._permission_executor(), policy, permission_inventory
        )
        if authority_gaps:
            raise RuntimeError(
                "Oracle permission executor preflight found multiple missing prerequisites: "
                + "; ".join(authority_gaps)
                + ". Apply only the listed one-time, PDB-local onboarding grants, then rerun the read-only preflight."
            )
        plan = oracle_permissions.preflight_target(
            self._conn,
            policy,
            {**source_inventory, **permission_inventory},
            source_owner=source_owner,
            target_owner=target_owner,
            target_ssl=bool(self._config.get("ssl")),
            executor_conn=self._permission_executor(),
            approved_legacy_keys=approved_legacy_keys,
            permit_legacy_candidates=permit_legacy_candidates,
            scope_id=scope_id,
        )
        if executor_user.casefold() in {
            str(name).casefold()
            for name in [*plan["policy"]["user_mappings"].values(),
                         *plan["policy"]["role_mappings"].values()]
        }:
            raise RuntimeError(
                "Oracle permission executor overlaps a SOURCE-mapped application principal; "
                "use a dedicated target executor identity"
            )
        plan["roles"] = permission_inventory["roles"]
        return plan

    def apply_permission_principals(self, permission_plan: dict[str, Any]) -> list[str]:
        return oracle_permissions.apply_principals(self._permission_executor(), permission_plan)

    def prepare_permission_provenance(self, permission_plan: dict[str, Any]) -> list[str]:
        with self._permission_executor().cursor() as cur:
            cur.execute("SELECT USER FROM dual")
            row = cur.fetchone()
        if not row or not row[0]:
            raise RuntimeError("Oracle permission provenance could not verify executor identity")
        executor = str(row[0])
        return oracle_permissions.prepare_provenance(
            self._conn, str(self._config.get("username", "")), executor,
            permission_plan.get("scope_id"),
        )

    def record_legacy_permission_approvals(self, permission_plan: dict[str, Any]) -> list[str]:
        """Durably record explicitly confirmed legacy grants in TARGET ledger."""
        owner = str(self._config.get("username", ""))
        with self._permission_executor().cursor() as cur:
            cur.execute("SELECT USER FROM dual")
            row = cur.fetchone()
        if not row or not row[0]:
            raise RuntimeError("Oracle permission approval could not verify executor identity")
        executor = str(row[0])
        oracle_permissions.prepare_provenance(
            self._conn, owner, executor, permission_plan.get("scope_id")
        )
        return oracle_permissions.record_legacy_adoptions(
            self._conn, owner,
            permission_plan["permission_reconciliation"].get("adopted_legacy", []),
            principal_conn=self._permission_executor(),
        )

    def apply_permission_role_memberships(self, permission_plan: dict[str, Any]) -> list[str]:
        return oracle_permissions.apply_memberships(self._permission_executor(), permission_plan)

    def apply_permission_grants(self, permission_plan: dict[str, Any]) -> list[str]:
        object_plan, _ = oracle_permissions.split_grants(permission_plan)
        return oracle_permissions.apply_grants(
            self._conn, object_plan, str(self._config.get("username", ""))
        )

    def apply_permission_system_grants(self, permission_plan: dict[str, Any]) -> list[str]:
        _, system_plan = oracle_permissions.split_grants(permission_plan)
        return oracle_permissions.apply_grants(
            self._permission_executor(), system_plan,
            str(self._config.get("username", "")),
        )

    def verify_permission_migration(self, permission_plan: dict[str, Any]) -> None:
        return oracle_permissions.verify(
            self._conn, permission_plan, principal_conn=self._permission_executor()
        )

    def inspect_object_privileges(self, **definitions: Any) -> dict[str, Any]:
        owner = str(self._config.get("username", "")).strip()
        validate_oracle_identifier(owner, "target owner")
        with self._conn.cursor() as cur:
            cur.execute("SELECT USER FROM dual")
            actual_owner = cur.fetchone()
        if not actual_owner or str(actual_owner[0]).casefold() != owner.casefold():
            raise RuntimeError(
                f"Oracle privilege inspection requires a direct connection as target owner {owner}"
            )
        requirements = oracle_objects.required_owner_privileges(**definitions)
        report = oracle_objects.inspect_privileges(self._conn, requirements)
        report["target_owner"] = owner
        report["proposed_grants"] = [
            f"GRANT {privilege} TO {quote_identifier(owner)};"
            for privilege in report["missing"]
        ]
        return report

    @staticmethod
    def _database_identity(conn: Any) -> dict[str, str]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT USER, SYS_CONTEXT('USERENV','DB_UNIQUE_NAME'), "
                "SYS_CONTEXT('USERENV','CON_NAME'), "
                "SYS_CONTEXT('USERENV','SERVICE_NAME') FROM dual"
            )
            row = cur.fetchone()
        if row is None or len(row) != 4 or any(value is None for value in row):
            raise RuntimeError("Oracle privilege bootstrap could not verify database/PDB identity")
        return {
            "user": str(row[0]), "db_unique_name": str(row[1]),
            "container_name": str(row[2]), "service_name": str(row[3]),
        }

    def bootstrap_object_privileges(
        self,
        privileges: list[str],
        admin_connection: dict[str, Any],
        *,
        target_owner: str | None = None,
    ) -> list[str]:
        """Grant only pre-approved owner privileges through a separate admin session."""
        import oracledb

        from core.connectors.oracle.objects import ORACLE_BOOTSTRAP_PRIVILEGES

        owner = str(target_owner or self._config.get("username", "")).strip()
        validate_oracle_identifier(owner, "target owner")
        if owner.upper() in {"SYS", "SYSTEM"}:
            raise RuntimeError("Oracle privilege bootstrap refuses SYS and SYSTEM target owners")
        requested = sorted({str(privilege).strip().upper() for privilege in privileges})
        unsupported = sorted(set(requested) - ORACLE_BOOTSTRAP_PRIVILEGES)
        if unsupported:
            raise RuntimeError(
                "Oracle privilege bootstrap refused unsupported privilege(s): "
                + ", ".join(unsupported)
            )
        admin_user = str(admin_connection.get("username", "")).strip()
        validate_oracle_identifier(admin_user, "bootstrap admin")
        if admin_user.upper() in {"SYS", "SYSTEM"} or admin_user.casefold() == owner.casefold():
            raise RuntimeError(
                "Oracle privilege bootstrap requires a separate, non-SYS administrative identity"
            )
        if self._conn is None:
            raise RuntimeError("Oracle target connection is not open for privilege bootstrap")
        if not requested:
            return []

        admin_conn = None
        granted: list[str] = []
        try:
            admin_conn = oracledb.connect(**make_connect_kwargs(admin_connection, oracledb))
            target_identity = self._database_identity(self._conn)
            admin_identity = self._database_identity(admin_conn)
            if target_identity["user"].casefold() != owner.casefold():
                raise RuntimeError(
                    "Oracle privilege bootstrap target session is not connected as the configured target owner; "
                    "no privileges were granted"
                )
            if admin_identity["user"].casefold() != admin_user.casefold():
                raise RuntimeError(
                    "Oracle privilege bootstrap admin session does not match the configured admin identity; "
                    "no privileges were granted"
                )
            identity_fields = ("db_unique_name", "container_name", "service_name")
            if any(
                target_identity[field].casefold() != admin_identity[field].casefold()
                for field in identity_fields
            ):
                raise RuntimeError(
                    "Oracle bootstrap admin is connected to a different database, PDB, or service; "
                    "no privileges were granted"
                )
            if admin_identity["user"].casefold() == owner.casefold():
                raise RuntimeError(
                    "Oracle privilege bootstrap admin session resolved to the target owner; "
                    "no privileges were granted"
                )

            with admin_conn.cursor() as cur:
                cur.execute(
                    "SELECT privilege, admin_option FROM user_sys_privs"
                )
                direct_admin_option: set[str] = set()
                for privilege, admin_option in cur.fetchall():
                    privilege = str(privilege).upper()
                    if str(admin_option).upper() == "YES":
                        direct_admin_option.add(privilege)
            unauthorized = [
                privilege for privilege in requested
                if privilege not in direct_admin_option
            ]
            if unauthorized:
                raise RuntimeError(
                    "Oracle bootstrap admin is not authorized to grant: "
                    + ", ".join(unauthorized)
                    + "; requires each specific privilege to be directly granted WITH ADMIN OPTION"
                )

            for privilege in requested:
                statement = f"GRANT {privilege} TO {quote_identifier(owner)}"
                try:
                    with admin_conn.cursor() as cur:
                        cur.execute(statement)
                    granted.append(privilege)
                    audit_log(
                        phase="oracle_privilege_bootstrap",
                        status="success",
                        details={
                            "target_owner": owner, "privilege": privilege,
                            "database": target_identity["db_unique_name"],
                            "container": target_identity["container_name"],
                            "service": target_identity["service_name"],
                        },
                    )
                except Exception as exc:
                    error_code = oracle_error_code(exc)
                    audit_log(
                        phase="oracle_privilege_bootstrap",
                        status="error",
                        details={
                            "target_owner": owner, "privilege": privilege,
                            "error_code": error_code,
                            "granted_before_failure": list(granted),
                        },
                    )
                    code = f" (ORA-{error_code})" if error_code else ""
                    raise RuntimeError(
                        f"Oracle privilege bootstrap grant failed for {privilege} to {owner}{code}; "
                        f"already granted in this attempt: {', '.join(granted) or 'none'}"
                    ) from None
        except RuntimeError as exc:
            audit_log(
                phase="oracle_privilege_bootstrap",
                status="error",
                details={
                    "target_owner": owner,
                    "privileges": requested,
                    "reason": str(exc).split(";", 1)[0][:240],
                    "granted_before_failure": list(granted),
                },
            )
            raise
        except Exception as exc:
            error_code = oracle_error_code(exc)
            code = f" (ORA-{error_code})" if error_code else ""
            audit_log(
                phase="oracle_privilege_bootstrap",
                status="error",
                details={
                    "target_owner": owner, "privileges": requested,
                    "error_code": error_code,
                    "granted_before_failure": list(granted),
                },
            )
            raise RuntimeError(
                f"Oracle privilege bootstrap could not complete safely{code}; "
                f"already granted in this attempt: {', '.join(granted) or 'none'}"
            ) from None
        finally:
            if admin_conn is not None:
                try:
                    admin_conn.close()
                except Exception:
                    pass
        return granted

    def create_view(self, view: Any) -> None:
        return oracle_objects.create_view(
            self._conn, view, self._managed_object_dependencies,
            target_owner=self._config.get("username"),
        )

    def create_materialized_view(self, mv: Any) -> None:
        return oracle_objects.create_materialized_view(
            self._conn, mv, target_owner=self._config.get("username")
        )

    def revalidate_existing_materialized_views(self, materialized_views: list[Any]) -> None:
        """Compile only existing MVs before schema cleanup; never create or refresh here."""
        for mv in materialized_views:
            validate_oracle_identifier(mv.name, "materialized view")
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM user_mviews WHERE mview_name = :1", (mv.name,)
                )
                exists = cur.fetchone() is not None
            if exists:
                oracle_objects.create_materialized_view(
                    self._conn, mv, target_owner=self._config.get("username"),
                    create_if_missing=False,
                )

    def refresh_materialized_view(self, name: str, schema_name: str | None = None) -> None:
        return oracle_objects.refresh_materialized_view(self._conn, name)

    def apply_comment(self, comment: Any) -> None:
        object_type = str(comment.object_type).upper()
        if object_type not in {"TABLE", "COLUMN"}:
            raise RuntimeError(f"Oracle FULL cannot apply comment type {object_type}")
        table = comment.object_name.split(".", 1)[0]
        validate_oracle_identifier(table, "comment table")
        column = comment.column_name
        if object_type == "COLUMN":
            if not column and "." in comment.object_name:
                table, column = comment.object_name.split(".", 1)
            validate_oracle_identifier(column, "comment column")
        literal = "'" + str(comment.comment).replace("'", "''") + "'"
        target = quote_identifier(table)
        if object_type == "COLUMN":
            target += "." + quote_identifier(column)
        with self._conn.cursor() as cur:
            cur.execute(f"COMMENT ON {object_type} {target} IS {literal}")
        self._conn.commit()

    def create_synonym(self, synonym: Any) -> bool | None:
        if synonym.is_public:
            raise RuntimeError("Oracle FULL never creates or modifies PUBLIC synonyms")
        if synonym.db_link:
            raise RuntimeError("Oracle FULL does not support database-link synonyms")
        source_owner = str(synonym.schema_name or "").upper()
        base_owner = str(synonym.base_owner or "").upper()
        target_owner = str(self._config.get("username", "")).strip().upper()
        base_name = synonym.base_name
        if not target_owner or base_owner != source_owner or not base_name:
            raise RuntimeError(
                f"Oracle FULL refuses unsafe synonym owner remapping for {synonym.name}"
            )
        validate_oracle_identifier(synonym.name, "synonym")
        validate_oracle_identifier(base_name, "synonym target")
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM user_objects WHERE object_name=:1 AND object_type IN "
                "('TABLE','VIEW','MATERIALIZED VIEW','SEQUENCE','FUNCTION','PROCEDURE','PACKAGE','SYNONYM')",
                (base_name,),
            )
            if cur.fetchone() is None:
                return False
            oracle_objects._assert_no_unmanaged_dependents(
                self._conn, synonym.name, ("SYNONYM",),
                self._managed_object_dependencies,
            )
            cur.execute(
                f"CREATE OR REPLACE SYNONYM {quote_identifier(synonym.name)} FOR "
                f"{quote_identifier(target_owner)}.{quote_identifier(base_name)}"
            )
        self._conn.commit()

    def partition_row_counts(self, table_name: str) -> dict[str, int]:
        validate_oracle_identifier(table_name, "table")
        partitioning = oracle_objects.discover_table_partitioning(self._conn, table_name)
        if partitioning is None:
            return {}
        counts: dict[str, int] = {}
        with self._conn.cursor() as cur:
            for part in partitioning["partitions"]:
                validate_oracle_identifier(part["name"], "partition")
                cur.execute(
                    f"SELECT COUNT(*) FROM {quote_identifier(table_name)} "
                    f"PARTITION ({quote_identifier(part['name'])})"
                )
                counts[part["name"]] = int(cur.fetchone()[0])
        return counts

    def materialized_view_rows(self, name: str) -> list[tuple[Any, ...]]:
        validate_oracle_identifier(name, "materialized view")
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {quote_identifier(name)}")
            return list(cur.fetchall())

    def create_function(self, func: Any) -> None:
        return oracle_objects.create_function(
            self._conn, func, self._managed_object_dependencies,
            target_owner=self._config.get("username"),
        )

    def set_managed_source_objects(self, definitions: dict[str, list[Any]]) -> None:
        self._managed_object_dependencies = oracle_objects.managed_source_objects(definitions)

    def create_trigger(self, trigger: Any) -> None:
        return oracle_objects.create_trigger(
            self._conn, trigger,
            target_owner=self._config.get("username"),
            managed_tables=set(self._managed_schemas),
        )

    def set_trigger_source_state(self, trigger: Any) -> None:
        validate_oracle_identifier(trigger.name, "trigger")
        oracle_objects._check_compile_status(self._conn, trigger.name, ("TRIGGER",))
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, status FROM user_triggers WHERE trigger_name = :1",
                (trigger.name,),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError(f"Oracle target trigger {trigger.name} is missing after preflight")
            if str(row[0]) != trigger.table:
                raise RuntimeError(
                    f"Oracle target trigger {trigger.name} is on {row[0]}, expected {trigger.table}"
                )
            desired = "DISABLED" if trigger.is_disabled else "ENABLED"
            if str(row[1]).upper() != desired:
                action = "DISABLE" if trigger.is_disabled else "ENABLE"
                cur.execute(
                    f"ALTER TRIGGER {quote_identifier(trigger.name)} {action}"
                )
                cur.execute(
                    "SELECT status FROM user_triggers WHERE trigger_name = :1",
                    (trigger.name,),
                )
                state = cur.fetchone()
                if state is None or str(state[0]).upper() != desired:
                    raise RuntimeError(
                        f"Oracle trigger {trigger.name} did not reach source state {desired}"
                    )
        self._conn.commit()

    def suspend_triggers_for_data_load(self, triggers: list[Any]) -> list[Any]:
        suspended = oracle_objects.suspend_triggers_for_data_load(self._conn, triggers)
        self._suspended_trigger_names = [trigger.name for trigger in suspended]
        return suspended

    def complete_trigger_data_load(self) -> None:
        # Oracle trigger application after loading sets the source enabled state.
        self._suspended_trigger_names = []

    def sync_auto_increment(self, table: str, column: str) -> None:
        validate_oracle_identifier(table, "table")
        validate_oracle_identifier(column, "column")
        managed_schema = self._managed_schemas.get(table)
        identity = next((
            item for item in (managed_schema.columns if managed_schema else [])
            if item.name == column and item.is_identity
        ), None)
        generation = (
            identity.identity_kind
            if identity and identity.identity_kind in {"ALWAYS", "BY DEFAULT"}
            else "BY DEFAULT"
        )
        with self._conn.cursor() as cur:
            cur.execute(
                f"ALTER TABLE {quote_identifier(table)} MODIFY "
                f"({quote_identifier(column)} GENERATED {generation} AS IDENTITY "
                "(START WITH LIMIT VALUE))"
            )
        self._conn.commit()

    def restore_suspended_triggers(self) -> None:
        if not self._conn or not self._suspended_trigger_names:
            return
        from core.connectors.base import quote_identifier

        with self._conn.cursor() as cur:
            for name in self._suspended_trigger_names:
                cur.execute(f"ALTER TRIGGER {quote_identifier(name)} ENABLE")
        self._conn.commit()
        self._suspended_trigger_names = []

    def ensure_database_exists(self) -> None:
        # Oracle service/PDB provisioning is intentionally outside migration.
        if self._conn is None:
            raise RuntimeError("Oracle target must connect to an existing configured service")

    def plan_schema_replacement(
        self, *, preserve_target_only: bool = True, **source_inventory: Any
    ) -> oracle_replacement.ReplacementPlan:
        """Return a read-only source-managed cleanup plan for Oracle FULL."""
        owner = str(self._config.get("username", "")).strip().upper()
        return oracle_replacement.build_plan(
            self._conn, owner, preserve_target_only=preserve_target_only,
            **source_inventory
        )

    def apply_schema_replacement(
        self,
        plan: oracle_replacement.ReplacementPlan,
        *, preserve_target_only: bool = True,
        **source_inventory: Any,
    ) -> list[str]:
        """Re-plan immediately before applying preflighted owner-local DDL."""
        current = self.plan_schema_replacement(
            preserve_target_only=preserve_target_only, **source_inventory
        )
        if current.plan_id != plan.plan_id:
            raise RuntimeError(
                "Oracle schema replacement inventory changed after preflight; "
                "cleanup was stopped before DDL"
            )
        return oracle_replacement.apply_plan(self._conn, current)

    def validate_replacement_tables(self, schemas: list[Schema]) -> None:
        """Fail before cleanup if an existing source table needs unsafe reconciliation."""
        for schema in schemas:
            actual = self.inspect_schema(schema.name)
            if actual is not None and not _schemas_compatible(
                schema, actual, str(self._config.get("source_engine", "oracle"))
            ):
                raise RuntimeError(
                    f"Oracle schema replacement is blocked before cleanup: existing table "
                    f"{schema.name!r} is not directly compatible with the source; "
                    "review or repair this table before approving owner cleanup"
                )

    def verify_schema_replacement(self, **source_inventory: Any) -> dict[str, Any]:
        """Check that migration left no extra objects and created every expected object."""
        self.verify_replacement_table_definitions(source_inventory["schemas"])
        for mv in source_inventory.get("materialized_views", []):
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT m.build_mode, m.refresh_mode, m.refresh_method, m.rewrite_enabled, "
                    "o.status, m.query FROM user_mviews m JOIN user_objects o "
                    "ON o.object_name=m.mview_name AND o.object_type='MATERIALIZED VIEW' "
                    "WHERE m.mview_name=:1", (mv.name,),
                )
                row = cur.fetchone()
            if row is None or tuple(str(value or "").upper() for value in row[:5]) != (
                "IMMEDIATE", "DEMAND", "COMPLETE", "N", "VALID"
            ):
                raise RuntimeError(f"Oracle replacement verification found invalid materialized view {mv.name}")
            actual_query = str(row[5].read() if hasattr(row[5], "read") else row[5] or "")
            expected_query = oracle_objects._remap_owner_qualifiers(
                mv.definition, mv.schema_name, self._config.get("username")
            )
            def normalize(value: Any) -> str:
                return " ".join(str(value).rstrip("; ").split()).casefold()
            if normalize(actual_query) != normalize(expected_query):
                raise RuntimeError(f"Oracle replacement verification found a definition mismatch for {mv.name}")
        for synonym in source_inventory.get("synonyms", []):
            expected_owner = str(self._config.get("username", "")).strip().upper()
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT s.table_owner, s.table_name, s.db_link, o.status "
                    "FROM user_synonyms s JOIN user_objects o "
                    "ON o.object_name=s.synonym_name AND o.object_type='SYNONYM' "
                    "WHERE s.synonym_name=:1",
                    (synonym.name,),
                )
                row = cur.fetchone()
            if (row is None or str(row[0]).upper() != expected_owner
                    or str(row[1]) != synonym.base_name or row[2] is not None
                    or str(row[3]).upper() != "VALID"):
                raise RuntimeError(
                    f"Oracle replacement verification found an invalid synonym {synonym.name}"
                )
        expected_comments = {
            (item.object_type.upper(), item.object_name, item.column_name, item.comment)
            for item in source_inventory.get("comments", [])
        }
        actual_comments: set[tuple[str, str, str | None, str]] = set()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, comments FROM user_tab_comments WHERE comments IS NOT NULL"
            )
            actual_comments.update(
                ("TABLE", str(name), None, str(value)) for name, value in cur.fetchall()
            )
            cur.execute(
                "SELECT table_name, column_name, comments FROM user_col_comments "
                "WHERE comments IS NOT NULL"
            )
            actual_comments.update(
                ("COLUMN", str(table), str(column), str(value))
                for table, column, value in cur.fetchall()
            )
        if not expected_comments.issubset(actual_comments):
            raise RuntimeError(
                "Oracle replacement verification found a missing or mismatched "
                "source table/column comment"
            )
        # This is a read-only postcondition check. Cross-owner catalogs are
        # required before a plan can authorize destructive replacement, but
        # are unrelated to verifying the target owner's resulting objects.
        # Keep the default full catalog inspection for pre-migration planning.
        plan = self.plan_schema_replacement(
            preserve_target_only=True,
            inspect_cross_owner_dependencies=False,
            **source_inventory,
        )
        report = plan.as_dict()
        if plan.blockers or plan.inspection_errors or plan.cleanup or plan.missing:
            reasons = [*plan.blockers, *plan.inspection_errors]
            if plan.cleanup:
                reasons.append("target-only objects remain: " + ", ".join(
                    f"{item['type']} {item['name']}" for item in plan.cleanup
                ))
            if plan.missing:
                reasons.append("source objects are missing: " + ", ".join(
                    f"{item['type']} {item['name']}" for item in plan.missing
                ))
            raise RuntimeError("Oracle schema replacement verification failed: " + "; ".join(reasons))
        report["status"] = "verified"
        return report

    def verify_replacement_table_definitions(self, schemas: list[Schema]) -> None:
        """Verify source-supported columns, constraints, and ordinary indexes."""
        for schema in schemas:
            actual = self.inspect_schema(schema.name)
            if actual is None or not _schemas_compatible(
                schema, actual, str(self._config.get("source_engine", "oracle"))
            ):
                raise RuntimeError(
                    f"Oracle replacement verification found a table definition mismatch for {schema.name}"
                )
            with self._conn.cursor() as cur:
                if schema.primary_key:
                    name = _oracle_constraint_name(
                        schema.name, "PK", schema.primary_key_name or ",".join(schema.primary_key)
                    )
                    if not _oracle_constraint_matches(cur, name, schema.name, "P", schema.primary_key):
                        raise RuntimeError(f"Oracle replacement verification found a missing/mismatched PK on {schema.name}")
                for unique in schema.unique_constraints:
                    name = _oracle_constraint_name(schema.name, "UQ", unique.name)
                    if not _oracle_constraint_matches(cur, name, schema.name, "U", unique.columns):
                        raise RuntimeError(f"Oracle replacement verification found a missing/mismatched UNIQUE {unique.name}")
                for foreign in schema.foreign_keys:
                    name = _oracle_constraint_name(schema.name, "FK", foreign.name)
                    if not _oracle_constraint_matches(
                        cur, name, schema.name, "R", foreign.columns,
                        foreign.ref_table, foreign.ref_columns,
                    ):
                        raise RuntimeError(f"Oracle replacement verification found a missing/mismatched FK {foreign.name}")
                for check in schema.check_constraints:
                    generated_check_names = set(
                        schema.options.get("oracle_generated_check_constraints", set())
                    )
                    not_null_column = _oracle_not_null_check_column(check.expression)
                    source_column = next(
                        (column for column in schema.columns
                         if not_null_column and column.name.casefold() == not_null_column.casefold()),
                        None,
                    )
                    if check.name in generated_check_names and not_null_column:
                        if source_column is None or source_column.nullable:
                            raise RuntimeError(
                                f"Oracle generated NOT NULL check {schema.name}.{check.name} "
                                "does not match a non-nullable source column"
                            )
                        self._verify_generated_not_null_check(
                            cur, schema.name, not_null_column
                        )
                        continue
                    name = _oracle_constraint_name(schema.name, "CK", check.name)
                    if not _oracle_constraint_matches(
                        cur, name, schema.name, "C", expression=check.expression,
                        strict_name=True,
                    ):
                        raise RuntimeError(f"Oracle replacement verification found a missing/mismatched CHECK {check.name}")
                indexes = _oracle_index_inventory(cur, schema.name)
                for name, definition in indexes.items():
                    if definition["constraint_backed"] and not _oracle_index_is_supported_target(definition):
                        raise RuntimeError(
                            f"Oracle replacement verification found an invalid constraint index {name}"
                        )
                expected_indexes = {index.name for index in schema.indexes if not index.constraint_backed}
                actual_indexes = {
                    name for name, definition in indexes.items()
                    if not definition["constraint_backed"]
                }
                if not expected_indexes.issubset(actual_indexes):
                    raise RuntimeError(
                        f"Oracle replacement verification found missing source indexes on {schema.name}: "
                        f"expected at least {sorted(expected_indexes)}, found {sorted(actual_indexes)}"
                    )
                for index in schema.indexes:
                    if index.constraint_backed:
                        continue
                    if not _oracle_index_definition_matches(indexes[index.name], index):
                        raise RuntimeError(
                            f"Oracle replacement verification found a mismatched index {index.name}"
                        )

    @staticmethod
    def _verify_generated_not_null_check(cursor: Any, table: str, column: str) -> None:
        """Match Oracle-generated NOT NULL checks structurally, not by SYS_C name."""
        cursor.execute(
            "SELECT search_condition_vc, status, validated, deferrable, deferred "
            "FROM user_constraints WHERE table_name = :1 AND constraint_type = 'C'",
            (table,),
        )
        candidates = cursor.fetchall()
        matching = [
            row for row in candidates
            if _oracle_not_null_check_column(row[0]) is not None
            and _oracle_not_null_check_column(row[0]).casefold() == column.casefold()
        ]
        if not matching:
            raise RuntimeError(
                f"Oracle replacement verification found no equivalent NOT NULL check "
                f"for {table}.{column}"
            )
        if any(
            tuple(str(value or "").upper() for value in row[1:5])
            != ("ENABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE")
            for row in matching
        ):
            raise RuntimeError(
                f"Oracle replacement verification found an unsupported NOT NULL constraint "
                f"state for {table}.{column}"
            )

    def validate_constraint_states(self, table_names: list[str]) -> None:
        """Reject managed target tables whose constraints are not fully enforced."""
        selected = {str(name).upper() for name in table_names}
        if not selected:
            return
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, constraint_name, status, validated, deferrable, deferred "
                "FROM user_constraints WHERE constraint_type IN ('P','U','R','C')"
            )
            rows = cur.fetchall()
        for table, name, status, validated, deferrable, deferred in rows:
            if str(table).upper() not in selected:
                continue
            state = tuple(
                str(value or "").upper()
                for value in (status, validated, deferrable, deferred)
            )
            if state == ("ENABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE"):
                continue
            initial_timing = (
                f"INITIALLY {state[3]}" if state[2] == "DEFERRABLE" else "NOT DEFERRABLE"
            )
            raise ValueError(
                f"Oracle target constraint {table}.{name} has unsupported state: "
                f"{state[0] or 'UNKNOWN'}, {state[1] or 'UNKNOWN'}, {initial_timing}; "
                "FULL migration requires ENABLED VALIDATED NOT DEFERRABLE constraints"
            )

    def validate_all_constraint_states(self) -> None:
        """Apply the existing constraint safety rule to every target-owner table."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT table_name FROM user_tables WHERE nested = 'NO'")
            names = [str(row[0]) for row in cur.fetchall()]
        self.validate_constraint_states(names)

    def create_object_if_missing(self, schema: Schema) -> str | None:
        validate_oracle_identifier(schema.name, "table")
        with self._conn.cursor() as cur:
            cur.execute("SELECT 1 FROM user_tables WHERE table_name = :1", (schema.name,))
            if cur.fetchone() is not None:
                actual = self.inspect_schema(schema.name)
                if actual is None or not _schemas_compatible(
                    schema, actual, str(self._config.get("source_engine", "oracle"))
                ):
                    raise RuntimeError(
                        f"Existing Oracle table {schema.name!r} does not match the source column structure"
                    )
                self._managed_schemas[schema.name] = schema
                return "already_exists"
            definitions: list[str] = []
            source_engine = str(self._config.get("source_engine", "oracle")).lower()
            object_types = schema.options.get("oracle_object_types", {})
            for column in schema.columns:
                validate_oracle_identifier(column.name, "column")
                object_type = object_types.get(column.name)
                if object_type:
                    if str(object_type.get("owner", "")).casefold() != str(schema.schema_name).casefold():
                        raise RuntimeError(
                            f"Oracle OBJECT column {schema.name}.{column.name} has an unmapped source owner"
                        )
                    type_name = validate_oracle_identifier(str(object_type["name"]), "type")
                    column_type = quote_identifier(type_name)
                else:
                    column_type = column.target_type or _oracle_column_type(source_engine, column)
                if not column_type:
                    raise UnmappedTypeError(
                        table=schema.name,
                        column=column.name,
                        source_type=column.source_type,
                        source_engine=source_engine,
                        target_engine="oracle",
                    )
                definition = f"{quote_identifier(column.name)} {column_type}"
                if column.generated_kind == "VIRTUAL":
                    expression = validate_oracle_virtual_expression(
                        column.generated,
                        {item.name for item in schema.columns if item.generated_kind != "VIRTUAL"},
                    )
                    definition += f" GENERATED ALWAYS AS ({expression}) VIRTUAL"
                    if not column.nullable:
                        definition += " NOT NULL"
                    definitions.append(definition)
                    continue
                if column.generated or column.generated_kind:
                    raise RuntimeError(
                        f"Unsupported generated-column mode for Oracle column {schema.name}.{column.name}"
                    )
                semantics = schema.options.get("oracle_char_semantics", {}).get(column.name)
                if (
                    source_engine == "oracle"
                    and semantics in {"BYTE", "CHAR"}
                    and column_type.startswith(("VARCHAR2(", "CHAR("))
                ):
                    definition = definition[:-1] + f" {semantics})"
                if column.is_identity or column.auto_increment:
                    generation = (
                        column.identity_kind
                        if column.identity_kind in {"ALWAYS", "BY DEFAULT"}
                        else "BY DEFAULT"
                    )
                    definition += f" GENERATED {generation} AS IDENTITY"
                    identity_options = []
                    if column.identity_seed is not None:
                        identity_options.append(f"START WITH {int(column.identity_seed)}")
                    if column.identity_increment is not None:
                        identity_options.append(f"INCREMENT BY {int(column.identity_increment)}")
                    if identity_options:
                        definition += " (" + " ".join(identity_options) + ")"
                elif column.default is not None and source_engine == "oracle":
                    default_expression = oracle_objects._remap_owner_qualifiers(
                        column.default, schema.schema_name, self._config.get("username")
                    )
                    definition += f" DEFAULT {default_expression}"
                if not (column.is_identity or column.auto_increment):
                    definition += " NULL" if column.nullable else " NOT NULL"
                definitions.append(definition)
            if schema.primary_key:
                pk = ", ".join(quote_identifier(name) for name in schema.primary_key)
                target_name = _oracle_constraint_name(
                    schema.name, "PK", schema.primary_key_name or ",".join(schema.primary_key)
                )
                _oracle_constraint_matches(
                    cur, target_name, schema.name, "P", schema.primary_key
                )
                constraint = f"CONSTRAINT {quote_identifier(target_name)} "
                definitions.append(f"{constraint}PRIMARY KEY ({pk})")
            table_ddl = f"CREATE TABLE {quote_identifier(schema.name)} ({', '.join(definitions)})"
            partitioning = schema.options.get("oracle_partitioning")
            if schema.partition_method and not partitioning:
                raise RuntimeError(
                    f"Oracle FULL refuses to flatten partitioned table {schema.name}: "
                    "partition metadata is incomplete"
                )
            if partitioning:
                method = str(partitioning.get("method", "")).upper()
                if method not in {"RANGE", "LIST"}:
                    raise RuntimeError(f"Unsupported Oracle partition method {method}")
                keys = partitioning.get("keys") or []
                parts = partitioning.get("partitions") or []
                if not keys or not parts:
                    raise RuntimeError(f"Incomplete Oracle partition metadata for {schema.name}")
                for key in keys:
                    validate_oracle_identifier(key, "partition key")
                partition_defs = []
                for part in parts:
                    validate_oracle_identifier(part["name"], "partition")
                    bound = str(part.get("high_value", "")).strip()
                    oracle_objects._validate_partition_bound_expression(
                        bound, schema.name, str(part["name"])
                    )
                    if method == "RANGE":
                        clause = f"VALUES LESS THAN ({bound})"
                    else:
                        list_bound = (
                            bound if bound.startswith("(") and bound.endswith(")")
                            else f"({bound})"
                        )
                        clause = f"VALUES {list_bound}"
                    partition_defs.append(
                        f"PARTITION {quote_identifier(part['name'])} {clause}"
                    )
                table_ddl += (
                    f" PARTITION BY {method} ("
                    + ", ".join(quote_identifier(key) for key in keys)
                    + ") (" + ", ".join(partition_defs) + ")"
                )
            cur.execute(table_ddl)
            self._conn.commit()
        self._managed_schemas[schema.name] = schema
        return "created"

    def reconcile_oracle_table(self, schema: Schema) -> str:
        """Reconcile columns on one source-managed table without table replacement.

        Only widening type changes and nullability relaxations are automatic.
        Column drops/type changes that touch constraints, indexes, or dependent
        views are rejected before DDL so target-local dependencies remain intact.
        """
        validate_oracle_identifier(schema.name, "table")
        source_engine = str(self._config.get("source_engine", "oracle")).lower()
        actual = self.inspect_schema(schema.name)
        if actual is None:
            return self.create_object_if_missing(schema) or "created"
        if schema.primary_key != actual.primary_key:
            raise RuntimeError(
                f"Cannot safely reconcile Oracle table {schema.name!r}: "
                "primary-key columns differ"
            )

        source_columns = {column.name: column for column in schema.columns}
        target_columns = {column.name: column for column in actual.columns}
        for name in source_columns.keys() | target_columns.keys():
            validate_oracle_identifier(name, "column")
        missing = sorted(source_columns.keys() - target_columns.keys())
        extra = sorted(target_columns.keys() - source_columns.keys())
        changed: list[str] = []
        identity_mode_changes: set[str] = set()
        narrowed: dict[str, tuple[int, str]] = {}
        expected_types: dict[str, str] = {}

        for name in missing:
            column = source_columns[name]
            expected = column.target_type or _oracle_column_type(source_engine, column)
            if not expected:
                raise UnmappedTypeError(
                    table=schema.name,
                    column=name,
                    source_type=column.source_type,
                    source_engine=source_engine,
                    target_engine="oracle",
                )
            if (column.generated or column.generated_kind) and column.generated_kind != "VIRTUAL":
                raise RuntimeError(
                    f"Unsupported generated-column mode for Oracle column {schema.name}.{name}"
                )
            if column.is_identity:
                raise RuntimeError(
                    f"Cannot safely add identity column {schema.name}.{name} to an existing Oracle table"
                )
            if column.generated_kind == "VIRTUAL":
                validate_oracle_virtual_expression(
                    column.generated,
                    {item.name for item in schema.columns if item.generated_kind != "VIRTUAL"},
                )
                expected_types[name] = expected
                continue
            if not column.nullable and not (source_engine == "oracle" and column.default is not None):
                raise RuntimeError(
                    f"Cannot safely add NOT NULL column {schema.name}.{name} without "
                    "a source default to a populated Oracle table"
                )
            expected_types[name] = expected

        for name in sorted(source_columns.keys() & target_columns.keys()):
            column = source_columns[name]
            expected = column.target_type or _oracle_column_type(source_engine, column)
            if not expected:
                raise UnmappedTypeError(
                    table=schema.name,
                    column=name,
                    source_type=column.source_type,
                    source_engine=source_engine,
                    target_engine="oracle",
                )
            target_column = target_columns[name]
            if column.generated_kind != target_column.generated_kind or (
                column.generated_kind == "VIRTUAL"
                and _normalize_virtual_expression(column.generated)
                != _normalize_virtual_expression(target_column.generated)
            ):
                raise RuntimeError(
                    f"Cannot safely reconcile Oracle virtual column {schema.name}.{name}: "
                    "target generated expression differs from source"
                )
            if column.is_identity != target_column.is_identity:
                raise RuntimeError(
                    f"Cannot safely reconcile Oracle identity presence for "
                    f"{schema.name}.{name}"
                )
            if column.is_identity and _oracle_identity_generation(column.identity_kind) != (
                _oracle_identity_generation(target_column.identity_kind)
            ):
                identity_mode_changes.add(name)
            if not _oracle_column_matches(
                column, target_column, expected, schema, actual, source_engine
            ):
                narrowing = _oracle_varchar2_narrowing(
                    column, target_column, expected, schema, actual, source_engine
                )
                if narrowing is not None:
                    narrowed[name] = narrowing
                elif not _oracle_column_change_is_widening(
                    column, target_column, expected, schema, actual, source_engine
                ):
                    raise RuntimeError(
                        f"Cannot safely reconcile Oracle column {schema.name}.{name}: "
                        f"target definition {target_column.source_type} differs from "
                        f"source definition {expected}"
                    )
                changed.append(name)
                expected_types[name] = expected

        affected = sorted(set(extra) | set(changed))
        if affected:
            with self._conn.cursor() as cur:
                for name in affected:
                    _assert_oracle_column_has_no_dependencies(cur, schema.name, name)
                for name, (limit, semantics) in narrowed.items():
                    _assert_oracle_varchar2_values_fit(
                        cur, schema.name, name, limit, semantics
                    )
                for name in extra:
                    cur.execute(
                        f"ALTER TABLE {quote_identifier(schema.name)} DROP COLUMN "
                        f"{quote_identifier(name)}"
                    )
                for name in changed:
                    column = source_columns[name]
                    target_column = target_columns[name]
                    desired_signature = _oracle_column_signature(
                        expected_types[name], column
                    )
                    current_signature = _oracle_column_signature(
                        target_column.source_type, target_column
                    )
                    attributes: list[str] = []
                    if desired_signature != current_signature:
                        column_type = expected_types[name]
                        semantics = schema.options.get("oracle_char_semantics", {}).get(name)
                        if name in narrowed:
                            semantics = narrowed[name][1]
                        if (
                            (source_engine == "oracle" or name in narrowed)
                            and semantics in {"BYTE", "CHAR"}
                            and column_type.startswith(("VARCHAR2(", "CHAR("))
                        ):
                            column_type = column_type[:-1] + f" {semantics})"
                        attributes.append(column_type)
                    if column.nullable != target_column.nullable:
                        attributes.append("NULL" if column.nullable else "NOT NULL")
                    if name in identity_mode_changes:
                        attributes.append(
                            f"GENERATED {_oracle_identity_generation(column.identity_kind)} "
                            "AS IDENTITY"
                        )
                    if not attributes:
                        continue
                    cur.execute(
                        f"ALTER TABLE {quote_identifier(schema.name)} MODIFY "
                        f"({quote_identifier(name)} {' '.join(attributes)})"
                    )

        mode_only_changes = identity_mode_changes - set(changed)
        if mode_only_changes:
            with self._conn.cursor() as cur:
                for name in sorted(mode_only_changes):
                    column = source_columns[name]
                    generation = _oracle_identity_generation(column.identity_kind)
                    cur.execute(
                        f"ALTER TABLE {quote_identifier(schema.name)} MODIFY "
                        f"({quote_identifier(name)} GENERATED {generation} AS IDENTITY)"
                    )

        if missing:
            with self._conn.cursor() as cur:
                for name in missing:
                    column = source_columns[name]
                    definition = (
                        f"{quote_identifier(name)} {expected_types[name]}"
                    )
                    if column.generated_kind == "VIRTUAL":
                        expression = validate_oracle_virtual_expression(
                            column.generated,
                            {item.name for item in schema.columns if item.generated_kind != "VIRTUAL"},
                        )
                        definition += f" GENERATED ALWAYS AS ({expression}) VIRTUAL"
                        if not column.nullable:
                            definition += " NOT NULL"
                        cur.execute(
                            f"ALTER TABLE {quote_identifier(schema.name)} ADD ({definition})"
                        )
                        continue
                    semantics = schema.options.get("oracle_char_semantics", {}).get(name)
                    if (
                        source_engine == "oracle"
                        and semantics in {"BYTE", "CHAR"}
                        and expected_types[name].startswith(("VARCHAR2(", "CHAR("))
                    ):
                        definition = definition[:-1] + f" {semantics})"
                    if source_engine == "oracle" and column.default is not None:
                        default_expression = oracle_objects._remap_owner_qualifiers(
                            column.default, schema.schema_name, self._config.get("username")
                        )
                        definition += f" DEFAULT {default_expression}"
                    definition += " NULL" if column.nullable else " NOT NULL"
                    cur.execute(
                        f"ALTER TABLE {quote_identifier(schema.name)} "
                        f"ADD ({definition})"
                    )

        if missing or extra or changed or identity_mode_changes:
            self._conn.commit()
        self._managed_schemas[schema.name] = schema
        return "reconciled" if missing or extra or changed or identity_mode_changes else "unchanged"

    def upsert_batch(
        self, object_name: str, rows: Iterator[dict[str, Any]], schema: Schema | None = None
    ) -> UpsertResult:
        validate_oracle_identifier(object_name, "table")
        batch = list(rows)
        result = UpsertResult()
        if not batch:
            return result
        columns = (
            [column.name for column in schema.columns if column.generated_kind != "VIRTUAL"]
            if schema else list(batch[0])
        )
        if not columns:
            result.failure_count = len(batch)
            result.errors.append("Oracle FULL load has no insertable columns")
            result.failed_items.extend(batch)
            return result
        names = ", ".join(quote_identifier(name) for name in columns)
        precision_transport = self._precision_safe_full_load and schema is not None
        schema_columns = {column.name: column for column in schema.columns} if schema else {}
        lob_kinds = {
            name: oracle_precision.oracle_lob_kind(schema_columns[name].source_type)
            for name in columns if precision_transport and name in schema_columns
            and oracle_precision.oracle_lob_kind(schema_columns[name].source_type)
        }
        object_types = schema.options.get("oracle_object_types", {}) if schema else {}
        object_names = [name for name in columns if name in object_types]
        object_type_objects: dict[str, Any] = {}
        if object_names:
            for name in object_names:
                metadata = object_types[name]
                type_owner = validate_oracle_identifier(str(metadata["owner"]), "type owner")
                type_name = validate_oracle_identifier(str(metadata["name"]), "type")
                type_object = self._conn.gettype(f"{type_owner}.{type_name}")
                object_type_objects[name] = type_object
            for row in batch:
                for name in object_names:
                    value = row.get(name)
                    if value is not None and not _is_oracle_object_payload(value):
                        raise RuntimeError(
                            f"Oracle OBJECT payload for {object_name}.{name} is malformed"
                        )
        lob_names = list(lob_kinds)
        grouped_rows: list[tuple[tuple[int, ...], list[dict[str, Any]]]] = []
        if lob_names:
            for row in batch:
                states = tuple(_oracle_lob_state(row.get(name)) for name in lob_names)
                if grouped_rows and grouped_rows[-1][0] == states:
                    grouped_rows[-1][1].append(row)
                else:
                    grouped_rows.append((states, [row]))
        else:
            grouped_rows.append(((), batch))
        always_identity_columns = [
            column.name for column in (schema.columns if schema else [])
            if column.is_identity and column.identity_kind == "ALWAYS"
            and column.name in columns
        ]
        temporarily_changed: list[str] = []
        temporary_lobs: list[Any] = []
        oracle_connection_unavailable = False
        try:
            with self._conn.cursor() as cur:
                oracledb = None
                if precision_transport:
                    import oracledb

                for name in always_identity_columns:
                    cur.execute(
                        f"ALTER TABLE {quote_identifier(object_name)} MODIFY "
                        f"({quote_identifier(name)} GENERATED BY DEFAULT AS IDENTITY)"
                    )
                    temporarily_changed.append(name)
                for states, group in grouped_rows:
                    state_by_name = dict(zip(lob_names, states))
                    bind_types: list[Any] = []
                    bind_index = 1
                    expressions: list[str] = []
                    for name in columns:
                        lob_kind = lob_kinds.get(name)
                        if name in object_type_objects:
                            expressions.append(f":{bind_index}")
                            bind_types.append(object_type_objects[name])
                            bind_index += 1
                        elif lob_kind:
                            state = state_by_name[name]
                            if state == 1:
                                if str(schema_columns[name].source_type).upper() == "XMLTYPE":
                                    raise ValueError(
                                        f"Oracle XMLTYPE column {object_name}.{name} serialized to empty content"
                                    )
                                expressions.append(
                                    "EMPTY_BLOB()" if lob_kind == "BLOB" else "EMPTY_CLOB()"
                                )
                            elif state == 2:
                                expressions.append("NULL")
                            else:
                                bind_name = f":{bind_index}"
                                expressions.append(
                                    oracle_precision.transport_bind_expression(
                                        schema_columns[name], bind_name
                                    ) if precision_transport else bind_name
                                )
                                bind_types.append(
                                    oracle_precision.input_type(schema_columns[name], oracledb)
                                )
                                bind_index += 1
                        else:
                            if precision_transport and name in schema_columns:
                                expressions.append(
                                    oracle_precision.transport_bind_expression(
                                        schema_columns[name], f":{bind_index}"
                                    )
                                )
                                bind_types.append(
                                    oracle_precision.input_type(schema_columns[name], oracledb)
                                )
                            else:
                                expressions.append(f":{bind_index}")
                                bind_types.append(None)
                            bind_index += 1
                    statement = (
                        f"INSERT INTO {quote_identifier(object_name)} ({names}) "
                        f"VALUES ({', '.join(expressions)})"
                    )
                    values: list[tuple[Any, ...]] = []
                    for row in group:
                        bound_row: list[Any] = []
                        for name in columns:
                            lob_kind = lob_kinds.get(name)
                            if name in object_type_objects:
                                payload = row.get(name)
                                bound_row.append(hydrate_object(
                                    payload, object_type_objects[name], self._oracle_collection_kinds
                                ))
                            elif lob_kind:
                                if state_by_name[name] == 0:
                                    bound_row.append(self._create_lob_bind(
                                        row[name], lob_kind, temporary_lobs, oracledb
                                    ))
                            else:
                                bound_row.append(row.get(name))
                        values.append(tuple(bound_row))
                    if any(value is not None for value in bind_types):
                        cur.setinputsizes(*bind_types)
                    if bind_index == 1:
                        for _ in group:
                            cur.execute(statement)
                    else:
                        cur.executemany(statement, values)
            self._conn.commit()
            result.success_count = len(batch)
        except Exception as exc:  # noqa: BLE001 - return per-batch failures to the orchestrator
            try:
                self._conn.rollback()
            except Exception as rollback_exc:  # preserve the DML exception as primary
                rollback_exc.add_note(
                    f"Rollback followed {type(exc).__name__}: {exc}"
                )
                oracle_connection_unavailable = is_oracle_connection_unavailable(
                    rollback_exc
                )
                raise rollback_exc from exc
            if is_oracle_connection_unavailable(exc):
                oracle_connection_unavailable = True
                raise
            result.failure_count = len(batch)
            result.errors.append(str(exc))
            result.failed_items.extend(batch)
        finally:
            # create_lob_bind() locators are temporary LOBs owned by this
            # connection. The bind tuples and this list hold them through the
            # commit; releasing those references at function exit lets
            # python-oracledb/Oracle reclaim the temporary LOBs without
            # issuing a late LOB.close() call on the still-owned connection.
            temporary_lobs.clear()
            for row in batch:
                for name in lob_names:
                    value = row.get(name)
                    if isinstance(value, oracle_precision.OracleLobPayload):
                        value.close()
            if temporarily_changed and not oracle_connection_unavailable:
                try:
                    with self._conn.cursor() as cur:
                        for name in temporarily_changed:
                            cur.execute(
                                f"ALTER TABLE {quote_identifier(object_name)} MODIFY "
                                f"({quote_identifier(name)} GENERATED ALWAYS AS IDENTITY "
                                "(START WITH LIMIT VALUE))"
                            )
                    self._conn.commit()
                except Exception as exc:
                    self._conn.rollback()
                    raise RuntimeError(
                        f"Failed to restore GENERATED ALWAYS identity mode for "
                        f"Oracle table {object_name!r}: {exc}"
                    ) from exc
        return result

    def _create_lob_bind(
        self, value: Any, kind: str, temporary_lobs: list[Any], oracledb: Any
    ) -> Any:
        lob_type = getattr(oracledb, f"DB_TYPE_{kind}")
        locator = self._conn.createlob(lob_type)
        temporary_lobs.append(locator)
        if isinstance(value, oracle_precision.OracleLobPayload):
            chunks = value.chunks()
        else:
            chunk_size = 64 * 1024
            chunks = (
                value[offset:offset + chunk_size]
                for offset in range(0, len(value), chunk_size)
            )
        offset = 1
        for chunk in chunks:
            locator.write(chunk, offset=offset)
            offset += oracle_precision.oracle_lob_units(chunk, kind)
        return locator

    def delete(
        self, object_name: str, document: dict[str, Any], schema: Schema | None = None
    ) -> None:
        validate_oracle_identifier(object_name, "table")
        keys = schema.primary_key if schema and schema.primary_key else ["ID"]
        where = " AND ".join(f"{quote_identifier(key)} = :{i}" for i, key in enumerate(keys, 1))
        values = tuple(document.get(key) for key in keys)
        with self._conn.cursor() as cur:
            cur.execute(f"DELETE FROM {quote_identifier(object_name)} WHERE {where}", values)
        self._conn.commit()

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        validate_oracle_identifier(object_name, "table")
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {quote_identifier(object_name)}")
            return int(cur.fetchone()[0])

    def export_full(
        self, object_name: str, schema_name: str | None = None
    ) -> Iterator[dict[str, Any]]:
        validate_oracle_identifier(object_name, "table")
        schema = self._managed_schemas.get(object_name)
        if self._precision_safe_full_load and schema is None:
            schema = self.inspect_schema(object_name, schema_name=schema_name)
        columns = schema.columns if schema is not None else []
        if self._precision_safe_full_load and not columns:
            raise RuntimeError(
                f"Oracle precision-safe validation cannot inspect columns for table {object_name!r}"
            )
        select_sql = "*"
        if self._precision_safe_full_load and columns:
            select_sql = ", ".join(
                oracle_precision.transport_select_expression(column) for column in columns
            )
        if self._precision_safe_full_load and not columns:
            raise RuntimeError(
                f"Oracle precision-safe validation cannot inspect columns for table {object_name!r}"
            )
        order_by = ""
        order_columns = (
            schema.primary_key if schema and schema.primary_key else [
                column.name for column in columns
                if not oracle_precision.oracle_lob_kind(column.source_type)
            ]
        )
        if self._precision_safe_full_load and order_columns:
            order_by = " ORDER BY " + ", ".join(
                quote_identifier(name) for name in order_columns
            )
        lob_kinds = {
            column.name: oracle_precision.oracle_lob_kind(column.source_type)
            for column in columns
        }
        object_types = schema.options.get("oracle_object_types", {}) if schema else {}
        object_type_objects: dict[str, Any] = {}
        for column_name, metadata in object_types.items():
            type_owner = validate_oracle_identifier(str(metadata["owner"]), "type owner")
            type_name = validate_oracle_identifier(str(metadata["name"]), "type")
            object_type_objects[column_name] = self._conn.gettype(f"{type_owner}.{type_name}")
        with self._conn.cursor() as cur:
            cur.arraysize = int(self._config.get("fetch_batch_size", 1000))
            if self._precision_safe_full_load:
                import oracledb

                cur.outputtypehandler = oracle_precision.decimal_output_type_handler(oracledb)
            cur.execute(
                f"SELECT {select_sql} FROM {quote_identifier(object_name)}{order_by}"
            )
            names = [desc[0] for desc in cur.description]
            while True:
                batch = cur.fetchmany(cur.arraysize)
                if not batch:
                    break
                for row in batch:
                    values = {
                        name: (
                            oracle_precision.OracleLobPayload.from_lob(value, lob_kinds[name])
                            if self._precision_safe_full_load
                            and lob_kinds.get(name)
                            and hasattr(value, "read") and callable(value.read)
                            else value.read() if hasattr(value, "read") and callable(value.read)
                            else value
                        )
                        for name, value in zip(names, row)
                    }
                    for column_name, type_object in object_type_objects.items():
                        values[column_name] = serialize_object(
                            values.get(column_name), type_object,
                            self._oracle_collection_kinds,
                        )
                    yield values

    def clear_objects_for_full_sync(self, objects: list[str]) -> list[str]:
        managed = {name: self._managed_schemas[name] for name in objects if name in self._managed_schemas}
        if not managed:
            return []
        delete_order = list(reversed(order_data_load_objects(managed, list(managed))))
        try:
            with self._conn.cursor() as cur:
                for name in delete_order:
                    cur.execute(f"DELETE FROM {quote_identifier(name)}")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return delete_order

    def apply_constraints(self, schema: Schema) -> list[str]:
        errors: list[str] = []
        table = quote_identifier(schema.name)
        with self._conn.cursor() as cur:
            for unique in schema.unique_constraints:
                columns = ", ".join(quote_identifier(name) for name in unique.columns)
                try:
                    target_name = _oracle_constraint_name(schema.name, "UQ", unique.name)
                    if not _oracle_constraint_matches(
                        cur, target_name, schema.name, "U", unique.columns
                    ):
                        cur.execute(
                            f"ALTER TABLE {table} ADD CONSTRAINT {quote_identifier(target_name)} "
                            f"UNIQUE ({columns})"
                        )
                        self._conn.commit()
                except Exception as exc:  # noqa: BLE001 - report constraint-specific failure
                    self._rollback_after_constraint_error(exc)
                    errors.append(f"unique constraint {unique.name}: {exc}")
            for foreign in schema.foreign_keys:
                target_owner = self._config.get("username")
                if target_owner and foreign.ref_schema and str(foreign.ref_schema).casefold() != str(target_owner).casefold():
                    errors.append(
                        f"foreign key {foreign.name}: cross-owner references are unsupported"
                    )
                    continue
                if foreign.ref_table.casefold() not in {
                    name.casefold() for name in self._managed_schemas
                }:
                    errors.append(
                        f"foreign key {foreign.name}: referenced table "
                        f"{foreign.ref_table!r} is outside the migrated table set"
                    )
                    continue
                columns = ", ".join(quote_identifier(name) for name in foreign.columns)
                ref_columns = ", ".join(quote_identifier(name) for name in foreign.ref_columns)
                try:
                    target_name = _oracle_constraint_name(schema.name, "FK", foreign.name)
                    if not _oracle_constraint_matches(
                        cur, target_name, schema.name, "R", foreign.columns,
                        foreign.ref_table, foreign.ref_columns,
                    ):
                        statement = (
                            f"ALTER TABLE {table} ADD CONSTRAINT {quote_identifier(target_name)} "
                            f"FOREIGN KEY ({columns}) REFERENCES {quote_identifier(foreign.ref_table)} ({ref_columns})"
                        )
                        if foreign.on_delete in {"CASCADE", "SET NULL"}:
                            statement += f" ON DELETE {foreign.on_delete}"
                        cur.execute(statement)
                        self._conn.commit()
                except Exception as exc:  # noqa: BLE001 - report constraint-specific failure
                    self._rollback_after_constraint_error(exc)
                    errors.append(f"foreign key {foreign.name}: {exc}")
            for check in schema.check_constraints:
                generated_check_names = set(
                    schema.options.get("oracle_generated_check_constraints", set())
                )
                not_null_column = _oracle_not_null_check_column(check.expression)
                if check.name in generated_check_names and not_null_column:
                    source_column = next(
                        (column for column in schema.columns
                         if column.name.casefold() == not_null_column.casefold()),
                        None,
                    )
                    if source_column is None or source_column.nullable:
                        errors.append(
                            f"check constraint {check.name}: generated NOT NULL condition "
                            "does not match a non-nullable source column"
                        )
                    # The table DDL has already recreated this condition as
                    # NOT NULL; Oracle assigns the target's own SYS_C name.
                    continue
                if str(self._config.get("source_engine", "oracle")).lower() != "oracle":
                    errors.append(
                        f"check constraint {check.name}: cross-engine check expressions are unsupported"
                    )
                    continue
                try:
                    target_name = _oracle_constraint_name(schema.name, "CK", check.name)
                    if not _oracle_constraint_matches(
                        cur, target_name, schema.name, "C", expression=check.expression,
                        strict_name=True,
                    ):
                        cur.execute(
                            f"ALTER TABLE {table} ADD CONSTRAINT {quote_identifier(target_name)} "
                            f"CHECK ({check.expression})"
                        )
                        self._conn.commit()
                except Exception as exc:  # noqa: BLE001 - report constraint-specific failure
                    self._rollback_after_constraint_error(exc)
                    errors.append(f"check constraint {check.name}: {exc}")

            if schema.indexes:
                try:
                    target_indexes = _oracle_index_inventory(cur, schema.name)
                except Exception as exc:  # noqa: BLE001 - cannot safely compare/create indexes
                    self._rollback_after_constraint_error(exc)
                    errors.append(f"index catalog for {schema.name}: {exc}")
                    target_indexes = None
                if target_indexes is not None:
                    for index in schema.indexes:
                        if index.constraint_backed:
                            continue
                        try:
                            _validate_oracle_index(index)
                            equivalent = next((
                                info for info in target_indexes.values()
                                if _oracle_index_definition_matches(info, index)
                            ), None)
                            if equivalent is not None:
                                continue

                            cur.execute(
                                "SELECT table_name FROM user_indexes WHERE index_name = :1",
                                (index.name,),
                            )
                            named_rows = cur.fetchall()
                            if named_rows and any(
                                str(row[0]).casefold() != schema.name.casefold()
                                for row in named_rows
                            ):
                                raise RuntimeError(
                                    f"index name {index.name!r} is already used on another table"
                                )
                            existing = target_indexes.get(index.name)
                            if existing is not None:
                                if existing["constraint_backed"]:
                                    raise RuntimeError(
                                        "target index backs a primary-key or unique constraint"
                                    )
                                if not _oracle_index_is_supported_target(existing):
                                    raise RuntimeError(
                                        "cannot safely replace an unsupported target index"
                                    )
                                cur.execute(f"DROP INDEX {quote_identifier(index.name)}")

                            key_columns = []
                            for column in index.columns:
                                validate_oracle_identifier(column, "index column")
                                direction = " DESC" if column in index.descending_columns else ""
                                key_columns.append(f"{quote_identifier(column)}{direction}")
                            unique = "UNIQUE " if index.unique else ""
                            cur.execute(
                                f"CREATE {unique}INDEX {quote_identifier(index.name)} "
                                f"ON {table} ({', '.join(key_columns)})"
                            )
                            self._conn.commit()
                            target_indexes[index.name] = {
                                "name": index.name,
                                "table": schema.name,
                                "unique": index.unique,
                                "index_type": "NORMAL",
                                "status": "VALID",
                                "partitioned": False,
                                "constraint_backed": False,
                                "columns": list(index.columns),
                                "descending_columns": list(index.descending_columns),
                            }
                        except Exception as exc:  # noqa: BLE001 - report index-specific failure
                            self._rollback_after_constraint_error(exc)
                            errors.append(f"index {index.name}: {exc}")
        return errors

    def _rollback_after_constraint_error(self, original: Exception) -> None:
        """Rollback without masking disconnects or the original DDL failure."""
        try:
            self._conn.rollback()
        except Exception as rollback_error:
            rollback_error.add_note(
                f"Rollback followed {type(original).__name__}: {original}"
            )
            raise rollback_error from original
        if is_oracle_connection_unavailable(original):
            raise original

    def inspect_schema(self, object_name: str, schema_name: str | None = None) -> Schema | None:
        validate_oracle_identifier(object_name, "table")
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, data_type, data_length, char_length, char_used, "
                "data_precision, data_scale, nullable, data_default, virtual_column "
                "FROM user_tab_cols WHERE table_name = :1 AND hidden_column = 'NO' ORDER BY column_id",
                (object_name,),
            )
            rows = cur.fetchall()
            cur.execute(
                "SELECT column_name, generation_type FROM user_tab_identity_cols "
                "WHERE table_name = :1",
                (object_name,),
            )
            identity_modes = {
                str(name): _oracle_identity_generation(str(mode))
                for name, mode in cur.fetchall()
            }
            cur.execute(
                "SELECT cc.column_name FROM user_constraints c "
                "JOIN user_cons_columns cc ON c.constraint_name = cc.constraint_name "
                "WHERE c.table_name = :1 AND c.constraint_type = 'P' "
                "ORDER BY cc.position",
                (object_name,),
            )
            primary_key = [row[0] for row in cur.fetchall()]
        if not rows:
            return None
        from core.connectors.base import Column
        partitioning = oracle_objects.discover_table_partitioning(self._conn, object_name)
        managed_schema = self._managed_schemas.get(object_name)
        object_types = managed_schema.options.get("oracle_object_types", {}) if managed_schema else {}
        return Schema(
            name=object_name,
            schema_name=str(self._config.get("username", "")).upper(),
            primary_key=primary_key,
            columns=[Column(
                name=row[0], source_type=row[1],
                target_type=str(object_types[row[0]]["name"]) if row[0] in object_types else None,
                size=row[3] if row[1] in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR"} else row[2],
                precision=row[5], scale=row[6], nullable=row[7] == "Y",
                is_identity=row[0] in identity_modes,
                identity_kind=identity_modes.get(row[0]),
                generated=(
                    str(row[8]).strip()
                    if row[9] == "YES" and row[8] is not None
                    and str(row[1]).upper() != "XMLTYPE" else None
                ),
                generated_kind=(
                    "VIRTUAL"
                    if row[9] == "YES" and str(row[1]).upper() != "XMLTYPE"
                    else None
                ),
            ) for row in rows],
            options={
                "oracle_object_types": object_types,
                "oracle_char_semantics": {
                    row[0]: "BYTE" if row[4] == "B" else "CHAR"
                    for row in rows if row[4] in {"B", "C"}
                },
                **({"oracle_partitioning": partitioning} if partitioning else {}),
            },
            partition_method=partitioning["method"] if partitioning else None,
            partition_key=(
                f"{partitioning['method']} (" + ", ".join(partitioning["keys"]) + ")"
                if partitioning else None
            ),
            partitions=[TablePartition(name=item["name"], description=item["high_value"])
                        for item in partitioning["partitions"]] if partitioning else [],
        )


def _oracle_column_type(source_engine: str, column: Any) -> str | None:
    """Map common relational source types to conservative Oracle SQL types."""
    typ = str(column.source_type or "").upper().strip()
    dimensions = re.search(r"\((\d+)(?:,\s*(\d+))?\)", typ)
    typ = " ".join(re.sub(r"\(\d+(?:,\s*\d+)?\)", "", typ).split())
    precision = column.precision
    scale = column.scale
    if dimensions:
        precision = precision if precision is not None else int(dimensions.group(1))
        scale = scale if scale is not None else int(dimensions.group(2) or 0)
    if source_engine == "oracle":
        if typ == "LONG":
            return "CLOB"
        if typ == "LONG RAW":
            return "BLOB"
        if typ == "XMLTYPE":
            return "XMLTYPE"
        if typ.startswith("TIMESTAMP") and dimensions:
            precision_text = dimensions.group(1)
            suffix = typ.removeprefix("TIMESTAMP").strip()
            return f"TIMESTAMP({precision_text})" + (f" {suffix}" if suffix else "")
        if typ == "FLOAT" and column.precision is not None:
            return f"FLOAT({int(column.precision)})"
        if typ in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW"} and column.size:
            return f"{typ}({int(column.size)})"
        if typ == "NUMBER":
            if column.precision is not None:
                return f"NUMBER({int(column.precision)},{int(column.scale or 0)})"
            return "NUMBER"
        return typ if typ in {
            "BINARY_FLOAT", "BINARY_DOUBLE", "DATE", "CLOB", "NCLOB", "BLOB", "JSON", "XMLTYPE", "LONG", "LONG RAW",
            "TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITH LOCAL TIME ZONE", "FLOAT",
        } else None
    mappings = {
        "SMALLINT": "NUMBER(5)", "INT": "NUMBER(10)", "INTEGER": "NUMBER(10)",
        "BIGINT": "NUMBER(19)", "TINYINT": "NUMBER(3)", "BIT": "NUMBER(1)",
        "BOOLEAN": "NUMBER(1)", "BOOL": "NUMBER(1)", "REAL": "BINARY_FLOAT", "FLOAT": "BINARY_DOUBLE",
        "DOUBLE": "BINARY_DOUBLE", "NUMERIC": "NUMBER", "DECIMAL": "NUMBER",
        "VARCHAR": "VARCHAR2", "CHARACTER VARYING": "VARCHAR2", "CHARACTER": "CHAR",
        "VARCHAR2": "VARCHAR2", "NVARCHAR": "NVARCHAR2",
        "NVARCHAR2": "NVARCHAR2", "CHAR": "CHAR", "NCHAR": "NCHAR",
        "TEXT": "CLOB", "NTEXT": "NCLOB", "CLOB": "CLOB", "NCLOB": "NCLOB", "BLOB": "BLOB",
        "LONG": "CLOB", "LONG RAW": "BLOB",
        "BYTEA": "BLOB", "DATE": "DATE", "DATETIME": "TIMESTAMP",
        "DATETIME2": "TIMESTAMP", "TIMESTAMP": "TIMESTAMP", "TIMESTAMP WITHOUT TIME ZONE": "TIMESTAMP",
        "TIMESTAMPTZ": "TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITH TIME ZONE": "TIMESTAMP WITH TIME ZONE",
        "DOUBLE PRECISION": "BINARY_DOUBLE", "JSON": "JSON",
        "DATETIMEOFFSET": "TIMESTAMP WITH TIME ZONE", "IMAGE": "BLOB",
        "VARBINARY": "BLOB", "BINARY": "RAW", "UNIQUEIDENTIFIER": "CHAR(36)",
        "UUID": "CHAR(36)",
    }
    target = mappings.get(typ)
    if target is None:
        return None
    if target in {"VARCHAR2", "NVARCHAR2"} and (
        column.size is None or int(column.size) <= 0 or int(column.size) > 4000
    ):
        return "CLOB" if target == "VARCHAR2" else "NCLOB"
    if target in {"CHAR", "NCHAR"} and (column.size is None or int(column.size) <= 0):
        return f"{target}(1)"
    if target in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW"} and column.size and int(column.size) > 0:
        return f"{target}({int(column.size)})"
    if target == "NUMBER" and precision is not None:
        return f"NUMBER({int(precision)},{int(scale or 0)})"
    return target


def _schemas_compatible(source: Schema, target: Schema, source_engine: str) -> bool:
    """Check existing Oracle table column names, type families, and nullability."""
    if len(source.columns) != len(target.columns) or source.primary_key != target.primary_key:
        return False
    source_partitioning = source.options.get("oracle_partitioning")
    target_partitioning = target.options.get("oracle_partitioning")
    if source_partitioning != target_partitioning:
        return False
    for expected, actual in zip(source.columns, target.columns):
        if expected.name != actual.name or expected.nullable != actual.nullable:
            return False
        if expected.generated_kind != actual.generated_kind or (
            expected.generated_kind == "VIRTUAL"
            and _normalize_virtual_expression(expected.generated)
            != _normalize_virtual_expression(actual.generated)
        ):
            return False
        if expected.is_identity != actual.is_identity:
            return False
        if expected.is_identity and _oracle_identity_generation(expected.identity_kind) != (
            _oracle_identity_generation(actual.identity_kind)
        ):
            return False
        expected_type = expected.target_type or _oracle_column_type(source_engine, expected)
        if expected_type is None:
            return False
        expected_base = " ".join(re.sub(r"\(\d+(?:,\s*\d+)?\)", "", expected_type.upper()).split())
        actual_base = " ".join(
            re.sub(r"\(\d+(?:,\s*\d+)?\)", "", str(actual.source_type).upper()).split()
        )
        if expected_base != actual_base:
            return False
        dimensions = re.search(r"\((\d+)(?:,\s*(\d+))?\)", expected_type)
        if expected_base == "NUMBER":
            precision = int(dimensions.group(1)) if dimensions else expected.precision
            scale = int(dimensions.group(2) or 0) if dimensions else expected.scale
            if precision != actual.precision or (scale or 0) != (actual.scale or 0):
                return False
        elif expected_base == "FLOAT" and dimensions:
            if int(dimensions.group(1)) != actual.precision:
                return False
        elif expected_base.startswith("TIMESTAMP") and dimensions:
            actual_dimensions = re.search(r"\((\d+)\)", str(actual.source_type))
            if not actual_dimensions or dimensions.group(1) != actual_dimensions.group(1):
                return False
        elif dimensions and expected_base in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW"}:
            if int(dimensions.group(1)) != actual.size:
                return False
        if source_engine == "oracle":
            expected_semantics = source.options.get("oracle_char_semantics", {}).get(expected.name)
            actual_semantics = target.options.get("oracle_char_semantics", {}).get(actual.name)
            if expected_semantics != actual_semantics:
                return False
    return True


def _oracle_identity_generation(kind: str | None) -> str:
    """Normalize Oracle's identity catalog modes to the shared two-mode contract."""
    return "ALWAYS" if str(kind or "").upper() == "ALWAYS" else "BY DEFAULT"


def _normalize_virtual_expression(expression: Any) -> str:
    compact = re.sub(r"\s+", "", str(expression or ""))
    return re.sub(
        r'"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$#]*',
        lambda match: match.group(0) if match.group(0).startswith('"') else match.group(0).upper(),
        compact,
    )


def _oracle_constraint_name(table: str, kind: str, source_name: str) -> str:
    """Return a stable, Oracle-safe owner-wide constraint name (<= 30 chars)."""
    identity = f"{table.casefold()}:{kind}:{source_name.casefold()}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:20]
    return f"DMS_{kind}_{digest}".upper()


def _oracle_constraint_matches(
    cursor: Any,
    name: str,
    table: str,
    kind: str,
    columns: list[str] | None = None,
    ref_table: str | None = None,
    ref_columns: list[str] | None = None,
    expression: str | None = None,
    strict_name: bool = False,
) -> bool:
    """Verify a deterministic target constraint before treating it as present.

    Oracle constraint names are unique within an owner, so a name on another
    table or with a different definition is a hard conflict, not an idempotent
    success.
    """
    cursor.execute(
        "SELECT table_name, constraint_type, search_condition_vc, r_constraint_name, "
        "status, validated, deferrable, deferred "
        "FROM user_constraints WHERE constraint_name = :1",
        (name,),
    )
    existing = cursor.fetchone()
    if existing is not None:
        if _oracle_constraint_definition_matches(
            cursor, name, existing, table, kind, columns, ref_table, ref_columns,
            expression,
        ):
            return True
    if strict_name:
        if existing is not None:
            raise RuntimeError(
                f"Oracle constraint name {name!r} is already used by a different constraint"
            )
        return False

    # A prior run may have used the source constraint name. Verify equivalent
    # table-local definitions before adding the generated-name version.
    cursor.execute(
        "SELECT constraint_name FROM user_constraints "
        "WHERE table_name = :1 AND constraint_type = :2",
        (table, kind),
    )
    for (candidate_name,) in cursor.fetchall():
        if candidate_name == name:
            continue
        cursor.execute(
            "SELECT table_name, constraint_type, search_condition_vc, r_constraint_name, "
            "status, validated, deferrable, deferred "
            "FROM user_constraints WHERE constraint_name = :1",
            (candidate_name,),
        )
        candidate = cursor.fetchone()
        if candidate and _oracle_constraint_definition_matches(
            cursor, candidate_name, candidate, table, kind, columns, ref_table,
            ref_columns, expression,
        ):
            return True

    if existing is not None:
        raise RuntimeError(
            f"Oracle constraint name {name!r} is already used by a different constraint"
        )
    return False


def _oracle_constraint_definition_matches(
    cursor: Any,
    name: str,
    metadata: Any,
    table: str,
    kind: str,
    columns: list[str] | None,
    ref_table: str | None,
    ref_columns: list[str] | None,
    expression: str | None,
) -> bool:
    actual_table, actual_kind, actual_expression, referenced_constraint = metadata[:4]
    if len(metadata) >= 8 and tuple(str(value).upper() for value in metadata[4:8]) != (
        "ENABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE"
    ):
        raise RuntimeError(
            f"Oracle target constraint {name!r} has a non-default state; "
            "expected ENABLED VALIDATED NOT DEFERRABLE INITIALLY IMMEDIATE"
        )
    if str(actual_table).casefold() != table.casefold() or actual_kind != kind:
        return False
    if columns is not None:
        cursor.execute(
            "SELECT column_name FROM user_cons_columns "
            "WHERE constraint_name = :1 ORDER BY position",
            (name,),
        )
        actual_columns = [row[0] for row in cursor.fetchall()]
        if [value.casefold() for value in actual_columns] != [
            value.casefold() for value in columns
        ]:
            return False
    if kind == "R":
        cursor.execute(
            "SELECT table_name FROM user_constraints WHERE constraint_name = :1",
            (referenced_constraint,),
        )
        parent = cursor.fetchone()
        cursor.execute(
            "SELECT column_name FROM user_cons_columns "
            "WHERE constraint_name = :1 ORDER BY position",
            (referenced_constraint,),
        )
        actual_ref_columns = [row[0] for row in cursor.fetchall()]
        if (
            parent is None
            or str(parent[0]).casefold() != str(ref_table).casefold()
            or [value.casefold() for value in actual_ref_columns]
            != [str(value).casefold() for value in (ref_columns or [])]
        ):
            return False
    if kind == "C" and _normalize_check_expression(actual_expression) != (
        _normalize_check_expression(expression)
    ):
        return False
    return True


def _normalize_check_expression(expression: Any) -> str:
    return " ".join(str(expression or "").strip().strip("() ").upper().split())


def _oracle_not_null_check_column(expression: Any) -> str | None:
    """Return the column only for a simple Oracle ``column IS NOT NULL`` check."""
    normalized = _normalize_check_expression(expression)
    normalized = re.sub(r"^\(+|\)+$", "", normalized).strip()
    match = re.fullmatch(r'"?([A-Z][A-Z0-9_$#]*)"?\s+IS\s+NOT\s+NULL', normalized)
    return match.group(1) if match else None


def _oracle_index_inventory(cursor: Any, table: str) -> dict[str, dict[str, Any]]:
    cursor.execute(
        "SELECT i.index_name, i.table_name, i.uniqueness, i.index_type, i.status, "
        "i.partitioned, c.constraint_type, ic.column_name, ic.column_position, ic.descend, "
        "ie.column_expression "
        "FROM user_indexes i "
        "LEFT JOIN user_constraints c ON c.table_name = i.table_name "
        "AND c.index_name = i.index_name AND c.constraint_type IN ('P','U') "
        "LEFT JOIN user_ind_columns ic ON ic.index_name = i.index_name "
        "AND ic.table_name = i.table_name "
        "LEFT JOIN user_ind_expressions ie ON ie.index_name = i.index_name "
        "AND ie.table_name = i.table_name AND ie.column_position = ic.column_position "
        "WHERE i.table_name = :1 AND NOT EXISTS (SELECT 1 FROM user_lobs l "
        "WHERE l.table_name = i.table_name AND l.index_name = i.index_name) "
        "ORDER BY i.index_name, ic.column_position",
        (table,),
    )
    catalog_rows = cursor.fetchall()
    if any(len(row) > 10 and row[10] is not None for row in catalog_rows):
        cursor.execute(
            "SELECT column_name, data_type FROM user_tab_cols "
            "WHERE table_name = :1 AND hidden_column = 'NO'",
            (table,),
        )
        column_types = {str(name): data_type for name, data_type in cursor.fetchall()}
    else:
        column_types = {}
    normalized = normalize_index_rows(
        [
            (row[0], row[2], row[3], row[4], row[5], row[7], row[8], row[9],
             row[10] if len(row) > 10 else None)
            for row in catalog_rows
        ],
        column_types,
    )
    indexes: dict[str, dict[str, Any]] = {}
    for row in catalog_rows:
        name, table_name, _uniqueness, kind, status, partitioned, constraint_type = row[:7]
        definition = normalized[name]
        indexes.setdefault(name, {
            "name": name,
            "table": table_name,
            "unique": definition["unique"],
            "index_type": definition["index_type"],
            "catalog_index_type": kind,
            "status": definition["status"],
            "partitioned": str(partitioned).upper() == "YES",
            "constraint_backed": constraint_type in {"P", "U"},
            "columns": definition["columns"],
            "descending_columns": definition["descending_columns"],
            "functional": definition["functional"],
            "supported": definition["supported"],
            "unsupported_reason": definition["reason"],
        })
    return indexes


def _oracle_lob_state(value: Any) -> int:
    """Map a LOB to bound, explicit-empty, or NULL SQL treatment."""
    if value is None:
        return 2
    if isinstance(value, oracle_precision.OracleLobPayload):
        return 1 if value.empty else 0
    if len(value) == 0:
        return 1
    return 0


def _validate_oracle_index(index: Any) -> None:
    validate_oracle_identifier(index.name, "index")
    if not index.columns:
        raise RuntimeError("index has no supported key columns")
    if index.index_type not in {None, "NORMAL"}:
        raise RuntimeError(
            f"unsupported Oracle index type {index.index_type!r}; only normal B-tree indexes are supported"
        )
    if index.included_columns or index.filter_definition or index.ddl:
        raise RuntimeError(
            "included columns, filtered indexes, and source-specific index DDL are unsupported on Oracle"
        )
    if set(index.descending_columns) - set(index.columns):
        raise RuntimeError("descending index columns must be key columns")
    for column in index.columns:
        validate_oracle_identifier(column, "index column")


def _oracle_index_is_supported_target(index: dict[str, Any]) -> bool:
    return (
        index.get("supported", True)
        and
        index["index_type"] == "NORMAL"
        and index["status"] == "VALID"
        and not index["partitioned"]
        and bool(index["columns"])
    )


def _oracle_index_definition_matches(index_info: dict[str, Any], source_index: Any) -> bool:
    return (
        _oracle_index_is_supported_target(index_info)
        and index_info["unique"] == bool(source_index.unique)
        and index_info["columns"] == list(source_index.columns)
        and index_info["descending_columns"] == list(source_index.descending_columns)
    )


def _oracle_column_signature(type_name: str, column: Any) -> tuple[str, int | None, int | None, int | None]:
    dimensions = re.search(r"\((\d+)(?:\s*,\s*(\d+))?\)", str(type_name))
    base = " ".join(re.sub(r"\(\d+(?:\s*,\s*\d+)?\)", "", str(type_name).upper()).split())
    first = int(dimensions.group(1)) if dimensions else None
    second = int(dimensions.group(2)) if dimensions and dimensions.group(2) else None
    if base in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW"}:
        return base, column.size or first, None, None
    if base == "NUMBER":
        return base, None, column.precision if column.precision is not None else first, (
            column.scale if column.scale is not None else (second or 0 if first is not None else None)
        )
    if base.startswith("TIMESTAMP"):
        return base, None, column.precision if column.precision is not None else first, None
    if base == "FLOAT":
        return base, None, column.precision if column.precision is not None else first, None
    return base, None, None, None


def _oracle_column_matches(
    source: Any, target: Any, expected_type: str, schema: Schema,
    target_schema: Schema, source_engine: str
) -> bool:
    expected = _oracle_column_signature(expected_type, source)
    actual = _oracle_column_signature(target.source_type, target)
    if expected != actual or source.nullable != target.nullable:
        return False
    if source_engine == "oracle":
        expected_semantics = schema.options.get("oracle_char_semantics", {}).get(source.name)
        actual_semantics = target_schema.options.get("oracle_char_semantics", {}).get(target.name)
        if expected_semantics != actual_semantics:
            return False
    return True


def _oracle_varchar2_narrowing(
    source: Any,
    target: Any,
    expected_type: str,
    schema: Schema,
    target_schema: Schema,
    source_engine: str,
) -> tuple[int, str] | None:
    expected = _oracle_column_signature(expected_type, source)
    actual = _oracle_column_signature(target.source_type, target)
    if (
        expected[0] != "VARCHAR2"
        or actual[0] != "VARCHAR2"
        or expected[1] is None
        or actual[1] is None
        or expected[1] >= actual[1]
        or (target.nullable and not source.nullable)
    ):
        return None

    target_semantics = target_schema.options.get("oracle_char_semantics", {}).get(target.name)
    if source_engine == "oracle":
        source_semantics = schema.options.get("oracle_char_semantics", {}).get(source.name)
        if source_semantics not in {"BYTE", "CHAR"} or source_semantics != target_semantics:
            return None
        semantics = source_semantics
    else:
        if target_semantics not in {"BYTE", "CHAR"}:
            return None
        semantics = target_semantics
    return int(expected[1]), semantics


def _assert_oracle_varchar2_values_fit(
    cursor: Any, table: str, column: str, limit: int, semantics: str
) -> None:
    length_function = "LENGTHB" if semantics == "BYTE" else "LENGTH"
    cursor.execute(
        f"SELECT MAX({length_function}({quote_identifier(column)})) "
        f"FROM {quote_identifier(table)}"
    )
    row = cursor.fetchone()
    max_length = row[0] if row else None
    if max_length is not None and int(max_length) > limit:
        unit = "bytes" if semantics == "BYTE" else "characters"
        raise RuntimeError(
            f"Cannot narrow Oracle column {table}.{column} to VARCHAR2({limit} {semantics}): "
            f"existing value length is {int(max_length)} {unit}, exceeding the limit; "
            "no ALTER or FULL target clearing/loading was performed"
        )


def _oracle_column_change_is_widening(
    source: Any, target: Any, expected_type: str, schema: Schema,
    target_schema: Schema, source_engine: str
) -> bool:
    expected = _oracle_column_signature(expected_type, source)
    actual = _oracle_column_signature(target.source_type, target)
    if expected[0] != actual[0]:
        return False
    if source_engine == "oracle":
        if schema.options.get("oracle_char_semantics", {}).get(source.name) != (
            target_schema.options.get("oracle_char_semantics", {}).get(target.name)
        ):
            return False
    if target.nullable and not source.nullable:
        return False
    if expected[0] in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW"}:
        type_widened = expected[1] is not None and actual[1] is not None and expected[1] >= actual[1]
    elif expected[0] == "NUMBER":
        type_widened = expected[3] == actual[3] and (
            expected[2] is None or (actual[2] is not None and expected[2] >= actual[2])
        )
    elif expected[0].startswith("TIMESTAMP") or expected[0] == "FLOAT":
        type_widened = expected[2] is not None and actual[2] is not None and expected[2] >= actual[2]
    else:
        type_widened = expected == actual
    nullability_relaxed = source.nullable and not target.nullable
    return type_widened or (expected == actual and nullability_relaxed)


def _assert_oracle_column_has_no_dependencies(cursor: Any, table: str, column: str) -> None:
    cursor.execute(
        "SELECT c.constraint_name FROM user_constraints c "
        "JOIN user_cons_columns cc ON cc.constraint_name = c.constraint_name "
        "WHERE c.table_name = :1 AND cc.column_name = :2",
        (table, column),
    )
    constraints = cursor.fetchall()
    cursor.execute(
        "SELECT fk.constraint_name FROM user_constraints fk "
        "WHERE fk.constraint_type = 'R' AND fk.r_constraint_name IN ("
        "SELECT c.constraint_name FROM user_constraints c "
        "JOIN user_cons_columns cc ON cc.constraint_name = c.constraint_name "
        "WHERE c.table_name = :1 AND cc.column_name = :2 "
        "AND c.constraint_type IN ('P', 'U'))",
        (table, column),
    )
    incoming = cursor.fetchall()
    cursor.execute(
        "SELECT index_name FROM user_ind_columns "
        "WHERE table_name = :1 AND column_name = :2",
        (table, column),
    )
    indexes = cursor.fetchall()
    cursor.execute(
        "SELECT name FROM user_dependencies WHERE referenced_name = :1 "
        "AND referenced_type = 'TABLE' AND type = 'VIEW'",
        (table,),
    )
    views = cursor.fetchall()
    if constraints or incoming or indexes or views:
        dependencies = []
        if constraints:
            dependencies.append("constraints")
        if incoming:
            dependencies.append("incoming foreign keys")
        if indexes:
            dependencies.append("indexes")
        if views:
            dependencies.append("dependent views")
        raise RuntimeError(
            f"Cannot safely reconcile Oracle column {table}.{column}; "
            f"it has {', '.join(dependencies)}. Resolve dependencies explicitly first."
        )
