from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.rail_compiler import RailCompiler
from scripts.base import RailRunRuntime


class ForNodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.sessions = self.root / "sessions"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_runtime_for_node_supports_nested_loops(self) -> None:
        cfg = {
            "protocol": "next-step-cfg/v1",
            "entry": "0",
            "nodes": {
                "0": {
                    "type": "For",
                    "items": ["A", "B"],
                    "item_key": "city",
                    "index_key": "i",
                    "on_iterate": "1",
                    "on_done": "9",
                },
                "1": {
                    "type": "For",
                    "items": [1, 2],
                    "item_key": "num",
                    "index_key": "j",
                    "on_iterate": "2",
                    "on_done": "0",
                },
                "2": {"type": "Step", "instruction": "c={{city}} i={{i}} n={{num}} j={{j}}", "next": "1"},
                "9": {"type": "Finished", "instruction": "done"},
            },
        }
        dag = self.root / "flow-cfg.json"
        dag.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

        rt = RailRunRuntime(dag, self.sessions)
        rt.init_session("f1")

        steps: list[str] = []
        previous_was_step = False
        while True:
            resp = rt.next_step("f1", output="nested loop output" if previous_was_step else None)
            if resp["type"] == "Finished":
                break
            previous_was_step = resp["type"] == "Step"
            if resp["type"] == "Step":
                steps.append(resp["instruction"])

        self.assertEqual(
            steps,
            [
                "c=A i=0 n=1 j=0",
                "c=A i=0 n=2 j=1",
                "c=B i=1 n=1 j=0",
                "c=B i=1 n=2 j=1",
            ],
        )

    def test_rail_compiler_emits_for_node(self) -> None:
        rail = self.root / "for.rail"
        rail.write_text(
            "for item in [\"x\", \"y\"]:\n"
            "  step:\n"
            "    v={{item}}\n",
            encoding="utf-8",
        )
        compiler = RailCompiler()
        cfg = compiler.compile(rail)
        for_nodes = [node for node in cfg["nodes"].values() if node.get("type") == "For"]
        self.assertEqual(len(for_nodes), 1)
        self.assertEqual(for_nodes[0]["item_key"], "item")
        self.assertEqual(for_nodes[0]["items_expr"], "[\"x\", \"y\"]")


if __name__ == "__main__":
    unittest.main()
