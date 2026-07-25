import unittest

from media_bot.colors import resolve_ass_color, resolve_drawtext_color


class ResolveColorTests(unittest.TestCase):
    def test_drawtext_named_and_hex(self):
        self.assertEqual(resolve_drawtext_color("white"), "#FFFFFF")
        self.assertEqual(resolve_drawtext_color("#ff0000"), "#FF0000")
        self.assertEqual(resolve_drawtext_color("#abc"), "#AABBCC")
        self.assertEqual(resolve_drawtext_color(None), "#FFFFFF")

    def test_ass_named_and_hex(self):
        self.assertEqual(resolve_ass_color("white"), "&H00FFFFFF")
        self.assertEqual(resolve_ass_color("#FF0000"), "&H000000FF")
        self.assertEqual(resolve_ass_color("red"), "&H000000FF")


class EdgeRateTests(unittest.TestCase):
    def test_edge_rate_sign(self):
        def rate(speed: float) -> str:
            rate_pct = int(round((speed - 1.0) * 100))
            return f"{rate_pct:+d}%"

        self.assertEqual(rate(0.5), "-50%")
        self.assertEqual(rate(1.0), "+0%")
        self.assertEqual(rate(2.0), "+100%")


if __name__ == "__main__":
    unittest.main()
