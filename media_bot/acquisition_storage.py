"""SQLite persistence adapter for :mod:`media_bot.acquisition`.

This module deliberately contains persistence only.  Admission, execution,
retry, and reconciliation policy remain in ``AcquisitionLifecycle``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .acquisition import (
    AcquisitionRequest,
    AcquisitionState,
    Admission,
    ClaimRecord,
    DownloadedMedia,
    PromotionResult,
    RequesterRecord,
    SourceIdentity,
)
from .storage import open_database


MAX_TEXT = 8192
MAX_ERROR = 4096
MAX_JSON = 65536


_SCHEMA = """
CREATE TABLE IF NOT EXISTS acquisition_claims (
    claim_id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL,
    source_url TEXT NOT NULL,
    preset TEXT NOT NULL,
    platform TEXT,
    media_id TEXT,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    output_path TEXT,
    output_metadata TEXT,
    asset_id TEXT,
    variant_id TEXT,
    promotion_metadata TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(source_key, preset)
);
CREATE INDEX IF NOT EXISTS idx_acquisition_claims_state
    ON acquisition_claims(state);
CREATE TABLE IF NOT EXISTS acquisition_requesters (
    job_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES acquisition_claims(claim_id),
    requester_id TEXT NOT NULL,
    state TEXT NOT NULL,
    delivery_state TEXT NOT NULL DEFAULT 'pending',
    asset_id TEXT,
    variant_id TEXT,
    output_metadata TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_acquisition_requesters_claim
    ON acquisition_requesters(claim_id);
CREATE INDEX IF NOT EXISTS idx_acquisition_requesters_state
    ON acquisition_requesters(state);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded(value: object, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:limit]


def _json(value: object, limit: int = MAX_JSON) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = json.dumps({"value": str(value)}, separators=(",", ":"))
    return encoded[:limit]


def _from_json(value: object) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _datetime(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.fromtimestamp(0, timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _optional_text(value: object, limit: int = MAX_TEXT) -> str | None:
    return None if value is None else _bounded(value, limit)


def _requester_from_row(row: Any) -> RequesterRecord:
    output = None
    if row["asset_id"] and row["variant_id"]:
        output = PromotionResult(
            str(row["asset_id"]), str(row["variant_id"]), _from_json(row["output_metadata"])
        )
    return RequesterRecord(
        job_id=str(row["job_id"]),
        claim_id=str(row["claim_id"]),
        requester_id=str(row["requester_id"]),
        state=AcquisitionState(str(row["state"])),
        delivery_state=str(row["delivery_state"] or "pending"),
        output=output,
        error_code=_optional_text(row["error_code"], MAX_TEXT),
        error_message=_optional_text(row["error_message"], MAX_ERROR),
    )


def _claim_from_row(row: Any) -> ClaimRecord:
    output = None
    if row["output_path"]:
        output = DownloadedMedia(Path(str(row["output_path"])), _from_json(row["output_metadata"]))
    promotion = None
    if row["asset_id"] and row["variant_id"]:
        promotion = PromotionResult(
            str(row["asset_id"]), str(row["variant_id"]), _from_json(row["promotion_metadata"])
        )
    return ClaimRecord(
        claim_id=str(row["claim_id"]),
        identity=SourceIdentity(
            source_key=str(row["source_key"]),
            source_url=str(row["source_url"]),
            preset=str(row["preset"]),
            platform=_optional_text(row["platform"]),
            media_id=_optional_text(row["media_id"]),
        ),
        state=AcquisitionState(str(row["state"])),
        attempt=int(row["attempt"] or 0),
        output=output,
        promotion=promotion,
        error_code=_optional_text(row["error_code"], MAX_TEXT),
        error_message=_optional_text(row["error_message"], MAX_ERROR),
    )


class AcquisitionStorage:
    """Concrete ``acquisition.Persistence`` backed by additive SQLite tables."""

    _events: dict[tuple[str, str], asyncio.Event] = {}
    _events_lock = asyncio.Lock()

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    async def init(self) -> None:
        """Create this adapter's tables and indexes; safe to call repeatedly."""
        async with open_database(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    initialize = init

    @classmethod
    async def _event_for(cls, db_path: Path, claim_id: str) -> asyncio.Event:
        key = (str(db_path), claim_id)
        async with cls._events_lock:
            return cls._events.setdefault(key, asyncio.Event())

    async def admit(self, request: AcquisitionRequest, identity: SourceIdentity) -> Admission:
        now = _now()
        claim_id = f"acq-{uuid.uuid4().hex}"
        job_id = f"acq-job-{uuid.uuid4().hex}"
        async with open_database(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """INSERT OR IGNORE INTO acquisition_claims
                (claim_id, source_key, source_url, preset, platform, media_id, state,
                 created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (claim_id, _bounded(identity.source_key, MAX_TEXT), _bounded(identity.source_url, MAX_TEXT),
                 _bounded(identity.preset, MAX_TEXT), _optional_text(identity.platform),
                 _optional_text(identity.media_id), AcquisitionState.QUEUED.value, now, now),
            )
            cursor = await db.execute(
                "SELECT claim_id FROM acquisition_claims WHERE source_key=? AND preset=?",
                (_bounded(identity.source_key, MAX_TEXT), _bounded(identity.preset, MAX_TEXT)),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("acquisition claim was not created")
            actual_claim_id = str(row[0])
            owner = actual_claim_id == claim_id
            await db.execute(
                """INSERT INTO acquisition_requesters
                (job_id, claim_id, requester_id, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (job_id, actual_claim_id, _bounded(request.requester_id, MAX_TEXT),
                 AcquisitionState.QUEUED.value, now, now),
            )
            await db.commit()
            claim_row = await (await db.execute(
                "SELECT * FROM acquisition_claims WHERE claim_id=?", (actual_claim_id,)
            )).fetchone()
            requester_row = await (await db.execute(
                "SELECT * FROM acquisition_requesters WHERE job_id=?", (job_id,)
            )).fetchone()
        if claim_row is None or requester_row is None:
            raise RuntimeError("acquisition admission disappeared")
        return Admission(_requester_from_row(requester_row), _claim_from_row(claim_row), owner)

    async def claim_for_execution(self, claim_id: str) -> ClaimRecord | None:
        now = _now()
        async with open_database(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """UPDATE acquisition_claims SET state=?, attempt=attempt+1, updated_at=?
                WHERE claim_id=? AND state=?""",
                (AcquisitionState.RUNNING.value, now, claim_id, AcquisitionState.QUEUED.value),
            )
            claimed = cursor.rowcount == 1
            row = await (await db.execute(
                "SELECT * FROM acquisition_claims WHERE claim_id=?", (claim_id,)
            )).fetchone()
            await db.commit()
        return _claim_from_row(row) if claimed and row is not None else None

    async def wait_for_claim(self, claim_id: str) -> ClaimRecord:
        event = await self._event_for(self.db_path, claim_id)
        while True:
            claim = await self.get_claim(claim_id)
            if claim is None:
                raise KeyError(f"unknown acquisition claim: {claim_id}")
            if claim.state in {AcquisitionState.COMPLETED, AcquisitionState.FAILED, AcquisitionState.CANCELLED}:
                return claim
            try:
                await asyncio.wait_for(event.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass
            event.clear()

    async def get_requester(self, job_id: str) -> RequesterRecord | None:
        async with open_database(self.db_path) as db:
            row = await (await db.execute(
                "SELECT * FROM acquisition_requesters WHERE job_id=?", (job_id,)
            )).fetchone()
        return _requester_from_row(row) if row is not None else None

    async def get_claim(self, claim_id: str) -> ClaimRecord | None:
        async with open_database(self.db_path) as db:
            row = await (await db.execute(
                "SELECT * FROM acquisition_claims WHERE claim_id=?", (claim_id,)
            )).fetchone()
        return _claim_from_row(row) if row is not None else None

    async def set_claim(self, claim_id: str, state: AcquisitionState, **values: Any) -> ClaimRecord:
        allowed = {"output", "promotion", "error_code", "error_message"}
        invalid = set(values) - allowed
        if invalid:
            raise ValueError(f"unsupported claim update field(s): {', '.join(sorted(invalid))}")
        sets: list[str] = ["state=?", "updated_at=?"]
        args: list[Any] = [state.value, _now()]
        if "output" in values:
            output = values["output"]
            if output is not None and not isinstance(output, DownloadedMedia):
                raise TypeError("output must be DownloadedMedia or None")
            sets += ["output_path=?", "output_metadata=?"]
            args += [_bounded(output.path, MAX_TEXT) if output else None, _json(output.metadata) if output else None]
        if "promotion" in values:
            promotion = values["promotion"]
            if promotion is not None and not isinstance(promotion, PromotionResult):
                raise TypeError("promotion must be PromotionResult or None")
            sets += ["asset_id=?", "variant_id=?", "promotion_metadata=?"]
            args += [_bounded(promotion.asset_id, MAX_TEXT) if promotion else None,
                     _bounded(promotion.variant_id, MAX_TEXT) if promotion else None,
                     _json(promotion.metadata) if promotion else None]
        if "error_code" in values:
            sets.append("error_code=?")
            args.append(_optional_text(values["error_code"], MAX_TEXT))
        if "error_message" in values:
            sets.append("error_message=?")
            args.append(_optional_text(values["error_message"], MAX_ERROR))
        if state in {AcquisitionState.COMPLETED, AcquisitionState.FAILED, AcquisitionState.CANCELLED}:
            sets.append("completed_at=COALESCE(completed_at, ?)")
            args.append(_now())
        args.append(claim_id)
        async with open_database(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(f"UPDATE acquisition_claims SET {', '.join(sets)} WHERE claim_id=?", args)
            row = await (await db.execute("SELECT * FROM acquisition_claims WHERE claim_id=?", (claim_id,))).fetchone()
            await db.commit()
        if row is None:
            raise KeyError(f"unknown acquisition claim: {claim_id}")
        result = _claim_from_row(row)
        if result.state in {AcquisitionState.COMPLETED, AcquisitionState.FAILED, AcquisitionState.CANCELLED}:
            (await self._event_for(self.db_path, claim_id)).set()
        return result

    async def set_requester(self, job_id: str, state: AcquisitionState, **values: Any) -> RequesterRecord:
        allowed = {"output", "delivery_state", "error_code", "error_message"}
        invalid = set(values) - allowed
        if invalid:
            raise ValueError(f"unsupported requester update field(s): {', '.join(sorted(invalid))}")
        sets: list[str] = ["state=?", "updated_at=?"]
        args: list[Any] = [state.value, _now()]
        if "output" in values:
            output = values["output"]
            if output is not None and not isinstance(output, PromotionResult):
                raise TypeError("output must be PromotionResult or None")
            sets += ["asset_id=?", "variant_id=?", "output_metadata=?"]
            args += [_bounded(output.asset_id, MAX_TEXT) if output else None,
                     _bounded(output.variant_id, MAX_TEXT) if output else None,
                     _json(output.metadata) if output else None]
        if "delivery_state" in values:
            sets.append("delivery_state=?")
            args.append(_bounded(values["delivery_state"], MAX_TEXT))
        if "error_code" in values:
            sets.append("error_code=?")
            args.append(_optional_text(values["error_code"], MAX_TEXT))
        if "error_message" in values:
            sets.append("error_message=?")
            args.append(_optional_text(values["error_message"], MAX_ERROR))
        if state in {AcquisitionState.COMPLETED, AcquisitionState.FAILED, AcquisitionState.CANCELLED}:
            sets.append("completed_at=COALESCE(completed_at, ?)")
            args.append(_now())
        args.append(job_id)
        async with open_database(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(f"UPDATE acquisition_requesters SET {', '.join(sets)} WHERE job_id=?", args)
            row = await (await db.execute("SELECT * FROM acquisition_requesters WHERE job_id=?", (job_id,))).fetchone()
            await db.commit()
        if row is None:
            raise KeyError(f"unknown acquisition requester: {job_id}")
        return _requester_from_row(row)

    async def list_for_reconciliation(self) -> Sequence[tuple[ClaimRecord, Sequence[RequesterRecord]]]:
        async with open_database(self.db_path) as db:
            claims = await (await db.execute(
                "SELECT * FROM acquisition_claims WHERE state IN (?, ?) ORDER BY created_at",
                (AcquisitionState.RUNNING.value, AcquisitionState.PROCESSING.value),
            )).fetchall()
            requesters = await (await db.execute(
                "SELECT * FROM acquisition_requesters WHERE state IN (?, ?) ORDER BY created_at",
                (AcquisitionState.RUNNING.value, AcquisitionState.PROCESSING.value),
            )).fetchall()
        grouped: dict[str, list[RequesterRecord]] = {}
        for row in requesters:
            grouped.setdefault(str(row["claim_id"]), []).append(_requester_from_row(row))
        return [(_claim_from_row(row), tuple(grouped.get(str(row["claim_id"]), ()))) for row in claims]


async def initialize_acquisition_storage(db_path: Path) -> AcquisitionStorage:
    """Initialize and return an acquisition SQLite adapter."""
    storage = AcquisitionStorage(db_path)
    await storage.init()
    return storage


SQLiteAcquisitionPersistence = AcquisitionStorage


__all__ = [
    "AcquisitionStorage",
    "SQLiteAcquisitionPersistence",
    "initialize_acquisition_storage",
]
