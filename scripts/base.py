from __future__ import annotations

import json
import os
import time
import contextlib
import sys
from pathlib import Path
from typing import Any

from .rail_compiler import RailCompiler
from .session_state import SessionState, now_str
from .session_runtime import StepInput, advance_session


@contextlib.contextmanager
def file_lock(file_path: str):
    """
    Acquires a non-blocking lock on a lock file associated with the session.
    If the lock is already held by another process, raises RuntimeError.
    """
    lock_path = file_path + ".lock"
    # Open/create the lock file in read/write mode
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        if os.name == "nt":
            import msvcrt
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("SESSION_PARALLEL_CALL_BLOCKED")
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("SESSION_PARALLEL_CALL_BLOCKED")
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)
        try:
            os.remove(lock_path)
        except OSError:
            pass


class RailRunRuntime:
    def __init__(self, procedure_path: Path, sessions_dir: Path, max_retries: int = 3, consts: dict[str, Any] = None):
        self.procedure_path = procedure_path.resolve()
        self.sessions_dir = sessions_dir.resolve()
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.consts = consts or {}
        self._is_cfg_input = self.procedure_path.suffix.lower() == ".json"
        if self._is_cfg_input:
            self.dag_path = self.procedure_path
        else:
            if self.procedure_path.suffix.lower() != ".rail":
                raise ValueError("只支持 .rail 或 -cfg.json 作为 procedure 输入。")
            if self.consts:
                import hashlib
                canonical = ",".join(f"{k}={v}" for k, v in sorted(self.consts.items()))
                h = hashlib.md5(canonical.encode('utf-8')).hexdigest()[:8]
                self.dag_path = self.procedure_path.with_name(f"{self.procedure_path.stem}-cfg-{h}.json")
            else:
                self.dag_path = self.procedure_path.with_name(f"{self.procedure_path.stem}-cfg.json")
        self.max_retries = max_retries
        self._ensure_cfg_ready()

    def _ensure_cfg_ready(self) -> None:
        if self._is_cfg_input:
            if not self.dag_path.exists():
                raise FileNotFoundError(f"CFG 文件不存在: {self.dag_path}")
            return
        if not self.dag_path.exists() or self.procedure_path.stat().st_mtime > self.dag_path.stat().st_mtime:
            compiler = RailCompiler(consts=self.consts)
            cfg = compiler.compile(self.procedure_path)
            self.dag_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _is_terminal_status(status: str) -> bool:
        return status in {"done", "interference", "terminated"}

    def _session_file(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.json"

    def _load_dag(self) -> dict[str, Any]:
        self._ensure_cfg_ready()
        dag = json.loads(self.dag_path.read_text(encoding="utf-8"))
        protocol = str(dag.get("protocol", ""))
        if protocol != "next-step-cfg/v1":
            raise ValueError(f"不支持的协议版本: {protocol}")
        if not isinstance(dag.get("entry"), (str, int)) or not isinstance(dag.get("nodes"), dict):
            raise ValueError("流程文件结构不合法，缺少 entry 或 nodes。")
        return dag

    def _load_session(self, session_id: str) -> SessionState | None:
        path = self._session_file(session_id)
        if not path.exists():
            return None
        try:
            return SessionState.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None

    def _save_session(self, session: SessionState) -> None:
        self._session_file(session.session).write_text(
            json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _validation_error(self, session: SessionState, message: str) -> dict[str, Any]:
        session.retry_count += 1
        if session.retry_count > session.max_retries:
            session.status = "interference"
            self._save_session(session)
            return {
                "type": "HumanInterferenceRequest",
                "message": "错误次数超过限制，请终止当前会话，请求人工检查当前 session 状态。",
            }
        self._save_session(session)
        return {"type": "ValidationError", "message": message}

    def _history_branch_decisions(self, history: list[dict[str, Any]]) -> dict[tuple[int, str], list[bool]]:
        decisions: dict[tuple[int, str], list[bool]] = {}
        for item in history:
            if item.get("type") != "BranchDecision":
                continue
            step_index = item.get("step_index")
            node_id = item.get("node_id")
            branch_value = item.get("branch_value")
            if not isinstance(step_index, int) or node_id is None or not isinstance(branch_value, bool):
                continue
            key = (step_index, str(node_id))
            decisions.setdefault(key, []).append(branch_value)
        return decisions

    def _build_rewound_state(self, session: SessionState, dag: dict[str, Any], target_step_index: int) -> SessionState:
        entry = dag["entry"]
        if not isinstance(entry, (str, int)):
            raise ValueError("流程文件结构不合法，entry 非法。")

        replay = SessionState(
            session=session.session,
            procedure_path=session.procedure_path,
            dag_path=session.dag_path,
            status="running",
            cursor={"step_index": 0, "node_id": str(entry)},
            max_retries=session.max_retries,
        )
        decisions = self._history_branch_decisions(session.history)
        decision_offsets: dict[tuple[int, str], int] = {}
        while int(replay.cursor.get("step_index", 0)) < target_step_index:
            if replay.status in {"done", "interference", "terminated"}:
                raise ValueError("目标 step_index 超出当前会话可回放范围。")

            if replay.waiting_for_branch:
                waiting_node = replay.waiting_branch_node
                if waiting_node is None:
                    raise ValueError("session 分支状态异常，无法回溯。")
                key = (int(replay.cursor["step_index"]), str(waiting_node))
                branch_values = decisions.get(key, [])
                offset = decision_offsets.get(key, 0)
                if offset >= len(branch_values):
                    raise ValueError("回溯失败：缺少分支决策历史。")
                branch_value = branch_values[offset]
                decision_offsets[key] = offset + 1
                _, replay = advance_session(
                    replay,
                    dag,
                    StepInput(branch_present=True, branch_value=branch_value),
                    now_fn=now_str,
                )
                continue

            _, replay = advance_session(
                replay,
                dag,
                StepInput(branch_present=False, branch_value=None),
                now_fn=now_str,
            )

        replay.status = "running"
        replay.retry_count = 0
        replay.history = [
            item
            for item in session.history
            if isinstance(item.get("step_index"), int) and int(item["step_index"]) < target_step_index
        ]
        return replay

    def init_session(self, session_id: str) -> dict[str, Any]:
        session_file = self._session_file(session_id)
        try:
            with file_lock(str(session_file)):
                dag = self._load_dag()
                session = self._load_session(session_id)
                if session is not None:
                    return {"type": "ValidationError", "message": "session 已存在，请直接使用 --session 读取下一步。"}
                session = SessionState(
                    session=session_id,
                    procedure_path=str(self.procedure_path),
                    dag_path=str(self.dag_path),
                    cursor={"step_index": 0, "node_id": dag["entry"]},
                    max_retries=self.max_retries,
                )
                self._save_session(session)
                return {
                    "type": "init",
                    "session": session_id,
                    "instruction": "已分配session_id，请再次调用 next_step --session <id> 读取下一步。请提示用户：如果需要查看可视化流程控制台，可在终端运行 `python next_step.py --ui` 并在浏览器中访问 http://127.0.0.1:8799/。",
                }
        except RuntimeError as exc:
            if str(exc) == "SESSION_PARALLEL_CALL_BLOCKED":
                return {
                    "type": "ConcurrencyWarning",
                    "code": "SESSION_PARALLEL_CALL_BLOCKED",
                    "message": "检测到同一 session 的并行 init_session 调用，已被锁定。",
                }
            raise

    def next_step(self, session_id: str, branch_value: Any = None, branch_present: bool = False, variables: dict[str, Any] = None) -> dict[str, Any]:
        session_file = self._session_file(session_id)
        try:
            with file_lock(str(session_file)):
                session = self._load_session(session_id)
                if session is None:
                    return {"type": "ValidationError", "message": "session 不存在，请先初始化。"}
                if session.status == "done":
                    return {
                        "type": "Finished",
                        "message": "所有指令已执行完毕。结束输出。",
                        "step_index": int(session.cursor.get("step_index", 0)),
                    }
                if session.status == "terminated":
                    return {
                        "type": "Finished",
                        "message": "当前 session 已停止。",
                        "step_index": int(session.cursor.get("step_index", 0)),
                    }
                if session.status == "interference":
                    return {
                        "type": "HumanInterferenceRequest",
                        "message": "请人工介入",
                        "step_index": int(session.cursor.get("step_index", 0)),
                    }

                dag = self._load_dag()
                try:
                    resp, updated = advance_session(
                        session,
                        dag,
                        StepInput(branch_present=branch_present, branch_value=branch_value, variables=variables),
                        now_fn=now_str,
                    )
                    updated.retry_count = 0
                    self._save_session(updated)
                    return resp
                except (ValueError, TypeError) as exc:
                    return self._validation_error(session, str(exc))
        except RuntimeError as exc:
            if str(exc) == "SESSION_PARALLEL_CALL_BLOCKED":
                return {
                    "type": "ConcurrencyWarning",
                    "code": "SESSION_PARALLEL_CALL_BLOCKED",
                    "message": (
                        "检测到同一 session 的并行 next_step 调用。"
                        "railrun 要求同一 session 串行调用：请等待上一条 next_step 完成后再重试。"
                    ),
                }
            raise

    def stop_session(self, session_id: str) -> dict[str, Any]:
        session_file = self._session_file(session_id)
        try:
            with file_lock(str(session_file)):
                session = self._load_session(session_id)
                if session is None:
                    return {"type": "ValidationError", "message": "session 不存在。"}
                if not self._is_terminal_status(session.status):
                    session.status = "terminated"
                    self._save_session(session)
                return {"type": "ok", "message": "session stopped", "session": session_id}
        except RuntimeError as exc:
            if str(exc) == "SESSION_PARALLEL_CALL_BLOCKED":
                return {
                    "type": "ConcurrencyWarning",
                    "code": "SESSION_PARALLEL_CALL_BLOCKED",
                    "message": "检测到同一 session 的并行 stop_session 调用，已被锁定。",
                }
            raise

    def rewind_session(self, session_id: str, step_index: int) -> dict[str, Any]:
        session_file = self._session_file(session_id)
        try:
            with file_lock(str(session_file)):
                session = self._load_session(session_id)
                if session is None:
                    return {"type": "ValidationError", "message": "session 不存在。"}
                if step_index < 0:
                    return {"type": "ValidationError", "message": "step_index 不能小于 0。"}

                current_step = int(session.cursor.get("step_index", 0))
                if step_index > current_step:
                    return {
                        "type": "ValidationError",
                        "message": f"step_index 超出范围，当前最大可用值为 {current_step}。",
                    }

                dag = self._load_dag()
                try:
                    rewound = self._build_rewound_state(session, dag, step_index)
                except (ValueError, TypeError) as exc:
                    return {"type": "ValidationError", "message": str(exc)}

                self._save_session(rewound)
                return {
                    "type": "ok",
                    "message": "session rewound",
                    "session": session_id,
                    "step_index": step_index,
                }
        except RuntimeError as exc:
            if str(exc) == "SESSION_PARALLEL_CALL_BLOCKED":
                return {
                    "type": "ConcurrencyWarning",
                    "code": "SESSION_PARALLEL_CALL_BLOCKED",
                    "message": "检测到同一 session 的并行 rewind_session 调用，已被锁定。",
                }
            raise
