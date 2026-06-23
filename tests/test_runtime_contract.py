from __future__ import annotations

import tempfile
import threading
import time
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from scripts import session_runtime
from scripts.base import RailRunRuntime
from scripts.host_output import CodexOutputProvider


LANGUAGE_MESSAGE = "执行过程必须使用中文输出。"


class RuntimeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.sessions = self.root / "sessions"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _runtime(self, text: str, *, max_retries: int = 3) -> RailRunRuntime:
        procedure = self.root / "flow.rail"
        procedure.write_text(text, encoding="utf-8")
        return RailRunRuntime(procedure, self.sessions, max_retries=max_retries)

    def test_sequential_step_and_finished(self) -> None:
        rt = self._runtime("输出 A\n输出 B\n")
        init = rt.init_session("s1")
        self.assertEqual(init["type"], "init")

        s1 = rt.next_step("s1")
        self.assertEqual(s1, {"type": "Step", "instruction": "输出 A", "step_index": 0, "message": LANGUAGE_MESSAGE})
        s2 = rt.next_step("s1", output="正式输出 A")
        self.assertEqual(s2, {"type": "Step", "instruction": "输出 B", "step_index": 1})
        done = rt.next_step("s1", output="正式输出 B")
        self.assertEqual(done["type"], "Finished")
        session = rt._load_session("s1")
        self.assertEqual(session.history[0]["output"], "正式输出 A")
        self.assertEqual(session.history[1]["output"], "正式输出 B")

    def test_function_return_skips_remaining_function_body(self) -> None:
        rt = self._runtime(
            "def f():\n"
            "  输出 A\n"
            "  return\n"
            "  输出 B\n"
            "f()\n"
            "输出 C\n"
        )
        rt.init_session("function-return")

        first = rt.next_step("function-return")
        self.assertEqual(first, {"type": "Step", "instruction": "输出 A", "step_index": 0, "message": LANGUAGE_MESSAGE})
        second = rt.next_step("function-return", output="正式输出 A")
        self.assertEqual(second, {"type": "Step", "instruction": "输出 C", "step_index": 1})
        done = rt.next_step("function-return", output="正式输出 C")
        self.assertEqual(done["type"], "Finished")

    def test_function_return_inside_branch_skips_remaining_function_body(self) -> None:
        rt = self._runtime(
            "def f():\n"
            "  if should_return:\n"
            "    return\n"
            "  输出 B\n"
            "f()\n"
            "输出 C\n"
        )
        rt.init_session("branch-return")

        branch = rt.next_step("branch-return")
        self.assertEqual(branch["type"], "Branch")
        self.assertEqual(branch["message"], LANGUAGE_MESSAGE)
        step = rt.next_step("branch-return", branch_value=True, branch_present=True, output="分支判断")
        self.assertEqual(step, {"type": "Step", "instruction": "输出 C", "step_index": 1, "message": LANGUAGE_MESSAGE})
        done = rt.next_step("branch-return", output="正式输出 C")
        self.assertEqual(done["type"], "Finished")

    def test_top_level_return_finishes_flow(self) -> None:
        rt = self._runtime("输出 A\nreturn\n输出 B\n")
        rt.init_session("top-level-return")

        first = rt.next_step("top-level-return")
        self.assertEqual(first, {"type": "Step", "instruction": "输出 A", "step_index": 0, "message": LANGUAGE_MESSAGE})
        done = rt.next_step("top-level-return", output="正式输出 A")
        self.assertEqual(done["type"], "Finished")

    def test_first_response_includes_custom_language_message_once(self) -> None:
        procedure = self.root / "flow.rail"
        procedure.write_text("Output A\nOutput B\n", encoding="utf-8")
        rt = RailRunRuntime(procedure, self.sessions, language="English")
        rt.init_session("language")

        first = rt.next_step("language")
        second = rt.next_step("language", output="A output")

        self.assertEqual(first["message"], "执行过程必须使用English输出。")
        self.assertNotIn("message", second)
        session = rt._load_session("language")
        self.assertEqual(session.language, "English")
        self.assertTrue(session.language_message_emitted)

    def test_init_consts_are_available_as_runtime_variables(self) -> None:
        procedure = self.root / "flow.rail"
        procedure.write_text(
            "params(input_path='')\n"
            "读取 {{input_path}}\n",
            encoding="utf-8",
        )
        rt = RailRunRuntime(
            procedure,
            self.sessions,
            consts={"input_path": "requirements.md"},
            output_persistence_enabled=False,
        )

        init = rt.init_session("vars-init")
        step = rt.next_step("vars-init")

        self.assertEqual(init["type"], "init")
        self.assertEqual(rt._load_session("vars-init").vars["input_path"], "requirements.md")
        self.assertEqual(step["instruction"], "读取 requirements.md")

    def test_missing_or_blank_output_does_not_advance_cursor(self) -> None:
        rt = self._runtime("输出 A\n输出 B\n")
        rt.init_session("missing-output")
        rt.next_step("missing-output")

        missing = rt.next_step("missing-output")
        self.assertEqual(missing["type"], "ValidationError")
        self.assertEqual(rt._load_session("missing-output").cursor["step_index"], 1)

        blank = rt.next_step("missing-output", output=" \r\n\t")
        self.assertEqual(blank["type"], "ValidationError")
        session = rt._load_session("missing-output")
        self.assertEqual(session.cursor["step_index"], 1)
        self.assertNotIn("output", session.history[0])

    def test_output_preserves_unicode_multiline_and_special_characters(self) -> None:
        rt = self._runtime("输出 A\n")
        rt.init_session("unicode-output")
        rt.next_step("unicode-output")
        output = "第一行：中文\n第二行：\"quotes\" <tag> & emoji 🚆"
        done = rt.next_step("unicode-output", output=output)
        self.assertEqual(done["type"], "Finished")
        self.assertEqual(rt._load_session("unicode-output").history[0]["output"], output)

    def test_codex_transcript_automatically_records_output(self) -> None:
        procedure = self.root / "flow.rail"
        procedure.write_text("输出 A\n输出 B\n", encoding="utf-8")
        transcript = self.root / "codex" / "rollout-thread-123.jsonl"
        transcript.parent.mkdir()
        transcript.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": "thread-123"}}) + "\n",
            encoding="utf-8",
        )
        provider = CodexOutputProvider(
            env={"CODEX_THREAD_ID": "thread-123"},
            sessions_root=transcript.parent,
        )
        rt = RailRunRuntime(
            procedure,
            self.sessions,
            output_persistence_enabled=True,
            host_output_provider=provider,
        )
        init = rt.init_session("auto-output")
        self.assertEqual(init["output_persistence"], {"mode": "host_transcript", "host": "codex"})
        rt.next_step("auto-output")

        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "来自 transcript 的正式输出",
                    "phase": "commentary",
                },
            }, ensure_ascii=False) + "\n")

        second = rt.next_step("auto-output", output="应被自动模式屏蔽")
        self.assertEqual(second["type"], "Step")
        session = rt._load_session("auto-output")
        self.assertEqual(session.history[0]["output"], "来自 transcript 的正式输出")

    def test_disabled_persistence_does_not_require_or_record_output(self) -> None:
        procedure = self.root / "flow.rail"
        procedure.write_text("输出 A\n", encoding="utf-8")
        rt = RailRunRuntime(
            procedure,
            self.sessions,
            output_persistence_enabled=False,
        )
        init = rt.init_session("no-output")
        self.assertEqual(init["output_persistence"], {"mode": "disabled"})
        rt.next_step("no-output")

        done = rt.next_step("no-output")

        self.assertEqual(done["type"], "Finished")
        session = rt._load_session("no-output")
        self.assertFalse(session.output_persistence_enabled)
        self.assertNotIn("output", session.history[0])

    def test_rewind_session_replays_from_target_step(self) -> None:
        rt = self._runtime("输出 A\n输出 B\n")
        rt.init_session("rw1")
        first = rt.next_step("rw1")
        second = rt.next_step("rw1", output="A output")
        self.assertEqual(first, {"type": "Step", "instruction": "输出 A", "step_index": 0, "message": LANGUAGE_MESSAGE})
        self.assertEqual(second, {"type": "Step", "instruction": "输出 B", "step_index": 1})

        rewound = rt.rewind_session("rw1", 1)
        self.assertEqual(rewound["type"], "ok")
        replay = rt.next_step("rw1")
        self.assertEqual(replay, {"type": "Step", "instruction": "输出 B", "step_index": 1})

    def test_rewind_session_from_terminal_reopens_flow(self) -> None:
        rt = self._runtime("输出 A\n")
        rt.init_session("rw2")
        rt.next_step("rw2")
        done = rt.next_step("rw2", output="A output")
        self.assertEqual(done["type"], "Finished")

        rewound = rt.rewind_session("rw2", 0)
        self.assertEqual(rewound["type"], "ok")
        self.assertEqual(rt._load_session("rw2").history, [])
        replay = rt.next_step("rw2")
        self.assertEqual(replay, {"type": "Step", "instruction": "输出 A", "step_index": 0})
        rt.next_step("rw2", output="A rerun output")
        self.assertEqual(rt._load_session("rw2").history[0]["output"], "A rerun output")

    def test_rewind_session_rejects_out_of_range_step(self) -> None:
        rt = self._runtime("输出 A\n")
        rt.init_session("rw3")
        rt.next_step("rw3")
        err = rt.rewind_session("rw3", 9)
        self.assertEqual(err["type"], "ValidationError")

    def test_branch_requires_bool_and_path(self) -> None:
        rt = self._runtime(
            "if is_raining:\n"
            "  输出 T\n"
            "else:\n"
            "  输出 F\n"
        )
        rt.init_session("s2")
        branch = rt.next_step("s2")
        self.assertEqual(branch["type"], "Branch")
        self.assertEqual(branch["step_index"], 0)
        self.assertEqual(branch["message"], LANGUAGE_MESSAGE)
        missing = rt.next_step("s2")
        self.assertEqual(missing["type"], "ValidationError")
        wrong_type = rt.next_step("s2", branch_value="true", branch_present=True, output="分支判断正式输出")
        self.assertEqual(wrong_type["type"], "ValidationError")

        step = rt.next_step("s2", branch_value=True, branch_present=True)
        self.assertEqual(step, {"type": "Step", "instruction": "输出 T", "step_index": 1, "message": LANGUAGE_MESSAGE})
        done = rt.next_step("s2", output="T output")
        self.assertEqual(done["type"], "Finished")
        self.assertEqual(rt._load_session("s2").history[0]["output"], "分支判断正式输出")

    def test_retry_limit_to_human_interference(self) -> None:
        rt = self._runtime("if is_raining:\n  输出 ok\n", max_retries=1)
        rt.init_session("s3")
        rt.next_step("s3")  # Branch
        err1 = rt.next_step("s3")
        self.assertEqual(err1["type"], "ValidationError")
        err2 = rt.next_step("s3")
        self.assertEqual(err2["type"], "HumanInterferenceRequest")
        locked = rt.next_step("s3")
        self.assertEqual(locked["type"], "HumanInterferenceRequest")

    def test_parallel_next_step_is_blocked_with_warning(self) -> None:
        rt = self._runtime("输出 A\n输出 B\n")
        rt.init_session("s4")

        results: list[dict] = []
        real_advance = session_runtime.advance_session

        def slow_advance(*args, **kwargs):
            time.sleep(0.15)
            return real_advance(*args, **kwargs)

        def call_next():
            results.append(rt.next_step("s4"))

        with patch("scripts.base.advance_session", side_effect=slow_advance):
            t1 = threading.Thread(target=call_next)
            t2 = threading.Thread(target=call_next)
            t1.start()
            # Ensure t1 enters critical section before t2.
            time.sleep(0.03)
            t2.start()
            t1.join()
            t2.join()

        types = sorted([r["type"] for r in results])
        self.assertEqual(types, ["ConcurrencyWarning", "Step"])
        warning = next(r for r in results if r["type"] == "ConcurrencyWarning")
        self.assertEqual(warning["code"], "SESSION_PARALLEL_CALL_BLOCKED")

    def test_script_mode_accepts_non_python_instruction_lines(self) -> None:
        rt = self._runtime(
            "if is_raining:\n"
            "  输出 \"今日出行建议：带伞\"\n"
        )
        rt.init_session("s5")
        branch = rt.next_step("s5")
        self.assertEqual(branch["type"], "Branch")
        step = rt.next_step("s5", branch_value=True, branch_present=True, output="需要带伞")
        self.assertEqual(step["type"], "Step")
        self.assertIn("输出", step["instruction"])
        self.assertIn("带伞", step["instruction"])
        done = rt.next_step("s5", output="今日出行建议：带伞")
        self.assertEqual(done["type"], "Finished")

    def test_call_supports_relative_path_outside_entry_dir(self) -> None:
        sub_dir = self.root / "sub"
        sub_dir.mkdir(parents=True, exist_ok=True)
        shared_dir = self.root / "shared"
        shared_dir.mkdir(parents=True, exist_ok=True)

        entry = sub_dir / "entry.procedure"
        entry = sub_dir / "entry.rail"
        entry.write_text('include "../shared/fetch_sub.rail"\n', encoding="utf-8")
        (shared_dir / "fetch_sub.rail").write_text("输出 from shared\n", encoding="utf-8")

        rt = RailRunRuntime(entry, self.sessions)
        rt.init_session("s6")
        step = rt.next_step("s6")
        self.assertEqual(step, {"type": "Step", "instruction": "输出 from shared", "step_index": 0, "message": LANGUAGE_MESSAGE})
        done = rt.next_step("s6", output="from shared")
        self.assertEqual(done["type"], "Finished")

    def test_call_supports_absolute_path(self) -> None:
        shared = self.root / "abs_shared.rail"
        shared.write_text("输出 from abs\n", encoding="utf-8")
        entry = self.root / "flow_abs.rail"
        entry.write_text(f'include "{shared.resolve()}"\n', encoding="utf-8")

        rt = RailRunRuntime(entry, self.sessions)
        rt.init_session("s7")
        step = rt.next_step("s7")
        self.assertEqual(step, {"type": "Step", "instruction": "输出 from abs", "step_index": 0, "message": LANGUAGE_MESSAGE})
        done = rt.next_step("s7", output="from abs")
        self.assertEqual(done["type"], "Finished")

    def test_call_callee_uses_own_mode_script(self) -> None:
        callee = self.root / "callee_script.rail"
        callee.write_text(
            "if is_raining:\n"
            "  输出 from script callee\n",
            encoding="utf-8",
        )
        entry = self.root / "entry_seq_call_script.rail"
        entry.write_text(f'include "{callee.resolve()}"\n', encoding="utf-8")

        rt = RailRunRuntime(entry, self.sessions)
        rt.init_session("s8")
        branch = rt.next_step("s8")
        self.assertEqual(branch["type"], "Branch")
        self.assertIn("is_raining", branch["instruction"])
        self.assertEqual(branch["message"], LANGUAGE_MESSAGE)
        step = rt.next_step("s8", branch_value=True, branch_present=True, output="branch output")
        self.assertEqual(step, {"type": "Step", "instruction": "输出 from script callee", "step_index": 1, "message": LANGUAGE_MESSAGE})
        done = rt.next_step("s8", output="callee output")
        self.assertEqual(done["type"], "Finished")

    def test_include_callee_with_multiple_steps(self) -> None:
        callee = self.root / "callee_multi.rail"
        callee.write_text("输出 first\n输出 second\n", encoding="utf-8")
        entry = self.root / "entry_seq_include_multi.rail"
        entry.write_text(f'include "{callee.resolve()}"\n', encoding="utf-8")

        rt = RailRunRuntime(entry, self.sessions)
        rt.init_session("s9")
        step1 = rt.next_step("s9")
        self.assertEqual(step1, {"type": "Step", "instruction": "输出 first", "step_index": 0, "message": LANGUAGE_MESSAGE})
        step2 = rt.next_step("s9", output="first output")
        self.assertEqual(step2, {"type": "Step", "instruction": "输出 second", "step_index": 1})
        done = rt.next_step("s9", output="second output")
        self.assertEqual(done["type"], "Finished")

    def test_for_node_advances_without_branch_value(self) -> None:
        cfg = self.root / "for-cfg.json"
        cfg.write_text(
            '{"protocol":"next-step-cfg/v1","entry":"0","nodes":{"0":{"type":"For","items":["A"],"item_key":"city","index_key":"i","on_iterate":"1","on_done":"2"},"1":{"type":"Step","instruction":"{{city}}-{{i}}","next":"0"},"2":{"type":"Finished","instruction":"done"}}}',
            encoding="utf-8",
        )
        rt = RailRunRuntime(cfg, self.sessions)
        rt.init_session("s10")
        resp = rt.next_step("s10")
        self.assertEqual(resp["type"], "For")
        self.assertEqual(resp["step_index"], 0)
        self.assertEqual(resp["message"], LANGUAGE_MESSAGE)
        step = rt.next_step("s10")
        self.assertEqual(step, {"type": "Step", "instruction": "A-0", "step_index": 1, "message": LANGUAGE_MESSAGE})
        done_for = rt.next_step("s10", output="A output")
        self.assertEqual(done_for["type"], "For")
        self.assertNotIn("message", done_for)

    def test_for_items_expr_invalid_returns_validation_error(self) -> None:
        cfg = self.root / "for-invalid-cfg.json"
        cfg.write_text(
            '{"protocol":"next-step-cfg/v1","entry":"0","nodes":{"0":{"type":"For","items_expr":"cities","item_key":"city","index_key":"i","on_iterate":"1","on_done":"2"},"1":{"type":"Step","instruction":"x","next":"0"},"2":{"type":"Finished","instruction":"done"}}}',
            encoding="utf-8",
        )
        rt = RailRunRuntime(cfg, self.sessions)
        rt.init_session("s11")
        resp = rt.next_step("s11")
        self.assertEqual(resp["type"], "ValidationError")

    def test_for_items_expr_range_supported(self) -> None:
        cfg = self.root / "for-range-cfg.json"
        cfg.write_text(
            '{"protocol":"next-step-cfg/v1","entry":"0","nodes":{"0":{"type":"For","items_expr":"range(3)","item_key":"n","index_key":"i","on_iterate":"1","on_done":"2"},"1":{"type":"Step","instruction":"{{n}}-{{i}}","next":"0"},"2":{"type":"Finished","instruction":"done"}}}',
            encoding="utf-8",
        )
        rt = RailRunRuntime(cfg, self.sessions)
        rt.init_session("s12")
        outputs: list[str] = []
        previous_was_step = False
        while True:
            resp = rt.next_step("s12", output="loop step output" if previous_was_step else None)
            if resp["type"] == "Finished":
                break
            previous_was_step = resp["type"] == "Step"
            if resp["type"] == "Step":
                outputs.append(resp["instruction"])
        self.assertEqual(outputs, ["0-0", "1-1", "2-2"])

    def test_ask_requires_output_but_guidance_and_for_do_not(self) -> None:
        cfg = self.root / "mixed-cfg.json"
        cfg.write_text(
            '{"protocol":"next-step-cfg/v1","entry":"0","nodes":{'
            '"0":{"type":"Guidance","instruction":"guide","next":"1"},'
            '"1":{"type":"For","items":[],"item_key":"item","index_key":"i","on_iterate":"2","on_done":"2"},'
            '"2":{"type":"Ask","instruction":"question","next":"3"},'
            '"3":{"type":"Finished","instruction":"done"}}}',
            encoding="utf-8",
        )
        rt = RailRunRuntime(cfg, self.sessions)
        rt.init_session("mixed")
        guidance = rt.next_step("mixed")
        self.assertEqual(guidance["type"], "Guidance")
        self.assertIn(LANGUAGE_MESSAGE, guidance["message"])
        for_resp = rt.next_step("mixed")
        self.assertEqual(for_resp["type"], "For")
        self.assertEqual(for_resp["message"], LANGUAGE_MESSAGE)
        ask = rt.next_step("mixed")
        self.assertEqual(ask["type"], "Ask")
        self.assertIn(LANGUAGE_MESSAGE, ask["message"])
        missing = rt.next_step("mixed")
        self.assertEqual(missing["type"], "ValidationError")
        done = rt.next_step("mixed", output="用户问题与反馈")
        self.assertEqual(done["type"], "Finished")
        self.assertEqual(rt._load_session("mixed").history[-1]["output"], "用户问题与反馈")

    def test_for_items_expr_range_invalid_returns_validation_error(self) -> None:
        cfg = self.root / "for-range-invalid-cfg.json"
        cfg.write_text(
            '{"protocol":"next-step-cfg/v1","entry":"0","nodes":{"0":{"type":"For","items_expr":"range(\'3\')","item_key":"n","index_key":"i","on_iterate":"1","on_done":"2"},"1":{"type":"Step","instruction":"x","next":"0"},"2":{"type":"Finished","instruction":"done"}}}',
            encoding="utf-8",
        )
        rt = RailRunRuntime(cfg, self.sessions)
        rt.init_session("s13")
        resp = rt.next_step("s13")
        self.assertEqual(resp["type"], "ValidationError")


if __name__ == "__main__":
    unittest.main()
