from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

LOGGER = logging.getLogger(__name__)

LAMA_MODEL_URL = "https://huggingface.co/Carve/LaMa-ONNX/resolve/main/lama_fp32.onnx"
LAMA_MODEL_SHA256 = "1faef5301d78db7dda502fe59966957ec4b79dd64e16f03ed96913c7a4eb68d6"
AUTO_ACCEPT_CONFIDENCE = 0.78
SAMPLE_COUNT = 28
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
        data["candidates"] = tuple(WatermarkCandidate(**item) for item in data["candidates"])
        data["selected"] = tuple(data.get("selected", ()))
        return cls(**data)


def _cv2():
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("opencv-python-headless is required for watermark analysis") from exc
    return cv2


def analyze_video(path: Path, sample_count: int = SAMPLE_COUNT) -> WatermarkAnalysis:
    """Find small, spatially persistent edge clusters across the middle 90%."""
    started = time.monotonic()
    cv2 = _cv2()
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise RuntimeError(f"could not open video: {path.name}")
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if frame_count < 2 or width < 8 or height < 8:
        capture.release()
        return WatermarkAnalysis(width, height, 0, (), (), False, time.monotonic() - started)

    scale = min(1.0, 640.0 / width, 360.0 / height)
    sw, sh = max(8, round(width * scale)), max(8, round(height * scale))
    indices = np.linspace(frame_count * .05, max(frame_count * .05, frame_count * .95 - 1),
                          min(sample_count, frame_count), dtype=int)
    edges: list[np.ndarray] = []
    grays: list[np.ndarray] = []
    for index in indices:
        capture.set(cv2.CAP_PROP_POS_FRAMES, int(index))
        ok, frame = capture.read()
        if not ok:
            continue
        small = cv2.resize(frame, (sw, sh), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        grays.append(gray)
        edges.append(cv2.Canny(gray, 50, 140) > 0)
    capture.release()
    if len(edges) < 4:
        return WatermarkAnalysis(width, height, len(edges), (), (), False, time.monotonic() - started)

    edge_stack = np.stack(edges)
    persistence_map = edge_stack.mean(axis=0)
    persistent = (persistence_map >= .58).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 3))
    persistent = cv2.morphologyEx(persistent, cv2.MORPH_CLOSE, kernel, iterations=2)
    persistent = cv2.dilate(persistent, kernel, iterations=2)
    count, _, stats, _ = cv2.connectedComponentsWithStats(persistent, 8)
    gray_stack = np.stack(grays).astype(np.float32)
    temporal_std = gray_stack.std(axis=0)
    candidates: list[WatermarkCandidate] = []
    frame_area = sw * sh
    for component in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[component])
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
        persistent_core = component_persistence[component_persistence >= .58]
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

    candidates.sort(key=lambda item: item.confidence, reverse=True)
    accepted: list[WatermarkCandidate] = []
    for candidate in candidates:
        if len(accepted) == 3:
            break
        if any(_intersection_ratio(candidate.box, other.box) > .25 for other in accepted):
            continue
        accepted.append(candidate)
    numbered = tuple(WatermarkCandidate(i + 1, *item.box, item.confidence,
                                         item.persistence, item.border_score)
                     for i, item in enumerate(accepted))
    selected = tuple(item.id for item in numbered if item.confidence >= AUTO_ACCEPT_CONFIDENCE)
    requires_review = bool(numbered) and len(selected) != len(numbered)
    return WatermarkAnalysis(width, height, len(edges), numbered, selected, requires_review,
                             round(time.monotonic() - started, 3))


def _intersection_ratio(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    area = max(1, min(aw * ah, bw * bh))
    overlap = max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(
        0, min(ay + ah, by + bh) - max(ay, by))
    return overlap / area


def create_preview(path: Path, analysis: WatermarkAnalysis, output: Path) -> Path:
    cv2 = _cv2()
    capture = cv2.VideoCapture(str(path))
    frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    tiles: list[np.ndarray] = []
    for index in np.linspace(frames * .15, max(frames * .15, frames * .85 - 1), 6, dtype=int):
        capture.set(cv2.CAP_PROP_POS_FRAMES, int(index))
        ok, frame = capture.read()
        if not ok:
            continue
        for candidate in analysis.candidates:
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


def provision_lama_model(tools_dir: Path, opener: Callable[..., Any] = urllib.request.urlopen) -> Path:
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
                shutil.copyfileobj(response, stream)
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


def inpaint_video(input_path: Path, output_path: Path, candidates: list[WatermarkCandidate],
                  model_path: Path, timeout_seconds: int = 600) -> tuple[str, float]:
    """Inpaint 512px crops, stabilize them with flow, then remux original audio."""
    import onnxruntime as ort
    cv2 = _cv2()
    started = time.monotonic()
    providers = select_onnx_providers(ort.get_available_providers())
    session = ort.InferenceSession(str(model_path), providers=providers)
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
        region_mask = np.zeros((height, width), np.uint8)
        for candidate in candidates:
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
        region_alpha = (
            cv2.GaussianBlur(region_mask, (0, 0), 3).astype(np.float32) / 255
        )[..., None]
        while True:
            if time.monotonic() - started > timeout_seconds:
                raise TimeoutError("LaMa watermark removal timed out")
            ok, frame = capture.read()
            if not ok:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            scene_cut = previous_gray is not None and float(cv2.absdiff(gray, previous_gray).mean()) > 32
            is_keyframe = (
                anchor_result is None
                or scene_cut
                or frame_index % keyframe_interval == 0
            )
            if is_keyframe:
                for candidate in candidates:
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
        if not silent.is_file() or silent.stat().st_size == 0:
            raise RuntimeError("LaMa produced no video")
        import subprocess
        command = ["ffmpeg", "-y", "-i", str(silent), "-i", str(input_path), "-map", "0:v:0",
                   "-map", "1:a?", "-c:v", "copy", "-c:a", "copy", "-shortest",
                   "-movflags", "+faststart", str(output_path)]
        subprocess.run(command, check=True, capture_output=True, timeout=timeout_seconds)
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
