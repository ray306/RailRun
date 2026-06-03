from __future__ import annotations

import argparse
import shlex
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
RUNTIME_FILE = ROOT / ".daemon_runtime.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8799
DEFAULT_SESSIONS_DIR = ROOT / "sessions"
DAEMON_LOG = ROOT / "daemon.log"
SESSION_ID_HEX_LEN = 4


def send_request(host: str, port: int, payload: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
    url = f"http://{host}:{port}/api/rpc"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        res_body = response.read().decode("utf-8")
        return json.loads(res_body)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RailRun next_step CLI")
    parser.add_argument("--session", help="Session ID")
    parser.add_argument("--step-index", type=int, help="Rewind session cursor to a historical step index")
    parser.add_argument("--branch-value", choices=["true", "false"], help="Branch boolean value")
    parser.add_argument("--procedure", help="Procedure path or [alias] from config.json rails_alias_and_path.")
    parser.add_argument("--shutdown", action="store_true", help="Stop daemon")
    parser.add_argument("--force-shutdown", action="store_true", help="Force stop daemon even with active sessions")
    parser.add_argument("--use-daemon", action="store_true", help="使用守护进程处理请求")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--sessions-dir", default=str(DEFAULT_SESSIONS_DIR))
    parser.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def iter_config_paths() -> list[Path]:
    return [
        Path.cwd() / "config.json",
        ROOT / "config.json",
        Path.cwd() / ".railrun.json",
        ROOT / ".railrun.json",
    ]


def load_first_config() -> tuple[dict[str, Any], Path] | tuple[None, None]:
    for config_path in iter_config_paths():
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config_data = json.load(f)
                if isinstance(config_data, dict):
                    return config_data, config_path
            except Exception:
                pass
    return None, None


def resolve_config_path(path_value: str, base_dir: Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else base_dir / path


def resolve_procedure_path(args: argparse.Namespace) -> str | None:
    if args.procedure:
        raw_text = str(args.procedure).strip()
        config_data, config_path = load_first_config()

        if raw_text.startswith("[") and raw_text.endswith("]"):
            alias = raw_text[1:-1].strip()
            if alias and Path(alias).suffix.lower() != ".rail" and isinstance(config_data, dict) and config_path:
                rail_aliases = config_data.get("rails_alias_and_path", {})
                if isinstance(rail_aliases, dict) and alias in rail_aliases:
                    alias_path = resolve_config_path(str(rail_aliases[alias]), config_path.parent)
                    return str(alias_path.resolve())

        raw = Path(raw_text)
        if raw.suffix == "":
            raw = raw.with_suffix(".rail")
        if raw.is_absolute():
            return str(raw.resolve())
        return str(raw.resolve())
    return None


def build_md_rewrite_instruction(args: argparse.Namespace) -> dict[str, Any] | None:
    procedure_path = resolve_procedure_path(args)
    if not procedure_path:
        return None

    procedure = Path(procedure_path)
    source_suffix = procedure.suffix.lower()
    if source_suffix not in {".md", ".txt"}:
        return None

    rail_path = procedure.with_suffix(".rail")
    retry_cmd = [
        "python",
        "next_step.py",
        "--procedure",
        str(rail_path),
        "--host",
        str(args.host),
        "--port",
        str(args.port),
        "--max-retries",
        str(args.max_retries),
        "--sessions-dir",
        str(Path(args.sessions_dir).resolve()),
    ]
    if args.branch_value is not None:
        retry_cmd.extend(["--branch-value", args.branch_value])
    if args.session:
        retry_cmd.extend(["--session", args.session])
    if args.use_daemon:
        retry_cmd.append("--use-daemon")

    prompt_text = (
        "如果你是Codex，请你：\n"
        "1) 当前主 Agent 不得直接执行转写任务；必须先启动一个子 Agent 执行转写。\n"
        "2) 子 Agent 必须依据 PROTOCOL.md 约定的协议将输入文本文件转写为 .rail 文件，输出到同目录同名 .rail。\n"
        "3) 仅当子 Agent 成功产出 .rail 后，主 Agent 才能执行重试命令。\n"
        "4) 若未启动子 Agent 或转写失败，必须返回失败，不得继续。\n\n"
        "如果你不是Codex，请你：\n"
        "直接依据 PROTOCOL.md 约定的协议将输入文本文件转写为 .rail 文件，输出到同目录同名 .rail。\n\n"
        f"输入文件: {procedure}\n"
        f"协议文件: {(ROOT / 'PROTOCOL.md').resolve()}\n"
        f"输出文件: {rail_path}\n"
        f"重试命令: {' '.join(shlex.quote(part) for part in retry_cmd)}"
    )

    return {
        # Reuse existing runtime contract so orchestrators treat this as executable prompt.
        "type": "Step",
        "instruction": prompt_text,
        # Keep machine-readable hints for adapters that support structured routing.
        "next_action": "spawn_subagent_and_retry",
        "required_executor": "subagent",
        "input_text_file": str(procedure),
        "protocol_file": str((ROOT / "PROTOCOL.md").resolve()),
        "output_rail": str(rail_path),
        "retry_command": " ".join(shlex.quote(part) for part in retry_cmd),
    }


def write_runtime(meta: dict) -> None:
    RUNTIME_FILE.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def read_runtime() -> dict | None:
    if not RUNTIME_FILE.exists():
        return None
    return json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))


def is_alive(host: str, port: int) -> bool:
    try:
        resp = send_request(host, port, {"action": "ping"})
        return isinstance(resp, dict) and resp.get("type") == "ok"
    except Exception:
        return False


def start_daemon(args: argparse.Namespace, procedure: str) -> dict:
    cmd = [
        sys.executable,
        "-u",
        str(Path(__file__).resolve()),
        "--daemon",
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--procedure",
        procedure,
        "--sessions-dir",
        args.sessions_dir,
        "--max-retries",
        str(args.max_retries),
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    log_handle = DAEMON_LOG.open("ab")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=log_handle,
            creationflags=creationflags,
        )
    finally:
        # Child process keeps its own inherited descriptor; close parent handle.
        log_handle.close()
    meta = {
        "pid": proc.pid,
        "host": args.host,
        "port": args.port,
        "procedure": str(Path(procedure).resolve()),
        "sessions_dir": str(Path(args.sessions_dir).resolve()),
        "max_retries": args.max_retries,
        "started_at": int(time.time()),
    }
    write_runtime(meta)
    for _ in range(50):
        if proc.poll() is not None:
            if RUNTIME_FILE.exists():
                current = read_runtime()
                if current and current.get("pid") == proc.pid:
                    RUNTIME_FILE.unlink()
            raise RuntimeError(f"daemon 启动失败，子进程已退出，退出码 {proc.returncode}。请查看 {DAEMON_LOG}")
        try:
            send_request(args.host, args.port, {"action": "ping"}, timeout=0.2)
            return meta
        except Exception:
            time.sleep(0.1)
    if RUNTIME_FILE.exists():
        current = read_runtime()
        if current and current.get("pid") == proc.pid:
            RUNTIME_FILE.unlink()
    raise RuntimeError("daemon 启动失败。")


def ensure_daemon(args: argparse.Namespace) -> dict:
    runtime = read_runtime()
    procedure_path = resolve_procedure_path(args)
    if runtime:
        host, port = runtime["host"], int(runtime["port"])
        try:
            send_request(host, port, {"action": "ping"})
            return runtime
        except Exception:
            # Daemon metadata exists but process is gone; restart from cached config.
            procedure = procedure_path or runtime.get("procedure")
            if procedure:
                args.host = host
                args.port = port
                args.sessions_dir = runtime.get("sessions_dir", args.sessions_dir)
                args.max_retries = int(runtime.get("max_retries", args.max_retries))
                return start_daemon(args, str(procedure))

    if not procedure_path:
        raise RuntimeError("daemon 未运行，请先用 --procedure 启动。")
    return start_daemon(args, procedure_path)


def run_daemon_mode(args: argparse.Namespace) -> int:
    from scripts.daemon import run_daemon

    if not args.procedure:
        print(json.dumps({"type": "ValidationError", "message": "daemon 模式需要 --procedure"}, ensure_ascii=False))
        return 2
    procedure_path = resolve_procedure_path(args)
    if not procedure_path:
        print(json.dumps({"type": "ValidationError", "message": "无法解析 procedure 路径"}, ensure_ascii=False))
        return 1
    run_daemon(
        host=args.host,
        port=args.port,
        procedure_path=Path(procedure_path),
        sessions_dir=Path(args.sessions_dir),
        max_retries=args.max_retries,
    )
    return 0


def parse_procedure_and_consts(procedure_str: str | None) -> tuple[str | None, dict[str, Any]]:
    if not procedure_str:
        return None, {}
    if "(" in procedure_str and procedure_str.endswith(")"):
        path_part, params_part = procedure_str.split("(", 1)
        params_part = params_part.rstrip(")")
        consts = {}
        import re
        pattern = re.compile(r"([a-zA-Z_]\w*)\s*=\s*('[^']*'|\"[^\"]*\"|[a-zA-Z0-9_\.]+)")
        for m in pattern.finditer(params_part):
            k = m.group(1)
            v = m.group(2).strip()
            if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
                v = v[1:-1]
            elif v.lower() == "true":
                v = True
            elif v.lower() == "false":
                v = False
            else:
                try:
                    if "." in v:
                        v = float(v)
                    else:
                        v = int(v)
                except ValueError:
                    pass
            consts[k] = v
        return path_part, consts
    return procedure_str, {}


def main() -> int:
    args = parse_args()
    
    # Load default use_daemon from config files if not explicitly set
    if not args.use_daemon:
        config_paths = [
            Path.cwd() / "config.json",
            ROOT / "config.json",
            Path.cwd() / ".railrun.json",
            ROOT / ".railrun.json"
        ]
        for config_path in config_paths:
            if config_path.exists():
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        config_data = json.load(f)
                        if isinstance(config_data, dict) and config_data.get("use_daemon") is True:
                            args.use_daemon = True
                            break
                except Exception:
                    pass

    procedure_clean, consts = parse_procedure_and_consts(args.procedure)
    args.procedure = procedure_clean

    if args.daemon:
        return run_daemon_mode(args)
    md_instruction = build_md_rewrite_instruction(args)
    if md_instruction is not None:
        print(json.dumps(md_instruction, ensure_ascii=False))
        return 0

    if args.shutdown and (args.use_daemon or not args.session):
        runtime = read_runtime()
        if not runtime:
            return 0
        if args.session:
            should_clear_runtime_file = False
            try:
                try:
                    resp = send_request(
                        runtime["host"],
                        int(runtime["port"]),
                        {"action": "shutdown_session", "session": args.session},
                    )
                except Exception:
                    should_clear_runtime_file = True
                    return 0
                else:
                    if bool(resp.get("daemon_shutdown", False)):
                        should_clear_runtime_file = True
                    if resp.get("type") == "error" or resp.get("type") == "ValidationError":
                        if bool(resp.get("daemon_shutdown", False)):
                            return 0
                        return 1
                    return 0
            finally:
                if should_clear_runtime_file and RUNTIME_FILE.exists():
                    RUNTIME_FILE.unlink()

        should_clear_runtime_file = False
        try:
            try:
                payload: dict = {"action": "shutdown"}
                if args.force_shutdown:
                    payload["force"] = True
                resp = send_request(runtime["host"], int(runtime["port"]), payload)
            except Exception:
                should_clear_runtime_file = True
                return 0
            else:
                if resp.get("type") == "ok":
                    should_clear_runtime_file = True
                    return 0
                return 1
        finally:
            if should_clear_runtime_file and RUNTIME_FILE.exists():
                RUNTIME_FILE.unlink()
        return 1

    # Route execution: Base (Local) vs Daemon (HTTP)
    if not args.use_daemon:
        from scripts.base import RailRunRuntime
        procedure_path = resolve_procedure_path(args)
        
        # Recover procedure path from session file if omitted but session exists
        if args.session and not procedure_path:
            session_file = Path(args.sessions_dir).resolve() / f"{args.session}.json"
            if session_file.exists():
                try:
                    with open(session_file, "r", encoding="utf-8") as f:
                        session_data = json.load(f)
                        procedure_path = session_data.get("procedure_path")
                except Exception:
                    pass

        if not procedure_path:
            print(json.dumps({"type": "ValidationError", "message": "base 模式初始化需要 --procedure"}, ensure_ascii=False))
            return 1

        try:
            local_rt = RailRunRuntime(
                procedure_path=Path(procedure_path),
                sessions_dir=Path(args.sessions_dir),
                max_retries=args.max_retries,
                consts=consts
            )
        except Exception as e:
            print(json.dumps({"type": "ValidationError", "message": f"初始化 base 运行时失败: {str(e)}"}, ensure_ascii=False))
            return 1

        if args.shutdown:
            if not args.session:
                print(json.dumps({"type": "ValidationError", "message": "base 模式下 --shutdown 必须与 --session 一起使用。"}, ensure_ascii=False))
                return 1
            resp = local_rt.stop_session(args.session)
            print(json.dumps(resp, ensure_ascii=False))
            return 0

        if args.step_index is not None:
            if not args.session:
                print(json.dumps({"type": "ValidationError", "message": "--step-index 必须与 --session 一起使用。"}, ensure_ascii=False))
                return 1
            resp = local_rt.rewind_session(args.session, int(args.step_index))
            print(json.dumps(resp, ensure_ascii=False))
            return 0

        if not args.session:
            new_session = secrets.token_hex(SESSION_ID_HEX_LEN // 2)
            resp = local_rt.init_session(new_session)
            print(json.dumps(resp, ensure_ascii=False))
            return 0

        branch_present = args.branch_value is not None
        branch_value = args.branch_value == "true" if branch_present else None
        resp = local_rt.next_step(args.session, branch_value, branch_present)
        print(json.dumps(resp, ensure_ascii=False))
        return 0

    # Daemon Path
    try:
        runtime = ensure_daemon(args)
        host, port = runtime["host"], int(runtime["port"])
    except Exception as e:
        print(json.dumps({"type": "ValidationError", "message": f"启动守护进程失败: {str(e)}"}, ensure_ascii=False))
        return 1

    try:
        if args.step_index is not None:
            if not args.session:
                print(json.dumps({"type": "ValidationError", "message": "--step-index 必须与 --session 一起使用。"}, ensure_ascii=False))
                return 1
            payload = {
                "action": "rewind",
                "session": args.session,
                "step_index": int(args.step_index),
            }
            resp = send_request(host, port, payload)
            print(json.dumps(resp, ensure_ascii=False))
            return 0

        if not args.session:
            new_session = secrets.token_hex(SESSION_ID_HEX_LEN // 2)
            payload: dict[str, Any] = {"action": "init", "session": new_session}
            procedure_path = resolve_procedure_path(args)
            if procedure_path:
                payload["procedure"] = procedure_path
                payload["sessions_dir"] = str(Path(args.sessions_dir).resolve())
                payload["max_retries"] = int(args.max_retries)
                if consts:
                    payload["consts"] = consts
            init_resp = send_request(host, port, payload)
            print(json.dumps(init_resp, ensure_ascii=False))
            return 0

        payload: dict = {"action": "next", "session": args.session}
        if args.branch_value is not None:
            payload["branch_value"] = args.branch_value == "true"
        resp = send_request(host, port, payload)
        print(json.dumps(resp, ensure_ascii=False))
        return 0
    except Exception as e:
        print(json.dumps({"type": "ValidationError", "message": f"请求守护进程失败: {str(e)}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
