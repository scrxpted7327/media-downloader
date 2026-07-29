from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from media_bot.watermark import (
    WatermarkAnalysis,
    analyze_video,
    provision_lama_model,
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


class WatermarkAnalysisTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
