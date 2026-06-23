from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.rail_compiler import RailCompiler
from scripts.base import RailRunRuntime


class VariableFeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.sessions = self.root / "sessions"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_runtime_variables_feedback_and_rendering(self) -> None:
        # Create a CFG flow:
        # Node 1: Step "获取股票价格", next is Node 2
        # Node 2: Step "股票价格是 {{price}}", next is Node 3
        # Node 3: Finished
        cfg = {
            "protocol": "next-step-cfg/v1",
            "entry": "1",
            "nodes": {
                "1": {"type": "Step", "instruction": "获取股票价格", "next": "2"},
                "2": {"type": "Step", "instruction": "股票价格是 {{price}}", "next": "3"},
                "3": {"type": "Finished", "instruction": "完成"},
            },
        }
        dag = self.root / "flow-cfg.json"
        dag.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

        rt = RailRunRuntime(dag, self.sessions)
        rt.init_session("s1")

        # Step 1: 获取股票价格
        resp1 = rt.next_step("s1")
        self.assertEqual(resp1["type"], "Step")
        self.assertEqual(resp1["instruction"], "获取股票价格")

        # Agent returns variables `price=150` on next step call
        resp2 = rt.next_step("s1", variables={"price": 150}, output="查询到价格为 150")
        self.assertEqual(resp2["type"], "Step")
        # Ensure template variables are correctly rendered with the passed variable
        self.assertEqual(resp2["instruction"], "股票价格是 150")

        # Session variables are persistently saved
        session = rt._load_session("s1")
        self.assertEqual(session.vars.get("price"), 150)

        # Step 3
        resp3 = rt.next_step("s1", output="股票价格是 150")
        self.assertEqual(resp3["type"], "Finished")


if __name__ == "__main__":
    unittest.main()
