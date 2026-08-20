from __future__ import annotations

import unittest
from pathlib import Path

from media_bot.edit_workflow import (
    DeliveryState,
    EditWorkflow,
    MetadataOutput,
    PhaseState,
    RenderArtifact,
    WorkflowState,
)


class MemoryPersistence:
    def __init__(self):
        self.rows = {}
        self.saves = []

    async def load(self, workflow_id):
        return self.rows.get(workflow_id)

    async def save(self, record):
        self.rows[record.workflow_id] = record
        self.saves.append(record)


class Events:
    def __init__(self):
        self.items = []

    async def emit(self, event):
        self.items.append(event)


class Renderer:
    def __init__(self, artifact=None, error=None):
        self.calls = []
        self.artifact = artifact or RenderArtifact(Path("/tmp/render.mp4"), 10)
        self.error = error

    async def render(self, request):
        self.calls.append(request)
        if self.error:
            raise self.error
        return self.artifact


class Metadata:
    def __init__(self, output=None, error=None):
        self.calls = []
        self.output = output or MetadataOutput("A clip", ("#one", "#two"))
        self.error = error

    async def generate(self, artifact, *, request):
        self.calls.append((artifact, request))
        if self.error:
            raise self.error
        return self.output


class EditWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def make_workflow(self, *, renderer=None, metadata=None, events=None):
        self.persistence = MemoryPersistence()
        self.renderer = renderer or Renderer()
        self.metadata = metadata or Metadata()
        self.events = events or Events()
        workflow = EditWorkflow(
            renderer=self.renderer,
            metadata_engine=self.metadata,
            persistence=self.persistence,
            progress=self.events,
        )
        await workflow.create("edit-1", source_path=Path("source.mp4"), output_path=Path("out.mp4"), settings={"quality": "high"})
        return workflow

    async def test_render_success_queues_and_completes_metadata(self):
        workflow = await self.make_workflow()
        record = await workflow.run("edit-1")
        self.assertEqual(record.state, WorkflowState.COMPLETED)
        self.assertEqual(record.attempt.render.state, PhaseState.COMPLETED)
        self.assertEqual(record.attempt.metadata.state, PhaseState.COMPLETED)
        self.assertEqual(len(self.renderer.calls), 1)
        self.assertEqual(len(self.metadata.calls), 1)

    async def test_metadata_unavailable_does_not_fail_render(self):
        workflow = await self.make_workflow(metadata=Metadata(error=RuntimeError("Codex unavailable")))
        record = await workflow.run("edit-1")
        self.assertEqual(record.state, WorkflowState.COMPLETED)
        self.assertEqual(record.attempt.render.state, PhaseState.COMPLETED)
        self.assertEqual(record.attempt.metadata.state, PhaseState.FAILED)
        retry = await workflow.retry_metadata("edit-1")
        self.assertEqual(retry.attempt.metadata.state, PhaseState.PENDING)

    async def test_delivery_retry_does_not_rerender(self):
        workflow = await self.make_workflow()
        await workflow.run("edit-1")
        await workflow.record_delivery("edit-1", success=False, error="temporary")
        record = await workflow.record_delivery("edit-1", success=True)
        self.assertEqual(record.attempt.delivery.state, DeliveryState.COMPLETED)
        self.assertEqual(len(self.renderer.calls), 1)

    async def test_cancelling_after_render_preserves_completed_artifact(self):
        workflow = await self.make_workflow()
        await workflow.run("edit-1")
        record = await workflow.cancel("edit-1")
        self.assertEqual(record.state, WorkflowState.COMPLETED)
        self.assertEqual(record.attempt.render.state, PhaseState.COMPLETED)
        self.assertEqual(record.attempt.metadata.state, PhaseState.COMPLETED)

    async def test_cancellation_at_checkpoint(self):
        workflow = await self.make_workflow()
        record = await workflow.cancel("edit-1")
        self.assertEqual(record.state, WorkflowState.CANCELLED)
        self.assertEqual(record.attempt.render.state, PhaseState.CANCELLED)
        self.assertEqual(self.renderer.calls, [])

    async def test_review_pause_resume_and_revise(self):
        workflow = await self.make_workflow()
        await workflow.create("reviewed", source_path=Path("source.mp4"), output_path=Path("out.mp4"), settings={"v": 1}, review_required=True)
        record = await workflow.run("reviewed")
        self.assertEqual(record.attempt.review.state, PhaseState.PAUSED)
        record = await workflow.resume_review("reviewed")
        self.assertEqual(record.attempt.review.state, PhaseState.COMPLETED)
        record = await workflow.revise("reviewed", {"v": 2})
        self.assertEqual(record.attempt.number, 2)
        self.assertEqual(len(record.history), 1)

    async def test_resume_is_idempotent(self):
        workflow = await self.make_workflow()
        first = await workflow.run("edit-1")
        second = await workflow.reconcile("edit-1")
        self.assertEqual(first.attempt.artifact, second.attempt.artifact)
        self.assertEqual(len(self.renderer.calls), 1)
        self.assertEqual(len(self.metadata.calls), 1)

    async def test_persistence_precedes_progress(self):
        workflow = await self.make_workflow()
        await workflow.run("edit-1")
        self.assertEqual([event.sequence for event in self.events.items], list(range(1, len(self.events.items) + 1)))
        for event in self.events.items:
            saved = [row for row in self.persistence.saves if row.sequence == event.sequence]
            self.assertTrue(saved)


if __name__ == "__main__":
    unittest.main()
