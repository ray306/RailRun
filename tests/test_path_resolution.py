from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path

from next_step import parse_procedure_and_consts, resolve_procedure_path


class PathResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.old_cwd = Path.cwd()
        os.chdir(self.root)

    def tearDown(self) -> None:
        os.chdir(self.old_cwd)
        self.tmpdir.cleanup()

    def _args(self, procedure: str) -> argparse.Namespace:
        return argparse.Namespace(procedure=procedure)

    def test_bracketed_alias_resolves_from_config(self) -> None:
        target = self.root / "flows" / "main.rail"
        target.parent.mkdir(parents=True)
        target.write_text("输出 A\n", encoding="utf-8")
        (self.root / "config.json").write_text(
            json.dumps({"rails_alias_and_path": {"main": "flows/main.rail"}}, ensure_ascii=False),
            encoding="utf-8",
        )

        resolved = resolve_procedure_path(self._args("[main]"))

        self.assertEqual(resolved, str(target.resolve()))

    def test_alias_call_with_consts_keeps_alias_resolvable_after_parse(self) -> None:
        target = self.root / "flows" / "main.rail"
        target.parent.mkdir(parents=True)
        target.write_text("输出 A\n", encoding="utf-8")
        (self.root / "config.json").write_text(
            json.dumps({"rails_alias_and_path": {"main": "flows/main.rail"}}, ensure_ascii=False),
            encoding="utf-8",
        )

        procedure, consts = parse_procedure_and_consts("[main](no_pause=true, env='prod')")
        resolved = resolve_procedure_path(self._args(procedure))

        self.assertEqual(resolved, str(target.resolve()))
        self.assertEqual(consts, {"no_pause": True, "env": "prod"})

    def test_relative_name_resolves_as_plain_path(self) -> None:
        (self.root / "config.json").write_text(
            json.dumps(
                {
                    "rails_alias_and_path": {"simple_todo_flow": "examples/simple_todo_flow.rail"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        resolved = resolve_procedure_path(self._args("simple_todo_flow"))

        self.assertEqual(resolved, str((self.root / "simple_todo_flow.rail").resolve()))


if __name__ == "__main__":
    unittest.main()
