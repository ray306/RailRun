from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from next_step import (
    ProcedureResolutionError,
    build_generator_consts_for_file,
    normalize_runtime_options,
    parse_procedure_and_consts,
    resolve_procedure_path,
)


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

    def test_reserved_language_can_be_extracted_from_procedure_consts(self) -> None:
        procedure, consts = parse_procedure_and_consts("[main](language='English', no_pause=true)")

        self.assertEqual(procedure, "[main]")
        self.assertEqual(consts, {"language": "English", "no_pause": True})

    def test_runtime_defaults_are_loaded_from_config(self) -> None:
        (self.root / "config.json").write_text(
            json.dumps(
                {
                    "runtime": {"persistence": False, "language": "English"},
                    "rails_alias_and_path": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        args = argparse.Namespace(procedure="[main]", persistence=None, language=None)

        ok, consts = normalize_runtime_options(args)

        self.assertTrue(ok)
        self.assertEqual(consts, {})
        self.assertEqual(args.procedure, "main")
        self.assertEqual(args.persistence, "false")
        self.assertEqual(args.language, "English")

    def test_procedure_runtime_params_override_config_defaults(self) -> None:
        (self.root / "config.json").write_text(
            json.dumps(
                {
                    "runtime": {"persistence": False, "language": "English"},
                    "rails_alias_and_path": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        args = argparse.Namespace(
            procedure="[main](persistence=true, language='中文', no_pause=true)",
            persistence=None,
            language=None,
        )

        ok, consts = normalize_runtime_options(args)

        self.assertTrue(ok)
        self.assertEqual(consts, {"no_pause": True})
        self.assertEqual(args.procedure, "[main]")
        self.assertEqual(args.persistence, "true")
        self.assertEqual(args.language, "中文")

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

    def test_generator_consts_for_text_file(self) -> None:
        md_target = self.root / "flows" / "main.md"
        md_target.parent.mkdir(parents=True)
        md_target.write_text("# Flow\n", encoding="utf-8")

        consts = build_generator_consts_for_file(
            str(md_target),
            {"no_pause": True, "env": "prod"},
        )

        self.assertEqual(consts["input_kind"], "file")
        self.assertEqual(consts["input_path"], str(md_target.resolve()))
        self.assertEqual(consts["suggested_output_rail"], str(md_target.with_suffix(".rail").resolve()))
        self.assertEqual(consts["source_flow_params"], "env='prod', no_pause=true")

    def test_next_step_routes_text_file_to_generator(self) -> None:
        md_target = self.root / "flows" / "main.md"
        sessions_dir = self.root / "sessions"
        md_target.parent.mkdir(parents=True)
        md_target.write_text("# Flow\n", encoding="utf-8")

        repo_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8",
                str(repo_root / "next_step.py"),
                "--procedure",
                f"{md_target}(persistence=false, language='English')",
                "--sessions-dir",
                str(sessions_dir),
            ],
            cwd=repo_root,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["type"], "init")
        session_file = sessions_dir / f"{payload['session']}.json"
        session = json.loads(session_file.read_text(encoding="utf-8"))
        self.assertTrue(session["procedure_path"].endswith("examples\\generate_rail_flow.rail") or session["procedure_path"].endswith("examples/generate_rail_flow.rail"))
        self.assertEqual(session["language"], "English")
        self.assertFalse(session["output_persistence_enabled"])
        self.assertEqual(session["vars"]["input_kind"], "file")
        self.assertEqual(session["vars"]["input_path"], str(md_target.resolve()))

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
