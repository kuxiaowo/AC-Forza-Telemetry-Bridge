from pathlib import Path
import unittest


class HelpTests(unittest.TestCase):
    def test_packaged_help_covers_protocol_and_usage(self):
        text = (Path(__file__).parents[1] / "help.html").read_text(encoding="utf-8")
        for required in ("开始使用", "完整字段映射", "0 IsRaceOn", "323 Padding/Unknown",
                         "power.lut", "suspensions.ini", "没有映射"):
            self.assertIn(required, text)


if __name__ == "__main__":
    unittest.main()
