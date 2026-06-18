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


ROOT = Path(__file__).resolve().parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8799
DEFAULT_SESSIONS_DIR = ROOT / "sessions"
SESSION_ID_HEX_LEN = 4
DIRECT_PROCEDURE_SUFFIXES = {".rail", ".md", ".txt"}


class ProcedureResolutionError(ValueError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RailRun next_step CLI")
    parser.add_argument("--session", help="Session ID")
    parser.add_argument("--step-index", type=int, help="Rewind session cursor to a historical step index")
    parser.add_argument("--branch-value", choices=["true", "false"], help="Branch boolean value")
    parser.add_argument("--procedure", help="Procedure path or [alias] from config.json rails_alias_and_path.")
    parser.add_argument("--shutdown", action="store_true", help="停止会话")
    parser.add_argument("--ui", action="store_true", help="启动 Web 可视化面板")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--sessions-dir", default=str(DEFAULT_SESSIONS_DIR))
    parser.add_argument("--var", action="append", default=[], help="回传变量值 (e.g., --var name=value)")
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


def resolve_existing_path(path: Path, raw_text: str) -> str:
    resolved = path.resolve()
    if not resolved.exists():
        raise ProcedureResolutionError(f"procedure 文件不存在: {raw_text} -> {resolved}")
    return str(resolved)


def resolve_procedure_path(args: argparse.Namespace) -> str | None:
    if args.procedure:
        raw_text = str(args.procedure).strip()
        # Strip quotes and brackets/parentheses recursively
        while raw_text:
            if (raw_text.startswith("'") and raw_text.endswith("'")) or \
               (raw_text.startswith('"') and raw_text.endswith('"')) or \
               (raw_text.startswith('[') and raw_text.endswith(']')) or \
               (raw_text.startswith('(') and raw_text.endswith(')')):
                raw_text = raw_text[1:-1].strip()
            else:
                break

        if not raw_text:
            raise ProcedureResolutionError("procedure 路径或别名为空。")

        suffix = Path(raw_text).suffix
        if suffix:
            # Has extension -> treat as path
            raw = Path(raw_text)
            return resolve_existing_path(raw, raw_text)
        else:
            # No extension -> treat as alias
            config_data, config_path = load_first_config()
            rail_aliases = {}
            if isinstance(config_data, dict):
                candidate_aliases = config_data.get("rails_alias_and_path", {})
                if isinstance(candidate_aliases, dict):
                    rail_aliases = candidate_aliases
            
            alias = raw_text
            if config_path and alias in rail_aliases:
                alias_path = resolve_config_path(str(rail_aliases[alias]), config_path.parent)
                return resolve_existing_path(alias_path, raw_text)
            raise ProcedureResolutionError(f"procedure 别名未找到: {raw_text}")
    return None


def print_procedure_resolution_error(exc: ProcedureResolutionError) -> None:
    print(json.dumps({"type": "ValidationError", "message": str(exc)}, ensure_ascii=False))


def build_md_rewrite_instruction(args: argparse.Namespace) -> dict[str, Any] | None:
    procedure_path = resolve_procedure_path(args)
    if not procedure_path:
        return None

    procedure = Path(procedure_path)
    source_suffix = procedure.suffix.lower()
    if source_suffix not in {".md", ".txt"}:
        return None

    rail_path = procedure.with_suffix(".rail")
    procedure_arg = str(rail_path)
    consts = getattr(args, "consts", None)
    if consts:
        parts = []
        for k, v in consts.items():
            if isinstance(v, bool):
                val_str = "true" if v else "false"
            elif isinstance(v, (int, float)):
                val_str = str(v)
            else:
                escaped = str(v).replace("'", "\\'")
                val_str = f"'{escaped}'"
            parts.append(f"{k}={val_str}")
        procedure_arg = f"{rail_path}({', '.join(parts)})"

    retry_cmd = [
        "python",
        "next_step.py",
        "--procedure",
        procedure_arg,
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





def parse_procedure_and_consts(procedure_str: str | None) -> tuple[str | None, dict[str, Any]]:
    if not procedure_str:
        return None, {}
    procedure_str = procedure_str.strip()
    if "(" in procedure_str and procedure_str.endswith(")") and not procedure_str.startswith("("):
        path_part, params_part = procedure_str.split("(", 1)
        params_part = params_part.rstrip(")")
        consts = {}
        import re
        import ast
        pattern = re.compile(
            r"([a-zA-Z_]\w*)\s*=\s*('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|[a-zA-Z0-9_\.]+)"
        )
        for m in pattern.finditer(params_part):
            k = m.group(1)
            v = m.group(2).strip()
            if v.lower() == "true":
                v = True
            elif v.lower() == "false":
                v = False
            else:
                try:
                    v = ast.literal_eval(v)
                except Exception:
                    if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
                        v = v[1:-1]
            consts[k] = v
        return path_part, consts
    else:
        # Strip quotes and brackets/parentheses recursively
        while procedure_str:
            if (procedure_str.startswith("'") and procedure_str.endswith("'")) or \
               (procedure_str.startswith('"') and procedure_str.endswith('"')) or \
               (procedure_str.startswith('[') and procedure_str.endswith(']')) or \
               (procedure_str.startswith('(') and procedure_str.endswith(')')):
                procedure_str = procedure_str[1:-1].strip()
            else:
                break
        return procedure_str, {}


def main() -> int:
    args = parse_args()
    
    # 1. Start Web UI if requested
    if args.ui:
        from scripts.dashboard import run_dashboard
        procedure_path = None
        if args.procedure:
            try:
                procedure_path = Path(resolve_procedure_path(args))
            except ProcedureResolutionError:
                pass
        
        print(f"Starting RailRun Dashboard on http://{args.host}:{args.port} ...", flush=True)
        try:
            run_dashboard(
                host=args.host,
                port=args.port,
                procedure_path=procedure_path,
                sessions_dir=Path(args.sessions_dir),
                max_retries=args.max_retries,
            )
        except KeyboardInterrupt:
            print("\nDashboard stopped.", flush=True)
        return 0

    # 2. Local/Base Mode Execution
    procedure_clean, consts = parse_procedure_and_consts(args.procedure)
    args.procedure = procedure_clean
    args.consts = consts

    try:
        md_instruction = build_md_rewrite_instruction(args)
    except ProcedureResolutionError as exc:
        print_procedure_resolution_error(exc)
        return 1
    if md_instruction is not None:
        print(json.dumps(md_instruction, ensure_ascii=False))
        return 0

    from scripts.base import RailRunRuntime
    try:
        procedure_path = resolve_procedure_path(args)
    except ProcedureResolutionError as exc:
        print_procedure_resolution_error(exc)
        return 1
    
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
        print(json.dumps({"type": "ValidationError", "message": "初始化需要 --procedure"}, ensure_ascii=False))
        return 1

    try:
        local_rt = RailRunRuntime(
            procedure_path=Path(procedure_path),
            sessions_dir=Path(args.sessions_dir),
            max_retries=args.max_retries,
            consts=consts
        )
    except Exception as e:
        print(json.dumps({"type": "ValidationError", "message": f"初始化运行时失败: {str(e)}"}, ensure_ascii=False))
        return 1

    if args.shutdown:
        if not args.session:
            print(json.dumps({"type": "ValidationError", "message": "--shutdown 必须与 --session 一起使用。"}, ensure_ascii=False))
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

    # 解析回传的运行时变量
    variables = {}
    for item in args.var:
        if "=" in item:
            k, v = item.split("=", 1)
            k = k.strip()
            v = v.strip()
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
            variables[k] = v

    resp = local_rt.next_step(args.session, branch_value, branch_present, variables)
    print(json.dumps(resp, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
