import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ci"))
from llm_review import parse_status  # noqa: E402


class ParseStatusTest(unittest.TestCase):

    def test_last_line_passed(self):
        self.assertEqual(parse_status("問題なし\n\nSTATUS: PASSED\n"), "PASSED")

    def test_last_line_failed(self):
        self.assertEqual(parse_status("RT 不一致\nSTATUS: FAILED"), "FAILED")

    def test_markdown_bold_is_accepted(self):
        self.assertEqual(parse_status("**STATUS: FAILED**"), "FAILED")

    def test_status_not_on_last_line_fails_closed(self):
        self.assertIsNone(parse_status("STATUS: PASSED\n追記: 念のため確認を"))

    def test_extra_text_on_status_line_fails_closed(self):
        self.assertIsNone(parse_status("STATUS: PASSED (たぶん)"))

    def test_empty_answer_fails_closed(self):
        self.assertIsNone(parse_status(""))


if __name__ == "__main__":
    unittest.main()
