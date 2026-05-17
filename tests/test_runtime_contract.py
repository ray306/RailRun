from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import session_runtime
from scripts.global_daemon import RailRunRuntime


class RuntimeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.sessions = self.root / "sessions"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _runtime(self, text: str, *, max_retries: int = 3) -> RailRunRuntime:
        procedure = self.root / "flow.arp"
        procedure.write_text(text, encoding="utf-8")
        return RailRunRuntime(procedure, self.sessions, max_retries=max_retries)

    def test_sequential_step_and_finished(self) -> None:
        rt = self._runtime("输出 A\n输出 B\n")
        init = rt.init_session("s1")
        self.assertEqual(init["type"], "init")

        s1 = rt.next_step("s1")
        self.assertEqual(s1, {"type": "Step", "instruction": "输出 A"})
        s2 = rt.next_step("s1")
        self.assertEqual(s2, {"type": "Step", "instruction": "输出 B"})
        done = rt.next_step("s1")
        self.assertEqual(done["type"], "Finished")

    def test_branch_requires_bool_and_path(self) -> None:
        rt = self._runtime(
            "if True:\n"
            "  输出 T\n"
            "else:\n"
            "  输出 F\n"
        )
        rt.init_session("s2")
        branch = rt.next_step("s2")
        self.assertEqual(branch["type"], "Branch")
        missing = rt.next_step("s2")
        self.assertEqual(missing["type"], "ValidationError")
        wrong_type = rt.next_step("s2", branch_value="true", branch_present=True)
        self.assertEqual(wrong_type["type"], "ValidationError")

        step = rt.next_step("s2", branch_value=True, branch_present=True)
        self.assertEqual(step, {"type": "Step", "instruction": "输出 T"})
        done = rt.next_step("s2")
        self.assertEqual(done["type"], "Finished")

    def test_retry_limit_to_human_interference(self) -> None:
        rt = self._runtime("if True:\n  输出 ok\n", max_retries=1)
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

        with patch("scripts.global_daemon.advance_session", side_effect=slow_advance):
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
            "if True:\n"
            "  输出 \"今日出行建议：带伞\"\n"
        )
        rt.init_session("s5")
        branch = rt.next_step("s5")
        self.assertEqual(branch["type"], "Branch")
        step = rt.next_step("s5", branch_value=True, branch_present=True)
        self.assertEqual(step["type"], "Step")
        self.assertIn("输出", step["instruction"])
        self.assertIn("带伞", step["instruction"])
        done = rt.next_step("s5")
        self.assertEqual(done["type"], "Finished")

    def test_call_supports_relative_path_outside_entry_dir(self) -> None:
        sub_dir = self.root / "sub"
        sub_dir.mkdir(parents=True, exist_ok=True)
        shared_dir = self.root / "shared"
        shared_dir.mkdir(parents=True, exist_ok=True)

        entry = sub_dir / "entry.procedure"
        entry = sub_dir / "entry.arp"
        entry.write_text('include "../shared/fetch_sub.arp"\n', encoding="utf-8")
        (shared_dir / "fetch_sub.arp").write_text("输出 from shared\n", encoding="utf-8")

        rt = RailRunRuntime(entry, self.sessions)
        rt.init_session("s6")
        step = rt.next_step("s6")
        self.assertEqual(step, {"type": "Step", "instruction": "输出 from shared"})
        done = rt.next_step("s6")
        self.assertEqual(done["type"], "Finished")

    def test_call_supports_absolute_path(self) -> None:
        shared = self.root / "abs_shared.arp"
        shared.write_text("输出 from abs\n", encoding="utf-8")
        entry = self.root / "flow_abs.arp"
        entry.write_text(f'include "{shared.resolve()}"\n', encoding="utf-8")

        rt = RailRunRuntime(entry, self.sessions)
        rt.init_session("s7")
        step = rt.next_step("s7")
        self.assertEqual(step, {"type": "Step", "instruction": "输出 from abs"})
        done = rt.next_step("s7")
        self.assertEqual(done["type"], "Finished")

    def test_call_callee_uses_own_mode_script(self) -> None:
        callee = self.root / "callee_script.arp"
        callee.write_text(
            "if True:\n"
            "  输出 from script callee\n",
            encoding="utf-8",
        )
        entry = self.root / "entry_seq_call_script.arp"
        entry.write_text(f'include "{callee.resolve()}"\n', encoding="utf-8")

        rt = RailRunRuntime(entry, self.sessions)
        rt.init_session("s8")
        branch = rt.next_step("s8")
        self.assertEqual(branch["type"], "Branch")
        self.assertIn("True", branch["instruction"])
        step = rt.next_step("s8", branch_value=True, branch_present=True)
        self.assertEqual(step, {"type": "Step", "instruction": "输出 from script callee"})
        done = rt.next_step("s8")
        self.assertEqual(done["type"], "Finished")

    def test_include_callee_with_multiple_steps(self) -> None:
        callee = self.root / "callee_multi.arp"
        callee.write_text("输出 first\n输出 second\n", encoding="utf-8")
        entry = self.root / "entry_seq_include_multi.arp"
        entry.write_text(f'include "{callee.resolve()}"\n', encoding="utf-8")

        rt = RailRunRuntime(entry, self.sessions)
        rt.init_session("s9")
        step1 = rt.next_step("s9")
        self.assertEqual(step1, {"type": "Step", "instruction": "输出 first"})
        step2 = rt.next_step("s9")
        self.assertEqual(step2, {"type": "Step", "instruction": "输出 second"})
        done = rt.next_step("s9")
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
        step = rt.next_step("s10")
        self.assertEqual(step, {"type": "Step", "instruction": "A-0"})

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
        while True:
            resp = rt.next_step("s12")
            if resp["type"] == "Finished":
                break
            if resp["type"] == "Step":
                outputs.append(resp["instruction"])
        self.assertEqual(outputs, ["0-0", "1-1", "2-2"])

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
