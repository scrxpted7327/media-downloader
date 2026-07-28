import unittest

from media_bot.menu import Menu


class MenuTests(unittest.TestCase):
    def test_toggle_targets_next_state(self):
        markup = (
            Menu()
            .toggle("Auto Captions", False, "edit:set:auto_captions:yes")
            .toggle("Remove Watermark", True, "edit:set:watermark_removal:no")
            .back("download:actions:1")
            .home("settings:menu")
            .build()
        )

        buttons = [row[0] for row in markup.inline_keyboard]
        self.assertEqual(buttons[0].text, "❌ Auto Captions")
        self.assertEqual(buttons[0].callback_data, "edit:set:auto_captions:yes")
        self.assertEqual(buttons[1].text, "✅ Remove Watermark")
        self.assertEqual(buttons[1].callback_data, "edit:set:watermark_removal:no")
        self.assertEqual(buttons[2].text, "← Back")
        self.assertEqual(buttons[3].text, "🏠 Home")


if __name__ == "__main__":
    unittest.main()
