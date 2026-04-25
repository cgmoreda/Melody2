from __future__ import annotations

import ast
from pathlib import Path


def _is_command_decorator(decorator: ast.expr) -> bool:
    if not isinstance(decorator, ast.Call):
        return False
    func = decorator.func
    return isinstance(func, ast.Attribute) and func.attr in {"command", "group", "hybrid_command", "hybrid_group"}


def _has_help_keywords(decorator: ast.expr) -> bool:
    if not isinstance(decorator, ast.Call):
        return False
    for keyword in decorator.keywords:
        if keyword.arg not in {"brief", "help"}:
            continue
        value = keyword.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value.strip():
            return True
        if isinstance(value, ast.JoinedStr) and value.values:
            return True
    return False


def _command_functions(module: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    commands: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(module):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(_is_command_decorator(decorator) for decorator in node.decorator_list):
            commands.append(node)
    return commands


def test_command_handlers_have_descriptions_for_help_output() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    cogs_dir = repo_root / "cogs"

    missing: list[str] = []
    for path in sorted(cogs_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in _command_functions(module):
            doc = ast.get_docstring(node)
            has_doc = bool(doc and doc.strip())
            has_keywords = any(_has_help_keywords(decorator) for decorator in node.decorator_list)
            if not has_doc and not has_keywords:
                missing.append(f"{path.name}:{node.name}")

    assert missing == []
