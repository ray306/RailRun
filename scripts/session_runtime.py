from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable
import re
import ast

from .session_state import SessionState


@dataclass
class StepInput:
    branch_present: bool = False
    branch_value: Any = None
    variables: dict[str, Any] = None
    output: str | None = None
    require_output: bool = False


_VAR_PATTERN = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")
_OUTPUT_REQUIRED_TYPES = {"Step", "Ask", "Branch"}
_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_LANGUAGE_FALLBACK = "中文"


def _configured_default_language() -> str:
    for config_path in (_ROOT / "config.json", _ROOT / ".railrun.json"):
        if not config_path.exists():
            continue
        try:
            config_data = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        runtime = config_data.get("runtime") if isinstance(config_data, dict) else None
        language = runtime.get("language") if isinstance(runtime, dict) else None
        if isinstance(language, str) and language.strip():
            return language.strip()
    return _DEFAULT_LANGUAGE_FALLBACK


def _language_message(language: str) -> str:
    language = language.strip() if isinstance(language, str) else ""
    if not language:
        language = _configured_default_language()
    return f"执行过程必须使用{language}输出。"


def _attach_language_message_until_first_step(resp: dict[str, Any], session: SessionState) -> dict[str, Any]:
    if session.language_message_emitted:
        return resp

    message = _language_message(session.language)
    existing = resp.get("message")
    if isinstance(existing, str) and existing.strip():
        resp["message"] = f"{existing}\n{message}"
    else:
        resp["message"] = message

    if resp.get("type") == "Step":
        session.language_message_emitted = True
    return resp


def _record_pending_output(session: SessionState, step_input: StepInput) -> None:
    if not step_input.require_output or not session.history:
        return

    previous = session.history[-1]
    if previous.get("type") not in _OUTPUT_REQUIRED_TYPES or "output" in previous:
        return

    if not isinstance(step_input.output, str) or not step_input.output.strip():
        raise ValueError("上一执行步骤缺少正式输出；请确认宿主 transcript 已写入，或通过 --output 传入非空内容。")

    previous["output"] = step_input.output


def _render_template(text: str, vars_map: dict[str, Any]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in vars_map:
            return match.group(0)
        return str(vars_map[key])

    return _VAR_PATTERN.sub(repl, text)


def _resolve_for_items(node: dict[str, Any], vars_map: dict[str, Any]) -> list[Any]:
    explicit_items = node.get("items")
    if isinstance(explicit_items, list) and explicit_items:
        return explicit_items
    expr = node.get("items_expr")
    if not isinstance(expr, str) or not expr.strip():
        if isinstance(explicit_items, list):
            return explicit_items
        raise ValueError("For 节点缺少可用的 items/items_expr。")
    key = expr.strip()
    if key in vars_map:
        value = vars_map[key]
        if isinstance(value, (list, tuple, range)):
            return list(value)
        raise ValueError(f"For items_expr 变量 `{key}` 不是可迭代列表/元组/range。")
    # Support a safe subset: range(stop), range(start, stop), range(start, stop, step)
    # with literal integer args only.
    try:
        expr_ast = ast.parse(key, mode="eval").body
        if (
            isinstance(expr_ast, ast.Call)
            and isinstance(expr_ast.func, ast.Name)
            and expr_ast.func.id == "range"
            and not expr_ast.keywords
            and 1 <= len(expr_ast.args) <= 3
            and all(
                isinstance(arg, ast.Constant) and isinstance(arg.value, int)
                for arg in expr_ast.args
            )
        ):
            args = [arg.value for arg in expr_ast.args]
            return list(range(*args))
    except SyntaxError:
        pass
    try:
        parsed = ast.literal_eval(key)
    except (ValueError, SyntaxError):
        raise ValueError(f"For items_expr 无法解析且变量不存在: `{key}`。") from None
    if not isinstance(parsed, list):
        raise ValueError("For items_expr 解析结果不是列表。")
    return parsed


def advance_session(
    session: SessionState,
    dag: dict[str, Any],
    step_input: StepInput,
    now_fn: Callable[[], str],
) -> tuple[dict[str, Any], SessionState]:
    _record_pending_output(session, step_input)

    if step_input.variables:
        session.vars.update(step_input.variables)

    nodes = dag["nodes"]
    current_step_index = int(session.cursor.get("step_index", 0))

    if session.status == "done":
        return _attach_language_message_until_first_step({
            "type": "Finished",
            "message": "所有指令已执行完毕。结束输出。",
            "step_index": current_step_index,
        }, session), session
    if session.status == "interference":
        return _attach_language_message_until_first_step({
            "type": "HumanInterferenceRequest",
            "message": "请人工介入",
            "step_index": current_step_index,
        }, session), session

    if session.waiting_for_branch:
        if not step_input.branch_present:
            raise ValueError("当前 session 正在等待分支求值结果，请传入布尔型 branch_value。")
        if not isinstance(step_input.branch_value, bool):
            raise TypeError("branch_value 必须是布尔值 true 或 false。")
        node = nodes.get(session.waiting_branch_node or "")
        if not node or node.get("type") != "Branch":
            raise ValueError("session 分支节点状态异常。")
        session.history.append(
            {
                "step_index": session.cursor["step_index"],
                "node_id": session.waiting_branch_node,
                "type": "BranchDecision",
                "branch_value": step_input.branch_value,
                "timestamp": now_fn(),
            }
        )
        next_id = node["on_true"] if step_input.branch_value else node["on_false"]
        session.waiting_for_branch = False
        session.waiting_branch_node = None
        session.cursor["step_index"] += 1
        session.cursor["node_id"] = next_id
    elif step_input.branch_present:
        raise ValueError("当前 session 未处于分支求值等待状态，请不要传入 branch_value。")

    if session.cursor.get("node_id") is None:
        raise ValueError("procedure 游标异常，node_id 为空。")
    node_id = str(session.cursor["node_id"])
    node = nodes.get(node_id)
    if node is None:
        raise ValueError("procedure 游标异常，无法找到节点。")

    node_type = node["type"]
    if node_type == "Finished":
        session.status = "done"
        return _attach_language_message_until_first_step({
            "type": "Finished",
            "message": node.get("message") or node.get("instruction") or "所有指令已执行完毕。结束输出。",
            "step_index": int(session.cursor["step_index"]),
        }, session), session

    if node_type == "HumanInterferenceRequest":
        session.status = "interference"
        return _attach_language_message_until_first_step({
            "type": "HumanInterferenceRequest",
            "message": node.get("message", "请人工介入"),
            "step_index": int(session.cursor["step_index"]),
        }, session), session

    if node_type == "Step":
        step_index = int(session.cursor["step_index"])
        rendered_instruction = _render_template(node["instruction"], session.vars)
        session.history.append(
            {
                "step_index": session.cursor["step_index"],
                "node_id": node_id,
                "type": "Step",
                "instruction": rendered_instruction,
                "timestamp": now_fn(),
            }
        )
        session.cursor["step_index"] += 1
        session.cursor["node_id"] = node["next"]
        return _attach_language_message_until_first_step(
            {"type": "Step", "instruction": rendered_instruction, "step_index": step_index},
            session,
        ), session

    if node_type == "Guidance":
        step_index = int(session.cursor["step_index"])
        session.history.append(
            {
                "step_index": session.cursor["step_index"],
                "node_id": node_id,
                "type": "Guidance",
                "instruction": node["instruction"],
                "timestamp": now_fn(),
            }
        )
        session.cursor["step_index"] += 1
        session.cursor["node_id"] = node["next"]
        return _attach_language_message_until_first_step({"type": "Guidance",
                "instruction": node["instruction"],
                "message": "以上是指导性说明，不需要实际执行。请直接调用 next_step 继续。",
            "step_index": step_index,
        }, session), session

    if node_type == "Ask":
        step_index = int(session.cursor["step_index"])
        session.history.append(
            {
                "step_index": session.cursor["step_index"],
                "node_id": node_id,
                "type": "Ask",
                "instruction": node["instruction"],
                "timestamp": now_fn(),
            }
        )
        session.cursor["step_index"] += 1
        session.cursor["node_id"] = node["next"]
        return _attach_language_message_until_first_step({
            "type": "Ask",
            "instruction": node["instruction"],
            "requires_user_input": True,
            "message": "请：1. 先询问用户问题；2. 然后暂停输出，等待用户的回答；3. 用户回答之后你给予反馈；4. 调用 next_step 继续 （你不知道、也不应预期流程什么时候结束）",
            "step_index": step_index,
        }, session), session

    if node_type == "Branch":
        step_index = int(session.cursor["step_index"])
        branch_text = node.get("condition")
        if not isinstance(branch_text, str) or not branch_text:
            raise ValueError("Branch 节点缺少 condition。")
        rendered_condition = _render_template(branch_text, session.vars)
        session.history.append(
            {
                "step_index": session.cursor["step_index"],
                "node_id": node_id,
                "type": "Branch",
                "condition": rendered_condition,
                "timestamp": now_fn(),
            }
        )
        session.waiting_for_branch = True
        session.waiting_branch_node = node_id
        return _attach_language_message_until_first_step({
            "type": "Branch",
            "instruction": f"请根据当前已执行步骤的实际结果判断 condition: “{rendered_condition}” 是否成立，并在下一次调用 next_step 时传入 --branch-value true|false 参数。",
            "requires_branch_value": True,
            "step_index": step_index,
        }, session), session

    if node_type == "For":
        step_index = int(session.cursor["step_index"])
        items = _resolve_for_items(node, session.vars)
        item_key = node.get("item_key")
        index_key = node.get("index_key")
        if not isinstance(item_key, str) or not item_key:
            raise ValueError("For 节点缺少 item_key。")
        if not isinstance(index_key, str) or not index_key:
            raise ValueError("For 节点缺少 index_key。")
        on_iterate = node.get("on_iterate")
        on_done = node.get("on_done")
        if on_iterate is None or on_done is None:
            raise ValueError("For 节点缺少 on_iterate 或 on_done。")

        cursor = int(session.for_cursors.get(node_id, 0))
        if cursor < len(items):
            session.vars[item_key] = items[cursor]
            session.vars[index_key] = cursor
            session.for_cursors[node_id] = cursor + 1
            next_id = on_iterate
            state = "iterate"
        else:
            next_id = on_done
            session.for_cursors[node_id] = 0
            state = "done"
        session.history.append(
            {
                "step_index": session.cursor["step_index"],
                "node_id": node_id,
                "type": "For",
                "state": state,
                "cursor": cursor,
                "timestamp": now_fn(),
            }
        )
        session.cursor["step_index"] += 1
        session.cursor["node_id"] = next_id
        return _attach_language_message_until_first_step({
            "type": "For",
            "instruction": "For 循环已由系统自动推进。",
            "state": state,
            "index": cursor if state == "iterate" else None,
            "item": items[cursor] if state == "iterate" else None,
            "step_index": step_index,
        }, session), session

    raise ValueError(f"未知节点类型: {node_type}")
