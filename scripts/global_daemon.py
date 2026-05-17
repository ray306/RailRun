from __future__ import annotations

import json
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any

from .arp_compiler import ArpCompiler
from .session_runtime import StepInput, advance_session
from .session_state import SessionState, now_str


class RailRunRuntime:
    def __init__(self, procedure_path: Path, sessions_dir: Path, max_retries: int = 3):
        self.procedure_path = procedure_path.resolve()
        self.sessions_dir = sessions_dir.resolve()
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._is_cfg_input = self.procedure_path.suffix.lower() == ".json"
        if self._is_cfg_input:
            self.dag_path = self.procedure_path
        else:
            if self.procedure_path.suffix.lower() != ".arp":
                raise ValueError("只支持 .arp 或 -cfg.json 作为 procedure 输入。")
            self.dag_path = self.procedure_path.with_name(f"{self.procedure_path.stem}-cfg.json")
        self.max_retries = max_retries
        # Strict per-session serialization: block parallel next_step calls for same session.
        self._session_locks: dict[str, threading.Lock] = {}
        self._session_locks_guard = threading.Lock()
        self._ref_guard = threading.Lock()
        self._active_refs = 0
        self._ensure_cfg_ready()
        self._active_refs = self._recount_active_refs()

    def _ensure_cfg_ready(self) -> None:
        if self._is_cfg_input:
            if not self.dag_path.exists():
                raise FileNotFoundError(f"CFG 文件不存在: {self.dag_path}")
            return
        if not self.dag_path.exists() or self.procedure_path.stat().st_mtime > self.dag_path.stat().st_mtime:
            compiler = ArpCompiler()
            cfg = compiler.compile(self.procedure_path)
            self.dag_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _is_terminal_status(status: str) -> bool:
        return status in {"done", "interference", "terminated"}

    def _recount_active_refs(self) -> int:
        active = 0
        for path in self.sessions_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not self._is_terminal_status(str(data.get("status", "running"))):
                    active += 1
            except Exception:
                # Ignore malformed files; runtime validations handle per-session errors.
                continue
        return active

    def _inc_ref(self) -> None:
        with self._ref_guard:
            self._active_refs += 1

    def _dec_ref(self) -> None:
        with self._ref_guard:
            if self._active_refs > 0:
                self._active_refs -= 1

    def active_refs(self) -> int:
        with self._ref_guard:
            return self._active_refs

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

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
        return SessionState.from_dict(json.loads(path.read_text(encoding="utf-8")))

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

    def init_session(self, session_id: str) -> dict[str, Any]:
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
        self._inc_ref()
        return {
            "type": "init",
            "session": session_id,
            "instruction": "已分配session_id，请再次调用 next_step --session <id> 读取下一步。",
        }

    def next_step(self, session_id: str, branch_value: Any = None, branch_present: bool = False) -> dict[str, Any]:
        lock = self._get_session_lock(session_id)
        if not lock.acquire(blocking=False):
            return {
                "type": "ConcurrencyWarning",
                "code": "SESSION_PARALLEL_CALL_BLOCKED",
                "message": (
                    "检测到同一 session 的并行 next_step 调用。"
                    "railrun 要求同一 session 串行调用：请等待上一条 next_step 完成后再重试。"
                ),
            }

        session = self._load_session(session_id)
        try:
            if session is None:
                return {"type": "ValidationError", "message": "session 不存在，请先初始化。"}
            if session.status == "done":
                return {
                    "type": "Finished",
                    "message": "所有指令已执行完毕。结束输出。",
                }
            if session.status == "terminated":
                return {
                    "type": "Finished",
                    "message": "当前 session 已停止。",
                }
            if session.status == "interference":
                return {
                    "type": "HumanInterferenceRequest",
                    "message": "请人工介入",
                }

            dag = self._load_dag()
            try:
                resp, updated = advance_session(
                    session,
                    dag,
                    StepInput(branch_present=branch_present, branch_value=branch_value),
                    now_fn=now_str,
                )
                updated.retry_count = 0
                self._save_session(updated)
                if not self._is_terminal_status(session.status) and self._is_terminal_status(updated.status):
                    self._dec_ref()
                return resp
            except (ValueError, TypeError) as exc:
                return self._validation_error(session, str(exc))
        finally:
            lock.release()

    def stop_session(self, session_id: str) -> dict[str, Any]:
        lock = self._get_session_lock(session_id)
        with lock:
            session = self._load_session(session_id)
            if session is None:
                return {"type": "ValidationError", "message": "session 不存在。"}
            if not self._is_terminal_status(session.status):
                session.status = "terminated"
                self._save_session(session)
                self._dec_ref()
            return {"type": "ok", "message": "session stopped", "session": session_id}

    def request_shutdown(self, force: bool = False) -> dict[str, Any]:
        refs = self.active_refs()
        if refs > 0 and not force:
            return {
                "type": "ValidationError",
                "code": "ACTIVE_REFS_BLOCK_SHUTDOWN",
                "message": f"当前仍有 {refs} 个活跃 session，拒绝关闭 daemon。可在无活跃 session 后重试，或使用 force 关闭。",
            }
        return {"type": "ok", "message": "daemon shutting down"}


class DaemonHandler(socketserver.StreamRequestHandler):
    runtimes: dict[tuple[str, str, int], RailRunRuntime]
    session_to_runtime: dict[str, tuple[str, str, int]]
    manager_lock: threading.Lock
    default_procedure_path: Path
    default_sessions_dir: Path
    default_max_retries: int
    stop_event: threading.Event

    def _runtime_key(self, procedure_path: Path, sessions_dir: Path, max_retries: int) -> tuple[str, str, int]:
        return (str(procedure_path.resolve()), str(sessions_dir.resolve()), int(max_retries))

    def _get_or_create_runtime(self, procedure_path: Path, sessions_dir: Path, max_retries: int) -> RailRunRuntime:
        key = self._runtime_key(procedure_path, sessions_dir, max_retries)
        with self.manager_lock:
            runtime = self.runtimes.get(key)
            if runtime is None:
                runtime = RailRunRuntime(procedure_path, sessions_dir, max_retries=max_retries)
                self.runtimes[key] = runtime
            return runtime

    def _find_runtime_for_session(self, session_id: str) -> tuple[tuple[str, str, int] | None, RailRunRuntime | None]:
        with self.manager_lock:
            key = self.session_to_runtime.get(session_id)
            if key is not None:
                runtime = self.runtimes.get(key)
                if runtime is not None:
                    return key, runtime
            for candidate_key, runtime in self.runtimes.items():
                if runtime._load_session(session_id) is not None:
                    self.session_to_runtime[session_id] = candidate_key
                    return candidate_key, runtime
        return None, None

    def _total_active_refs(self) -> int:
        with self.manager_lock:
            runtimes = list(self.runtimes.values())
        return sum(rt.active_refs() for rt in runtimes)

    def handle(self) -> None:
        raw = self.rfile.readline()
        if not raw:
            return
        try:
            req = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._write({"type": "ValidationError", "message": "请求不是合法 JSON。"})
            return

        action = req.get("action")
        if action == "ping":
            self._write({"type": "ok", "message": "pong"})
            return
        if action == "shutdown":
            force = bool(req.get("force", False))
            refs = self._total_active_refs()
            if refs > 0 and not force:
                resp = {
                    "type": "ValidationError",
                    "code": "ACTIVE_REFS_BLOCK_SHUTDOWN",
                    "message": f"当前仍有 {refs} 个活跃 session，拒绝关闭 daemon。可在无活跃 session 后重试，或使用 force 关闭。",
                }
            else:
                resp = {"type": "ok", "message": "daemon shutting down"}
            self._write(resp)
            if resp.get("type") == "ok":
                self.stop_event.set()
            return

        if action == "init":
            session = req.get("session")
            if not session:
                self._write({"type": "ValidationError", "message": "缺少 session 参数。"})
                return
            procedure = req.get("procedure")
            sessions_dir = req.get("sessions_dir")
            max_retries = req.get("max_retries")
            procedure_path = Path(str(procedure)) if procedure else self.default_procedure_path
            target_sessions_dir = Path(str(sessions_dir)) if sessions_dir else self.default_sessions_dir
            target_max_retries = int(max_retries) if max_retries is not None else int(self.default_max_retries)
            runtime = self._get_or_create_runtime(procedure_path, target_sessions_dir, target_max_retries)
            resp = runtime.init_session(str(session))
            if resp.get("type") == "init":
                key = self._runtime_key(procedure_path, target_sessions_dir, target_max_retries)
                with self.manager_lock:
                    self.session_to_runtime[str(session)] = key
                resp["procedure"] = str(procedure_path.resolve())
            self._write(resp)
            return

        if action == "next":
            session = req.get("session")
            if not session:
                self._write({"type": "ValidationError", "message": "缺少 session 参数。"})
                return
            _, runtime = self._find_runtime_for_session(str(session))
            if runtime is None:
                self._write({"type": "ValidationError", "message": "session 不存在，请先初始化。"})
                return
            branch_present = "branch_value" in req
            branch_value = req.get("branch_value")
            self._write(runtime.next_step(str(session), branch_value, branch_present))
            return

        if action == "shutdown_session":
            session = req.get("session")
            if not session:
                self._write({"type": "ValidationError", "message": "缺少 session 参数。"})
                return
            key, runtime = self._find_runtime_for_session(str(session))
            if runtime is None:
                refs = self._total_active_refs()
                resp = {
                    "type": "ValidationError",
                    "message": "session 不存在。",
                    "daemon_shutdown": refs == 0,
                }
                self._write(resp)
                if refs == 0:
                    self.stop_event.set()
                return
            resp = runtime.stop_session(str(session))
            refs = self._total_active_refs()
            resp["daemon_shutdown"] = refs == 0
            self._write(resp)
            if refs == 0:
                self.stop_event.set()
            if key is not None:
                with self.manager_lock:
                    self.session_to_runtime.pop(str(session), None)
            return

        self._write({"type": "ValidationError", "message": "未知 action。"})

    def _write(self, data: dict[str, Any]) -> None:
        self.wfile.write((json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8"))


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True


def run_daemon(host: str, port: int, procedure_path: Path, sessions_dir: Path, max_retries: int = 3) -> None:
    stop_event = threading.Event()
    DaemonHandler.runtimes = {}
    DaemonHandler.session_to_runtime = {}
    DaemonHandler.manager_lock = threading.Lock()
    DaemonHandler.default_procedure_path = procedure_path.resolve()
    DaemonHandler.default_sessions_dir = sessions_dir.resolve()
    DaemonHandler.default_max_retries = int(max_retries)
    DaemonHandler.runtimes[
        (
            str(DaemonHandler.default_procedure_path),
            str(DaemonHandler.default_sessions_dir),
            DaemonHandler.default_max_retries,
        )
    ] = RailRunRuntime(
        DaemonHandler.default_procedure_path,
        DaemonHandler.default_sessions_dir,
        max_retries=DaemonHandler.default_max_retries,
    )
    DaemonHandler.stop_event = stop_event
    server = ThreadedTCPServer((host, port), DaemonHandler)
    server.timeout = 0.5
    try:
        while not stop_event.is_set():
            server.handle_request()
    finally:
        server.server_close()


def send_request(host: str, port: int, payload: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        f = sock.makefile("rb")
        line = f.readline()
        if not line:
            return {"type": "ValidationError", "message": "daemon 无响应。"}
        return json.loads(line.decode("utf-8"))
