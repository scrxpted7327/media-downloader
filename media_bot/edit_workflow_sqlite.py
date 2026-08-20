"""SQLite persistence adapter for :mod:`media_bot.edit_workflow`.

This module is deliberately limited to durable workflow records.  It does not
know about Telegram, PWA requests, editors, or metadata providers.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from .edit_workflow import (
    AttemptRecord,
    DeliveryRecord,
    DeliveryState,
    MetadataOutput,
    PhaseRecord,
    PhaseState,
    RenderArtifact,
    SettingsSnapshot,
    WorkflowRecord,
    WorkflowState,
)


SQLITE_BUSY_TIMEOUT_MS = 5_000
PAYLOAD_VERSION = 1


class WorkflowPayloadError(ValueError):
    """Raised when a stored workflow payload is not the supported schema."""


class ConcurrentWorkflowSaveError(RuntimeError):
    """Raised when a save would overwrite a newer or conflicting snapshot."""


async def init(db_path: Path) -> None:
    """Create the adapter's additive schema, safely and idempotently."""

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with _open(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS edit_workflow_records (
                workflow_id TEXT PRIMARY KEY,
                payload_version INTEGER NOT NULL,
                sequence INTEGER NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_edit_workflow_records_sequence
                ON edit_workflow_records(sequence);
            """
        )
        await db.commit()


class SQLiteWorkflowPersistence:
    """Transactional SQLite implementation of ``WorkflowPersistence``."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    async def init(self) -> None:
        await init(self.db_path)

    async def load(self, workflow_id: str) -> WorkflowRecord | None:
        async with _open(self.db_path) as db:
            async with db.execute(
                "SELECT workflow_id, payload_version, sequence, payload "
                "FROM edit_workflow_records WHERE workflow_id = ?",
                (workflow_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        if row[0] != workflow_id:
            raise WorkflowPayloadError("workflow ID does not match record key")
        if row[1] != PAYLOAD_VERSION:
            raise WorkflowPayloadError(f"unknown payload version: {row[1]!r}")
        if row[2] < 0:
            raise WorkflowPayloadError("sequence must be non-negative")
        record = _decode_record(row[3])
        if record.workflow_id != row[0] or record.sequence != row[2]:
            raise WorkflowPayloadError("database envelope does not match payload")
        return record

    async def save(self, record: WorkflowRecord) -> None:
        payload = _encode_record(record)
        async with _open(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT payload_version, sequence, payload "
                    "FROM edit_workflow_records WHERE workflow_id = ?",
                    (record.workflow_id,),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing is not None:
                    if existing[0] != PAYLOAD_VERSION:
                        raise WorkflowPayloadError(f"unknown payload version: {existing[0]!r}")
                    if existing[1] > record.sequence:
                        raise ConcurrentWorkflowSaveError(
                            f"workflow {record.workflow_id!r} has a newer sequence"
                        )
                    if existing[1] == record.sequence:
                        if existing[2] == payload:
                            await db.commit()
                            return
                        raise ConcurrentWorkflowSaveError(
                            f"conflicting save for workflow {record.workflow_id!r} "
                            f"at sequence {record.sequence}"
                        )
                await db.execute(
                    "INSERT INTO edit_workflow_records "
                    "(workflow_id, payload_version, sequence, payload) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(workflow_id) DO UPDATE SET "
                    "payload_version=excluded.payload_version, sequence=excluded.sequence, "
                    "payload=excluded.payload, updated_at=datetime('now')",
                    (record.workflow_id, PAYLOAD_VERSION, record.sequence, payload),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise


@asynccontextmanager
async def _open(db_path: Path):
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    try:
        yield db
    finally:
        await db.close()


def _encode_record(record: WorkflowRecord) -> str:
    payload = {
        "version": PAYLOAD_VERSION,
        "workflow_id": record.workflow_id,
        "source_path": str(record.source_path),
        "output_path": str(record.output_path),
        "state": record.state.value,
        "attempt": _encode_attempt(record.attempt),
        "history": [_encode_attempt(item) for item in record.history],
        "sequence": record.sequence,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _encode_attempt(attempt: AttemptRecord) -> dict[str, Any]:
    return {
        "number": attempt.number,
        "settings": _encode_settings(attempt.settings),
        "render": _encode_phase(attempt.render),
        "review": _encode_phase(attempt.review),
        "metadata": _encode_phase(attempt.metadata),
        "delivery": {"state": attempt.delivery.state.value, "error": attempt.delivery.error},
        "artifact": None if attempt.artifact is None else {
            "path": str(attempt.artifact.path),
            "size_bytes": attempt.artifact.size_bytes,
            "content_hash": attempt.artifact.content_hash,
            "validated": attempt.artifact.validated,
        },
        "metadata_output": None if attempt.metadata_output is None else {
            "title": attempt.metadata_output.title,
            "hashtags": list(attempt.metadata_output.hashtags),
            "provenance": _encode_value(attempt.metadata_output.provenance),
        },
        "cancel_requested": attempt.cancel_requested,
        "error": attempt.error,
    }


def _encode_settings(settings: SettingsSnapshot) -> dict[str, Any]:
    return {
        "values": [[key, _encode_value(value)] for key, value in settings.values],
        "fingerprint": settings.fingerprint,
    }


def _encode_phase(phase: PhaseRecord) -> dict[str, Any]:
    return {"state": phase.state.value, "idempotency_key": phase.idempotency_key, "error": phase.error}


def _encode_value(value: Any) -> Any:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("workflow mappings require string keys")
        return {"type": "mapping", "items": [[key, _encode_value(item)] for key, item in sorted(value.items())]}
    if isinstance(value, tuple):
        return {"type": "tuple", "items": [_encode_value(item) for item in value]}
    if value is None or isinstance(value, (str, bool, int, float)):
        return {"type": "scalar", "value": value}
    raise TypeError(f"unsupported workflow JSON value: {type(value).__name__}")


def _decode_record(raw: str) -> WorkflowRecord:
    value = _loads(raw)
    _keys(value, {"version", "workflow_id", "source_path", "output_path", "state", "attempt", "history", "sequence"})
    if value["version"] != PAYLOAD_VERSION:
        raise WorkflowPayloadError(f"unknown payload version: {value['version']!r}")
    workflow_id = _string(value["workflow_id"], "workflow_id")
    sequence = _nonnegative_int(value["sequence"], "sequence")
    history = tuple(_decode_attempt(item) for item in _list(value["history"], "history"))
    record = WorkflowRecord(
        workflow_id=workflow_id,
        source_path=Path(_string(value["source_path"], "source_path")),
        output_path=Path(_string(value["output_path"], "output_path")),
        state=_enum(WorkflowState, value["state"], "state"),
        attempt=_decode_attempt(value["attempt"]),
        history=history,
        sequence=sequence,
    )
    if record.attempt.number <= 0:
        raise WorkflowPayloadError("attempt number must be positive")
    return record


def _decode_attempt(value: Any) -> AttemptRecord:
    _keys(value, {"number", "settings", "render", "review", "metadata", "delivery", "artifact", "metadata_output", "cancel_requested", "error"})
    attempt = AttemptRecord(
        number=_positive_int(value["number"], "attempt.number"),
        settings=_decode_settings(value["settings"]),
        render=_decode_phase(value["render"]),
        review=_decode_phase(value["review"]),
        metadata=_decode_phase(value["metadata"]),
        delivery=_decode_delivery(value["delivery"]),
        artifact=_decode_artifact(value["artifact"]),
        metadata_output=_decode_metadata(value["metadata_output"]),
        cancel_requested=_bool(value["cancel_requested"], "attempt.cancel_requested"),
        error=_optional_string(value["error"], "attempt.error"),
    )
    return attempt


def _decode_settings(value: Any) -> SettingsSnapshot:
    _keys(value, {"values", "fingerprint"})
    pairs = []
    for pair in _list(value["values"], "settings.values"):
        if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str):
            raise WorkflowPayloadError("settings values must be [string, value] pairs")
        if any(existing[0] == pair[0] for existing in pairs):
            raise WorkflowPayloadError("settings contains duplicate keys")
        pairs.append((pair[0], _decode_value(pair[1])))
    settings = SettingsSnapshot(tuple(pairs), _string(value["fingerprint"], "settings.fingerprint"))
    if SettingsSnapshot.from_mapping(dict(settings.values)).fingerprint != settings.fingerprint:
        raise WorkflowPayloadError("settings fingerprint mismatch")
    return settings


def _decode_phase(value: Any) -> PhaseRecord:
    _keys(value, {"state", "idempotency_key", "error"})
    return PhaseRecord(
        state=_enum(PhaseState, value["state"], "phase.state"),
        idempotency_key=_optional_string(value["idempotency_key"], "phase.idempotency_key"),
        error=_optional_string(value["error"], "phase.error"),
    )


def _decode_delivery(value: Any) -> DeliveryRecord:
    _keys(value, {"state", "error"})
    return DeliveryRecord(
        state=_enum(DeliveryState, value["state"], "delivery.state"),
        error=_optional_string(value["error"], "delivery.error"),
    )


def _decode_artifact(value: Any) -> RenderArtifact | None:
    if value is None:
        return None
    _keys(value, {"path", "size_bytes", "content_hash", "validated"})
    return RenderArtifact(
        path=Path(_string(value["path"], "artifact.path")),
        size_bytes=_nonnegative_int(value["size_bytes"], "artifact.size_bytes"),
        content_hash=_optional_string(value["content_hash"], "artifact.content_hash"),
        validated=_bool(value["validated"], "artifact.validated"),
    )


def _decode_metadata(value: Any) -> MetadataOutput | None:
    if value is None:
        return None
    _keys(value, {"title", "hashtags", "provenance"})
    hashtags = tuple(_string(item, "metadata_output.hashtag") for item in _list(value["hashtags"], "metadata_output.hashtags"))
    provenance = _decode_value(value["provenance"])
    if not isinstance(provenance, dict):
        raise WorkflowPayloadError("metadata provenance must be a mapping")
    return MetadataOutput(
        title=_string(value["title"], "metadata_output.title"),
        hashtags=hashtags,
        provenance=provenance,
    )


def _decode_value(value: Any) -> Any:
    _keys(value, {"type", "items"} if isinstance(value, dict) and value.get("type") in {"tuple", "mapping"} else {"type", "value"})
    kind = value["type"]
    if kind == "scalar":
        scalar = value["value"]
        if scalar is not None and not isinstance(scalar, (str, bool, int, float)):
            raise WorkflowPayloadError("invalid scalar value")
        return scalar
    if kind == "tuple":
        return tuple(_decode_value(item) for item in _list(value["items"], "tuple.items"))
    if kind == "mapping":
        result: dict[str, Any] = {}
        for pair in _list(value["items"], "mapping.items"):
            if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str):
                raise WorkflowPayloadError("mapping values must be [string, value] pairs")
            if pair[0] in result:
                raise WorkflowPayloadError("mapping contains duplicate keys")
            result[pair[0]] = _decode_value(pair[1])
        return result
    raise WorkflowPayloadError(f"unknown encoded value type: {kind!r}")


def _loads(raw: Any) -> Any:
    if not isinstance(raw, str):
        raise WorkflowPayloadError("payload must be text")
    try:
        value = json.loads(raw, parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WorkflowPayloadError("malformed workflow JSON") from exc
    if not isinstance(value, dict):
        raise WorkflowPayloadError("workflow payload must be an object")
    return value


def _keys(value: Any, expected: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise WorkflowPayloadError("unexpected or missing workflow payload fields")


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise WorkflowPayloadError(f"{name} must be a list")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise WorkflowPayloadError(f"{name} must be a string")
    return value


def _optional_string(value: Any, name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise WorkflowPayloadError(f"{name} must be a string or null")
    return value


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise WorkflowPayloadError(f"{name} must be boolean")
    return value


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorkflowPayloadError(f"{name} must be a non-negative integer")
    return value


def _positive_int(value: Any, name: str) -> int:
    value = _nonnegative_int(value, name)
    if value == 0:
        raise WorkflowPayloadError(f"{name} must be positive")
    return value


def _enum(enum_type: type[Any], value: Any, name: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise WorkflowPayloadError(f"unknown {name}: {value!r}") from exc
