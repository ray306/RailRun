from __future__ import annotations

import unittest

from scripts.dashboard import build_session_markdown


class DashboardExportTests(unittest.TestCase):
    def test_build_session_markdown_groups_steps_and_removes_duplicate_outputs(self) -> None:
        session = {
            "session": "abcd",
            "status": "done",
            "procedure_path": r"C:\flows\analysis.rail",
            "history": [
                {"step_index": 0, "type": "Guidance", "instruction": "internal"},
                {"step_index": 1, "type": "Step", "output": "第一步结果"},
                {"step_index": 1, "type": "Branch", "output": "补充判断"},
                {"step_index": 2, "type": "Step", "output": "  第一步结果\n"},
                {"step_index": 3, "type": "Step", "output": "最终结果\n\n- A\n- B"},
            ],
        }

        markdown = build_session_markdown(session)

        self.assertIn("# analysis — Session abcd", markdown)
        self.assertIn("## 步骤 1", markdown)
        self.assertIn("第一步结果", markdown)
        self.assertIn("补充判断", markdown)
        self.assertIn("## 步骤 3", markdown)
        self.assertIn("最终结果\n\n- A\n- B", markdown)
        self.assertNotIn("## 步骤 2", markdown)
        self.assertEqual(markdown.count("第一步结果"), 1)

    def test_build_session_markdown_handles_no_recorded_outputs(self) -> None:
        markdown = build_session_markdown({
            "session": "empty",
            "status": "running",
            "procedure_path": "flow.rail",
            "history": [{"step_index": 0, "type": "Guidance"}],
        })

        self.assertIn("尚未记录正式输出", markdown)


if __name__ == "__main__":
    unittest.main()
