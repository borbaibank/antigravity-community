"""Shared transform helpers for move-only bot/core.py domain extraction."""

from __future__ import annotations

import ast
import re


def function_names(chunk: str) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(chunk).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names


def collect_exports(chunk: str) -> list[str]:
    exports: list[str] = []
    for node in ast.parse(chunk).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            exports.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    exports.append(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            exports.append(node.target.id)
    return exports


DEFAULT_BARE_CORE_CALLS = (
    "save_state",
    "load_state",
    "record_error_event",
    "send_telegram",
    "close_position",
    "trigger_income_sync",
    "execute_entry",
    "get_klines",
    "_finalize_exit_notify",
    "_reconcile_pnl_from_binance",
    "purge_orphaned_algo_orders",
)


def transform_chunk(
    chunk: str,
    local_funcs: set[str],
    *,
    core_attrs: tuple[str, ...] = (),
    config_via_core: tuple[str, ...] = (),
    accessor_via_core: tuple[str, ...] = (),
    preserve_nested_calls: bool = False,
    bare_core_calls: tuple[str, ...] = DEFAULT_BARE_CORE_CALLS,
) -> str:
    chunk = re.sub(r"^\s*global ([^\n]+)\n", "", chunk, flags=re.MULTILINE)

    nested_funcs: set[str] = set()
    if preserve_nested_calls:
        for m in re.finditer(r"^(\s+)(async )?def (_[a-zA-Z]\w*)\(", chunk, re.MULTILINE):
            nested_funcs.add(m.group(3))

    placeholders: dict[str, tuple[str, str, str]] = {}

    def stash_def(match: re.Match[str]) -> str:
        key = f"__DEF_{len(placeholders)}__"
        placeholders[key] = (match.group(1) or "", match.group(2), match.group(3))
        return key

    chunk = re.sub(
        r"^(async )?def ([a-zA-Z_]\w*)(\([^)]*\))(?:\s*->[^\n]+)?:\n",
        stash_def,
        chunk,
        flags=re.MULTILINE,
    )

    for attr in core_attrs + config_via_core + accessor_via_core:
        chunk = re.sub(rf"(?<!c\.)\b{re.escape(attr)}\b", f"c.{attr}", chunk)

    chunk = re.sub(r"(?<![a-zA-Z_])state(?![a-zA-Z_])", "c.state", chunk)

    def repl_helper(match: re.Match[str]) -> str:
        name = match.group(1)
        if preserve_nested_calls and f"_{name}" in nested_funcs:
            return f"_{name}("
        return f"c._{name}("

    chunk = re.sub(
        r"(?<!def )(?<!async )(?<![.\w])_([a-zA-Z]\w*)\(",
        repl_helper,
        chunk,
    )

    for name in bare_core_calls:
        chunk = re.sub(
            rf"(?<!c\.)(?<![.\w]){re.escape(name)}\(",
            f"c.{name}(",
            chunk,
        )

    for key, (async_kw, name, args) in placeholders.items():
        chunk = chunk.replace(key, f"{async_kw}def {name}{args}:\n    c = _core()\n")

    for attr in core_attrs:
        chunk = re.sub(
            rf"^(\s*){re.escape(attr)}(\s*=)",
            rf"\1c.{attr}\2",
            chunk,
            flags=re.MULTILINE,
        )

    return chunk


def build_import_block(module: str, exports: list[str]) -> str:
    lines = [f"from {module} import ("]
    for name in exports:
        lines.append(f"    {name},")
    lines.append(")\n\n")
    return "\n".join(lines)


def slice_by_markers(lines: list[str], start_marker: str, end_marker: str) -> tuple[str, int, int]:
    start = next(i for i, l in enumerate(lines) if l.startswith(start_marker))
    end = next(i for i, l in enumerate(lines) if l.startswith(end_marker))
    return "".join(lines[start:end]), start, end


def apply_domain_extraction(
    core_path,
    out_path,
    *,
    import_module: str,
    import_check: str,
    start_marker: str,
    end_marker: str,
    header: str,
    core_attrs: tuple[str, ...] = (),
    config_via_core: tuple[str, ...] = (),
    accessor_via_core: tuple[str, ...] = (),
    extra_exports: list[str] | None = None,
    insert_before: str | None = None,
    preserve_nested_calls: bool = False,
    bare_core_calls: tuple[str, ...] = DEFAULT_BARE_CORE_CALLS,
) -> bool:
    """Extract one domain chunk from core into out_path. Returns True if applied."""
    from pathlib import Path

    core_path = Path(core_path)
    out_path = Path(out_path)
    text = core_path.read_text(encoding="utf-8")
    if import_check in text:
        print(f"Skip {out_path.name} — already imported in core")
        return False

    lines = text.splitlines(keepends=True)
    chunk, start, end = slice_by_markers(lines, start_marker, end_marker)
    local_funcs = function_names(chunk)
    module = header + transform_chunk(
        chunk,
        local_funcs,
        core_attrs=core_attrs,
        config_via_core=config_via_core,
        accessor_via_core=accessor_via_core,
        preserve_nested_calls=preserve_nested_calls,
        bare_core_calls=bare_core_calls,
    ).rstrip() + "\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(module, encoding="utf-8")

    exports = collect_exports(chunk)
    if extra_exports:
        exports.extend(extra_exports)
    import_block = build_import_block(import_module, exports)

    new_lines = lines[:start] + lines[end:]
    if insert_before:
        idx = next(i for i, l in enumerate(new_lines) if l.startswith(insert_before))
        new_lines = new_lines[:idx] + [import_block] + new_lines[idx:]
    else:
        new_lines = new_lines[:start] + [import_block] + new_lines[start:]

    core_path.write_text("".join(new_lines), encoding="utf-8")
    print(f"Wrote {out_path} ({len(module.splitlines())} lines, {len(exports)} exports)")
    return True
