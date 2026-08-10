from __future__ import annotations

import ast
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


def _frontend_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in sorted([*FRONTEND.glob("*.js"), *FRONTEND.glob("*.html")])
    )


def _python_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _call_path(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_path(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _defined_functions(tree: ast.AST) -> set[str]:
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_frontend_does_not_ship_server_processing_or_wordlist_access() -> None:
    """Keep screenshot/OCR, solving and dictionary access exclusively in Python."""
    source = _frontend_source()
    server_only_identifiers = (
        "extract_board",
        "generate_moves",
        "load_wordlist",
        "suggest_words",
        "add_words_to_wordlist",
        "remove_word_from_wordlist",
        "gaddag",
        "tesseract",
        "pytesseract",
        "openrouter",
        "wordlist",
        "fileReader",
        "canvas",
    )

    for identifier in server_only_identifiers:
        assert not re.search(rf"\b{re.escape(identifier.lower())}\b", source), (
            f"server-only identifier {identifier!r} leaked into frontend assets"
        )


def test_server_owns_screenshot_solving_and_wordlist_operations() -> None:
    """Make the intended ownership visible in the application entrypoint."""
    app_tree = _python_tree(ROOT / "app.py")
    vision_tree = _python_tree(ROOT / "wordfeud_analyzer" / "vision.py")
    move_tree = _python_tree(ROOT / "wordfeud_analyzer" / "move_generator.py")
    app_calls = {_call_path(call.func) for call in _calls(app_tree)}

    assert "st.file_uploader" in app_calls
    assert "extract_board" in app_calls
    assert "generate_moves" in app_calls
    assert "load_wordlist" in app_calls
    assert any(module == "PIL" or module.startswith("PIL.") for module in _imported_modules(vision_tree))
    assert "requests" in _imported_modules(vision_tree)
    assert {"generate_moves", "load_wordlist"} <= _defined_functions(move_tree)


def test_ocr_words_require_explicit_dictionary_confirmation() -> None:
    app_tree = _python_tree(ROOT / "app.py")
    calls = _calls(app_tree)

    assert any(
        _call_path(call.func) == "suggest_words"
        and call.args
        and isinstance(call.args[0], ast.Call)
        and _call_path(call.args[0].func) == "board_words"
        for call in calls
    )
    assert any(
        _call_path(call.func) == "st.form"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == "ocr_word_suggestions"
        for call in calls
    )
    assert any(
        _call_path(call.func) == "st.form_submit_button"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == "Voeg geselecteerde woorden toe"
        for call in calls
    )
    assert any(
        _call_path(call.func) == "add_words_to_wordlist"
        and call.args
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "selected"
        for call in calls
    )
    assert "learn_words" not in {_call_path(call.func) for call in calls}
