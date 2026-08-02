from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


def _frontend_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in sorted([*FRONTEND.glob("*.js"), *FRONTEND.glob("*.html")])
    )


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
        assert identifier.lower() not in source, (
            f"server-only identifier {identifier!r} leaked into frontend assets"
        )


def test_server_owns_screenshot_solving_and_wordlist_operations() -> None:
    """Make the intended ownership visible in the application entrypoint."""
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    vision_source = (ROOT / "wordfeud_analyzer" / "vision.py").read_text(encoding="utf-8")
    move_source = (ROOT / "wordfeud_analyzer" / "move_generator.py").read_text(encoding="utf-8")

    assert "st.file_uploader" in app_source
    assert "extract_board(" in app_source
    assert "generate_moves(" in app_source
    assert "load_wordlist(" in app_source
    assert "PIL" in vision_source
    assert "requests" in vision_source
    assert "def generate_moves" in move_source
    assert "def load_wordlist" in move_source


def test_ocr_words_require_explicit_dictionary_confirmation() -> None:
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")

    assert "suggest_words(board_words(extraction.state)" in app_source
    assert 'with st.form("ocr_word_suggestions")' in app_source
    assert 'st.form_submit_button("Voeg geselecteerde woorden toe")' in app_source
    assert "add_words_to_wordlist(selected" in app_source
    assert "learn_words" not in app_source
