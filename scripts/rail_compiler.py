from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class RailCompileError(Exception):
    def __init__(
        self,
        message: str,
        file: Path | None = None,
        line: int | None = None,
        references: list[str] | None = None,
    ):
        loc = f"{file}:{line}" if file is not None and line is not None else None
        super().__init__(f"{message}" + (f" @ {loc}" if loc else ""))
        self.message = message
        self.file = file
        self.line = line
        self.references = references or []


@dataclass
class Origin:
    file: Path
    line: int


@dataclass
class Stmt:
    kind: str
    origin: Origin
    text: str = ""
    cond: str = ""
    body: list["Stmt"] = field(default_factory=list)
    orelse: list["Stmt"] = field(default_factory=list)
    elifs: list[tuple[str, list["Stmt"], Origin]] = field(default_factory=list)
    name: str = ""
    params: list[str] = field(default_factory=list)
    args_text: str = ""
    include_path: str = ""
    from_step_block: bool = False
    iter_var: str = ""
    iter_expr: str = ""


def _normalize_colon(s: str) -> str:
    return s.replace("：", ":")


def _strip_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    i = 0
    while i < len(line):
        ch = line[i]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
                i += 1
                continue
            if ch == "#":
                return line[:i]
            i += 1
            continue

        # Inside a quoted string: honor escapes and only close on the same quote.
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if ch == quote:
            quote = None
        i += 1
    return line


def _parse_indent(raw_line: str) -> tuple[int, str]:
    indent = len(raw_line) - len(raw_line.lstrip(" "))
    if raw_line[:indent].count("\t") > 0:
        raise RailCompileError("不支持 tab 缩进，请改为空格缩进")
    return indent, raw_line[indent:]


def _read_guidance(lines: list[str], start: int, origin_file: Path) -> tuple[str, int]:
    first = lines[start]
    stripped = first.strip()
    if stripped.count('"""') >= 2:
        inner = stripped.split('"""', 1)[1].rsplit('"""', 1)[0]
        return inner.strip(), start + 1
    parts: list[str] = []
    i = start + 1
    while i < len(lines):
        ln = lines[i]
        if '"""' in ln:
            before = ln.split('"""', 1)[0]
            if before.strip():
                parts.append(before.rstrip())
            return "\n".join(parts).strip(), i + 1
        parts.append(ln.rstrip("\n"))
        i += 1
    raise RailCompileError("三引号未闭合", origin_file, start + 1)


def _split_args(args_text: str) -> list[str]:
    text = args_text
    out: list[str] = []
    cur: list[str] = []
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is not None:
            cur.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            cur.append(ch)
            i += 1
            continue
        if ch in "([{":
            depth += 1
            cur.append(ch)
            i += 1
            continue
        if ch in ")]}":
            depth = max(0, depth - 1)
            cur.append(ch)
            i += 1
            continue
        if ch in (",", "，") and depth == 0:
            out.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def _parse_scope(lines: list[str], start: int, base_indent: int, file: Path) -> tuple[list[Stmt], int]:
    stmts: list[Stmt] = []
    i = start
    while i < len(lines):
        raw = lines[i].rstrip("\n")
        if not raw.strip():
            i += 1
            continue
        indent, content = _parse_indent(raw)
        if indent < base_indent:
            break
        if indent > base_indent:
            raise RailCompileError("缩进不合法", file, i + 1)
        stmt_text, next_i = _collect_logical_statement(lines, i, base_indent)
        norm = _normalize_colon(stmt_text.strip())
        no_comment = _strip_comment(norm).rstrip()
        if not no_comment:
            i = next_i
            continue
        origin = Origin(file=file, line=i + 1)

        if no_comment.startswith('"""'):
            text, i2 = _read_guidance(lines, i, file)
            stmts.append(Stmt(kind="guidance", origin=origin, text=text))
            i = i2
            continue

        if no_comment.startswith("include "):
            m = re.match(r"""^include\s+(['"])(.+?)\1$""", no_comment)
            if not m:
                raise RailCompileError("include 语法错误", file, i + 1)
            stmts.append(Stmt(kind="include", origin=origin, include_path=m.group(2)))
            i = next_i
            continue

        if no_comment.startswith("def ") and no_comment.endswith(":"):
            m = re.match(r"^def\s+([^\s(]+)\((.*)\):$", no_comment)
            if not m:
                raise RailCompileError("def 语法错误", file, i + 1)
            name = m.group(1)
            raw_params = m.group(2).strip()
            params = [] if not raw_params else [x.strip() for x in _split_args(raw_params)]
            body, i2 = _parse_child_scope(lines, i + 1, base_indent, file)
            stmts.append(Stmt(kind="def", origin=origin, name=name, params=params, body=body))
            i = i2
            continue

        if no_comment == "step:":
            step_items, i2 = _split_step_block_items(lines, i + 1, base_indent, file)
            if not step_items:
                raise RailCompileError("step 不能为空", file, i + 1)
            stmts.extend(step_items)
            i = i2
            continue

        if no_comment == "ask:":
            text_block, i2 = _collect_text_block(lines, i + 1, base_indent)
            if not text_block:
                raise RailCompileError("ask 不能为空", file, i + 1)
            stmts.append(Stmt(kind="ask", origin=origin, text=text_block))
            i = i2
            continue

        if no_comment.startswith("if ") and no_comment.endswith(":"):
            cond = no_comment[3:-1].strip()
            if not cond:
                raise RailCompileError("if 条件为空", file, i + 1)
            body, i2 = _parse_child_scope(lines, i + 1, base_indent, file)
            if_stmt = Stmt(kind="if", origin=origin, cond=cond, body=body)
            i = i2
            while i < len(lines):
                raw2 = lines[i].rstrip("\n")
                if not raw2.strip():
                    i += 1
                    continue
                indent2, content2 = _parse_indent(raw2)
                if indent2 != base_indent:
                    break
                norm2 = _normalize_colon(content2.strip())
                no_comment2 = _strip_comment(norm2).rstrip()
                if not no_comment2:
                    i += 1
                    continue
                if no_comment2.startswith("elif ") and no_comment2.endswith(":"):
                    cond2 = no_comment2[5:-1].strip()
                    body2, i = _parse_child_scope(lines, i + 1, base_indent, file)
                    if_stmt.elifs.append((cond2, body2, Origin(file=file, line=i)))
                    continue
                if no_comment2 == "else:":
                    body3, i = _parse_child_scope(lines, i + 1, base_indent, file)
                    if_stmt.orelse = body3
                    break
                break
            stmts.append(if_stmt)
            continue

        if no_comment.startswith("while ") and no_comment.endswith(":"):
            cond = no_comment[6:-1].strip()
            if not cond:
                raise RailCompileError("while 条件为空", file, i + 1)
            body, i2 = _parse_child_scope(lines, i + 1, base_indent, file)
            stmts.append(Stmt(kind="while", origin=origin, cond=cond, body=body))
            i = i2
            continue

        if no_comment.startswith("for ") and no_comment.endswith(":"):
            m = re.match(r"^for\s+([^\s]+)\s+in\s+(.+):$", no_comment, re.S)
            if not m:
                raise RailCompileError("for 语法错误", file, i + 1)
            iter_var = m.group(1).strip()
            iter_expr = m.group(2).strip()
            if not iter_var or not iter_expr:
                raise RailCompileError("for 语法错误：缺少迭代变量或迭代源", file, i + 1)
            body, i2 = _parse_child_scope(lines, i + 1, base_indent, file)
            stmts.append(Stmt(kind="for", origin=origin, iter_var=iter_var, iter_expr=iter_expr, body=body))
            i = i2
            continue

        if no_comment == "break":
            stmts.append(Stmt(kind="break", origin=origin))
            i = next_i
            continue
        if no_comment == "continue":
            stmts.append(Stmt(kind="continue", origin=origin))
            i = next_i
            continue
        if no_comment == "return":
            stmts.append(Stmt(kind="return", origin=origin))
            i = next_i
            continue

        call_m = re.match(r"^([^\s(]+)\((.*)\)$", no_comment, re.S)
        if call_m:
            stmts.append(
                Stmt(kind="call", origin=origin, name=call_m.group(1), args_text=call_m.group(2).strip())
            )
            i = next_i
            continue

        stmts.append(Stmt(kind="step", origin=origin, text=no_comment))
        i = next_i
    return stmts, i


def _parse_child_scope(lines: list[str], start: int, parent_indent: int, file: Path) -> tuple[list[Stmt], int]:
    i = start
    while i < len(lines):
        raw = lines[i].rstrip("\n")
        if not raw.strip():
            i += 1
            continue
        indent, _ = _parse_indent(raw)
        if indent <= parent_indent:
            return [], start
        return _parse_scope(lines, start, indent, file)
    return [], start


def _collect_text_block(lines: list[str], start: int, parent_indent: int) -> tuple[str, int]:
    i = start
    block: list[str] = []
    min_indent: int | None = None
    while i < len(lines):
        raw = lines[i].rstrip("\n")
        if not raw.strip():
            if block:
                block.append("")
            i += 1
            continue
        indent, content = _parse_indent(raw)
        if indent <= parent_indent:
            break
        min_indent = indent if min_indent is None else min(min_indent, indent)
        block.append(raw)
        i += 1
    if min_indent is None:
        return "", start
    normalized = []
    for raw in block:
        if not raw:
            normalized.append("")
            continue
        normalized.append(raw[min_indent:])
    return "\n".join(normalized).strip(), i


def _split_step_block_items(lines: list[str], start: int, parent_indent: int, file: Path) -> tuple[list[Stmt], int]:
    i = start
    while i < len(lines):
        raw = lines[i].rstrip("\n")
        if not raw.strip():
            i += 1
            continue
        indent, _ = _parse_indent(raw)
        if indent <= parent_indent:
            return [], start
        block_indent = indent
        break
    else:
        return [], start

    items: list[Stmt] = []
    text_buf: list[str] = []
    text_line: int | None = None

    def flush_text() -> None:
        nonlocal text_buf, text_line
        text = "\n".join(text_buf).strip()
        if text:
            items.append(
                Stmt(
                    kind="step",
                    origin=Origin(file=file, line=text_line or (start + 1)),
                    text=text,
                )
            )
        text_buf = []
        text_line = None

    cur = i
    while cur < len(lines):
        raw = lines[cur].rstrip("\n")
        if not raw.strip():
            if text_buf:
                text_buf.append("")
            cur += 1
            continue

        indent, _ = _parse_indent(raw)
        if indent < block_indent:
            break
        if indent > block_indent:
            if text_line is None:
                text_line = cur + 1
            text_buf.append(raw[block_indent:])
            cur += 1
            continue

        stmt_text, next_i = _collect_logical_statement(lines, cur, block_indent - 1)
        stmt_norm = _normalize_colon(stmt_text.strip())
        call_m = re.match(r"^([^\s(]+)\((.*)\)$", stmt_norm, re.S)
        if call_m:
            flush_text()
            items.append(
                Stmt(
                    kind="call",
                    origin=Origin(file=file, line=cur + 1),
                    name=call_m.group(1),
                    args_text=call_m.group(2).strip(),
                    from_step_block=True,
                )
            )
            cur = next_i
            continue

        if text_line is None:
            text_line = cur + 1
        text_buf.append(stmt_text.rstrip())
        cur = next_i

    flush_text()
    return items, cur


def _paren_unclosed(text: str) -> bool:
    depth = 0
    i = 0
    quote: str | None = None
    triple = False
    while i < len(text):
        ch = text[i]
        nxt3 = text[i : i + 3]
        if quote is not None:
            if triple and nxt3 == quote * 3:
                quote = None
                triple = False
                i += 3
                continue
            if not triple and ch == quote:
                quote = None
                i += 1
                continue
            i += 1
            continue
        if nxt3 in ("'''", '"""'):
            quote = nxt3[0]
            triple = True
            i += 3
            continue
        if ch in ("'", '"'):
            quote = ch
            triple = False
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        i += 1
    return depth > 0


def _collect_logical_statement(lines: list[str], start: int, base_indent: int) -> tuple[str, int]:
    first = lines[start].rstrip("\n")
    _, content = _parse_indent(first)
    text = content.rstrip()
    i = start + 1
    if not _paren_unclosed(text):
        return text, i
    parts = [text]
    while i < len(lines):
        raw = lines[i].rstrip("\n")
        if not raw.strip():
            parts.append("")
            i += 1
            continue
        indent, content = _parse_indent(raw)
        # When previous lines leave an unclosed "(", keep consuming continuation
        # lines even if they are at the same indent level.
        if indent <= base_indent and not _paren_unclosed("\n".join(parts)):
            break
        parts.append(content if indent > 0 else raw)
        if not _paren_unclosed("\n".join(parts)):
            return "\n".join(parts), i + 1
        i += 1
    return "\n".join(parts), i


def _stmt_to_text(stmt: Stmt) -> str:
    if stmt.kind in {"step", "ask", "guidance"}:
        return stmt.text
    if stmt.kind == "call":
        return f"{stmt.name}({stmt.args_text})"
    if stmt.kind == "include":
        return f'include "{stmt.include_path}"'
    return stmt.kind


@dataclass
class FuncDef:
    name: str
    params: list[str]
    body: list[Stmt]
    origin: Origin


class RailCompiler:
    def __init__(self, consts: dict[str, Any] = None) -> None:
        self.nodes: dict[str, dict] = {}
        self.next_id = 0
        self.sources: dict[str, float] = {}
        self.include_stack: list[Path] = []
        self.functions: dict[str, FuncDef] = {}
        self.root_dir = Path.cwd().resolve()
        self._synthetic_counter = 0
        self.warnings: list[dict] = []
        self._warning_keys: set[tuple[str, int, str]] = set()
        self.invocation_consts = consts or {}
        self.default_consts: dict[str, Any] = {}
        self.merged_consts: dict[str, Any] = {}

    def alloc(self) -> str:
        nid = str(self.next_id)
        self.next_id += 1
        return nid

    def _evaluate_ast_node(self, node: Any) -> Any:
        import ast
        if isinstance(node, ast.Name):
            if node.id in self.merged_consts:
                return self.merged_consts[node.id]
            if node.id == "True":
                return True
            if node.id == "False":
                return False
            if node.id == "None":
                return None
            raise NameError(f"Name {node.id} not defined")
        
        # Support for Constant in Python 3.8+
        if hasattr(ast, "Constant") and isinstance(node, ast.Constant):
            return node.value
            
        # Support for older Python versions
        if hasattr(ast, "Num") and isinstance(node, ast.Num):
            return node.n
        if hasattr(ast, "Str") and isinstance(node, ast.Str):
            return node.s
        if hasattr(ast, "NameConstant") and isinstance(node, ast.NameConstant):
            return node.value
            
        if isinstance(node, ast.UnaryOp):
            operand = self._evaluate_ast_node(node.operand)
            if isinstance(node.op, ast.Not):
                return not operand
            elif isinstance(node.op, ast.UAdd):
                return +operand
            elif isinstance(node.op, ast.USub):
                return -operand
            raise TypeError("Unsupported UnaryOp")
            
        if isinstance(node, ast.BinOp):
            left = self._evaluate_ast_node(node.left)
            right = self._evaluate_ast_node(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            elif isinstance(node.op, ast.Sub):
                return left - right
            elif isinstance(node.op, ast.Mult):
                return left * right
            elif isinstance(node.op, ast.Div):
                return left / right
            raise TypeError("Unsupported BinOp")
            
        if isinstance(node, ast.BoolOp):
            values = [self._evaluate_ast_node(val) for val in node.values]
            if isinstance(node.op, ast.And):
                return all(values)
            elif isinstance(node.op, ast.Or):
                return any(values)
            raise TypeError("Unsupported BoolOp")
            
        if isinstance(node, ast.Compare):
            left = self._evaluate_ast_node(node.left)
            right = self._evaluate_ast_node(node.comparators[0])
            op = node.ops[0]
            if isinstance(op, ast.Eq):
                return left == right
            elif isinstance(op, ast.NotEq):
                return left != right
            elif isinstance(op, ast.Lt):
                return left < right
            elif isinstance(op, ast.LtE):
                return left <= right
            elif isinstance(op, ast.Gt):
                return left > right
            elif isinstance(op, ast.GtE):
                return left >= right
            raise TypeError("Unsupported Compare")
            
        raise TypeError("Unsupported AST node")

    def _evaluate_expr(self, expr_str: str) -> tuple[bool, Any]:
        import ast
        try:
            tree = ast.parse(expr_str, mode='eval')
            val = self._evaluate_ast_node(tree.body)
            return True, val
        except Exception:
            return False, None

    def parse_file(self, path: Path) -> list[Stmt]:
        rp = path.resolve()
        if rp in self.include_stack:
            chain = " -> ".join(str(x) for x in (self.include_stack + [rp]))
            raise RailCompileError(f"include 循环引用: {chain}")
        if not rp.exists():
            raise RailCompileError("include 文件不存在", rp, 1)
        self.include_stack.append(rp)
        rel = self._to_relative(rp)
        self.sources[rel] = rp.stat().st_mtime
        text = rp.read_text(encoding="utf-8")
        lines = text.splitlines()

        # Parse params(...) signature at the very top of the root file
        if len(self.include_stack) == 1:
            params_line_idx = -1
            default_consts = {}
            for idx, line in enumerate(lines):
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("#"):
                    continue
                if stripped.startswith("params(") and stripped.endswith(")"):
                    params_line_idx = idx
                    args_text = stripped[7:-1].strip()
                    if args_text:
                        parts = _split_args(args_text)
                        for p in parts:
                            if "=" in p:
                                k, v = p.split("=", 1)
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
                                default_consts[k] = v
                break
            if params_line_idx != -1:
                self.default_consts.update(default_consts)
                # Clear the signature line but preserve line numbers
                lines[params_line_idx] = ""

        stmts, _ = _parse_scope(lines, 0, 0, rp)
        expanded = self._expand_includes_in_stmts(stmts, rp)
        self.include_stack.pop()
        return expanded

    def _expand_includes_in_stmts(self, stmts: list[Stmt], base_file: Path) -> list[Stmt]:
        out: list[Stmt] = []
        for s in stmts:
            if s.kind == "include":
                inc = Path(s.include_path)
                target = inc if inc.is_absolute() else (base_file.parent / inc)
                try:
                    out.extend(self.parse_file(target))
                except RailCompileError as e:
                    ref = f"{s.origin.file}:{s.origin.line}"
                    raise RailCompileError(e.message, e.file, e.line, [ref] + e.references) from e
                continue

            # Recursively expand includes inside nested blocks, regardless of runtime control flow.
            s.body = self._expand_includes_in_stmts(s.body, base_file) if s.body else s.body
            s.orelse = self._expand_includes_in_stmts(s.orelse, base_file) if s.orelse else s.orelse
            if s.elifs:
                new_elifs: list[tuple[str, list[Stmt], Origin]] = []
                for cond, body, origin in s.elifs:
                    new_elifs.append((cond, self._expand_includes_in_stmts(body, base_file), origin))
                s.elifs = new_elifs
            out.append(s)
        return out

    def _to_relative(self, path: Path) -> str:
        try:
            return os.path.relpath(str(path.resolve()), str(self.root_dir))
        except Exception:
            return str(path.resolve())

    def _meta_file(self, path: Path) -> str:
        return self._to_relative(path)

    def _add_warning(self, file: Path, line: int, message: str) -> None:
        rel = self._meta_file(file)
        key = (rel, line, message)
        if key in self._warning_keys:
            return
        self._warning_keys.add(key)
        self.warnings.append({"file": rel, "line": line, "message": message})

    def _new_sentinel(self, prefix: str) -> str:
        self._synthetic_counter += 1
        return f"__{prefix}_{self._synthetic_counter}__"

    def _register_defs(self, stmts: list[Stmt]) -> list[Stmt]:
        out: list[Stmt] = []
        for s in stmts:
            if s.kind == "def":
                self.functions[s.name] = FuncDef(name=s.name, params=s.params, body=s.body, origin=s.origin)
            else:
                out.append(s)
        return out

    def _replace_vars(self, text: str, env: dict[str, str]) -> str:
        out = text
        for k, v in env.items():
            out = out.replace("{{" + k + "}}", v)
        return out

    def _instantiate_stmt(self, s: Stmt, env: dict[str, str]) -> Stmt:
        c = Stmt(
            kind=s.kind,
            origin=s.origin,
            text=self._replace_vars(s.text, env),
            cond=self._replace_vars(s.cond, env),
            body=[],
            orelse=[],
            elifs=[],
            name=s.name,
            params=list(s.params),
            args_text=self._replace_vars(s.args_text, env),
            include_path=s.include_path,
            iter_var=s.iter_var,
            iter_expr=self._replace_vars(s.iter_expr, env),
        )
        c.body = [self._instantiate_stmt(x, env) for x in s.body]
        c.orelse = [self._instantiate_stmt(x, env) for x in s.orelse]
        c.elifs = [(self._replace_vars(cc, env), [self._instantiate_stmt(x, env) for x in bb], oo) for cc, bb, oo in s.elifs]
        return c

    def _parse_call_args(self, call: Stmt, fn: FuncDef) -> dict[str, str]:
        raw_parts = _split_args(call.args_text) if call.args_text else []
        # Compatibility rule with boundary:
        # If a keyword argument value contains unquoted top-level commas, fragments may be split.
        # We only merge orphan fragments back to the nearest previous keyword argument value.
        # If there is no safe merge target, fail fast instead of silent rewrite.
        merged_parts: list[str] = []
        last_kw_idx: int | None = None
        last_kw_name: str | None = None
        merged_orphans = 0
        for p in raw_parts:
            if "=" in p:
                merged_parts.append(p)
                last_kw_idx = len(merged_parts) - 1
                last_kw_name = p.split("=", 1)[0].strip() or None
                continue
            if not merged_parts:
                merged_parts.append(p)
                continue
            if last_kw_idx is not None:
                merged_parts[last_kw_idx] = merged_parts[last_kw_idx] + "，" + p
                merged_orphans += 1
                continue
            merged_parts.append(p)
        raw_parts = merged_parts
        if merged_orphans > 0:
            self._add_warning(
                call.origin.file,
                call.origin.line,
                (
                    f"函数 `{fn.name}` 的参数 `{last_kw_name or '<unknown>'}` 可能缺少引号："
                    "检测到未加引号的逗号片段，编译器已自动并回前一个命名参数值。"
                    "建议改为显式字符串，示例："
                    f"`{last_kw_name or 'arg'}=\"...，...\"` 或 "
                    f"`{last_kw_name or 'arg'}='''...，...'''`。"
                ),
            )
        pos: list[str] = []
        kw: dict[str, str] = {}
        seen_kw = False
        for p in raw_parts:
            if "=" in p:
                k, v = p.split("=", 1)
                kw[k.strip()] = v.strip()
                seen_kw = True
            else:
                if seen_kw:
                    raise RailCompileError(
                        f"函数 {fn.name} 参数解析失败：命名参数后出现裸值片段。请为含逗号的参数值加引号或三引号。",
                        call.origin.file,
                        call.origin.line,
                    )
                pos.append(p.strip())
        if len(pos) > len(fn.params):
            raise RailCompileError(f"函数 {fn.name} 参数过多", call.origin.file, call.origin.line)
        env: dict[str, str] = {}
        for idx, p in enumerate(pos):
            env[fn.params[idx]] = p
        for k, v in kw.items():
            if k not in fn.params:
                raise RailCompileError(f"函数 {fn.name} 未定义参数: {k}", call.origin.file, call.origin.line)
            if k in env:
                raise RailCompileError(f"函数 {fn.name} 参数重复赋值: {k}", call.origin.file, call.origin.line)
            env[k] = v
        for p in fn.params:
            if p not in env:
                raise RailCompileError(f"函数 {fn.name} 缺少参数: {p}", call.origin.file, call.origin.line)
        return env

    def _compile_stmt_list(
        self,
        stmts: list[Stmt],
        after: str,
        loop_head: str | None,
        loop_exit: str | None,
        call_stack: list[str],
    ) -> str:
        next_id = after
        for s in reversed(stmts):
            next_id = self._compile_stmt(s, next_id, loop_head, loop_exit, call_stack)
        return next_id

    def _compile_if(self, s: Stmt, after: str, loop_head: str | None, loop_exit: str | None, call_stack: list[str]) -> str:
        # Build the initial chain of branches: (condition_str_or_None, body, origin)
        raw_chain: list[tuple[str | None, list[Stmt], Origin]] = []
        raw_chain.append((s.cond, s.body, s.origin))
        for cond, body, origin in s.elifs:
            raw_chain.append((cond, body, origin))
        if s.orelse:
            raw_chain.append((None, s.orelse, s.origin))

        # Filter the chain using compile-time constants
        filtered: list[tuple[str | None, list[Stmt], Origin]] = []
        for cond, body, origin in raw_chain:
            if cond is None:
                filtered.append((None, body, origin))
                break
            
            is_const, val = self._evaluate_expr(cond)
            if is_const:
                if val:
                    filtered.append((None, body, origin))
                    break
                else:
                    continue
            else:
                filtered.append((cond, body, origin))

        # Compile the filtered chain
        if not filtered:
            return after

        if filtered[0][0] is None:
            return self._compile_stmt_list(filtered[0][1], after, loop_head, loop_exit, call_stack)

        if filtered[-1][0] is None:
            fallback_body = filtered[-1][1]
            fallback = self._compile_stmt_list(fallback_body, after, loop_head, loop_exit, call_stack)
            filtered_elifs = filtered[1:-1]
        else:
            fallback = after
            filtered_elifs = filtered[1:]

        on_false = fallback
        for cond, body, origin in reversed(filtered_elifs):
            branch_id = self.alloc()
            on_true = self._compile_stmt_list(body, after, loop_head, loop_exit, call_stack)
            self.nodes[branch_id] = {
                "type": "Branch",
                "condition": cond,
                "on_true": on_true,
                "on_false": on_false,
                "meta": {"file": self._meta_file(origin.file), "line": origin.line},
            }
            on_false = branch_id

        cond0, body0, origin0 = filtered[0]
        branch_id = self.alloc()
        on_true = self._compile_stmt_list(body0, after, loop_head, loop_exit, call_stack)
        self.nodes[branch_id] = {
            "type": "Branch",
            "condition": cond0,
            "on_true": on_true,
            "on_false": on_false,
            "meta": {"file": self._meta_file(origin0.file), "line": origin0.line},
        }
        return branch_id

    def _compile_stmt(
        self,
        s: Stmt,
        after: str,
        loop_head: str | None,
        loop_exit: str | None,
        call_stack: list[str],
    ) -> str:
        if s.kind == "step":
            nid = self.alloc()
            self.nodes[nid] = {
                "type": "Step",
                "instruction": s.text,
                "next": after,
                "meta": {"file": self._meta_file(s.origin.file), "line": s.origin.line},
            }
            return nid
        if s.kind == "guidance":
            nid = self.alloc()
            self.nodes[nid] = {
                "type": "Guidance",
                "instruction": s.text,
                "next": after,
                "meta": {"file": self._meta_file(s.origin.file), "line": s.origin.line},
            }
            return nid
        if s.kind == "ask":
            nid = self.alloc()
            self.nodes[nid] = {
                "type": "Ask",
                "instruction": s.text,
                "next": after,
                "meta": {"file": self._meta_file(s.origin.file), "line": s.origin.line},
            }
            return nid
        if s.kind == "if":
            return self._compile_if(s, after, loop_head, loop_exit, call_stack)
        if s.kind == "while":
            if s.cond == "True":
                loop_back = self._new_sentinel("while_true_loop_back")
                body_entry = self._compile_stmt_list(s.body, loop_back, loop_back, after, call_stack)
                for node in self.nodes.values():
                    if node.get("next") == loop_back:
                        node["next"] = body_entry
                    if node.get("on_true") == loop_back:
                        node["on_true"] = body_entry
                    if node.get("on_false") == loop_back:
                        node["on_false"] = body_entry
                sid = self.alloc()
                self.nodes[sid] = {
                    "type": "Guidance",
                    "instruction": "准备开始循环执行",
                    "next": body_entry,
                    "meta": {"file": self._meta_file(s.origin.file), "line": s.origin.line},
                }
                return sid
            hid = self.alloc()
            body_entry = self._compile_stmt_list(s.body, hid, hid, after, call_stack)
            self.nodes[hid] = {
                "type": "Branch",
                "condition": s.cond,
                "on_true": body_entry,
                "on_false": after,
                "meta": {"file": self._meta_file(s.origin.file), "line": s.origin.line},
            }
            return hid
        if s.kind == "for":
            hid = self.alloc()
            body_entry = self._compile_stmt_list(s.body, hid, hid, after, call_stack)
            self.nodes[hid] = {
                "type": "For",
                "items_expr": s.iter_expr,
                "items": [],
                "item_key": s.iter_var,
                "index_key": f"{s.iter_var}_index",
                "on_iterate": body_entry,
                "on_done": after,
                "meta": {"file": self._meta_file(s.origin.file), "line": s.origin.line},
            }
            return hid
        if s.kind == "break":
            if loop_exit is None:
                raise RailCompileError("break 出现在循环外", s.origin.file, s.origin.line)
            return loop_exit
        if s.kind == "continue":
            if loop_head is None:
                raise RailCompileError("continue 出现在循环外", s.origin.file, s.origin.line)
            return loop_head
        if s.kind == "return":
            return after
        if s.kind == "call":
            if s.from_step_block:
                self._add_warning(
                    s.origin.file,
                    s.origin.line,
                    "step 块内函数调用已自动拆分为独立调用并展开。",
                )
            if s.name not in self.functions:
                raise RailCompileError(f"未定义函数: {s.name}", s.origin.file, s.origin.line)
            if s.name in call_stack:
                chain = " -> ".join(call_stack + [s.name])
                raise RailCompileError(f"函数调用循环引用: {chain}", s.origin.file, s.origin.line)
            fn = self.functions[s.name]
            env = self._parse_call_args(s, fn)
            instantiated = [self._instantiate_stmt(x, env) for x in fn.body]
            body = self._register_defs(instantiated)
            try:
                return self._compile_stmt_list(body, after, loop_head, loop_exit, call_stack + [s.name])
            except RailCompileError as e:
                ref = f"{s.origin.file}:{s.origin.line}"
                raise RailCompileError(e.message, e.file, e.line, [ref] + e.references) from e
        if s.kind in {"include", "def"}:
            return after
        raise RailCompileError(f"不支持的语句类型: {s.kind}", s.origin.file, s.origin.line)

    def _renumber_nodes(self, entry: str, nodes: dict) -> tuple[str, dict]:
        """BFS from entry, reassign node IDs in traversal order (1, 2, 3, …).
        Finished nodes are placed last. Returns (new_entry, new_nodes)."""
        from collections import deque

        visited: list[str] = []
        seen: set[str] = set()
        queue: deque[str] = deque([entry])
        # Collect Finished nodes separately so they always come last.
        finished_ids: list[str] = []

        while queue:
            nid = queue.popleft()
            if nid in seen:
                continue
            seen.add(nid)
            node = nodes.get(nid)
            if node is None:
                continue
            if node.get("type") == "Finished":
                finished_ids.append(nid)
            else:
                visited.append(nid)
            # Enqueue successors in forward order.
            for key in ("next", "on_true", "on_false", "on_iterate", "on_done"):
                succ = node.get(key)
                if succ is not None and succ not in seen:
                    queue.append(succ)

        ordered = visited + finished_ids

        # Build old -> new mapping (1-based).
        old_to_new: dict[str, str] = {old: str(i + 1) for i, old in enumerate(ordered)}

        def remap(v: str | None) -> str | None:
            if v is None:
                return None
            return old_to_new.get(v, v)

        new_nodes: dict[str, dict] = {}
        for old_id in ordered:
            new_id = old_to_new[old_id]
            old_node = nodes[old_id]
            new_node = dict(old_node)
            for key in ("next", "on_true", "on_false", "on_iterate", "on_done"):
                if key in new_node:
                    new_node[key] = remap(new_node[key])
            new_nodes[new_id] = new_node

        new_entry = old_to_new.get(entry, entry)
        return new_entry, new_nodes

    def compile(self, input_path: Path) -> dict:
        self.default_consts = {}
        self.merged_consts = {}
        
        stmts = self.parse_file(input_path.resolve())
        
        # Validate that invocation constants match defined params
        for k in self.invocation_consts:
            if k not in self.default_consts:
                raise RailCompileError(
                    f"未定义编译期参数: {k}。可用参数: {list(self.default_consts.keys())}"
                )
        
        # Merge invocation consts over default consts
        self.merged_consts = {**self.default_consts, **self.invocation_consts}
        
        executable = self._register_defs(stmts)
        finished_id = self.alloc()
        self.nodes[finished_id] = {"type": "Finished", "instruction": "所有指令已执行完毕。结束输出。"}
        entry = self._compile_stmt_list(executable, finished_id, None, None, [])

        # Renumber nodes so IDs align with execution order (entry == "1").
        entry, self.nodes = self._renumber_nodes(entry, self.nodes)

        return {
            "protocol": "next-step-cfg/v1",
            "entry": entry,
            "nodes": self.nodes,
            "meta": {
                "sources": dict(sorted(self.sources.items())),
                "warnings": self.warnings,
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile RAIL into CFG JSON")
    parser.add_argument("--input", required=True, help="Input .rail file")
    parser.add_argument("--output", required=True, help="Output .json file")
    parser.add_argument("--const", action="append", default=[], help="Compile-time constants (e.g. name=value)")
    args = parser.parse_args()

    consts = {}
    for item in args.const:
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
            consts[k] = v

    compiler = RailCompiler(consts=consts)
    try:
        cfg = compiler.compile(Path(args.input))
    except RailCompileError as e:
        err: dict[str, object] = {
            "type": "CompileError",
            "错误原因": e.message,
            "错误位置": f"{e.file}:{e.line}" if e.file and e.line else None,
        }
        if e.references:
            err["引用位置"] = e.references
        print(json.dumps(err, ensure_ascii=False, indent=2))
        return 2

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "type": "ok",
                "output": str(out.resolve()),
                "nodes": len(cfg["nodes"]),
                "warnings": len(cfg.get("meta", {}).get("warnings", [])),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
