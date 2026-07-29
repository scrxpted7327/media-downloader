import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from telegram.error import BadRequest

from media_bot.pool_ui import FlowState, _State, _answer_callback, _edit_message, pool_callback


class PoolCallbackTests(unittest.TestCase):
    def test_expired_callback_does_not_stop_pool_action(self):
        query = AsyncMock()
        query.answer.side_effect = BadRequest(
            "Query is too old and response timeout expired or query id is invalid"
        )

        answered = asyncio.run(_answer_callback(query))

        self.assertFalse(answered)
        query.answer.assert_awaited_once_with(None, show_alert=False)

    def test_unrelated_bad_request_is_not_hidden(self):
        query = AsyncMock()
        query.answer.side_effect = BadRequest("message is not modified")

        with self.assertRaises(BadRequest):
            asyncio.run(_answer_callback(query))

    def test_message_edit_failure_reaches_error_handler(self):
        query = AsyncMock()
        query.edit_message_text.side_effect = BadRequest("chat not found")

        with self.assertRaises(BadRequest):
            asyncio.run(_edit_message(query, "Pool"))

    def test_unchanged_message_edit_is_safe(self):
        query = AsyncMock()
        query.edit_message_text.side_effect = BadRequest("Message is not modified")

        asyncio.run(_edit_message(query, "Pool"))

    def test_cannot_select_another_users_source_job(self):
        query = AsyncMock()
        query.data = "pool:addpick:41"
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=7),
        )
        context = SimpleNamespace(
            user_data={"pool_flow": FlowState(action=_State.POOL_ADD_PICK)},
            application=SimpleNamespace(bot_data={"db_path": "test.db"}),
        )
        foreign_job = SimpleNamespace(id=41, user_id=8, file_path="/tmp/video.mp4")

        with patch("media_bot.pool_ui.get_job", new=AsyncMock(return_value=foreign_job)):
            asyncio.run(pool_callback(update, context))

        self.assertEqual(context.user_data["pool_flow"].action, _State.POOL_ADD_PICK)
        self.assertNotIn("source_job_id", context.user_data["pool_flow"].data)
        self.assertIn(
            call("Source not found", show_alert=True),
            query.answer.await_args_list,
        )

    def test_cannot_toggle_another_users_workflow(self):
        query = AsyncMock()
        query.data = "workflow:toggle:12"
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=7),
        )
        context = SimpleNamespace(
            user_data={"pool_flow": FlowState(action=_State.WORKFLOW_LIST)},
            application=SimpleNamespace(bot_data={"db_path": "test.db"}),
        )
        foreign_workflow = SimpleNamespace(id=12, user_id=8, enabled=True)

        with (
            patch("media_bot.pool_ui.get_workflow", new=AsyncMock(return_value=foreign_workflow)),
            patch("media_bot.pool_ui.update_workflow", new=AsyncMock()) as update_workflow,
            patch("media_bot.pool_ui.list_workflows", new=AsyncMock(return_value=[])),
        ):
            asyncio.run(pool_callback(update, context))

        update_workflow.assert_not_awaited()
        self.assertIn(
            call("Workflow not found", show_alert=True),
            query.answer.await_args_list,
        )


if __name__ == "__main__":
    unittest.main()
