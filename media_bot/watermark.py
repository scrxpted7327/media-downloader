from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable

import numpy as np

LOGGER = logging.getLogger(__name__)

LAMA_MODEL_URL = "https://huggingface.co/Carve/LaMa-ONNX/resolve/main/lama_fp32.onnx"
LAMA_MODEL_SHA256 = "1faef5301d78db7dda502fe59966957ec4b79dd64e16f03ed96913c7a4eb68d6"
AUTO_ACCEPT_CONFIDENCE = 0.78
SAMPLE_COUNT = 72
LAMA_KEYFRAME_FPS = 2.0


def select_onnx_providers(available: list[str]) -> list[str]:
    """Use CPU by default; accelerators are an explicit, validated opt-in."""
    requested = os.getenv("MEDIA_BOT_ONNX_PROVIDER", "CPUExecutionProvider").strip()
    if requested in available:
        return [requested]
    if "CPUExecutionProvider" in available:
        LOGGER.warning(
            "Requested ONNX provider %s is unavailable; using CPUExecutionProvider",
            requested,
        )
        return ["CPUExecutionProvider"]
    raise RuntimeError(f"no usable ONNX execution provider (available: {available})")


@dataclass(frozen=True)
class WatermarkCandidate:
    id: int
    x: int
    y: int
    width: int
    height: int
    confidence: float
    persistence: float
    border_score: float
    start_seconds: float | None = None
    end_seconds: float | None = None
    active_ranges: tuple[tuple[float, float], ...] = ()

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.width, self.height


@dataclass(frozen=True)
class WatermarkAnalysis:
    width: int
    height: int
    sample_count: int
    candidates: tuple[WatermarkCandidate, ...]
    selected: tuple[int, ...]
    requires_review: bool
    duration_seconds: float

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> "WatermarkAnalysis":
        data = json.loads(value)
        data["candidates"] = tuple(
            WatermarkCandidate(
                **{
                    **item,
                    "active_ranges": tuple(
                        tuple(pair) for pair in item.get("active_ranges", ())
                    ),
                }
            )
            for item in data["candidates"]
        )
        data["selected"] = tuple(data.get("selected", ()))
        return cls(**data)


def _cv2():
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("opencv-python-headless is required for watermark analysis") from exc
    return cv2


def analyze_video(path: Path, sample_count: int = SAMPLE_COUNT) -> WatermarkAnalysis:
    """Find static and time-windowed edge watermarks throughout a video."""
    started = time.monotonic()
    cv2 = _cv2()
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise RuntimeError(f"could not open video: {path.name}")
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    if frame_count < 2 or width < 8 or height < 8:
        capture.release()
        return WatermarkAnalysis(width, height, 0, (), (), False, time.monotonic() - started)

    scale = min(1.0, 640.0 / width, 360.0 / height)
    sw, sh = max(8, round(width * scale)), max(8, round(height * scale))
    indices = np.linspace(frame_count * .05, max(frame_count * .05, frame_count * .95 - 1),
                          min(sample_count, frame_count), dtype=int)
    edges: list[np.ndarray] = []
    grays: list[np.ndarray] = []
    positions: list[float] = []
    sample_seconds: list[float] = []
    tiktok_observations: list[tuple[str, tuple[int, int, int, int]] | None] = []
    for index in indices:
        capture.set(cv2.CAP_PROP_POS_FRAMES, int(index))
        ok, frame = capture.read()
        if not ok:
            continue
        tiktok_observations.append(_detect_tiktok_watermark(frame, cv2))
        small = cv2.resize(frame, (sw, sh), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        grays.append(gray)
        edges.append(cv2.Canny(gray, 50, 140) > 0)
        positions.append(index / max(1, frame_count - 1))
        sample_seconds.append(index / fps)
    capture.release()
    if len(edges) < 4:
        return WatermarkAnalysis(width, height, len(edges), (), (), False, time.monotonic() - started)

    duration = frame_count / max(1.0, fps)
    letterbox_bounds = _detect_letterbox_bounds(grays)
    candidates = _find_candidates(
        edges, grays, width, height, sw, sh, cv2,
        letterbox_bounds=letterbox_bounds,
    )
    candidates.extend(_tiktok_candidates(
        tiktok_observations, sample_seconds, duration,
    ))
    for start, end in ((0.0, .55), (.45, 1.0)):
        selected_indices = [
            index for index, position in enumerate(positions)
            if start <= position <= end
        ]
        if len(selected_indices) < 4:
            continue
        window_candidates = _find_candidates(
            [edges[index] for index in selected_indices],
            [grays[index] for index in selected_indices],
            width, height, sw, sh, cv2, threshold=.55,
            letterbox_bounds=letterbox_bounds,
        )
        candidates.extend(
            replace(
                candidate,
                start_seconds=round(duration * start, 3),
                end_seconds=round(duration * end, 3),
            )
            for candidate in window_candidates
        )

    candidates.sort(
        key=lambda item: (item.start_seconds is None, item.confidence), reverse=True,
    )
    accepted: list[WatermarkCandidate] = []
    for candidate in candidates:
        overlap = next((
            (index, other) for index, other in enumerate(accepted)
            if _intersection_ratio(candidate.box, other.box) > .25
        ), None)
        if overlap:
            index, other = overlap
            if (
                candidate.start_seconds is not None
                and other.start_seconds is not None
                and candidate.start_seconds != other.start_seconds
            ):
                better = candidate if candidate.confidence > other.confidence else other
                combined_start = min(candidate.start_seconds, other.start_seconds)
                combined_end = max(
                    candidate.end_seconds or duration,
                    other.end_seconds or duration,
                )
                covers_video = combined_start <= duration * .05 and combined_end >= duration * .95
                accepted[index] = replace(
                    better,
                    start_seconds=None if covers_video else combined_start,
                    end_seconds=None if covers_video else combined_end,
                )
            continue
        if len(accepted) < 6:
            accepted.append(candidate)

    accepted.sort(key=lambda item: item.confidence, reverse=True)
    numbered = tuple(replace(item, id=index + 1) for index, item in enumerate(accepted))
    selected = tuple(item.id for item in numbered if item.confidence >= AUTO_ACCEPT_CONFIDENCE)
    requires_review = bool(numbered) and len(selected) != len(numbered)
    return WatermarkAnalysis(width, height, len(edges), numbered, selected, requires_review,
                             round(time.monotonic() - started, 3))


def _detect_letterbox_bounds(
    grays: list[np.ndarray],
) -> tuple[int, int] | None:
    """Find persistent uniform black rows so they cannot become edit regions."""
    if not grays:
        return None
    stack = np.stack(grays).astype(np.float32)
    row_means = stack.mean(axis=2)
    row_spreads = stack.std(axis=2)
    dark_uniform = (
        (row_means <= 24.0) & (row_spreads <= 16.0)
    ).mean(axis=0) >= 0.8

    top = 0
    while top < len(dark_uniform) and dark_uniform[top]:
        top += 1
    bottom_rows = 0
    while bottom_rows < len(dark_uniform) and dark_uniform[-1 - bottom_rows]:
        bottom_rows += 1
    if top < 2:
        top = 0
    if bottom_rows < 2:
        bottom_rows = 0
    if top == 0 and bottom_rows == 0:
        return None
    if top + bottom_rows > len(dark_uniform) * 0.45:
        return None
    return top, len(dark_uniform) - bottom_rows


def _find_candidates(
    edges: list[np.ndarray],
    grays: list[np.ndarray],
    width: int,
    height: int,
    sw: int,
    sh: int,
    cv2,
    threshold: float = .58,
    letterbox_bounds: tuple[int, int] | None = None,
) -> list[WatermarkCandidate]:
    edge_stack = np.stack(edges)
    persistence_map = edge_stack.mean(axis=0)
    persistent = (persistence_map >= threshold).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 3))
    persistent = cv2.morphologyEx(persistent, cv2.MORPH_CLOSE, kernel, iterations=2)
    persistent = cv2.dilate(persistent, kernel, iterations=2)
    if letterbox_bounds is not None:
        top, bottom = letterbox_bounds
        if top:
            persistent[:top, :] = 0
        if bottom < sh:
            persistent[bottom:, :] = 0
    count, _, stats, _ = cv2.connectedComponentsWithStats(persistent, 8)
    gray_stack = np.stack(grays).astype(np.float32)
    temporal_std = gray_stack.std(axis=0)
    candidates: list[WatermarkCandidate] = []
    frame_area = sw * sh
    for component in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[component])
        if letterbox_bounds is not None:
            top, bottom = letterbox_bounds
            margin = max(2, round(sh * .01))
            if (top and y < top + margin) or (bottom < sh and y + h > bottom - margin):
                continue
        # A TV corner bug often touches a full-width lower-third/ticker and is
        # therefore returned as one oversized component. Isolate the compact
        # upper-left brand block instead of discarding the entire component.
        if w > sw * .45 and x < sw * .1 and y < sh * .2 and h < sh * .22:
            x = max(x, round(sw * .05))
            y = max(y, round(sh * .035))
            w = min(round(sw * .22), sw - x)
            h = min(round(sh * .07), sh - y)
            area = int(np.count_nonzero(persistent[y:y + h, x:x + w]))
        box_area = w * h
        if area < 12 or box_area > frame_area * .08 or w > sw * .45 or h > sh * .30:
            continue
        pad = max(2, round(min(sw, sh) * .01))
        x1, y1 = max(0, x - pad), max(0, y - pad)
        x2, y2 = min(sw, x + w + pad), min(sh, y + h + pad)
        component_persistence = persistence_map[y:y + h, x:x + w]
        # Morphology intentionally expands thin text strokes into one removable
        # region. Measuring the expanded background would dilute a genuinely
        # persistent logo (especially a small pill-shaped username), so score
        # only the edge pixels that formed the component.
        persistent_core = component_persistence[component_persistence >= threshold]
        persistence = float(persistent_core.mean()) if persistent_core.size else 0.0
        region_std = float(temporal_std[y1:y2, x1:x2].mean())
        # Static scenery has low variance but generally lacks a compact persistent
        # edge cluster; variance only supplies a modest scene-independence term.
        stability = max(0.0, min(1.0, 1.0 - region_std / 45.0))
        border_distance = min(x, y, sw - (x + w), sh - (y + h))
        border_score = max(0.0, 1.0 - border_distance / max(1.0, min(sw, sh) * .35))
        density = min(1.0, area / max(1.0, box_area * .32))
        confidence = .52 * persistence + .18 * stability + .16 * density + .14 * border_score
        if confidence < .48:
            continue
        sx, sy = width / sw, height / sh
        candidates.append(WatermarkCandidate(
            0, max(0, round(x1 * sx)), max(0, round(y1 * sy)),
            min(width, round((x2 - x1) * sx)), min(height, round((y2 - y1) * sy)),
            round(confidence, 3), round(persistence, 3), round(border_score, 3),
        ))

    return candidates


def _intersection_ratio(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    area = max(1, min(aw * ah, bw * bh))
    overlap = max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(
        0, min(ay + ah, by + bh) - max(ay, by))
    return overlap / area


def _detect_tiktok_watermark(frame, cv2) -> tuple[str, tuple[int, int, int, int]] | None:
    """Locate TikTok's distinctive cyan/red musical-note mark in one frame."""
    height, width = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    cyan = (
        (hsv[..., 0] >= 75) & (hsv[..., 0] <= 105)
        & (hsv[..., 1] >= 80) & (hsv[..., 2] >= 140)
    ).astype(np.uint8) * 255
    count, _, stats, _ = cv2.connectedComponentsWithStats(cyan, 8)
    options: list[tuple[int, tuple[int, int, int, int]]] = []
    for component in range(1, count):
        x, y, box_width, box_height, area = (int(v) for v in stats[component])
        center_x = x + box_width / 2
        if width * .18 < center_x < width * .82:
            continue
        if not (height * .012 <= box_height <= height * .16):
            continue
        if box_height < box_width * 1.25 or area < max(5, width * height * .000004):
            continue
        pad = max(5, round(box_height * .8))
        x1, y1 = max(0, x - pad), max(0, y - pad)
        x2, y2 = min(width, x + box_width + pad), min(height, y + box_height + pad)
        nearby = hsv[y1:y2, x1:x2]
        red = (
            ((nearby[..., 0] <= 12) | (nearby[..., 0] >= 170))
            & (nearby[..., 1] >= 90) & (nearby[..., 2] >= 130)
        )
        white = (nearby[..., 1] <= 80) & (nearby[..., 2] >= 170)
        if np.count_nonzero(red) < 3 or np.count_nonzero(white) < 8:
            continue
        # Include the "TikTok" label and @username below/beside the note, not
        # only the colored glyph that made the location easy to identify.
        mark_width = round(width * .22)
        mark_height = round(height * .095)
        mark_x = max(0, min(width - mark_width, round(center_x - mark_width / 2)))
        mark_y = max(0, min(height - mark_height, y - round(height * .005)))
        options.append((area, (mark_x, mark_y, mark_width, mark_height)))
    if not options:
        return None
    box = max(options, key=lambda item: item[0])[1]
    side = "left" if box[0] + box[2] / 2 < width / 2 else "right"
    return side, box


def _tiktok_candidates(
    observations: list[tuple[str, tuple[int, int, int, int]] | None],
    sample_seconds: list[float],
    duration: float,
) -> list[WatermarkCandidate]:
    candidates: list[WatermarkCandidate] = []
    for side in ("left", "right"):
        boxes = [item[1] for item in observations if item and item[0] == side]
        if len(boxes) < 2:
            continue
        values = np.asarray(boxes)
        box = tuple(int(round(value)) for value in np.median(values, axis=0))
        present = [bool(item and item[0] == side) for item in observations]
        ranges = _presence_ranges(present, sample_seconds, duration)
        if not ranges:
            continue
        persistence = len(boxes) / max(1, len(observations))
        candidates.append(WatermarkCandidate(
            id=0, x=box[0], y=box[1], width=box[2], height=box[3],
            confidence=.92, persistence=round(persistence, 3), border_score=1.0,
            active_ranges=tuple(ranges),
        ))
    return candidates


def _presence_ranges(
    present: list[bool], sample_seconds: list[float], duration: float,
) -> list[tuple[float, float]]:
    ranges: list[tuple[float, float]] = []
    start_index: int | None = None
    for index, is_present in enumerate([*present, False]):
        if is_present and start_index is None:
            start_index = index
        elif not is_present and start_index is not None:
            end_index = index - 1
            start = 0.0 if start_index == 0 else (
                sample_seconds[start_index - 1] + sample_seconds[start_index]
            ) / 2
            end = duration if end_index == len(present) - 1 else (
                sample_seconds[end_index] + sample_seconds[end_index + 1]
            ) / 2
            ranges.append((float(round(start, 3)), float(round(end, 3))))
            start_index = None
    return ranges


def candidate_active(candidate: WatermarkCandidate, seconds: float) -> bool:
    if candidate.active_ranges:
        return any(start <= seconds <= end for start, end in candidate.active_ranges)
    return (
        (candidate.start_seconds is None or seconds >= candidate.start_seconds)
        and (candidate.end_seconds is None or seconds <= candidate.end_seconds)
    )


def create_preview(path: Path, analysis: WatermarkAnalysis, output: Path) -> Path:
    cv2 = _cv2()
    capture = cv2.VideoCapture(str(path))
    frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    tiles: list[np.ndarray] = []
    for index in np.linspace(frames * .15, max(frames * .15, frames * .85 - 1), 6, dtype=int):
        capture.set(cv2.CAP_PROP_POS_FRAMES, int(index))
        ok, frame = capture.read()
        if not ok:
            continue
        for candidate in analysis.candidates:
            if not candidate_active(candidate, index / fps):
                continue
            color = (40, 220, 40) if candidate.id in analysis.selected else (30, 180, 255)
            cv2.rectangle(frame, (candidate.x, candidate.y),
                          (candidate.x + candidate.width, candidate.y + candidate.height), color, 3)
            cv2.putText(frame, str(candidate.id), (candidate.x + 4, candidate.y + 28),
                        cv2.FONT_HERSHEY_SIMPLEX, .9, color, 3, cv2.LINE_AA)
        tiles.append(cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA))
    capture.release()
    if not tiles:
        raise RuntimeError("could not create watermark preview")
    while len(tiles) < 6:
        tiles.append(tiles[-1].copy())
    sheet = np.vstack((np.hstack(tiles[:3]), np.hstack(tiles[3:6])))
    output.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output), sheet):
        raise RuntimeError("could not save watermark preview")
    return output


def _candidate_preview_seconds(
    candidate: WatermarkCandidate,
    duration_seconds: float,
) -> float:
    """Choose a representative frame while respecting time-windowed marks."""
    if candidate.active_ranges:
        start, end = max(
            candidate.active_ranges,
            key=lambda pair: max(0.0, pair[1] - pair[0]),
        )
    else:
        start = candidate.start_seconds if candidate.start_seconds is not None else 0.0
        end = candidate.end_seconds if candidate.end_seconds is not None else duration_seconds
    return max(0.0, min(duration_seconds, (start + end) / 2.0))


def _annotate_candidate_frame(frame, candidate: WatermarkCandidate, cv2):
    """Mark one candidate on a full-size frame for a readable Telegram preview."""
    height, width = frame.shape[:2]
    x1 = max(0, min(width - 1, candidate.x))
    y1 = max(0, min(height - 1, candidate.y))
    x2 = max(x1 + 1, min(width - 1, candidate.x + candidate.width))
    y2 = max(y1 + 1, min(height - 1, candidate.y + candidate.height))
    thickness = max(2, round(min(width, height) / 180))
    color = (35, 220, 35)
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, thickness)

    label = f"Candidate {candidate.id}  {candidate.confidence:.0%} confidence"
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = max(0.55, min(1.2, width / 900))
    label_thickness = max(1, round(font_scale * 2))
    (label_width, label_height), baseline = cv2.getTextSize(
        label, font, font_scale, label_thickness,
    )
    pad = max(6, round(min(width, height) / 120))
    label_y = max(label_height + baseline + pad, y1)
    label_x2 = min(width - 1, x1 + label_width + pad * 2)
    label_y1 = max(0, label_y - label_height - baseline - pad)
    cv2.rectangle(frame, (x1, label_y1), (label_x2, label_y), color, -1)
    cv2.putText(
        frame,
        label,
        (x1 + pad, label_y - baseline - pad // 2),
        font,
        font_scale,
        (0, 0, 0),
        label_thickness,
        cv2.LINE_AA,
    )
    return frame


def create_candidate_previews(
    path: Path,
    analysis: WatermarkAnalysis,
    output_dir: Path,
) -> list[Path]:
    """Create one readable full-frame preview per detected candidate.

    Telegram renders an album as a swipeable set of photos.  A separate
    full-size frame for each candidate is substantially easier to inspect than
    the old six-tile contact sheet, especially on a phone.
    """
    cv2 = _cv2()
    if not analysis.candidates:
        return []
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise RuntimeError(f"could not open video: {path.name}")
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    output_dir.mkdir(parents=True, exist_ok=True)
    previews: list[Path] = []
    try:
        for candidate in analysis.candidates[:6]:
            seconds = _candidate_preview_seconds(candidate, analysis.duration_seconds)
            capture.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000.0)
            ok, frame = capture.read()
            if not ok:
                capture.set(
                    cv2.CAP_PROP_POS_FRAMES,
                    round(seconds * fps),
                )
                ok, frame = capture.read()
            if not ok:
                raise RuntimeError(
                    f"could not read preview frame for candidate {candidate.id}"
                )
            annotated = _annotate_candidate_frame(frame, candidate, cv2)
            output = output_dir / f"watermark-candidate-{candidate.id:02d}.jpg"
            if not cv2.imwrite(str(output), annotated, [cv2.IMWRITE_JPEG_QUALITY, 92]):
                raise RuntimeError(
                    f"could not save preview for candidate {candidate.id}"
                )
            previews.append(output)
    finally:
        capture.release()
    return previews


def provision_lama_model(
    tools_dir: Path,
    opener: Callable[..., Any] = urllib.request.urlopen,
    *,
    cancel_event: Any | None = None,
) -> Path:
    target = tools_dir / "lama_fp32.onnx"
    if target.is_file() and _sha256(target) == LAMA_MODEL_SHA256:
        return target
    if target.exists():
        target.unlink()
    tools_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=tools_dir, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            with opener(LAMA_MODEL_URL, timeout=120) as response:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError("LaMa model download cancelled")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    if _sha256(temporary) != LAMA_MODEL_SHA256:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("LaMa model checksum validation failed")
    temporary.replace(target)
    return target


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inpaint_video(
    input_path: Path,
    output_path: Path,
    candidates: list[WatermarkCandidate],
    model_path: Path,
    timeout_seconds: int = 600,
    cancel_event: Any | None = None,
) -> tuple[str, float]:
    """Inpaint 512px crops, stabilize them with flow, then remux original audio."""
    import onnxruntime as ort
    cv2 = _cv2()
    started = time.monotonic()
    providers = select_onnx_providers(ort.get_available_providers())
    session = ort.InferenceSession(str(model_path), providers=providers)
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("LaMa watermark removal cancelled")
    inputs = session.get_inputs()
    if len(inputs) < 2:
        raise RuntimeError("LaMa ONNX model does not expose image and mask inputs")
    capture = cv2.VideoCapture(str(input_path))
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    keyframe_interval = max(1, round(fps / LAMA_KEYFRAME_FPS))
    with tempfile.TemporaryDirectory(prefix="media-bot-lama-") as tmp:
        silent = Path(tmp) / "silent.mp4"
        writer = cv2.VideoWriter(str(silent), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
        previous_gray = None
        anchor_gray = None
        anchor_result = None
        frame_index = 0
        candidate_masks: dict[int, np.ndarray] = {}
        for candidate in candidates:
            region_mask = np.zeros((height, width), np.uint8)
            inset_x = round(candidate.width * .06)
            inset_y = round(candidate.height * .06)
            left, top = candidate.x + inset_x, candidate.y + inset_y
            right = candidate.x + candidate.width - inset_x
            bottom = candidate.y + candidate.height - inset_y
            radius = max(2, min((bottom - top) // 2, (right - left) // 6))
            cv2.rectangle(region_mask, (left + radius, top), (right - radius, bottom), 255, -1)
            cv2.rectangle(region_mask, (left, top + radius), (right, bottom - radius), 255, -1)
            for center in (
                (left + radius, top + radius),
                (right - radius, top + radius),
                (left + radius, bottom - radius),
                (right - radius, bottom - radius),
            ):
                cv2.circle(region_mask, center, radius, 255, -1)
            candidate_masks[candidate.id] = region_mask
        active_ids: tuple[int, ...] = ()
        stop_error: Exception | None = None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                stop_error = InterruptedError("LaMa watermark removal cancelled")
                break
            if time.monotonic() - started > timeout_seconds:
                stop_error = TimeoutError("LaMa watermark removal timed out")
                break
            ok, frame = capture.read()
            if not ok:
                break
            frame_seconds = frame_index / fps
            active_candidates = [
                item for item in candidates
                if candidate_active(item, frame_seconds)
            ]
            current_ids = tuple(item.id for item in active_candidates)
            if current_ids != active_ids:
                active_ids = current_ids
                anchor_gray = None
                anchor_result = None
            region_mask = np.zeros((height, width), np.uint8)
            for candidate in active_candidates:
                region_mask = cv2.bitwise_or(region_mask, candidate_masks[candidate.id])
            region_alpha = (
                cv2.GaussianBlur(region_mask, (0, 0), 3).astype(np.float32) / 255
            )[..., None]
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            scene_cut = previous_gray is not None and float(cv2.absdiff(gray, previous_gray).mean()) > 32
            is_keyframe = (
                anchor_result is None
                or scene_cut
                or frame_index % keyframe_interval == 0
            )
            if is_keyframe:
                for candidate in active_candidates:
                    frame = _inpaint_candidate(frame, candidate, session, inputs, cv2)
                anchor_gray, anchor_result = gray.copy(), frame.copy()
            elif anchor_result is not None and anchor_gray is not None:
                flow = cv2.calcOpticalFlowFarneback(anchor_gray, gray, None, .5, 3, 15, 3, 5, 1.2, 0)
                gx, gy = np.meshgrid(np.arange(width), np.arange(height))
                # remap expects current-output -> previous-source coordinates;
                # Farneback returns previous -> current displacement.
                warped = cv2.remap(
                    anchor_result,
                    (gx - flow[..., 0]).astype(np.float32),
                    (gy - flow[..., 1]).astype(np.float32),
                    cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REFLECT,
                )
                ring = cv2.dilate(
                    region_mask, np.ones((31, 31), np.uint8), iterations=1,
                )
                ring = (ring > 0) & (region_mask == 0)
                if np.any(ring):
                    matched = warped.astype(np.float32)
                    current_float = frame.astype(np.float32)
                    for channel in range(3):
                        source_values = matched[..., channel][ring]
                        target_values = current_float[..., channel][ring]
                        source_std = max(1.0, float(source_values.std()))
                        target_std = max(1.0, float(target_values.std()))
                        matched[..., channel] = (
                            (matched[..., channel] - float(source_values.mean()))
                            * min(2.0, target_std / source_std)
                            + float(target_values.mean())
                        )
                    warped = np.clip(matched, 0, 255).astype(np.uint8)
                fresh = cv2.inpaint(
                    frame,
                    cv2.dilate(region_mask, np.ones((5, 5), np.uint8)),
                    5,
                    cv2.INPAINT_TELEA,
                )
                stabilized = (
                    fresh.astype(np.float32) * .92
                    + warped.astype(np.float32) * .08
                )
                frame = (
                    frame * (1 - region_alpha) + stabilized * region_alpha
                ).astype(np.uint8)
            writer.write(frame)
            previous_gray = gray
            frame_index += 1
        capture.release()
        writer.release()
        if stop_error is not None:
            raise stop_error
        if not silent.is_file() or silent.stat().st_size == 0:
            raise RuntimeError("LaMa produced no video")
        import subprocess
        command = ["ffmpeg", "-y", "-i", str(silent), "-i", str(input_path), "-map", "0:v:0",
                   "-map", "1:a?", "-c:v", "copy", "-c:a", "copy", "-shortest",
                   "-movflags", "+faststart", str(output_path)]
        # A file-backed stderr sink cannot fill and deadlock a long remux while
        # this synchronous worker polls for cancellation.
        with tempfile.TemporaryFile() as error_stream:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=error_stream,
                start_new_session=(os.name == "posix"),
            )
            deadline = time.monotonic() + max(
                1.0, timeout_seconds - (time.monotonic() - started),
            )
            try:
                while process.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError("LaMa watermark removal cancelled")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("LaMa audio remux timed out")
                    time.sleep(0.1)
                if process.returncode:
                    error_stream.seek(0)
                    stderr = error_stream.read(64 * 1024)
                    raise subprocess.CalledProcessError(
                        process.returncode, command, stderr=stderr,
                    )
            except BaseException:
                if process.poll() is None:
                    try:
                        if os.name == "posix":
                            os.killpg(process.pid, 15)
                        else:
                            process.terminate()
                        process.wait(timeout=3)
                    except (OSError, subprocess.TimeoutExpired):
                        try:
                            if os.name == "posix":
                                os.killpg(process.pid, 9)
                            else:
                                process.kill()
                        except OSError:
                            pass
                        process.wait()
                raise
    return providers[0] if providers else "CPUExecutionProvider", time.monotonic() - started


def _inpaint_candidate(frame, candidate, session, inputs, cv2):
    height, width = frame.shape[:2]
    margin = max(24, round(max(candidate.width, candidate.height) * .5))
    x1, y1 = max(0, candidate.x - margin), max(0, candidate.y - margin)
    x2 = min(width, candidate.x + candidate.width + margin)
    y2 = min(height, candidate.y + candidate.height + margin)
    crop = frame[y1:y2, x1:x2]
    resized = cv2.resize(crop, (512, 512), interpolation=cv2.INTER_AREA)
    mask = np.zeros((512, 512), np.float32)
    inset_x = round(candidate.width * .06)
    inset_y = round(candidate.height * .06)
    mx1 = round((candidate.x + inset_x - x1) / (x2 - x1) * 512)
    my1 = round((candidate.y + inset_y - y1) / (y2 - y1) * 512)
    mx2 = round((candidate.x + candidate.width - inset_x - x1) / (x2 - x1) * 512)
    my2 = round((candidate.y + candidate.height - inset_y - y1) / (y2 - y1) * 512)
    radius = max(2, min((my2 - my1) // 2, (mx2 - mx1) // 6))
    cv2.rectangle(mask, (mx1 + radius, my1), (mx2 - radius, my2), 1, -1)
    cv2.rectangle(mask, (mx1, my1 + radius), (mx2, my2 - radius), 1, -1)
    cv2.circle(mask, (mx1 + radius, my1 + radius), radius, 1, -1)
    cv2.circle(mask, (mx2 - radius, my1 + radius), radius, 1, -1)
    cv2.circle(mask, (mx1 + radius, my2 - radius), radius, 1, -1)
    cv2.circle(mask, (mx2 - radius, my2 - radius), radius, 1, -1)
    mask = cv2.dilate(mask, np.ones((7, 7), np.uint8), iterations=1).astype(np.float32)
    image = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255
    image = np.transpose(image, (2, 0, 1))[None]
    result = session.run(None, {inputs[0].name: image, inputs[1].name: mask[None, None]})[0]
    result = np.transpose(result[0], (1, 2, 0))
    # The pinned export returns 0..255 floats; tolerate normalized mock sessions.
    if float(result.max(initial=0)) <= 1.5:
        result = result * 255
    result = np.clip(result, 0, 255).astype(np.uint8)
    result = cv2.cvtColor(result, cv2.COLOR_RGB2BGR)
    result = cv2.resize(result, (x2 - x1, y2 - y1), interpolation=cv2.INTER_CUBIC)
    blend_mask = cv2.resize(mask, (x2 - x1, y2 - y1), interpolation=cv2.INTER_LINEAR)
    blend_mask = cv2.GaussianBlur(blend_mask, (0, 0), 2)[..., None]
    frame[y1:y2, x1:x2] = (crop * (1 - blend_mask) + result * blend_mask).astype(np.uint8)
    return frame
