"""0.5.9: the install guide is read with NVDA by the participant, before the kit is unpacked."""
import re
import unittest
from pathlib import Path

GUIDE = (Path(__file__).resolve().parents[1] / "docs" / "kit-install-guide.html").read_text(encoding="utf-8")
TEXT = re.sub(r"<style.*?</style>|<[^>]+>", " ", GUIDE, flags=re.S)


class InstallGuideTests(unittest.TestCase):
    def test_steps_are_headings_a_screen_reader_can_jump_to(self):
        self.assertIn('<html lang="ko">', GUIDE)
        self.assertEqual(1, GUIDE.count("<h1>"))
        steps = re.findall(r"<h2>(\d)단계\.", GUIDE)
        self.assertEqual([str(n) for n in range(1, 8)], steps)
        self.assertNotIn("<img", GUIDE)
        self.assertNotIn("<table", GUIDE)

    def test_every_action_is_a_key_not_a_look_or_a_click(self):
        for visual in ("클릭", "마우스", "보이", "보세요", "검은", "화면 위쪽", "아이콘", "더블"):
            self.assertNotIn(visual, TEXT, visual)
        for key in ("Windows</kbd>+<kbd>R", "Shift</kbd>+<kbd>F10", "Space</kbd>", "Shift</kbd>+<kbd>Tab", "Alt</kbd>+<kbd>F4"):
            self.assertIn(key, GUIDE, key)

    def test_no_access_code_and_no_server_upload_promise(self):
        self.assertNotIn("접속 코드", TEXT)
        self.assertNotIn("연구진 서버로 전송", TEXT)


if __name__ == "__main__":
    unittest.main()
