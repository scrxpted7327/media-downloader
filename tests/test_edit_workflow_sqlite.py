import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import aiosqlite

from media_bot.edit_workflow import (
    AttemptRecord,
    MetadataOutput,
    PhaseRecord,
    PhaseState,
    RenderArtifact,
    SettingsSnapshot,
    WorkflowRecord,
    WorkflowState,
)
from media_bot.edit_workflow_sqlite import (
    ConcurrentWorkflowSaveError,
    SQLiteWorkflowPersistence,
    WorkflowPayloadError,
    init,
)


class SQLiteWorkflowPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "workflow.db"

    def tearDown(self):
        self.tmpdir.cleanup()

    def _record(self) -> WorkflowRecord:
        settings = SettingsSnapshot.from_mapping(
            {"quality": "1080p", "filters": ["crop", "captions"], "nested": {"enabled": True}}
        )
        attempt = AttemptRecord(
            number=1,
            settings=settings,
            render=PhaseRecord(PhaseState.COMPLETED, "wf:render", None),
            review=PhaseRecord(PhaseState.COMPLETED, "wf:review", None),
            metadata=PhaseRecord(PhaseState.COMPLETED, "wf:metadata", None),
            artifact=RenderArtifact(Path("/tmp/render.mp4"), 1234, "abc123", True),
            metadata_output=MetadataOutput(
                "A title", ("#one", "#two"), {"provider": "test", "parameters": {"temperature": 0}}
            ),
            error=None,
        )
        return WorkflowRecord(
            workflow_id="wf-1",
            source_path=Path("/tmp/source.mp4"),
            output_path=Path("/tmp/render.mp4"),
            state=WorkflowState.COMPLETED,
            attempt=attempt,
            sequence=7,
        )

    def test_round_trip_fidelity_and_restart_recovery(self):
        async def run():
            persistence = SQLiteWorkflowPersistence(self.db_path)
            await persistence.init()
            record = self._record()
            await persistence.save(record)

            recovered = await SQLiteWorkflowPersistence(self.db_path).load("wf-1")
            self.assertEqual(recovered, record)
            self.assertIsInstance(recovered, WorkflowRecord)
            self.assertIsInstance(recovered.attempt, AttemptRecord)
            self.assertIsInstance(recovered.attempt.artifact, RenderArtifact)
            self.assertEqual(recovered.attempt.settings.as_dict()["filters"], ("crop", "captions"))

        asyncio.run(run())

    def test_init_is_idempotent_and_sequence_persists(self):
        async def run():
            await init(self.db_path)
            await init(self.db_path)
            persistence = SQLiteWorkflowPersistence(self.db_path)
            record = self._record()
            await persistence.save(record)
            loaded = await persistence.load(record.workflow_id)
            self.assertEqual(loaded.sequence, 7)

        asyncio.run(run())

    def test_revision_history_round_trips(self):
        async def run():
            persistence = SQLiteWorkflowPersistence(self.db_path)
            await persistence.init()
            original = self._record()
            revised = replace(
                original,
                sequence=8,
                attempt=replace(original.attempt, number=2, settings=SettingsSnapshot.from_mapping({"quality": "4k"})),
                history=(original.attempt,),
                state=WorkflowState.QUEUED,
            )
            await persistence.save(revised)
            self.assertEqual(await persistence.load("wf-1"), revised)

        asyncio.run(run())

    def test_malformed_and_unknown_payloads_are_rejected(self):
        async def run():
            await init(self.db_path)
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO edit_workflow_records VALUES (?, ?, ?, ?, datetime('now'))",
                    ("bad-json", 1, 0, "not json"),
                )
                await db.execute(
                    "INSERT INTO edit_workflow_records VALUES (?, ?, ?, ?, datetime('now'))",
                    ("bad-version", 99, 0, "{}"),
                )
                await db.commit()
            persistence = SQLiteWorkflowPersistence(self.db_path)
            with self.assertRaises(WorkflowPayloadError):
                await persistence.load("bad-json")
            with self.assertRaises(WorkflowPayloadError):
                await persistence.load("bad-version")

        asyncio.run(run())

    def test_concurrent_stale_save_is_rejected(self):
        async def run():
            persistence = SQLiteWorkflowPersistence(self.db_path)
            await persistence.init()
            base = self._record()
            await persistence.save(base)
            first = replace(base, sequence=8, state=WorkflowState.PROCESSING)
            second = replace(base, sequence=8, state=WorkflowState.FAILED)
            await persistence.save(first)
            with self.assertRaises(ConcurrentWorkflowSaveError):
                await persistence.save(second)
            self.assertEqual((await persistence.load("wf-1")).state, WorkflowState.PROCESSING)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
