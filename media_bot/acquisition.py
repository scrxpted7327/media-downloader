"""Framework-neutral durable acquisition lifecycle.

The module owns the state machine around one shared acquisition claim and its
requesters.  Persistence, downloading, promotion, cancellation, time, and
progress delivery are adapters at the seam; no channel objects are part of
this interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class AcquisitionState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset({AcquisitionState.COMPLETED, AcquisitionState.FAILED, AcquisitionState.CANCELLED})
TRACKING_QUERY_KEYS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})


def normalize_source_url(value: str) -> str:
    """Return a stable URL identity, removing tracking-only query fields."""
    raw = value.strip()
    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return raw
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
    ]
    query.sort()
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", urlencode(query), ""))


@dataclass(frozen=True)
class SourceIdentity:
    """The immutable shared-claim identity: source plus requested variant."""

    source_key: str
    source_url: str
    preset: str
    platform: str | None = None
    media_id: str | None = None

    @classmethod
    def from_request(cls, request: "AcquisitionRequest") -> "SourceIdentity":
        url = normalize_source_url(request.source_url)
        platform = request.platform.strip().lower() if request.platform else None
        media_id = request.media_id.strip() if request.media_id else None
        source_key = f"native:{platform or 'unknown'}:{media_id}" if media_id else f"url:{url}"
        preset = request.preset.strip().lower()
        if not preset:
            raise ValueError("preset is required")
        return cls(source_key, url, preset, platform, media_id)


@dataclass(frozen=True)
class AcquisitionRequest:
    requester_id: str
    source_url: str
    preset: str
    platform: str | None = None
    media_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DownloadedMedia:
    path: Path
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromotionResult:
    asset_id: str
    variant_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RequesterRecord:
    job_id: str
    claim_id: str
    requester_id: str
    state: AcquisitionState
    delivery_state: str = "pending"
    output: PromotionResult | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class ClaimRecord:
    claim_id: str
    identity: SourceIdentity
    state: AcquisitionState
    attempt: int = 0
    output: DownloadedMedia | None = None
    promotion: PromotionResult | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class Admission:
    requester: RequesterRecord
    claim: ClaimRecord
    owner: bool


@dataclass(frozen=True)
class ProgressEvent:
    job_id: str
    claim_id: str
    state: AcquisitionState
    phase: str
    at: datetime
    percent: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ResultEvent:
    job_id: str
    claim_id: str
    state: AcquisitionState
    result: PromotionResult | None
    at: datetime


@dataclass(frozen=True)
class ErrorEvent:
    job_id: str
    claim_id: str
    state: AcquisitionState
    code: str
    message: str
    retryable: bool
    at: datetime


class Persistence(Protocol):
    async def admit(self, request: AcquisitionRequest, identity: SourceIdentity) -> Admission: ...
    async def claim_for_execution(self, claim_id: str) -> ClaimRecord | None: ...
    async def wait_for_claim(self, claim_id: str) -> ClaimRecord: ...
    async def get_requester(self, job_id: str) -> RequesterRecord | None: ...
    async def get_claim(self, claim_id: str) -> ClaimRecord | None: ...
    async def set_claim(self, claim_id: str, state: AcquisitionState, **values: Any) -> ClaimRecord: ...
    async def set_requester(self, job_id: str, state: AcquisitionState, **values: Any) -> RequesterRecord: ...
    async def list_for_reconciliation(self) -> Sequence[tuple[ClaimRecord, Sequence[RequesterRecord]]]: ...


class Downloader(Protocol):
    async def download(
        self, identity: SourceIdentity, progress: Callable[[int], Awaitable[None]]
    ) -> DownloadedMedia: ...


class LibraryPromoter(Protocol):
    async def promote(self, identity: SourceIdentity, media: DownloadedMedia) -> PromotionResult: ...


class Cancellation(Protocol):
    async def requested(self, job_id: str) -> bool: ...
    async def signal(self, job_id: str) -> None: ...


class ProgressSink(Protocol):
    async def emit(self, event: ProgressEvent | ResultEvent | ErrorEvent) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class RetryPolicy:
    def __init__(self, max_attempts: int = 3, retryable_codes: frozenset[str] | None = None) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.max_attempts = max_attempts
        self.retryable_codes = retryable_codes or frozenset({"transient", "download_failed", "promotion_failed"})

    def decision(self, *, attempt: int, code: str) -> bool:
        return attempt < self.max_attempts and code in self.retryable_codes


class AcquisitionError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class _Cancelled(AcquisitionError):
    def __init__(self) -> None:
        super().__init__("cancelled", "acquisition cancelled", retryable=False)


class AcquisitionLifecycle:
    """Small caller interface for a durable, shared acquisition lifecycle."""

    def __init__(
        self,
        *,
        persistence: Persistence,
        downloader: Downloader,
        promoter: LibraryPromoter,
        cancellation: Cancellation,
        progress: ProgressSink,
        clock: Clock | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.persistence = persistence
        self.downloader = downloader
        self.promoter = promoter
        self.cancellation = cancellation
        self.progress = progress
        self.clock = clock or SystemClock()
        self.retry_policy = retry_policy or RetryPolicy()

    async def submit(self, request: AcquisitionRequest) -> RequesterRecord:
        admission = await self.persistence.admit(request, SourceIdentity.from_request(request))
        await self._emit_progress(admission.requester, "admission")
        return admission.requester

    async def run(self, job_id: str) -> RequesterRecord:
        requester = await self.persistence.get_requester(job_id)
        if requester is None:
            raise KeyError(f"unknown acquisition requester: {job_id}")
        claim = await self.persistence.get_claim(requester.claim_id)
        if claim is None:
            raise KeyError(f"unknown acquisition claim: {requester.claim_id}")
        if claim.state in TERMINAL_STATES:
            return await self._finish_joiner(requester, claim)
        claimed = await self.persistence.claim_for_execution(claim.claim_id)
        if claimed is None:
            claim = await self.persistence.wait_for_claim(claim.claim_id)
            return await self._finish_joiner(requester, claim)
        return await self._execute_claim(requester, claimed)

    async def retry(self, job_id: str) -> bool:
        """Requeue a failed claim only when its bounded policy allows another attempt."""
        requester = await self.persistence.get_requester(job_id)
        if requester is None:
            raise KeyError(f"unknown acquisition requester: {job_id}")
        claim = await self.persistence.get_claim(requester.claim_id)
        if claim is None:
            raise KeyError(f"unknown acquisition claim: {requester.claim_id}")
        if claim.state != AcquisitionState.FAILED or not self.retry_policy.decision(attempt=claim.attempt, code=claim.error_code or ""):
            return False
        await self.persistence.set_claim(claim.claim_id, AcquisitionState.QUEUED, error_code=None, error_message=None)
        await self.persistence.set_requester(job_id, AcquisitionState.QUEUED, error_code=None, error_message=None)
        return True

    async def cancel(self, job_id: str) -> RequesterRecord:
        requester = await self.persistence.get_requester(job_id)
        if requester is None:
            raise KeyError(f"unknown acquisition requester: {job_id}")
        await self.cancellation.signal(job_id)
        if requester.state not in TERMINAL_STATES:
            requester = await self.persistence.set_requester(job_id, AcquisitionState.CANCELLED, delivery_state="cancelled")
            await self._emit_progress(requester, "cancelled", detail="cancellation requested")
        return requester

    async def record_delivery(self, job_id: str, *, success: bool, error: str | None = None) -> RequesterRecord:
        requester = await self.persistence.get_requester(job_id)
        if requester is None:
            raise KeyError(f"unknown acquisition requester: {job_id}")
        if await self.cancellation.requested(job_id):
            return await self._cancel_if_needed(requester, "delivery")
        values = {"delivery_state": "completed" if success else "failed", "error_message": error}
        return await self.persistence.set_requester(job_id, requester.state, **values)

    async def reconcile(self) -> int:
        """Re-admit safe queued work; the higher authority calls this after restart."""
        resumed = 0
        for claim, requesters in await self.persistence.list_for_reconciliation():
            if claim.state in {AcquisitionState.RUNNING, AcquisitionState.PROCESSING}:
                await self.persistence.set_claim(claim.claim_id, AcquisitionState.QUEUED, error_code=None, error_message=None)
                resumed += 1
            for requester in requesters:
                if requester.state in {AcquisitionState.RUNNING, AcquisitionState.PROCESSING}:
                    await self.persistence.set_requester(requester.job_id, AcquisitionState.QUEUED, error_code=None, error_message=None)
        return resumed

    async def _execute_claim(self, requester: RequesterRecord, claim: ClaimRecord) -> RequesterRecord:
        # claim_for_execution is the atomic durable transition.  Its returned
        # attempt is already incremented, which prevents duplicate downloaders.
        requester = await self.persistence.set_requester(requester.job_id, AcquisitionState.RUNNING)
        await self._emit_progress(requester, "download")
        try:
            await self._checkpoint(requester.job_id, "claim")
            media = claim.output
            if media is None:
                media = await self.downloader.download(
                    claim.identity,
                    lambda percent: self._download_progress(requester, percent),
                )
                await self._checkpoint(requester.job_id, "download")
            claim = await self.persistence.set_claim(claim.claim_id, AcquisitionState.PROCESSING, output=media)
            requester = await self.persistence.set_requester(requester.job_id, AcquisitionState.PROCESSING)
            await self._emit_progress(requester, "promotion")
            await self._checkpoint(requester.job_id, "promotion")
            try:
                promotion = await self.promoter.promote(claim.identity, media)
            except Exception as exc:
                error = self._as_promotion_error(exc)
                # The downloaded output is already durable.  A failed
                # promotion completes acquisition without forcing a redownload
                # and leaves the requester able to retry delivery separately.
                await self.persistence.set_claim(
                    claim.claim_id,
                    AcquisitionState.COMPLETED,
                    output=media,
                    error_code=error.code,
                    error_message=str(error),
                )
                requester = await self.persistence.set_requester(
                    requester.job_id, AcquisitionState.COMPLETED
                )
                await self._emit_error(
                    requester,
                    error,
                    retryable=self.retry_policy.decision(
                        attempt=claim.attempt, code=error.code
                    ),
                )
                await self._emit_result(requester, None)
            else:
                await self.persistence.set_claim(
                    claim.claim_id,
                    AcquisitionState.COMPLETED,
                    output=media,
                    promotion=promotion,
                    error_code=None,
                    error_message=None,
                )
                requester = await self.persistence.set_requester(
                    requester.job_id, AcquisitionState.COMPLETED, output=promotion
                )
                await self._emit_result(requester, promotion)
            return requester
        except _Cancelled as exc:
            await self.persistence.set_claim(claim.claim_id, AcquisitionState.CANCELLED, error_code=exc.code, error_message=str(exc))
            requester = await self.persistence.set_requester(requester.job_id, AcquisitionState.CANCELLED, delivery_state="cancelled", error_code=exc.code, error_message=str(exc))
            await self._emit_error(requester, exc, retryable=False)
            return requester
        except Exception as exc:
            error = self._as_error(exc)
            retryable = error.retryable if error.retryable is not None else self.retry_policy.decision(attempt=claim.attempt, code=error.code)
            await self.persistence.set_claim(claim.claim_id, AcquisitionState.FAILED, error_code=error.code, error_message=str(error))
            requester = await self.persistence.set_requester(requester.job_id, AcquisitionState.FAILED, error_code=error.code, error_message=str(error))
            await self._emit_error(requester, error, retryable=retryable)
            return requester

    async def _finish_joiner(self, requester: RequesterRecord, claim: ClaimRecord) -> RequesterRecord:
        if claim.state == AcquisitionState.COMPLETED:
            requester = await self.persistence.set_requester(requester.job_id, AcquisitionState.COMPLETED, output=claim.promotion)
            await self._emit_result(requester, claim.promotion)
        elif claim.state == AcquisitionState.CANCELLED:
            requester = await self.persistence.set_requester(requester.job_id, AcquisitionState.CANCELLED, delivery_state="cancelled")
        elif claim.state == AcquisitionState.FAILED:
            requester = await self.persistence.set_requester(requester.job_id, AcquisitionState.FAILED, error_code=claim.error_code, error_message=claim.error_message)
        return requester

    async def _checkpoint(self, job_id: str, phase: str) -> None:
        if await self.cancellation.requested(job_id):
            raise _Cancelled()

    async def _cancel_if_needed(self, requester: RequesterRecord, phase: str) -> RequesterRecord:
        if requester.state in TERMINAL_STATES:
            return requester
        updated = await self.persistence.set_requester(requester.job_id, AcquisitionState.CANCELLED, delivery_state="cancelled")
        await self._emit_progress(updated, phase, detail="cancelled")
        return updated

    async def _download_progress(self, requester: RequesterRecord, percent: int) -> None:
        current = await self.persistence.get_requester(requester.job_id)
        if current is None or current.state in TERMINAL_STATES:
            return
        await self._emit_progress(current, "download", percent=max(0, min(100, int(percent))))

    async def _emit_progress(self, requester: RequesterRecord, phase: str, *, percent: int | None = None, detail: str | None = None) -> None:
        await self.progress.emit(ProgressEvent(requester.job_id, requester.claim_id, requester.state, phase, self.clock.now(), percent, detail))

    async def _emit_result(self, requester: RequesterRecord, result: PromotionResult | None) -> None:
        await self.progress.emit(ResultEvent(requester.job_id, requester.claim_id, requester.state, result, self.clock.now()))

    async def _emit_error(self, requester: RequesterRecord, error: AcquisitionError, *, retryable: bool) -> None:
        await self.progress.emit(ErrorEvent(requester.job_id, requester.claim_id, requester.state, error.code, str(error), retryable, self.clock.now()))

    @staticmethod
    def _as_error(exc: Exception) -> AcquisitionError:
        if isinstance(exc, AcquisitionError):
            return exc
        return AcquisitionError("download_failed", str(exc) or exc.__class__.__name__)

    @staticmethod
    def _as_promotion_error(exc: Exception) -> AcquisitionError:
        if isinstance(exc, AcquisitionError):
            return exc
        return AcquisitionError("promotion_failed", str(exc) or exc.__class__.__name__)


__all__ = [
    "AcquisitionError", "AcquisitionLifecycle", "AcquisitionRequest", "AcquisitionState",
    "Admission", "Cancellation", "ClaimRecord", "Clock", "DownloadedMedia", "Downloader",
    "ErrorEvent", "LibraryPromoter", "Persistence", "ProgressEvent", "ProgressSink",
    "PromotionResult", "RequesterRecord", "RetryPolicy", "ResultEvent", "SourceIdentity",
    "normalize_source_url",
]
