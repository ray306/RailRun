from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path

from next_step import ProcedureResolutionError, parse_procedure_and_consts, resolve_procedure_path


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

    def test_alias_call_with_escaped_quotes_and_spaces(self) -> None:
        target = self.root / "flows" / "main.rail"
        target.parent.mkdir(parents=True)
        target.write_text("输出 A\n", encoding="utf-8")
        (self.root / "config.json").write_text(
            json.dumps({"rails_alias_and_path": {"main": "flows/main.rail"}}, ensure_ascii=False),
            encoding="utf-8",
        )

        procedure, consts = parse_procedure_and_consts(" [main](no_pause=true, title='won\\'t', text=\"hello \\\"world\\\"\")  ")
        resolved = resolve_procedure_path(self._args(procedure))

        self.assertEqual(resolved, str(target.resolve()))
        self.assertEqual(consts, {"no_pause": True, "title": "won't", "text": 'hello "world"'})

    def test_strip_quotes_and_brackets_without_parameters(self) -> None:
        target = self.root / "flows" / "main.rail"
        target.parent.mkdir(parents=True)
        target.write_text("输出 A\n", encoding="utf-8")
        (self.root / "config.json").write_text(
            json.dumps({"rails_alias_and_path": {"main": "flows/main.rail"}}, ensure_ascii=False),
            encoding="utf-8",
        )

        # Test double quotes, single quotes, brackets and parentheses recursively
        inputs = [
            '"[main]"',
            "'[main]'",
            "([main])",
            "'main'",
            '"main"',
            "[main]",
            " (main) "
        ]
        for inp in inputs:
            procedure, consts = parse_procedure_and_consts(inp)
            resolved = resolve_procedure_path(self._args(procedure))
            self.assertEqual(resolved, str(target.resolve()), f"Failed on input: {inp}")
            self.assertEqual(consts, {})

    def test_md_rewrite_retains_parameters_in_retry_cmd(self) -> None:
        md_target = self.root / "flows" / "main.md"
        md_target.parent.mkdir(parents=True)
        md_target.write_text("# Flow\n", encoding="utf-8")

        from next_step import build_md_rewrite_instruction
        args = self._args("flows/main.md")
        procedure_clean, consts = parse_procedure_and_consts("flows/main.md(no_pause=true, env='prod')")
        args.procedure = procedure_clean
        args.consts = consts
        args.host = "127.0.0.1"
        args.port = 8799
        args.max_retries = 3
        args.sessions_dir = str(self.root / "sessions")
        args.branch_value = None
        args.session = "1234"

        instruction = build_md_rewrite_instruction(args)
        self.assertIsNotNone(instruction)
        retry_command = instruction["retry_command"]
        import shlex
        parts = shlex.split(retry_command)
        self.assertEqual(parts[3], f"{md_target.with_suffix('.rail')}(no_pause=true, env='prod')")

    def test_direct_rail_path_resolves_from_cwd(self) -> None:
        target = self.root / "examples" / "simple_todo_flow.rail"
        target.parent.mkdir(parents=True)
        target.write_text("输出 A\n", encoding="utf-8")

        resolved = resolve_procedure_path(self._args("examples/simple_todo_flow.rail"))

        self.assertEqual(resolved, str(target.resolve()))

    def test_direct_md_and_txt_paths_resolve_from_cwd(self) -> None:
        md_target = self.root / "flows" / "main.md"
        txt_target = self.root / "flows" / "main.txt"
        md_target.parent.mkdir(parents=True)
        md_target.write_text("# Flow\n", encoding="utf-8")
        txt_target.write_text("Flow\n", encoding="utf-8")

        self.assertEqual(resolve_procedure_path(self._args("flows/main.md")), str(md_target.resolve()))
        self.assertEqual(resolve_procedure_path(self._args("flows/main.txt")), str(txt_target.resolve()))

    def test_unbracketed_alias_resolves_successfully(self) -> None:
        target = self.root / "examples" / "simple_todo_flow.rail"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("输出 A\n", encoding="utf-8")
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
        self.assertEqual(resolved, str(target.resolve()))

    def test_missing_direct_file_raises_resolution_error(self) -> None:
        with self.assertRaises(ProcedureResolutionError):
            resolve_procedure_path(self._args("missing.rail"))

    def test_missing_alias_raises_resolution_error(self) -> None:
        (self.root / "config.json").write_text(
            json.dumps({"rails_alias_and_path": {"other": "examples/other.rail"}}, ensure_ascii=False),
            encoding="utf-8",
        )

        with self.assertRaises(ProcedureResolutionError):
            resolve_procedure_path(self._args("[simple_todo_flow]"))

    def test_alias_with_missing_target_raises_resolution_error(self) -> None:
        (self.root / "config.json").write_text(
            json.dumps({"rails_alias_and_path": {"main": "flows/main.rail"}}, ensure_ascii=False),
            encoding="utf-8",
        )

        with self.assertRaises(ProcedureResolutionError):
            resolve_procedure_path(self._args("[main]"))


if __name__ == "__main__":
    unittest.main()
