from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from media_bot.shared_media_library import (
    AuthorizationError,
    LibraryPrincipal,
    PromotionError,
    SharedMediaLibrary,
    UnsafeLibraryPathError,
    VariantPendingError,
    source_identity,
)
from media_bot.storage import get_media_asset, get_media_variant, init_db, update_media_variant


class SharedMediaLibraryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root / "library"
        self.source_root = root / "sources"
        self.source_root.mkdir()
        self.db = root / "media.db"
        await init_db(self.db)
        self.owner = LibraryPrincipal("owner", frozenset({"media.library.read", "media.library.variant_request", "media.library.manage"}))
        self.reader = LibraryPrincipal("reader", frozenset({"media.library.read"}))
        self.library = SharedMediaLibrary(self.db, self.root)

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_promote_is_idempotent_and_uses_native_identity(self) -> None:
        source = self.source_root / "input.webm"
        source.write_bytes(b"fixture-media")
        first = await self.library.promote(
            self.owner, source, "https://example.test/watch?id=1&utm_source=x",
            source_platform="Example", source_media_id="native-1",
        )
        second = await self.library.promote(
            self.owner, source, "https://example.test/other", source_platform="example",
            source_media_id="native-1",
        )
        self.assertEqual(first.asset.id, second.asset.id)
        self.assertEqual(first.variant.id, second.variant.id)
        self.assertTrue(second.idempotent)
        self.assertEqual(source_identity("https://example.test/watch?id=1", platform="Example", media_id="native-1").source_key, "native:example:native-1")

    async def test_auth_and_safe_handle_reject_arbitrary_paths(self) -> None:
        source = self.source_root / "input.mp4"
        source.write_bytes(b"safe")
        result = await self.library.promote(self.owner, source, "https://example.test/a")
        with self.assertRaises(AuthorizationError):
            await self.library.open_variant(LibraryPrincipal("blocked"), result.asset.id)
        variant = await get_media_variant(self.db, result.variant.id)
        assert variant is not None
        await update_media_variant(self.db, variant.id, file_path=str(self.root / ".." / "outside"))
        with self.assertRaises(UnsafeLibraryPathError):
            await self.library.open_variant(self.owner, result.asset.id)

    async def test_delete_is_logical_and_preserves_requester_source_file(self) -> None:
        source = self.source_root / "input.mp4"
        source.write_bytes(b"requester-file")
        result = await self.library.promote(self.owner, source, "https://example.test/a")
        promoted = result.variant.file_path
        assert promoted
        await self.library.delete(self.owner, result.asset.id)
        self.assertTrue(source.exists())
        self.assertIsNotNone(await get_media_asset(self.db, result.asset.id))
        # Logical deletion does not destroy a canonical file or the requester file;
        # a later retention pass may remove it only after reference checks.
        self.assertTrue(Path(promoted).exists())
        removed = await self.library.cleanup_deleted(self.owner, result.asset.id)
        self.assertEqual(removed, (Path(promoted),))
        self.assertFalse(Path(promoted).exists())
        self.assertTrue(source.exists())
        with self.assertRaises(Exception):
            await self.library.open_variant(self.owner, result.asset.id)

    async def test_failed_promotion_cleans_partial_file_and_records_retryable_failure(self) -> None:
        source = self.source_root / "input.mp4"
        source.write_bytes(b"failure-fixture")
        library = SharedMediaLibrary(self.db, self.root / "not-a-file")
        # A directory at the deterministic destination forces the atomic copy to fail.
        destination = self.root / "not-a-file" / "assets"
        destination.mkdir(parents=True)
        (destination / "1").write_text("not a directory")
        with self.assertRaises(PromotionError):
            await library.promote(self.owner, source, "https://example.test/failure")
        asset = await get_media_asset(self.db, 1)
        variant = await get_media_variant(self.db, 1)
        self.assertIsNotNone(asset)
        self.assertIsNotNone(variant)
        assert variant is not None
        self.assertEqual(variant.status, "failed")
        self.assertIsNone(variant.file_path)

    async def test_variant_request_selects_ready_source_and_failure_is_retryable(self) -> None:
        source = self.source_root / "input.mp4"
        source.write_bytes(b"source")
        result = await self.library.promote(self.owner, source, "https://example.test/source")
        requested = await self.library.request_variant(self.owner, result.asset.id, "video_720p")
        self.assertEqual(requested.source_variant.id, result.variant.id)
        self.assertEqual(requested.variant.status, "queued")
        failed = await self.library.fail_variant(self.owner, requested.variant.id, code="encoder_unavailable", message="retry me")
        self.assertEqual(failed.status, "failed")
        retried = await self.library.request_variant(self.owner, result.asset.id, "video_720p")
        self.assertEqual(retried.variant.status, "queued")

    async def test_request_without_ready_source_is_pending(self) -> None:
        source = self.source_root / "input.mp4"
        source.write_bytes(b"source")
        result = await self.library.promote(self.owner, source, "https://example.test/source")
        await self.library.fail_variant(self.owner, result.variant.id, code="temporary", message="retry")
        with self.assertRaises(VariantPendingError):
            await self.library.request_variant(self.owner, result.asset.id, "video_720p")


if __name__ == "__main__":
    unittest.main()
