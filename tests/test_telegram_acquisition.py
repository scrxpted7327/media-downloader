from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from media_bot.acquisition import DownloadedMedia, PromotionResult
from media_bot.storage import create_job, get_job, init_db
from media_bot.telegram_acquisition import TelegramAcquisitionRuntime


class _Downloader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def download(self, identity, progress) -> DownloadedMedia:
        del identity
        self.calls += 1
        self.started.set()
        await progress(25)
        await self.release.wait()
        return DownloadedMedia(
            self.path,
            {"output_filename": self.path.name, "output_mime_type": "video/mp4"},
        )


class _Promoter:
    def __init__(self) -> None:
        self.calls = 0

    async def promote(self, identity, media) -> PromotionResult:
        del media
        self.calls += 1
        return PromotionResult("asset-1", "variant-1", {"preset_key": identity.preset})


class _Status:
    def __init__(self) -> None:
        self.edits: list[str] = []

    async def edit_text(self, text: str, **kwargs) -> None:
        del kwargs
        self.edits.append(text)


class TelegramAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_requesters_share_claim_and_delivery_is_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_path = root / "media.db"
            storage_dir = root / "jobs"
            storage_dir.mkdir()
            await init_db(db_path)
            runtime = TelegramAcquisitionRuntime(
                db_path=db_path,
                storage_dir=storage_dir,
                ytdlp=Path("/bin/false"),
                gallerydl=Path("/bin/false"),
                max_filesize_mb=47,
                timeout_seconds=30,
                library_min_free_space_mb=0,
            )
            output = storage_dir / "fixture.mp4"
            output.write_bytes(b"shared telegram output")
            downloader = _Downloader(output)
            promoter = _Promoter()
            runtime.lifecycle.downloader = downloader
            runtime.lifecycle.promoter = promoter

            first = await runtime.submit(
                "telegram:1", "https://www.youtube.com/watch?v=one", owner_id="7", job_id=1
            )
            second = await runtime.submit(
                "telegram:2", "https://www.youtube.com/watch?v=one", owner_id="8", job_id=2
            )
            self.assertEqual(first.claim_id, second.claim_id)

            first_task = asyncio.create_task(runtime.run(first.job_id))
            second_task = asyncio.create_task(runtime.run(second.job_id))
            await asyncio.wait_for(downloader.started.wait(), timeout=1)
            downloader.release.set()
            first_result, second_result = await asyncio.gather(first_task, second_task)

            self.assertEqual(downloader.calls, 1)
            self.assertEqual(promoter.calls, 1)
            self.assertEqual(first_result.state.value, "completed")
            self.assertEqual(second_result.state.value, "completed")
            self.assertIsNotNone(first_result.output)
            self.assertIsNotNone(second_result.output)

            delivered = await runtime.record_delivery(first.job_id, success=True)
            self.assertEqual(delivered.delivery_state, "completed")

    async def test_telegram_job_adapter_records_shared_output_before_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_path = root / "media.db"
            storage_dir = root / "jobs"
            storage_dir.mkdir()
            await init_db(db_path)
            runtime = TelegramAcquisitionRuntime(
                db_path=db_path,
                storage_dir=storage_dir,
                ytdlp=Path("/bin/false"),
                gallerydl=Path("/bin/false"),
                max_filesize_mb=47,
                timeout_seconds=30,
                library_min_free_space_mb=0,
            )
            output = storage_dir / "fixture.mp4"
            output.write_bytes(b"telegram integration output")
            downloader = _Downloader(output)
            downloader.release.set()
            runtime.lifecycle.downloader = downloader
            runtime.lifecycle.promoter = _Promoter()
            job = await create_job(
                db_path,
                "https://www.youtube.com/watch?v=adapter",
                7,
                9,
            )
            status = _Status()
            context = SimpleNamespace(
                application=SimpleNamespace(
                    bot_data={
                        "telegram_acquisition": runtime,
                        "download_work": SimpleNamespace(
                            cancellation_requested=lambda label: False
                        ),
                    }
                )
            )
            settings = SimpleNamespace(local_api_url=None)
            with patch(
                "media_bot.__main__._send_secure_link", new=AsyncMock()
            ) as send_link:
                from media_bot.__main__ import _process_single_url_with_acquisition

                result = await _process_single_url_with_acquisition(
                    SimpleNamespace(),
                    context,
                    job,
                    status,
                    job.url,
                    7,
                    settings,
                    db_path,
                    runtime,
                )
            self.assertEqual(result, f"#{job.id}: OK")
            send_link.assert_awaited_once()
            stored = await get_job(db_path, job.id)
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored.status, "uploaded")
            self.assertTrue(stored.file_path and Path(stored.file_path).is_file())
            self.assertIn("library_asset_id", stored.output_metadata or "")


if __name__ == "__main__":
    unittest.main()
