from __future__ import annotations

import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from media_bot.watermark import (
    WatermarkAnalysis,
    analyze_video,
    provision_lama_model,
    select_onnx_providers,
)


def _video(path: Path, *, logo: bool) -> None:
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), 12, (320, 180),
    )
    rng = np.random.default_rng(4)
    for index in range(36):
        frame = np.zeros((180, 320, 3), np.uint8)
        frame[:] = (25 + index * 3, 55, 90)
        x = (index * 9) % 260
        cv2.rectangle(frame, (x, 55), (x + 55, 120), (30, 180, 80), -1)
        frame = cv2.add(frame, rng.integers(0, 8, frame.shape, dtype=np.uint8))
        if logo:
            cv2.putText(frame, "WM", (245, 28), cv2.FONT_HERSHEY_SIMPLEX,
                        .7, (245, 245, 245), 2, cv2.LINE_AA)
        writer.write(frame)
    writer.release()


def _top_center_pill_video(path: Path) -> None:
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), 12, (320, 180),
    )
    for index in range(36):
        frame = np.full((180, 320, 3), (20 + index * 2, 35, 50), np.uint8)
        cv2.circle(frame, ((index * 11) % 320, 105), 34, (100, 60, 180), -1)
        overlay = frame.copy()
        cv2.rectangle(overlay, (112, 9), (208, 35), (3, 3, 3), -1)
        cv2.addWeighted(overlay, .82, frame, .18, 0, frame)
        cv2.putText(frame, ".hot_tea.", (119, 28), cv2.FONT_HERSHEY_SIMPLEX,
                    .42, (245, 245, 245), 1, cv2.LINE_AA)
        writer.write(frame)
    writer.release()


def _moving_tiktok_video(path: Path) -> None:
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), 12, (320, 180),
    )
    for index in range(72):
        frame = np.full((180, 320, 3), (35 + index, 45, 60), np.uint8)
        cv2.circle(frame, ((index * 7) % 240 + 40, 80), 24, (90, 80, 160), -1)
        x = 18 if index < 36 else 294
        # Cyan/red/white offset strokes reproduce TikTok's distinctive note.
        cv2.line(frame, (x + 3, 124), (x + 3, 141), (20, 20, 245), 4)
        cv2.line(frame, (x, 122), (x, 139), (245, 220, 10), 4)
        cv2.line(frame, (x + 1, 123), (x + 1, 138), (245, 245, 245), 1)
        cv2.circle(frame, (x - 3, 141), 5, (245, 220, 10), 2)
        cv2.circle(frame, (x, 143), 5, (20, 20, 245), 2)
        cv2.putText(frame, "TikTok", (max(1, x - 16), 158), cv2.FONT_HERSHEY_SIMPLEX,
                    .28, (245, 245, 245), 1, cv2.LINE_AA)
        writer.write(frame)
    writer.release()


class WatermarkAnalysisTests(unittest.TestCase):
    def test_onnx_defaults_to_cpu_even_when_accelerators_are_available(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                select_onnx_providers(
                    ["CoreMLExecutionProvider", "CPUExecutionProvider"]
                ),
                ["CPUExecutionProvider"],
            )

    def test_onnx_accelerator_is_explicit_opt_in(self):
        with patch.dict(
            "os.environ", {"MEDIA_BOT_ONNX_PROVIDER": "CoreMLExecutionProvider"}
        ):
            self.assertEqual(
                select_onnx_providers(
                    ["CoreMLExecutionProvider", "CPUExecutionProvider"]
                ),
                ["CoreMLExecutionProvider"],
            )

    def test_detects_persistent_corner_text_and_round_trips_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logo.mp4"
            _video(path, logo=True)
            analysis = analyze_video(path, sample_count=24)
            self.assertTrue(analysis.candidates)
            candidate = analysis.candidates[0]
            self.assertGreater(candidate.x, 220)
            self.assertLess(candidate.y, 45)
            self.assertEqual(analysis, WatermarkAnalysis.from_json(analysis.to_json()))

    def test_rejects_moving_scene_without_watermark(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clean.mp4"
            _video(path, logo=False)
            self.assertEqual(analyze_video(path, sample_count=24).candidates, ())

    def test_detects_small_top_center_username_pill(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "top-center.mp4"
            _top_center_pill_video(path)

            analysis = analyze_video(path, sample_count=24)

            self.assertTrue(analysis.candidates)
            candidate = analysis.candidates[0]
            self.assertLess(candidate.y, 45)
            self.assertGreater(candidate.x, 90)
            self.assertLess(candidate.x + candidate.width, 230)

    def test_tracks_tiktok_watermark_when_it_switches_sides(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "moving-tiktok.mp4"
            _moving_tiktok_video(path)

            analysis = analyze_video(path, sample_count=60)
            moving = [item for item in analysis.candidates if item.active_ranges]

            self.assertEqual(len(moving), 2)
            self.assertLess(min(item.x for item in moving), 40)
            self.assertGreater(max(item.x for item in moving), 240)
            left = min(moving, key=lambda item: item.x)
            right = max(moving, key=lambda item: item.x)
            self.assertLess(left.active_ranges[0][1], right.active_ranges[0][1])
            self.assertEqual(analysis, WatermarkAnalysis.from_json(analysis.to_json()))

    def test_model_download_is_checksum_verified_and_cached(self):
        payload = b"pinned-model"
        digest = hashlib.sha256(payload).hexdigest()

        class Response:
            def __enter__(self):
                from io import BytesIO
                self.stream = BytesIO(payload)
                return self.stream

            def __exit__(self, *args):
                return False

        with tempfile.TemporaryDirectory() as directory, patch(
            "media_bot.watermark.LAMA_MODEL_SHA256", digest,
        ):
            calls = []

            def opener(*args, **kwargs):
                calls.append(args)
                return Response()

            path = provision_lama_model(Path(directory), opener)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(provision_lama_model(Path(directory), opener), path)
            self.assertEqual(len(calls), 1)

    def test_bad_model_checksum_is_rejected(self):
        class Response:
            def __enter__(self):
                from io import BytesIO
                return BytesIO(b"wrong")

            def __exit__(self, *args):
                return False

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                provision_lama_model(Path(directory), lambda *a, **k: Response())
            self.assertFalse((Path(directory) / "lama_fp32.onnx").exists())

    def test_model_download_honors_cancellation_and_removes_partial(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, _size):
                return b"model"

        cancel_event = threading.Event()
        cancel_event.set()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(InterruptedError, "cancelled"):
                provision_lama_model(
                    root,
                    lambda *a, **k: Response(),
                    cancel_event=cancel_event,
                )
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
