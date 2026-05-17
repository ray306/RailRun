from __future__ import annotations

import argparse
import shlex
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from scripts.global_daemon import send_request

ROOT = Path(__file__).resolve().parent
RUNTIME_FILE = ROOT / ".daemon_runtime.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8799
DEFAULT_SESSIONS_DIR = ROOT / "sessions"
DAEMON_LOG = ROOT / "daemon.log"
SESSION_ID_HEX_LEN = 4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RailRun next_step CLI")
    parser.add_argument("--session", help="Session ID")
    parser.add_argument("--branch-value", choices=["true", "false"], help="Branch boolean value")
    parser.add_argument("--procedure", help="Procedure path. Defaults to examples/ and .arp suffix when omitted.")
    parser.add_argument("--shutdown", action="store_true", help="Stop daemon")
    parser.add_argument("--force-shutdown", action="store_true", help="Force stop daemon even with active sessions")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--sessions-dir", default=str(DEFAULT_SESSIONS_DIR))
    parser.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def resolve_procedure_path(args: argparse.Namespace) -> str | None:
    if args.procedure:
        raw = Path(args.procedure)
        if raw.suffix == "":
            raw = raw.with_suffix(".arp")
        if raw.parent == Path("."):
            raw = Path("examples") / raw
        return str((ROOT / raw).resolve())
    return None


def build_md_rewrite_instruction(args: argparse.Namespace) -> dict[str, Any] | None:
    procedure_path = resolve_procedure_path(args)
    if not procedure_path:
        return None

    procedure = Path(procedure_path)
    source_suffix = procedure.suffix.lower()
    if source_suffix not in {".md", ".txt"}:
        return None

    arp_path = procedure.with_suffix(".arp")
    retry_cmd = [
        "python",
        "next_step.py",
        "--procedure",
        str(arp_path),
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

    prompt_text = (
        "如果你是Codex，请你：\n"
        "1) 当前主 Agent 不得直接执行转写任务；必须先启动一个子 Agent 执行转写。\n"
        "2) 子 Agent 必须依据 PROTOCOL.md 约定的协议将输入文本文件转写为 .arp 文件，输出到同目录同名 .arp。\n"
        "3) 仅当子 Agent 成功产出 .arp 后，主 Agent 才能执行重试命令。\n"
        "4) 若未启动子 Agent 或转写失败，必须返回失败，不得继续。\n\n"
        "如果你不是Codex，请你：\n"
        "直接依据 PROTOCOL.md 约定的协议将输入文本文件转写为 .arp 文件，输出到同目录同名 .arp。\n\n"
        f"输入文件: {procedure}\n"
        f"协议文件: {(ROOT / 'PROTOCOL.md').resolve()}\n"
        f"输出文件: {arp_path}\n"
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
        "output_arp": str(arp_path),
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
        return isinstance(resp, dict)
    except Exception:
        return False


def start_daemon(args: argparse.Namespace, procedure: str) -> dict:
    cmd = [
        sys.executable,
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
        try:
            send_request(args.host, args.port, {"action": "ping"})
            return meta
        except Exception:
            time.sleep(0.1)
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
    from scripts.global_daemon import run_daemon

    if not args.procedure:
        print(json.dumps({"type": "ValidationError", "message": "daemon 模式需要 --procedure"}, ensure_ascii=False))
        return 2
    run_daemon(
        host=args.host,
        port=args.port,
        procedure_path=Path(args.procedure),
        sessions_dir=Path(args.sessions_dir),
        max_retries=args.max_retries,
    )
    return 0


def main() -> int:
    args = parse_args()
    if args.daemon:
        return run_daemon_mode(args)
    md_instruction = build_md_rewrite_instruction(args)
    if md_instruction is not None:
        print(json.dumps(md_instruction, ensure_ascii=False))
        return 0

    if args.shutdown:
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
                    # 如果已经没有存活 session，daemon 会自行关闭，清理 runtime 文件。
                    if bool(resp.get("daemon_shutdown", False)):
                        should_clear_runtime_file = True
                    # session 不存在也要遵循“无存活 session 则整体关闭”的行为。
                    if resp.get("type") == "ValidationError":
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
                # shutdown blocked (e.g., active refs) or validation failure:
                # keep response internal and signal with exit code only.
                return 1
        finally:
            if should_clear_runtime_file and RUNTIME_FILE.exists():
                RUNTIME_FILE.unlink()
        return 1

    runtime = ensure_daemon(args)
    host, port = runtime["host"], int(runtime["port"])

    if not args.session:
        new_session = secrets.token_hex(SESSION_ID_HEX_LEN // 2)
        payload: dict[str, Any] = {"action": "init", "session": new_session}
        procedure_path = resolve_procedure_path(args)
        if procedure_path:
            payload["procedure"] = procedure_path
            payload["sessions_dir"] = str(Path(args.sessions_dir).resolve())
            payload["max_retries"] = int(args.max_retries)
        init_resp = send_request(host, port, payload)
        print(json.dumps(init_resp, ensure_ascii=False))
        return 0

    payload: dict = {"action": "next", "session": args.session}
    if args.branch_value is not None:
        payload["branch_value"] = args.branch_value == "true"
    resp = send_request(host, port, payload)
    print(json.dumps(resp, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
