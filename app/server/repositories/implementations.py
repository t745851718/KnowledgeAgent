"""Persistence abstractions for document and conversation metadata.

Both implementations expose an asynchronous API. ``MongoRepository`` moves
PyMongo's blocking operations to worker threads, while ``MemoryRepository`` is
useful for tests and local development without MongoDB.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol, runtime_checkable

from ..core.ids import (
    new_conversation_id,
    new_document_id,
    new_message_id,
    to_iso8601,
    utc_now,
)


Record = dict[str, Any]


def _utc_datetime(value: Any) -> Any:
    """Convert supported timestamps to UTC datetimes for MongoDB storage."""

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return value


def _prepare(record: Mapping[str, Any], *, prefix: str) -> Record:
    prepared = deepcopy(dict(record))
    identifier = prepared.pop("_id", None) or prepared.pop("id", None)
    if identifier is None:
        generators = {
            "doc": new_document_id,
            "conv": new_conversation_id,
            "msg": new_message_id,
        }
        identifier = generators[prefix]()
    prepared["_id"] = str(identifier)

    now = utc_now()
    prepared["created_at"] = _utc_datetime(prepared.get("created_at", now))
    if prefix in {"doc", "conv"}:
        prepared["updated_at"] = _utc_datetime(prepared.get("updated_at", now))

    # A sparse Mongo index still indexes explicit null values. Omitting an
    # absent request ID allows multiple ordinary messages to coexist.
    if prefix == "msg" and prepared.get("request_id") is None:
        prepared.pop("request_id", None)
    return prepared


def serialize_record(record: Mapping[str, Any] | None) -> Record | None:
    """Convert a database record to an API-safe dictionary.

    Mongo's ``_id`` becomes ``id`` and all datetimes are rendered as ISO 8601
    UTC timestamps. Nested citation and metadata values are converted too.
    """

    if record is None:
        return None

    def convert(value: Any) -> Any:
        if isinstance(value, datetime):
            return to_iso8601(value)
        if isinstance(value, Mapping):
            return {str(key): convert(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(item) for item in value]
        return value

    serialized = convert(record)
    if "_id" in serialized:
        serialized["id"] = str(serialized.pop("_id"))
    return serialized


@runtime_checkable
class Repository(Protocol):
    async def initialize(self) -> None: ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...

    async def create_document(self, document: Mapping[str, Any]) -> Record: ...

    async def get_document(
        self, document_id: str, *, owner_id: str | None = None
    ) -> Record | None: ...

    async def list_documents(
        self, owner_id: str, status: str | None = None
    ) -> list[Record]: ...

    async def get_document_by_idempotency_key(
        self, owner_id: str, idempotency_key: str
    ) -> Record | None: ...

    async def update_document(
        self,
        document_id: str,
        updates: Mapping[str, Any],
        *,
        owner_id: str | None = None,
    ) -> Record | None: ...

    async def delete_document(
        self, document_id: str, *, owner_id: str | None = None
    ) -> bool: ...

    async def create_conversation(self, conversation: Mapping[str, Any]) -> Record: ...

    async def get_conversation(
        self, conversation_id: str, *, owner_id: str | None = None
    ) -> Record | None: ...

    async def list_conversations(
        self, *, owner_id: str | None = None, limit: int = 20
    ) -> list[Record]: ...

    async def update_conversation(
        self,
        conversation_id: str,
        updates: Mapping[str, Any],
        *,
        owner_id: str | None = None,
    ) -> Record | None: ...

    async def delete_conversation(
        self, conversation_id: str, *, owner_id: str | None = None
    ) -> bool: ...

    async def create_message(self, message: Mapping[str, Any]) -> Record: ...

    async def get_message(self, message_id: str) -> Record | None: ...

    async def get_message_by_request_id(self, request_id: str) -> Record | None: ...

    async def list_messages(
        self, conversation_id: str, *, limit: int = 100
    ) -> list[Record]: ...


class MongoRepository:
    """PyMongo-backed repository with an async, event-loop-safe facade."""

    def __init__(
        self,
        uri: str | None = None,
        database: str | None = None,
        *,
        client: Any | None = None,
    ) -> None:
        if client is None:
            if not uri:
                raise ValueError("uri is required when client is not provided")
            from pymongo import MongoClient

            client = MongoClient(uri, connect=False)
        if not database:
            raise ValueError("database is required")

        self.client = client
        self.database = client[database]
        self.documents = self.database["documents"]
        self.conversations = self.database["conversations"]
        self.messages = self.database["messages"]

    async def initialize(self) -> None:
        def create_indexes() -> None:
            self.documents.create_index(
                [("owner_id", 1), ("created_at", -1)],
                name="documents_owner_created_at",
            )
            self.documents.create_index(
                [("owner_id", 1), ("idempotency_key", 1)],
                unique=True,
                partialFilterExpression={"idempotency_key": {"$type": "string"}},
                name="documents_owner_idempotency_key",
            )
            self.conversations.create_index(
                [("owner_id", 1), ("updated_at", -1)],
                name="conversations_owner_updated_at",
            )
            self.messages.create_index(
                [("conversation_id", 1), ("created_at", 1)],
                name="messages_conversation_created_at",
            )
            self.messages.create_index(
                [("request_id", 1)],
                unique=True,
                sparse=True,
                name="messages_request_id_unique",
            )

        await asyncio.to_thread(create_indexes)

    async def ping(self) -> bool:
        result = await asyncio.to_thread(self.database.command, "ping")
        return bool(result.get("ok"))

    async def close(self) -> None:
        await asyncio.to_thread(self.client.close)

    @staticmethod
    def _resource_filter(identifier: str, owner_id: str | None) -> Record:
        query: Record = {"_id": identifier}
        if owner_id is not None:
            query["owner_id"] = owner_id
        return query

    async def create_document(self, document: Mapping[str, Any]) -> Record:
        prepared = _prepare(document, prefix="doc")
        await asyncio.to_thread(self.documents.insert_one, prepared)
        return serialize_record(prepared) or {}

    async def get_document(
        self, document_id: str, *, owner_id: str | None = None
    ) -> Record | None:
        query = self._resource_filter(document_id, owner_id)
        record = await asyncio.to_thread(self.documents.find_one, query)
        return serialize_record(record)

    async def list_documents(
        self, owner_id: str, status: str | None = None
    ) -> list[Record]:
        query: Record = {"owner_id": owner_id}
        if status is not None:
            query["status"] = status

        def find() -> list[Mapping[str, Any]]:
            return list(self.documents.find(query).sort("created_at", -1))

        records = await asyncio.to_thread(find)
        return [serialize_record(record) or {} for record in records]

    async def get_document_by_idempotency_key(
        self, owner_id: str, idempotency_key: str
    ) -> Record | None:
        record = await asyncio.to_thread(
            self.documents.find_one,
            {"owner_id": owner_id, "idempotency_key": idempotency_key},
        )
        return serialize_record(record)

    async def update_document(
        self,
        document_id: str,
        updates: Mapping[str, Any],
        *,
        owner_id: str | None = None,
    ) -> Record | None:
        changes = deepcopy(dict(updates))
        for immutable in ("_id", "id", "created_at"):
            changes.pop(immutable, None)
        changes["updated_at"] = utc_now()
        query = self._resource_filter(document_id, owner_id)

        def update() -> Mapping[str, Any] | None:
            from pymongo import ReturnDocument

            return self.documents.find_one_and_update(
                query,
                {"$set": changes},
                return_document=ReturnDocument.AFTER,
            )

        return serialize_record(await asyncio.to_thread(update))

    async def delete_document(
        self, document_id: str, *, owner_id: str | None = None
    ) -> bool:
        query = self._resource_filter(document_id, owner_id)
        result = await asyncio.to_thread(self.documents.delete_one, query)
        return result.deleted_count == 1

    async def create_conversation(self, conversation: Mapping[str, Any]) -> Record:
        prepared = _prepare(conversation, prefix="conv")
        await asyncio.to_thread(self.conversations.insert_one, prepared)
        return serialize_record(prepared) or {}

    async def get_conversation(
        self, conversation_id: str, *, owner_id: str | None = None
    ) -> Record | None:
        query = self._resource_filter(conversation_id, owner_id)
        record = await asyncio.to_thread(self.conversations.find_one, query)
        return serialize_record(record)

    async def list_conversations(
        self, *, owner_id: str | None = None, limit: int = 20
    ) -> list[Record]:
        query: Record = {} if owner_id is None else {"owner_id": owner_id}

        def find() -> list[Mapping[str, Any]]:
            return list(
                self.conversations.find(query).sort("updated_at", -1).limit(limit)
            )

        records = await asyncio.to_thread(find)
        return [serialize_record(record) or {} for record in records]

    async def update_conversation(
        self,
        conversation_id: str,
        updates: Mapping[str, Any],
        *,
        owner_id: str | None = None,
    ) -> Record | None:
        changes = deepcopy(dict(updates))
        for immutable in ("_id", "id", "owner_id", "created_at"):
            changes.pop(immutable, None)
        changes["updated_at"] = utc_now()
        query = self._resource_filter(conversation_id, owner_id)

        def update() -> Mapping[str, Any] | None:
            from pymongo import ReturnDocument

            return self.conversations.find_one_and_update(
                query,
                {"$set": changes},
                return_document=ReturnDocument.AFTER,
            )

        return serialize_record(await asyncio.to_thread(update))

    async def delete_conversation(
        self, conversation_id: str, *, owner_id: str | None = None
    ) -> bool:
        query = self._resource_filter(conversation_id, owner_id)

        def delete() -> bool:
            result = self.conversations.delete_one(query)
            if result.deleted_count:
                self.messages.delete_many({"conversation_id": conversation_id})
                return True
            return False

        return await asyncio.to_thread(delete)

    async def create_message(self, message: Mapping[str, Any]) -> Record:
        prepared = _prepare(message, prefix="msg")
        request_id = prepared.get("request_id")
        if request_id:
            existing = await self.get_message_by_request_id(str(request_id))
            if existing is not None:
                return existing

        try:
            await asyncio.to_thread(self.messages.insert_one, prepared)
        except Exception as error:
            # Handle concurrent retries without masking unrelated database
            # failures. Importing here keeps module import lightweight.
            from pymongo.errors import DuplicateKeyError

            if not isinstance(error, DuplicateKeyError) or not request_id:
                raise
            existing = await self.get_message_by_request_id(str(request_id))
            if existing is None:
                raise
            return existing

        conversation_id = prepared.get("conversation_id")
        if conversation_id:
            await asyncio.to_thread(
                self.conversations.update_one,
                {"_id": conversation_id},
                {"$set": {"updated_at": prepared["created_at"]}},
            )
        return serialize_record(prepared) or {}

    async def get_message(self, message_id: str) -> Record | None:
        record = await asyncio.to_thread(self.messages.find_one, {"_id": message_id})
        return serialize_record(record)

    async def get_message_by_request_id(self, request_id: str) -> Record | None:
        record = await asyncio.to_thread(
            self.messages.find_one, {"request_id": request_id}
        )
        return serialize_record(record)

    async def list_messages(
        self, conversation_id: str, *, limit: int = 100
    ) -> list[Record]:
        def find() -> list[Mapping[str, Any]]:
            records = list(
                self.messages.find({"conversation_id": conversation_id})
                .sort("created_at", -1)
                .limit(limit)
            )
            records.reverse()
            return records

        records = await asyncio.to_thread(find)
        return [serialize_record(record) or {} for record in records]


class MemoryRepository:
    """In-process repository with MongoRepository-compatible semantics."""

    def __init__(self) -> None:
        self._documents: dict[str, Record] = {}
        self._conversations: dict[str, Record] = {}
        self._messages: dict[str, Record] = {}
        self._messages_by_request_id: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        return None

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    @staticmethod
    def _matches_owner(record: Mapping[str, Any], owner_id: str | None) -> bool:
        return owner_id is None or record.get("owner_id") == owner_id

    async def create_document(self, document: Mapping[str, Any]) -> Record:
        prepared = _prepare(document, prefix="doc")
        async with self._lock:
            if prepared["_id"] in self._documents:
                raise ValueError(f"document {prepared['_id']!r} already exists")
            idempotency_key = prepared.get("idempotency_key")
            if idempotency_key and any(
                record.get("owner_id") == prepared.get("owner_id")
                and record.get("idempotency_key") == idempotency_key
                for record in self._documents.values()
            ):
                raise ValueError("document idempotency key already exists")
            self._documents[prepared["_id"]] = deepcopy(prepared)
        return serialize_record(prepared) or {}

    async def get_document(
        self, document_id: str, *, owner_id: str | None = None
    ) -> Record | None:
        async with self._lock:
            record = self._documents.get(document_id)
            if record is None or not self._matches_owner(record, owner_id):
                return None
            return serialize_record(deepcopy(record))

    async def list_documents(
        self, owner_id: str, status: str | None = None
    ) -> list[Record]:
        async with self._lock:
            records = [
                deepcopy(record)
                for record in self._documents.values()
                if record.get("owner_id") == owner_id
                and (status is None or record.get("status") == status)
            ]
        records.sort(
            key=lambda item: item.get(
                "created_at", datetime.min.replace(tzinfo=UTC)
            ),
            reverse=True,
        )
        return [serialize_record(record) or {} for record in records]

    async def get_document_by_idempotency_key(
        self, owner_id: str, idempotency_key: str
    ) -> Record | None:
        async with self._lock:
            for record in self._documents.values():
                if (
                    record.get("owner_id") == owner_id
                    and record.get("idempotency_key") == idempotency_key
                ):
                    return serialize_record(deepcopy(record))
        return None

    async def update_document(
        self,
        document_id: str,
        updates: Mapping[str, Any],
        *,
        owner_id: str | None = None,
    ) -> Record | None:
        async with self._lock:
            record = self._documents.get(document_id)
            if record is None or not self._matches_owner(record, owner_id):
                return None
            changes = deepcopy(dict(updates))
            for immutable in ("_id", "id", "created_at"):
                changes.pop(immutable, None)
            record.update(changes)
            record["updated_at"] = utc_now()
            return serialize_record(deepcopy(record))

    async def delete_document(
        self, document_id: str, *, owner_id: str | None = None
    ) -> bool:
        async with self._lock:
            record = self._documents.get(document_id)
            if record is None or not self._matches_owner(record, owner_id):
                return False
            del self._documents[document_id]
            return True

    async def create_conversation(self, conversation: Mapping[str, Any]) -> Record:
        prepared = _prepare(conversation, prefix="conv")
        async with self._lock:
            if prepared["_id"] in self._conversations:
                raise ValueError(f"conversation {prepared['_id']!r} already exists")
            self._conversations[prepared["_id"]] = deepcopy(prepared)
        return serialize_record(prepared) or {}

    async def get_conversation(
        self, conversation_id: str, *, owner_id: str | None = None
    ) -> Record | None:
        async with self._lock:
            record = self._conversations.get(conversation_id)
            if record is None or not self._matches_owner(record, owner_id):
                return None
            return serialize_record(deepcopy(record))

    async def list_conversations(
        self, *, owner_id: str | None = None, limit: int = 20
    ) -> list[Record]:
        async with self._lock:
            records = [
                deepcopy(record)
                for record in self._conversations.values()
                if self._matches_owner(record, owner_id)
            ]
        records.sort(
            key=lambda item: item.get(
                "updated_at", datetime.min.replace(tzinfo=UTC)
            ),
            reverse=True,
        )
        return [serialize_record(record) or {} for record in records[:limit]]

    async def update_conversation(
        self,
        conversation_id: str,
        updates: Mapping[str, Any],
        *,
        owner_id: str | None = None,
    ) -> Record | None:
        async with self._lock:
            record = self._conversations.get(conversation_id)
            if record is None or not self._matches_owner(record, owner_id):
                return None
            changes = deepcopy(dict(updates))
            for immutable in ("_id", "id", "owner_id", "created_at"):
                changes.pop(immutable, None)
            record.update(changes)
            record["updated_at"] = utc_now()
            return serialize_record(deepcopy(record))

    async def delete_conversation(
        self, conversation_id: str, *, owner_id: str | None = None
    ) -> bool:
        async with self._lock:
            record = self._conversations.get(conversation_id)
            if record is None or not self._matches_owner(record, owner_id):
                return False
            del self._conversations[conversation_id]
            message_ids = [
                identifier
                for identifier, message in self._messages.items()
                if message.get("conversation_id") == conversation_id
            ]
            for identifier in message_ids:
                message = self._messages.pop(identifier)
                request_id = message.get("request_id")
                if request_id:
                    self._messages_by_request_id.pop(str(request_id), None)
            return True

    async def create_message(self, message: Mapping[str, Any]) -> Record:
        prepared = _prepare(message, prefix="msg")
        async with self._lock:
            request_id = prepared.get("request_id")
            if request_id:
                existing_id = self._messages_by_request_id.get(str(request_id))
                if existing_id is not None:
                    return serialize_record(deepcopy(self._messages[existing_id])) or {}
            if prepared["_id"] in self._messages:
                raise ValueError(f"message {prepared['_id']!r} already exists")
            self._messages[prepared["_id"]] = deepcopy(prepared)
            if request_id:
                self._messages_by_request_id[str(request_id)] = prepared["_id"]
            conversation_id = prepared.get("conversation_id")
            conversation = self._conversations.get(str(conversation_id))
            if conversation is not None:
                conversation["updated_at"] = prepared["created_at"]
        return serialize_record(prepared) or {}

    async def get_message(self, message_id: str) -> Record | None:
        async with self._lock:
            return serialize_record(deepcopy(self._messages.get(message_id)))

    async def get_message_by_request_id(self, request_id: str) -> Record | None:
        async with self._lock:
            message_id = self._messages_by_request_id.get(request_id)
            if message_id is None:
                return None
            return serialize_record(deepcopy(self._messages[message_id]))

    async def list_messages(
        self, conversation_id: str, *, limit: int = 100
    ) -> list[Record]:
        async with self._lock:
            records = [
                deepcopy(record)
                for record in self._messages.values()
                if record.get("conversation_id") == conversation_id
            ]
        records.sort(
            key=lambda item: item.get(
                "created_at", datetime.min.replace(tzinfo=UTC)
            )
        )
        return [serialize_record(record) or {} for record in records[-limit:]]


__all__ = [
    "MemoryRepository",
    "MongoRepository",
    "Record",
    "Repository",
    "serialize_record",
]
