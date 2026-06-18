from __future__ import annotations

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from typing import Any

from .base import RailRunRuntime
from .session_state import now_str

# A beautiful single-file glassmorphic Web UI.
# Load HTML page dynamically from the same folder
RUNNING_SESSION_TIMEOUT_SECONDS = 10 * 60

_index_html_path = Path(__file__).parent / "index.html"
if _index_html_path.exists():
    HTML_PAGE = _index_html_path.read_text(encoding="utf-8")
else:
    HTML_PAGE = "<h1>Error: index.html not found</h1>"


class DashboardHTTPHandler(BaseHTTPRequestHandler):
    runtimes: dict[tuple[str, str, int, tuple[tuple[str, Any], ...]], RailRunRuntime] = {}
    session_to_runtime: dict[str, tuple[str, str, int, tuple[tuple[str, Any], ...]]] = {}
    manager_lock: threading.Lock = threading.Lock()
    session_locks: dict[str, threading.Lock] = {}
    session_locks_guard: threading.Lock = threading.Lock()
    
    default_procedure_path: Path | None
    default_sessions_dir: Path
    default_max_retries: int
    stop_event: threading.Event

    def _request_server_shutdown(self) -> None:
        self.stop_event.set()
        threading.Thread(target=self.server.shutdown).start()

    def _terminate_stale_running_session(self, path: Path, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
        try:
            session_data = data if data is not None else json.loads(path.read_text(encoding="utf-8"))
            if session_data.get("status", "running") != "running":
                return session_data
            if time.time() - path.stat().st_mtime < RUNNING_SESSION_TIMEOUT_SECONDS:
                return session_data
            session_data["status"] = "terminated"
            path.write_text(json.dumps(session_data, ensure_ascii=False, indent=2), encoding="utf-8")
            return session_data
        except Exception:
            return data

    def _runtime_key(self, procedure_path: Path, sessions_dir: Path, max_retries: int, consts: dict[str, Any] = None) -> tuple[str, str, int, tuple[tuple[str, Any], ...]]:
        consts_tuple = tuple(sorted((consts or {}).items()))
        return (str(procedure_path.resolve()), str(sessions_dir.resolve()), int(max_retries), consts_tuple)

    def _get_or_create_runtime(self, procedure_path: Path, sessions_dir: Path, max_retries: int, consts: dict[str, Any] = None) -> RailRunRuntime:
        key = self._runtime_key(procedure_path, sessions_dir, max_retries, consts)
        with self.manager_lock:
            runtime = self.runtimes.get(key)
            if runtime is None:
                runtime = RailRunRuntime(procedure_path, sessions_dir, max_retries=max_retries, consts=consts)
                self.runtimes[key] = runtime
            return runtime

    def _find_runtime_for_session(self, session_id: str) -> tuple[tuple[str, str, int, tuple[tuple[str, Any], ...]] | None, RailRunRuntime | None]:
        with self.manager_lock:
            key = self.session_to_runtime.get(session_id)
            if key is not None:
                runtime = self.runtimes.get(key)
                if runtime is not None:
                    return key, runtime
            # Scan sessions_dir in all runtimes
            for candidate_key, runtime in self.runtimes.items():
                if runtime._load_session(session_id) is not None:
                    self.session_to_runtime[session_id] = candidate_key
                    return candidate_key, runtime
        return None, None

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        with self.session_locks_guard:
            lock = self.session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self.session_locks[session_id] = lock
            return lock

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path

            if path in {"", "/", "/index.html"}:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                index_html_path = Path(__file__).parent / "index.html"
                if index_html_path.exists():
                    html_content = index_html_path.read_text(encoding="utf-8")
                else:
                    html_content = HTML_PAGE
                self.wfile.write(html_content.encode("utf-8"))
                return

            if path == "/api/sessions":
                # List sessions across the default sessions directory
                sessions = []
                sessions_dir = self.default_sessions_dir
                for p in sessions_dir.glob("*.json"):
                    if p.name.startswith(".") or p.name.endswith(".lock"):
                        continue
                    try:
                        data = json.loads(p.read_text(encoding="utf-8"))
                        if "session" in data and "status" in data:
                            data = self._terminate_stale_running_session(p, data) or data
                            sessions.append({
                                "id": p.stem,
                                "status": data.get("status", "running"),
                                "procedure_path": data.get("procedure_path", ""),
                                "mtime": p.stat().st_mtime
                            })
                    except Exception:
                        continue
                sessions.sort(key=lambda x: x["mtime"], reverse=True)
                self._write_json({"sessions": sessions})
                return

            if path.startswith("/api/sessions/"):
                session_id = path[len("/api/sessions/"):]
                _, runtime = self._find_runtime_for_session(session_id)
                if runtime is None:
                    # Try finding from default sessions dir directly
                    fallback_path = self.default_sessions_dir / f"{session_id}.json"
                    if fallback_path.exists():
                        try:
                            data = json.loads(fallback_path.read_text(encoding="utf-8"))
                            data = self._terminate_stale_running_session(fallback_path, data) or data
                            self._write_json(data)
                            return
                        except Exception:
                            pass
                    self.send_error(404, "Session not found")
                    return
                session = runtime._load_session(session_id)
                if session is None:
                    self.send_error(404, "Session not found")
                    return
                session_path = runtime._session_file(session_id)
                data = self._terminate_stale_running_session(session_path, session.to_dict())
                if data is not None and data.get("status") == "terminated" and session.status == "running":
                    session.status = "terminated"
                self._write_json(session.to_dict())
                return

            if path == "/api/dag":
                queries = parse_qs(parsed.query)
                dag_path_list = queries.get("path")
                if not dag_path_list:
                    self.send_error(400, "Missing path query parameter")
                    return
                dag_path = Path(dag_path_list[0])
                # Security check: must exist and be JSON
                if not dag_path.exists() or dag_path.suffix.lower() != ".json":
                    self.send_error(403, "Invalid DAG path")
                    return
                try:
                    data = json.loads(dag_path.read_text(encoding="utf-8"))
                    self._write_json(data)
                except Exception as e:
                    self.send_error(500, f"Error reading DAG: {str(e)}")
                return

            self.send_error(404, "Not Found")
        except Exception as e:
            try:
                self.send_error(500, f"Internal Server Error: {str(e)}")
            except Exception:
                pass

    def do_POST(self) -> None:
        try:
            if self.path != "/api/rpc":
                self.send_error(404, "Not Found")
                return

            content_length = int(self.headers.get("Content-Length", 0))
            if content_length == 0:
                self._write_json({"type": "ValidationError", "message": "Body is empty"})
                return

            body = self.rfile.read(content_length)
            try:
                req = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                self._write_json({"type": "ValidationError", "message": "请求不是合法 JSON。"})
                return

            action = req.get("action")
            if action == "ping":
                self._write_json({"type": "ok", "message": "pong"})
                return

            if action == "shutdown":
                resp = {"type": "ok", "message": "dashboard shutting down"}
                self._write_json(resp)
                self._request_server_shutdown()
                return

            if action == "init":
                session = req.get("session")
                if not session:
                    self._write_json({"type": "ValidationError", "message": "缺少 session 参数。"})
                    return
                procedure = req.get("procedure")
                sessions_dir = req.get("sessions_dir")
                max_retries = req.get("max_retries")
                consts = req.get("consts", {})
                if procedure:
                    procedure_path = Path(str(procedure))
                elif self.default_procedure_path:
                    procedure_path = self.default_procedure_path
                else:
                    procedure_path = Path("dummy.rail")
                target_sessions_dir = Path(str(sessions_dir)) if sessions_dir else self.default_sessions_dir
                target_max_retries = int(max_retries) if max_retries is not None else int(self.default_max_retries)
                
                try:
                    runtime = self._get_or_create_runtime(procedure_path, target_sessions_dir, target_max_retries, consts)
                    resp = runtime.init_session(str(session))
                    if resp.get("type") == "init":
                        key = self._runtime_key(procedure_path, target_sessions_dir, target_max_retries, consts)
                        with self.manager_lock:
                            self.session_to_runtime[str(session)] = key
                        resp["procedure"] = str(procedure_path.resolve())
                except Exception as e:
                    resp = {"type": "ValidationError", "message": f"编译或初始化失败: {str(e)}"}
                
                self._write_json(resp)
                return

            # Next, rewind, stop_session, pause_session all require a session lock
            session = req.get("session")
            if not session:
                self._write_json({"type": "ValidationError", "message": "缺少 session 参数。"})
                return

            session_id = str(session)
            lock = self._get_session_lock(session_id)
            if not lock.acquire(blocking=False):
                self._write_json({
                    "type": "ConcurrencyWarning",
                    "code": "SESSION_PARALLEL_CALL_BLOCKED",
                    "message": "检测到同一 session 的并行 next_step 调用。请等待上一条完成后再重试。",
                })
                return

            try:
                _, runtime = self._find_runtime_for_session(session_id)
                if runtime is None:
                    # If not tracked, try to create from default settings
                    try:
                        runtime = self._get_or_create_runtime(
                            self.default_procedure_path or Path("dummy.rail"),
                            self.default_sessions_dir,
                            self.default_max_retries
                        )
                    except Exception as e:
                        self._write_json({"type": "ValidationError", "message": f"Session 运行时加载失败: {str(e)}"})
                        return

                if action == "next":
                    branch_present = "branch_value" in req
                    branch_value = req.get("branch_value")
                    variables = req.get("variables")
                    self._write_json(runtime.next_step(session_id, branch_value, branch_present, variables))
                    return

                if action == "rewind":
                    step_index = req.get("step_index")
                    if not isinstance(step_index, int):
                        self._write_json({"type": "ValidationError", "message": "缺少或非法 step_index 参数。"})
                        return
                    self._write_json(runtime.rewind_session(session_id, int(step_index)))
                    return

                if action == "shutdown_session":
                    resp = runtime.stop_session(session_id)
                    self._write_json(resp)
                    return

                if action == "pause_session":
                    session_obj = runtime._load_session(session_id)
                    if session_obj is None:
                        self._write_json({"type": "ValidationError", "message": "Session 不存在。"})
                        return
                    session_obj.status = "interference"
                    runtime._save_session(session_obj)
                    self._write_json({"type": "ok", "message": "session status set to interference", "session": session_id})
                    return

                if action == "resume_session":
                    session_obj = runtime._load_session(session_id)
                    if session_obj is None:
                        self._write_json({"type": "ValidationError", "message": "Session 不存在。"})
                        return
                    session_obj.status = "running"
                    runtime._save_session(session_obj)
                    # Immediately advance the session by one step upon resume
                    self._write_json(runtime.next_step(session_id, None, False))
                    return

                if action == "jump_to_node":
                    node_id = req.get("node_id")
                    if not node_id:
                        self._write_json({"type": "ValidationError", "message": "缺少 node_id 参数。"})
                        return
                    session_obj = runtime._load_session(session_id)
                    if session_obj is None:
                        self._write_json({"type": "ValidationError", "message": "Session 不存在。"})
                        return
                    
                    # Force cursor, clear branch wait, and set state to running
                    session_obj.status = "running"
                    session_obj.cursor["node_id"] = str(node_id)
                    session_obj.waiting_for_branch = False
                    session_obj.waiting_branch_node = None
                    runtime._save_session(session_obj)
                    
                    # Immediately execute next_step from this newly jumped target node
                    self._write_json(runtime.next_step(session_id, None, False))
                    return

                self._write_json({"type": "ValidationError", "message": "未知 action。"})
            finally:
                lock.release()
        except Exception as e:
            try:
                self._write_json({"type": "ValidationError", "message": f"内部服务器错误: {str(e)}"})
            except Exception:
                pass

    def _write_json(self, data: dict[str, Any]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))


class ThreadedHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def run_dashboard(host: str, port: int, procedure_path: Path | None, sessions_dir: Path, max_retries: int = 3) -> None:
    stop_event = threading.Event()
    DashboardHTTPHandler.runtimes = {}
    DashboardHTTPHandler.session_to_runtime = {}
    DashboardHTTPHandler.session_locks = {}
    DashboardHTTPHandler.manager_lock = threading.Lock()
    DashboardHTTPHandler.session_locks_guard = threading.Lock()
    DashboardHTTPHandler.default_procedure_path = procedure_path.resolve() if procedure_path else None
    DashboardHTTPHandler.default_sessions_dir = sessions_dir.resolve()
    DashboardHTTPHandler.default_max_retries = int(max_retries)
    
    if DashboardHTTPHandler.default_procedure_path:
        DashboardHTTPHandler.runtimes[
            (
                str(DashboardHTTPHandler.default_procedure_path),
                str(DashboardHTTPHandler.default_sessions_dir),
                DashboardHTTPHandler.default_max_retries,
                ()
            )
        ] = RailRunRuntime(
            DashboardHTTPHandler.default_procedure_path,
            DashboardHTTPHandler.default_sessions_dir,
            max_retries=DashboardHTTPHandler.default_max_retries,
            consts={}
        )
    DashboardHTTPHandler.stop_event = stop_event
    
    server = ThreadedHTTPServer((host, port), DashboardHTTPHandler)
    try:
        print(
            f"[{now_str()}] dashboard started on http://{host}:{port}/ "
            f"procedure={DashboardHTTPHandler.default_procedure_path}",
            file=sys.stderr,
            flush=True,
        )
        server.serve_forever(poll_interval=0.5)
    finally:
        print(f"[{now_str()}] dashboard stopped", file=sys.stderr, flush=True)
        server.server_close()
