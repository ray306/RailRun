from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from scripts.global_daemon import RailRunRuntime

RAIN_LIKE_CODES = {51, 53, 55, 61, 63, 65, 71, 73, 75, 80, 81, 82, 95, 96, 99}


def _copy_example(src_name: str, dst_dir: Path) -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    src = repo_root / "examples" / src_name
    dst = dst_dir / src_name
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst


class AgentHarness:
    """
    Minimal deterministic "agent" for runtime contract tests:
    - Consumes Step/Branch from next_step
    - Executes small semantic actions from Step instructions
    - Computes branch decisions from maintained context
    """

    def __init__(self, runtime: RailRunRuntime, session_id: str, weather_by_city: dict[str, dict[str, Any]]) -> None:
        self.runtime = runtime
        self.session_id = session_id
        self.weather_by_city = weather_by_city
        self.ctx: dict[str, Any] = {
            "cities": ["Berlin", "London", "Oslo", "Singapore"],
            "city_idx": 0,
            "city": "Berlin",
            "precipitation": 0.0,
            "weather_code": 0,
            "umbrella_count": 0,
        }
        berlin = self.weather_by_city.get("Berlin")
        if berlin is not None:
            self.ctx["precipitation"] = float(berlin.get("precipitation", 0.0))
            self.ctx["weather_code"] = int(berlin.get("weather_code", 0))
            self.ctx["wind_speed_10m"] = float(berlin.get("wind_speed_10m", 0.0))
        self.log: list[str] = []

    def _execute_step(self, instruction: str) -> None:
        if ("请查询城市天气并提取 precipitation/weather_code" in instruction) or (
            "检索天气" in instruction and "precipitation/weather_code" in instruction
        ):
            city_match = re.search(r"(Berlin|London|Oslo|Singapore)", instruction)
            if city_match:
                self.ctx["city"] = city_match.group(1)
            city = self.ctx.get("city", "Berlin")
            weather = self.weather_by_city.get(city, {"precipitation": 0.0, "weather_code": 0})
            self.ctx["precipitation"] = float(weather["precipitation"])
            self.ctx["weather_code"] = int(weather["weather_code"])
            self.log.append(f"weather:{city}:{self.ctx['precipitation']}:{self.ctx['weather_code']}")

        if "temperature_2m" in instruction and "weather_code" in instruction and "提取并记录四个字段" in instruction:
            # Sequential 子流程步骤：模拟提取完成
            berlin = self.weather_by_city.get("Berlin", {})
            self.ctx["precipitation"] = float(berlin.get("precipitation", 0.0))
            self.ctx["weather_code"] = int(berlin.get("weather_code", 0))
            self.ctx["wind_speed_10m"] = float(berlin.get("wind_speed_10m", 0.0))

        if "umbrella_count = umbrella_count + 1" in instruction:
            self.ctx["umbrella_count"] = int(self.ctx.get("umbrella_count", 0)) + 1

        if "umbrella_score" in instruction:
            score = 0
            precipitation = float(self.ctx.get("precipitation", 0.0))
            weather_code = int(self.ctx.get("weather_code", 0))
            if precipitation > 0:
                score += 2
            if weather_code in RAIN_LIKE_CODES:
                score += 2
            self.ctx["umbrella_score"] = score
            self.log.append(f"score:{score}")

        if ("今日出行建议" in instruction) or ("今日建议" in instruction):
            self.log.append(instruction)

        if ("汇总结论" in instruction) or ("流程结束" in instruction):
            self.log.append(instruction)

    def _decide_branch(self, instruction: str) -> bool:
        if "precipitation > 0" in instruction:
            return float(self.ctx.get("precipitation", 0.0)) > 0

        if "weather_code in [" in instruction:
            return int(self.ctx.get("weather_code", 0)) in RAIN_LIKE_CODES

        if "umbrella_count >= 2" in instruction:
            return int(self.ctx.get("umbrella_count", 0)) >= 2

        if "wind_speed_10m > 15" in instruction:
            return float(self.ctx.get("wind_speed_10m", 0.0)) > 15

        # Fallback for unknown condition patterns in tests.
        return False

    def run(self, max_steps: int = 200) -> dict[str, Any]:
        resp = self.runtime.next_step(self.session_id)
        for _ in range(max_steps):
            rtype = resp["type"]
            if rtype == "Finished":
                return resp
            if rtype == "ValidationError":
                raise AssertionError(f"unexpected validation error: {resp}")
            if rtype == "HumanInterferenceRequest":
                raise AssertionError(f"unexpected interference: {resp}")
            if rtype == "Step":
                self._execute_step(resp["instruction"])
                resp = self.runtime.next_step(self.session_id)
                continue
            if rtype == "Guidance":
                resp = self.runtime.next_step(self.session_id)
                continue
            if rtype == "Ask":
                # Contract tests use deterministic defaults; simulate "continue without user change".
                resp = self.runtime.next_step(self.session_id)
                continue
            if rtype == "For":
                resp = self.runtime.next_step(self.session_id)
                continue
            if rtype == "Branch":
                decision = self._decide_branch(resp["instruction"])
                resp = self.runtime.next_step(
                    self.session_id,
                    branch_value=decision,
                    branch_present=True,
                )
                continue
        raise AssertionError("agent harness did not reach Finished within max_steps")


class AgentWeatherFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.sessions = self.root / "sessions"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _runtime_from_example(self, name: str, extra_files: list[str] | None = None) -> RailRunRuntime:
        _copy_example(name, self.root)
        for item in extra_files or []:
            _copy_example(item, self.root)
        return RailRunRuntime(self.root / name, self.sessions)

    def test_sequential_weather_with_call_executes_and_finishes(self) -> None:
        rt = self._runtime_from_example(
            "weather_deicision_reuse.rail",
            extra_files=["weather_fetch_sub.rail"],
        )
        rt.init_session("w1")
        harness = AgentHarness(
            rt,
            "w1",
            weather_by_city={"Berlin": {"precipitation": 0.2, "weather_code": 63}},
        )
        done = harness.run()

        self.assertEqual(done["type"], "Finished")
        self.assertIn("score:4", harness.log)
        joined = "\n".join(harness.log)
        self.assertIn("今日出行建议", joined)

    def test_script_default_sep_executes_branches_and_finishes(self) -> None:
        rt = self._runtime_from_example("weather_deicision_branch.rail")
        rt.init_session("w2")
        harness = AgentHarness(
            rt,
            "w2",
            weather_by_city={"Berlin": {"precipitation": 0.0, "weather_code": 53}},
        )
        done = harness.run()

        self.assertEqual(done["type"], "Finished")
        joined = "\n".join(harness.log)
        self.assertIn("今日出行建议", joined)

    def test_multi_weather_loop_script_executes_loop_and_summary(self) -> None:
        rt = self._runtime_from_example("weather_deicision_loop.rail")
        rt.init_session("w3")
        harness = AgentHarness(
            rt,
            "w3",
            weather_by_city={
                "Berlin": {"precipitation": 0.4, "weather_code": 63},
                "London": {"precipitation": 0.0, "weather_code": 82},
                "Oslo": {"precipitation": 0.0, "weather_code": 0},
                "Singapore": {"precipitation": 0.0, "weather_code": 0},
            },
        )
        done = harness.run(max_steps=400)

        self.assertEqual(done["type"], "Finished")
        self.assertGreaterEqual(int(harness.ctx["umbrella_count"]), 2)
        joined = "\n".join(harness.log)
        self.assertIn("汇总结论", joined)
        # Ensure at least 4 city fetch actions occurred (loop truly iterated).
        fetch_lines = [line for line in harness.log if line.startswith("weather:")]
        self.assertEqual(len(fetch_lines), 4)

    def test_parameterized_weather_branch_check_wind(self) -> None:
        _copy_example("weather_deicision_branch.rail", self.root)
        rt = RailRunRuntime(
            self.root / "weather_deicision_branch.rail",
            self.sessions,
            consts={"check_wind": True},
        )
        rt.init_session("w_param")
        harness = AgentHarness(
            rt,
            "w_param",
            weather_by_city={"Berlin": {"precipitation": 0.0, "weather_code": 0, "wind_speed_10m": 18.0}},
        )
        done = harness.run()
        self.assertEqual(done["type"], "Finished")
        joined = "\n".join(harness.log)
        self.assertIn("建议带伞", joined)
        self.assertIn("风速较大", joined)


if __name__ == "__main__":
    unittest.main()

