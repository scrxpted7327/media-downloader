import asyncio
import unittest
from unittest.mock import AsyncMock

from telegram.error import BadRequest

from media_bot.pool_ui import _answer_callback


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


if __name__ == "__main__":
    unittest.main()
