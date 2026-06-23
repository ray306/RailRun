from __future__ import annotations

import argparse
import ast
import json
import re
import secrets
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8799
DEFAULT_LANGUAGE = "中文"
DEFAULT_PERSISTENCE = True
DEFAULT_SESSIONS_DIR = ROOT / "sessions"
SESSION_ID_HEX_LEN = 4
RAIL_GENERATOR_ALIAS = "rail_generator"
GENERATE_INPUT_SUFFIXES = {".md", ".txt"}
OUTPUT_HISTORY_TYPES = {"Step", "Ask", "Branch"}


class ProcedureResolutionError(ValueError):
    pass


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def validation_error(message: str) -> dict[str, Any]:
    return {"type": "ValidationError", "message": message}


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
    parser.add_argument("--output", help="上一执行步骤向用户展示的完整正式输出")
    parser.add_argument(
        "--language",
        help="要求 Agent 执行过程使用的输出语言；未传入时读取 config.json runtime.language。初始化后保存到 session。",
    )
    parser.add_argument(
        "--persistence",
        choices=["true", "false"],
        help="是否记录正式输出；未传入时读取 config.json runtime.persistence。初始化后保存到 session。",
    )
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


def load_runtime_config_defaults() -> tuple[bool, str, dict[str, str]]:
    config_data, _ = load_first_config()
    persistence = DEFAULT_PERSISTENCE
    language = DEFAULT_LANGUAGE
    errors: dict[str, str] = {}

    runtime_config = {}
    if isinstance(config_data, dict):
        candidate = config_data.get("runtime", {})
        if isinstance(candidate, dict):
            runtime_config = candidate
        elif "runtime" in config_data:
            errors["runtime"] = "config.json 中 runtime 必须是对象。"

    if "persistence" in runtime_config:
        configured_persistence = runtime_config["persistence"]
        if isinstance(configured_persistence, bool):
            persistence = configured_persistence
        else:
            errors["persistence"] = "config.json 中 runtime.persistence 必须是 true 或 false。"

    if "language" in runtime_config:
        configured_language = runtime_config["language"]
        if isinstance(configured_language, str) and configured_language.strip():
            language = configured_language.strip()
        else:
            errors["language"] = "config.json 中 runtime.language 必须是非空字符串。"

    return persistence, language, errors


def resolve_config_path(path_value: str, base_dir: Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else base_dir / path


def resolve_existing_path(path: Path, raw_text: str) -> str:
    resolved = path.resolve()
    if not resolved.exists():
        raise ProcedureResolutionError(f"procedure 文件不存在: {raw_text} -> {resolved}")
    return str(resolved)


def strip_procedure_wrappers(text: str) -> str:
    text = text.strip()
    while text:
        if (
            (text.startswith("'") and text.endswith("'"))
            or (text.startswith('"') and text.endswith('"'))
            or (text.startswith("[") and text.endswith("]"))
            or (text.startswith("(") and text.endswith(")"))
        ):
            text = text[1:-1].strip()
        else:
            break
    return text


def parse_procedure_const_value(value: str) -> Any:
    value = value.strip()
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return ast.literal_eval(value)
    except Exception:
        if (value.startswith("'") and value.endswith("'")) or (
            value.startswith('"') and value.endswith('"')
        ):
            return value[1:-1]
    return value


def parse_variable_value(value: str) -> Any:
    value = value.strip()
    if (value.startswith("'") and value.endswith("'")) or (
        value.startswith('"') and value.endswith('"')
    ):
        return value[1:-1]
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def resolve_procedure_path(args: argparse.Namespace) -> str | None:
    if args.procedure:
        raw_text = strip_procedure_wrappers(str(args.procedure))

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
    print_json(validation_error(str(exc)))


def session_persistence_mode(session_data: dict[str, Any] | None) -> str | None:
    if not isinstance(session_data, dict):
        return None
    if not session_data.get("output_persistence_enabled", True):
        return "disabled"
    if isinstance(session_data.get("host_output_capture"), dict):
        return "host_transcript"
    return "manual_output"


def pending_history_needs_output(session_data: dict[str, Any] | None) -> bool:
    if not isinstance(session_data, dict):
        return False
    history = session_data.get("history")
    if not isinstance(history, list) or not history:
        return False
    previous = history[-1]
    return (
        isinstance(previous, dict)
        and previous.get("type") in OUTPUT_HISTORY_TYPES
        and "output" not in previous
    )


def attach_output_argument_instruction(
    resp: dict[str, Any],
    *,
    session_data: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(resp, dict):
        return resp

    resp_type = resp.get("type")
    if resp_type in {"Finished", "HumanInterferenceRequest"}:
        return resp

    mode = session_persistence_mode(session_data)
    requires_output_argument = (
        mode == "manual_output"
        and (
            resp_type in OUTPUT_HISTORY_TYPES
            or pending_history_needs_output(session_data)
        )
    )
    if requires_output_argument:
        resp["output_argument_required"] = {
            "argument": "--output",
            "value": "<上一执行步骤的正式输出>",
            "message": "下一次调用 next_step 时必须传入上一执行步骤向用户展示的完整正式输出。",
        }
    return resp


def read_session_data(sessions_dir: str, session_id: str) -> dict[str, Any] | None:
    session_file = Path(sessions_dir).resolve() / f"{session_id}.json"
    if not session_file.exists():
        return None
    try:
        with open(session_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def format_const_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def format_consts_for_procedure(consts: dict[str, Any]) -> str:
    return ", ".join(f"{key}={format_const_value(value)}" for key, value in sorted(consts.items()))


def build_generator_consts_for_file(input_path: str, source_flow_consts: dict[str, Any]) -> dict[str, Any]:
    source = Path(input_path).resolve()
    return {
        "input_kind": "file",
        "input_path": str(source),
        "suggested_output_rail": str(source.with_suffix(".rail")),
        "source_flow_params": format_consts_for_procedure(source_flow_consts),
    }


def parse_procedure_and_consts(procedure_str: str | None) -> tuple[str | None, dict[str, Any]]:
    if not procedure_str:
        return None, {}
    procedure_str = procedure_str.strip()
    if "(" in procedure_str and procedure_str.endswith(")") and not procedure_str.startswith("("):
        path_part, params_part = procedure_str.split("(", 1)
        params_part = params_part.rstrip(")")
        consts = {}
        pattern = re.compile(
            r"([a-zA-Z_]\w*)\s*=\s*('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|[a-zA-Z0-9_\.]+)"
        )
        for m in pattern.finditer(params_part):
            k = m.group(1)
            consts[k] = parse_procedure_const_value(m.group(2))
        return path_part, consts
    else:
        return strip_procedure_wrappers(procedure_str), {}


def parse_vars(items: list[str]) -> dict[str, Any]:
    variables = {}
    for item in items:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        variables[key.strip()] = parse_variable_value(value)
    return variables


def normalize_runtime_options(args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    procedure_clean, consts = parse_procedure_and_consts(args.procedure)
    persistence_from_procedure = consts.pop("persistence", None)
    language_from_procedure = consts.pop("language", None)
    config_persistence, config_language, config_errors = load_runtime_config_defaults()
    if config_errors:
        print_json(validation_error(" ".join(config_errors.values())))
        return False, consts

    if args.persistence is None and persistence_from_procedure is not None:
        if not isinstance(persistence_from_procedure, bool):
            print_json(validation_error("persistence 参数必须是 true 或 false。"))
            return False, consts
        args.persistence = "true" if persistence_from_procedure else "false"

    if args.language is None and language_from_procedure is not None:
        if not isinstance(language_from_procedure, str) or not language_from_procedure.strip():
            print_json(validation_error("language 参数必须是非空字符串。"))
            return False, consts
        args.language = language_from_procedure

    if args.language is None:
        args.language = config_language
    if args.persistence is None:
        args.persistence = "true" if config_persistence else "false"
    args.procedure = procedure_clean
    return True, consts


def recover_procedure_path_from_session(sessions_dir: str, session_id: str | None) -> str | None:
    if not session_id:
        return None
    session_data = read_session_data(sessions_dir, session_id)
    if not isinstance(session_data, dict):
        return None
    procedure_path = session_data.get("procedure_path")
    return procedure_path if isinstance(procedure_path, str) else None


def run_ui(args: argparse.Namespace) -> int:
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


def build_runtime(args: argparse.Namespace, consts: dict[str, Any]):
    from scripts.base import RailRunRuntime

    try:
        procedure_path = resolve_procedure_path(args)
    except ProcedureResolutionError as exc:
        print_procedure_resolution_error(exc)
        return None

    if args.session and not procedure_path:
        procedure_path = recover_procedure_path_from_session(args.sessions_dir, args.session)

    if not procedure_path:
        print_json(validation_error("初始化需要 --procedure"))
        return None

    if Path(procedure_path).suffix.lower() in GENERATE_INPUT_SUFFIXES:
        consts = build_generator_consts_for_file(procedure_path, consts)
        try:
            procedure_path = resolve_procedure_path(argparse.Namespace(procedure=f"[{RAIL_GENERATOR_ALIAS}]"))
        except ProcedureResolutionError as exc:
            print_procedure_resolution_error(exc)
            return None

    try:
        runtime = RailRunRuntime(
            procedure_path=Path(procedure_path),
            sessions_dir=Path(args.sessions_dir),
            max_retries=args.max_retries,
            consts=consts,
            output_persistence_enabled=args.persistence != "false",
            language=args.language,
            host="auto",
        )
    except Exception as exc:
        print_json(validation_error(f"初始化运行时失败: {str(exc)}"))
        return None
    return runtime


def run_cli(args: argparse.Namespace) -> int:
    ok, consts = normalize_runtime_options(args)
    if not ok:
        return 1

    local_rt = build_runtime(args, consts)
    if local_rt is None:
        return 1

    if args.shutdown:
        if not args.session:
            print_json(validation_error("--shutdown 必须与 --session 一起使用。"))
            return 1
        print_json(local_rt.stop_session(args.session))
        return 0

    if args.step_index is not None:
        if not args.session:
            print_json(validation_error("--step-index 必须与 --session 一起使用。"))
            return 1
        resp = local_rt.rewind_session(args.session, int(args.step_index))
        resp = attach_output_argument_instruction(
            resp,
            session_data=read_session_data(args.sessions_dir, args.session),
        )
        print_json(resp)
        return 0

    if not args.session:
        new_session = secrets.token_hex(SESSION_ID_HEX_LEN // 2)
        resp = local_rt.init_session(new_session)
        resp = attach_output_argument_instruction(
            resp,
            session_data=read_session_data(args.sessions_dir, new_session),
        )
        print_json(resp)
        return 0

    branch_present = args.branch_value is not None
    branch_value = args.branch_value == "true" if branch_present else None
    variables = parse_vars(args.var)

    resp = local_rt.next_step(args.session, branch_value, branch_present, variables, args.output)
    resp = attach_output_argument_instruction(
        resp,
        session_data=read_session_data(args.sessions_dir, args.session),
    )
    print_json(resp)
    return 0


def main() -> int:
    args = parse_args()
    if args.ui:
        return run_ui(args)
    return run_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
