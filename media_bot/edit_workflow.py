"""Framework-neutral lifecycle for rendered edits and generated metadata.

The module owns workflow state and ordering.  Telegram, PWA, SQLite, ffmpeg,
and Codex integrations are adapters at this seam and are intentionally absent
from this implementation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol


class WorkflowState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Phase(str, Enum):
    RENDER = "render"
    REVIEW = "review"
    METADATA = "metadata"
    DELIVERY = "delivery"


class PhaseState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PAUSED = "paused"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class DeliveryState(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class SettingsSnapshot:
    """Canonical, immutable settings captured for one edit attempt."""

    values: tuple[tuple[str, Any], ...]
    fingerprint: str

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "SettingsSnapshot":
        items = tuple(sorted((str(key), _freeze(value)) for key, value in values.items()))
        encoded = repr(items).encode("utf-8")
        return cls(items, hashlib.sha256(encoded).hexdigest())

    def as_dict(self) -> dict[str, Any]:
        return dict(self.values)


@dataclass(frozen=True)
class RenderRequest:
    workflow_id: str
    attempt: int
    source_path: Path
    output_path: Path
    settings: SettingsSnapshot
    idempotency_key: str


@dataclass(frozen=True)
class RenderArtifact:
    path: Path
    size_bytes: int
    content_hash: str | None = None
    validated: bool = True


@dataclass(frozen=True)
class MetadataOutput:
    title: str
    hashtags: tuple[str, ...]
    provenance: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProgressEvent:
    workflow_id: str
    state: WorkflowState
    phase: Phase
    phase_state: PhaseState
    attempt: int
    sequence: int
    occurred_at: datetime
    detail: str | None = None


@dataclass(frozen=True)
class PhaseRecord:
    state: PhaseState = PhaseState.PENDING
    idempotency_key: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class DeliveryRecord:
    state: DeliveryState = DeliveryState.PENDING
    error: str | None = None


@dataclass(frozen=True)
class AttemptRecord:
    number: int
    settings: SettingsSnapshot
    render: PhaseRecord = field(default_factory=PhaseRecord)
    review: PhaseRecord = field(default_factory=PhaseRecord)
    metadata: PhaseRecord = field(default_factory=PhaseRecord)
    delivery: DeliveryRecord = field(default_factory=DeliveryRecord)
    artifact: RenderArtifact | None = None
    metadata_output: MetadataOutput | None = None
    cancel_requested: bool = False
    error: str | None = None


@dataclass(frozen=True)
class WorkflowRecord:
    workflow_id: str
    source_path: Path
    output_path: Path
    state: WorkflowState
    attempt: AttemptRecord
    history: tuple[AttemptRecord, ...] = ()
    sequence: int = 0


class Renderer(Protocol):
    async def render(self, request: RenderRequest) -> RenderArtifact:
        """Render and durably validate the output artifact."""


class MetadataEngine(Protocol):
    async def generate(
        self,
        artifact: RenderArtifact,
        *,
        request: RenderRequest,
    ) -> MetadataOutput:
        """Generate metadata from the durable render artifact."""


class WorkflowPersistence(Protocol):
    async def load(self, workflow_id: str) -> WorkflowRecord | None:
        ...

    async def save(self, record: WorkflowRecord) -> None:
        ...


class Clock(Protocol):
    def now(self) -> datetime:
        ...


class ProgressSink(Protocol):
    async def emit(self, event: ProgressEvent) -> None:
        ...


class WorkflowError(RuntimeError):
    """Base error for invalid lifecycle operations."""


class MissingWorkflow(WorkflowError):
    pass


class ReviewRequired(WorkflowError):
    pass


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class EditWorkflow:
    """Deep edit lifecycle with durable-first transitions."""

    def __init__(
        self,
        *,
        renderer: Renderer,
        metadata_engine: MetadataEngine,
        persistence: WorkflowPersistence,
        clock: Clock | None = None,
        progress: ProgressSink | None = None,
    ) -> None:
        self._renderer = renderer
        self._metadata = metadata_engine
        self._persistence = persistence
        self._clock = clock or _SystemClock()
        self._progress = progress

    async def create(
        self,
        workflow_id: str,
        *,
        source_path: Path,
        output_path: Path,
        settings: Mapping[str, Any],
        review_required: bool = False,
    ) -> WorkflowRecord:
        snapshot = SettingsSnapshot.from_mapping(settings)
        attempt = AttemptRecord(
            number=1,
            settings=snapshot,
            review=PhaseRecord(
                state=PhaseState.PENDING if not review_required else PhaseState.PAUSED,
                idempotency_key=self._key(workflow_id, 1, Phase.REVIEW),
            ),
        )
        record = WorkflowRecord(
            workflow_id=workflow_id,
            source_path=Path(source_path),
            output_path=Path(output_path),
            state=WorkflowState.QUEUED,
            attempt=attempt,
        )
        await self._persistence.save(record)
        return record

    async def render_only(self, workflow_id: str) -> WorkflowRecord:
        """Run and durably record only the render phase.

        Channel callers that schedule metadata separately can use this method
        without inventing a fake metadata result in the workflow record.
        """
        record = await self._require(workflow_id)
        if record.state in {WorkflowState.CANCELLED, WorkflowState.FAILED}:
            return record
        return await self._run_render(record)

    async def run(self, workflow_id: str) -> WorkflowRecord:
        record = await self._require(workflow_id)
        if record.state in {WorkflowState.CANCELLED, WorkflowState.FAILED}:
            return record
        record = await self._run_render(record)
        if record.state in {WorkflowState.CANCELLED, WorkflowState.FAILED}:
            return record

        if record.attempt.cancel_requested:
            return await self._cancel_at_checkpoint(record, Phase.METADATA)
        if record.attempt.review.state is PhaseState.PAUSED:
            return record
        if record.attempt.metadata.state in {PhaseState.PENDING, PhaseState.RUNNING}:
            record = await self._run_metadata(record)
        return record

    async def _run_render(self, record: WorkflowRecord) -> WorkflowRecord:
        if record.attempt.render.state is PhaseState.COMPLETED:
            return record
        workflow_id = record.workflow_id
        attempt = record.attempt
        record = await self._transition(
            record,
            state=WorkflowState.RUNNING,
            attempt=replace(
                attempt,
                render=replace(
                    attempt.render,
                    state=PhaseState.RUNNING,
                    idempotency_key=self._key(workflow_id, attempt.number, Phase.RENDER),
                ),
            ),
            phase=Phase.RENDER,
            detail="render started",
        )
        if record.attempt.cancel_requested:
            return await self._cancel_at_checkpoint(record, Phase.RENDER)
        try:
            artifact = await self._renderer.render(
                RenderRequest(
                    workflow_id=workflow_id,
                    attempt=attempt.number,
                    source_path=record.source_path,
                    output_path=record.output_path,
                    settings=attempt.settings,
                    idempotency_key=self._key(workflow_id, attempt.number, Phase.RENDER),
                )
            )
            if not artifact.validated or artifact.size_bytes <= 0:
                raise WorkflowError("renderer returned an unvalidated or empty artifact")
        except Exception as exc:
            return await self._transition(
                record,
                state=WorkflowState.FAILED,
                attempt=replace(
                    record.attempt,
                    render=replace(record.attempt.render, state=PhaseState.FAILED, error=str(exc)),
                    error=str(exc),
                ),
                phase=Phase.RENDER,
                detail="render failed",
            )
        return await self._transition(
            record,
            state=WorkflowState.COMPLETED,
            attempt=replace(
                record.attempt,
                render=replace(record.attempt.render, state=PhaseState.COMPLETED, error=None),
                artifact=artifact,
                metadata=replace(
                    record.attempt.metadata,
                    state=PhaseState.PENDING,
                    idempotency_key=self._key(workflow_id, attempt.number, Phase.METADATA),
                ),
            ),
            phase=Phase.RENDER,
            detail="render durable",
        )

    async def cancel(self, workflow_id: str) -> WorkflowRecord:
        record = await self._require(workflow_id)
        if record.state in {WorkflowState.FAILED, WorkflowState.CANCELLED}:
            return record
        if record.attempt.render.state is PhaseState.COMPLETED:
            return await self._transition(
                record,
                attempt=replace(
                    record.attempt,
                    cancel_requested=True,
                    metadata=replace(
                        record.attempt.metadata,
                        state=PhaseState.CANCELLED
                        if record.attempt.metadata.state in {PhaseState.PENDING, PhaseState.RUNNING}
                        else record.attempt.metadata.state,
                    ),
                ),
                phase=Phase.DELIVERY,
                detail="downstream work cancelled after render",
            )
        return await self._cancel_at_checkpoint(
            await self._transition(
                record,
                attempt=replace(record.attempt, cancel_requested=True),
                phase=Phase.RENDER,
                detail="cancellation requested",
            ),
            Phase.RENDER,
        )

    async def resume_review(self, workflow_id: str) -> WorkflowRecord:
        record = await self._require(workflow_id)
        if record.attempt.review.state is not PhaseState.PAUSED:
            raise WorkflowError("workflow is not waiting for review")
        return await self._transition(
            record,
            attempt=replace(
                record.attempt,
                review=replace(record.attempt.review, state=PhaseState.COMPLETED),
            ),
            phase=Phase.REVIEW,
            detail="review approved",
        )

    async def reject_review(self, workflow_id: str, reason: str) -> WorkflowRecord:
        record = await self._require(workflow_id)
        if record.attempt.review.state is not PhaseState.PAUSED:
            raise WorkflowError("workflow is not waiting for review")
        return await self._transition(
            record,
            state=WorkflowState.FAILED,
            attempt=replace(
                record.attempt,
                review=replace(record.attempt.review, state=PhaseState.REJECTED, error=reason),
                error=reason,
            ),
            phase=Phase.REVIEW,
            detail="review rejected",
        )

    async def revise(self, workflow_id: str, settings: Mapping[str, Any]) -> WorkflowRecord:
        record = await self._require(workflow_id)
        snapshot = SettingsSnapshot.from_mapping(settings)
        previous = record.attempt
        number = previous.number + 1
        attempt = AttemptRecord(
            number=number,
            settings=snapshot,
            review=replace(
                previous.review,
                state=PhaseState.PENDING,
                idempotency_key=self._key(workflow_id, number, Phase.REVIEW),
                error=None,
            ),
        )
        return await self._transition(
            record,
            state=WorkflowState.QUEUED,
            attempt=attempt,
            history=(*record.history, previous),
            phase=Phase.RENDER,
            detail="new revision queued",
        )

    async def retry_metadata(self, workflow_id: str) -> WorkflowRecord:
        record = await self._require(workflow_id)
        if record.attempt.render.state is not PhaseState.COMPLETED:
            raise WorkflowError("metadata requires a durable render")
        attempt = replace(
            record.attempt,
            metadata=replace(
                record.attempt.metadata,
                state=PhaseState.PENDING,
                error=None,
                idempotency_key=self._key(workflow_id, record.attempt.number, Phase.METADATA),
            ),
        )
        return await self._transition(record, attempt=attempt, phase=Phase.METADATA, detail="metadata retry queued")

    async def record_delivery(
        self, workflow_id: str, *, success: bool, error: str | None = None
    ) -> WorkflowRecord:
        record = await self._require(workflow_id)
        delivery = DeliveryRecord(
            state=DeliveryState.COMPLETED if success else DeliveryState.FAILED,
            error=None if success else error or "delivery failed",
        )
        return await self._transition(
            record,
            attempt=replace(record.attempt, delivery=delivery),
            phase=Phase.DELIVERY,
            detail="delivery completed" if success else "delivery failed",
        )

    async def reconcile(self, workflow_id: str) -> WorkflowRecord:
        """Idempotently resume safe checkpoints after process recovery."""
        return await self.run(workflow_id)

    async def _run_metadata(self, record: WorkflowRecord) -> WorkflowRecord:
        attempt = record.attempt
        artifact = attempt.artifact
        if artifact is None:
            raise WorkflowError("metadata phase has no durable artifact")
        record = await self._transition(
            record,
            state=WorkflowState.PROCESSING,
            attempt=replace(
                attempt,
                metadata=replace(attempt.metadata, state=PhaseState.RUNNING),
            ),
            phase=Phase.METADATA,
            detail="metadata started",
        )
        try:
            output = await self._metadata.generate(
                artifact,
                request=RenderRequest(
                    workflow_id=record.workflow_id,
                    attempt=attempt.number,
                    source_path=record.source_path,
                    output_path=record.output_path,
                    settings=attempt.settings,
                    idempotency_key=self._key(record.workflow_id, attempt.number, Phase.METADATA),
                ),
            )
        except Exception as exc:
            return await self._transition(
                record,
                state=WorkflowState.COMPLETED,
                attempt=replace(
                    record.attempt,
                    metadata=replace(record.attempt.metadata, state=PhaseState.FAILED, error=str(exc)),
                ),
                phase=Phase.METADATA,
                detail="metadata failed; render remains complete",
            )
        return await self._transition(
            record,
            state=WorkflowState.COMPLETED,
            attempt=replace(
                record.attempt,
                metadata=replace(record.attempt.metadata, state=PhaseState.COMPLETED, error=None),
                metadata_output=output,
            ),
            phase=Phase.METADATA,
            detail="metadata completed",
        )

    async def _cancel_at_checkpoint(self, record: WorkflowRecord, phase: Phase) -> WorkflowRecord:
        if record.attempt.render.state is PhaseState.COMPLETED:
            return await self._transition(
                record,
                state=WorkflowState.COMPLETED,
                attempt=replace(
                    record.attempt,
                    cancel_requested=True,
                    metadata=replace(
                        record.attempt.metadata,
                        state=PhaseState.CANCELLED
                        if record.attempt.metadata.state in {PhaseState.PENDING, PhaseState.RUNNING}
                        else record.attempt.metadata.state,
                    ),
                ),
                phase=phase,
                detail="downstream work cancelled after render",
            )
        return await self._transition(
            record,
            state=WorkflowState.CANCELLED,
            attempt=replace(record.attempt, render=replace(record.attempt.render, state=PhaseState.CANCELLED)),
            phase=phase,
            detail="cancelled at checkpoint",
        )

    async def _transition(
        self,
        record: WorkflowRecord,
        *,
        phase: Phase,
        detail: str,
        state: WorkflowState | None = None,
        attempt: AttemptRecord | None = None,
        history: tuple[AttemptRecord, ...] | None = None,
    ) -> WorkflowRecord:
        next_record = replace(
            record,
            state=state or record.state,
            attempt=attempt or record.attempt,
            history=history if history is not None else record.history,
            sequence=record.sequence + 1,
        )
        await self._persistence.save(next_record)
        if self._progress is not None:
            delivery_phase_state = {
                DeliveryState.PENDING: PhaseState.PENDING,
                DeliveryState.COMPLETED: PhaseState.COMPLETED,
                DeliveryState.FAILED: PhaseState.FAILED,
            }[next_record.attempt.delivery.state]
            await self._progress.emit(
                ProgressEvent(
                    workflow_id=next_record.workflow_id,
                    state=next_record.state,
                    phase=phase,
                    phase_state=getattr(next_record.attempt, phase.value).state
                    if phase is not Phase.DELIVERY
                    else delivery_phase_state,
                    attempt=next_record.attempt.number,
                    sequence=next_record.sequence,
                    occurred_at=self._clock.now(),
                    detail=detail,
                )
            )
        return next_record

    async def _require(self, workflow_id: str) -> WorkflowRecord:
        record = await self._persistence.load(workflow_id)
        if record is None:
            raise MissingWorkflow(workflow_id)
        return record

    @staticmethod
    def _key(workflow_id: str, attempt: int, phase: Phase) -> str:
        return f"{workflow_id}:attempt-{attempt}:{phase.value}"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(_freeze(item) for item in value))
    return value
