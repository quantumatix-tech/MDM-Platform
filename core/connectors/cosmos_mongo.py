from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from core.connectors.base import (
    TargetConnector,
    CDCEngine,
    Schema,
    UpsertResult,
    ApplyResult,
    ChangeEvent,
    validate_identifier,
)
from core.driver_installer import ensure_driver
from core.retry import retry_with_backoff
from core.audit_logger import audit_log


class CosmosMongoTargetConnector(TargetConnector):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._client: Any = None
        self._db: Any = None
        self._max_doc_size_bytes = config.get("max_document_size_mb", 2) * 1024 * 1024

    @retry_with_backoff(
        max_retries=5,
        base_delay=1.0,
        max_delay=60.0,
        retry_on=(Exception,),
    )
    def connect(self) -> None:
        ensure_driver("pymongo")
        from pymongo import MongoClient

        host = self._config["host"]
        database = self._config["database"]
        username = self._config.get("username")
        password = self._config.get("password")
        ssl = self._config.get("ssl", True)

        validate_identifier(database, "database")

        conn_kwargs: dict[str, Any] = {
            "host": host,
            "tls": ssl,
            "tlsAllowInvalidCertificates": False,
            "retryWrites": True,
            "serverSelectionTimeoutMS": 30000,
        }

        if username and password:
            conn_kwargs["authSource"] = "admin"
            conn_kwargs["username"] = username
            conn_kwargs["password"] = password

        self._client = MongoClient(**conn_kwargs)
        self._db = self._client[database]
        audit_log(phase="connect", status="success", details={"engine": "cosmos_mongo", "role": "target"})

    def ensure_database_exists(self) -> None:
        db_name = self._config["database"]
        validate_identifier(db_name, "database")
        self._client[db_name].command("ping")

    def create_object_if_missing(self, schema: Schema) -> None:
        validate_identifier(schema.name, "collection")
        existing = self._db.list_collection_names()
        if schema.name in existing:
            return
        self._db.create_collection(schema.name)
        audit_log(phase="create_collection", status="created", details={"collection": schema.name})

    def upsert_batch(self, object_name: str, rows: Iterator[dict[str, Any]], schema: Schema | None = None) -> UpsertResult:
        validate_identifier(object_name, "collection")
        result = UpsertResult()
        batch = list(rows)

        if not batch:
            return result

        from pymongo import ReplaceOne

        operations = []
        for row in batch:
            doc_size = len(str(row).encode("utf-8"))
            if doc_size > self._max_doc_size_bytes:
                result.failure_count += 1
                result.errors.append(
                    f"Document exceeds {self._max_doc_size_bytes} bytes limit: {doc_size} bytes"
                )
                result.failed_items.append(row)
                continue

            doc_id = row.pop("_id", None)
            if doc_id is not None:
                filter_doc = {"_id": doc_id}
            else:
                filter_doc = row
            operations.append(ReplaceOne(filter_doc, row, upsert=True))

        if not operations:
            return result

        try:
            write_result = self._db[object_name].bulk_write(operations, ordered=False)
            result.success_count = write_result.upserted_count + write_result.modified_count
            audit_log(phase="upsert_batch", status="success", details={"collection": object_name, "count": result.success_count})
        except Exception as exc:
            result.failure_count = len(operations)
            result.errors.append(str(exc))
            result.failed_items.extend(batch)
            audit_log(phase="upsert_batch", status="failure", details={"collection": object_name, "error": str(exc)})

        return result

    def get_object_count(self, object_name: str, schema_name: str | None = None) -> int:
        validate_identifier(object_name, "collection")
        return self._db[object_name].count_documents({})

    def delete(self, object_name: str, document: dict[str, Any], schema: Schema | None = None) -> None:
        validate_identifier(object_name, "collection")
        doc_id = document.get("_id")
        if doc_id is not None:
            self._db[object_name].delete_one({"_id": doc_id})
        elif schema and schema.primary_key:
            filter_doc = {pk: document.get(pk) for pk in schema.primary_key}
            self._db[object_name].delete_one(filter_doc)
        audit_log(phase="cdc_delete", status="deleted", details={"collection": object_name})

    def export_full(self, object_name: str, schema_name: str | None = None) -> Iterator[dict[str, Any]]:
        validate_identifier(object_name, "collection")
        cursor = self._db[object_name].find().batch_size(100)
        for doc in cursor:
            if "_id" in doc:
                doc["_id"] = str(doc["_id"])
            yield doc


class CosmosMongoCDCEngine(CDCEngine):
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._client: Any = None
        self._db: Any = None
        self._last_change_token: str | None = None

    @retry_with_backoff(max_retries=3, base_delay=1.0)
    def connect(self) -> None:
        from pymongo import MongoClient

        host = self._config["host"]
        database = self._config["database"]
        username = self._config.get("username")
        password = self._config.get("password")
        ssl = self._config.get("ssl", True)

        conn_kwargs: dict[str, Any] = {
            "host": host,
            "tls": ssl,
            "tlsAllowInvalidCertificates": False,
            "retryWrites": True,
            "serverSelectionTimeoutMS": 30000,
        }

        if username and password:
            conn_kwargs["authSource"] = "admin"
            conn_kwargs["username"] = username
            conn_kwargs["password"] = password

        self._client = MongoClient(**conn_kwargs)
        self._db = self._client[database]

    def start(self) -> None:
        audit_log(phase="cdc_start", status="success", details={"engine": "cosmos_mongo"})

    def poll_changes(self) -> list[ChangeEvent]:

        pipeline = []
        if self._last_change_token is not None:
            pipeline.append({"$resumeAfter": self._last_change_token})

        change_stream = self._db.watch(pipeline)
        events = []
        try:
            for change in change_stream:
                ns = change.get("ns", {}).get("coll", "unknown")
                events.append(
                    ChangeEvent(
                        operation=change.get("operationType", "insert"),
                        document=change.get("fullDocument", {}),
                        object_name=ns,
                        watermark=change.get("_id"),
                    )
                )
        except Exception:
            pass
        finally:
            change_stream.close()

        return events

    def apply(self, events: list[ChangeEvent], target: TargetConnector) -> ApplyResult:
        result = ApplyResult()
        if not events:
            return result

        for event in events:
            collection_name = event.object_name or "unknown"
            try:
                if event.operation == "insert":
                    target.upsert_batch(collection_name, iter([event.document]), event.schema)
                elif event.operation == "update":
                    target.upsert_batch(collection_name, iter([event.document]), event.schema)
                elif event.operation == "delete":
                    target.delete(collection_name, event.document, event.schema)
                result.success_count += 1
            except Exception as exc:
                result.failure_count += 1
                result.errors.append(str(exc))

        if result.failure_count == 0:
            result.last_checkpoint = events[-1].watermark
            audit_log(phase="cdc_apply", status="success", details={"applied": result.success_count})
        else:
            audit_log(phase="cdc_apply", status="partial_failure", details={"success": result.success_count, "failure": result.failure_count})

        return result

    def checkpoint(self, result: ApplyResult) -> None:
        if result.last_checkpoint is None:
            return

        self._last_change_token = result.last_checkpoint
        audit_log(phase="cdc_checkpoint", status="advanced", details={"change_token": str(result.last_checkpoint)})