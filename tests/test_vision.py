import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from wordfeud_analyzer.vision import (
    MINIMUM_CONFIDENCE,
    CompactVisionState,
    LowConfidenceError,
    VisionExtractionError,
    _align_compact_to_visible_tiles,  # pyright: ignore[reportPrivateUsage]
    _locate_board_top,  # pyright: ignore[reportPrivateUsage]
    _to_board_state,  # pyright: ignore[reportPrivateUsage]
    detect_visible_bonuses,
    detect_visible_tiles,
    extract_board,
    wordfeud_crops,
)


def test_wordfeud_crops_produce_square_board_and_rack(tmp_path: Path) -> None:
    screenshot = tmp_path / "screenshot.jpg"
    Image.new("RGB", (588, 1275), "black").save(screenshot)
    board, rack = wordfeud_crops(screenshot)
    assert board.startswith("data:image/jpeg;base64,")
    assert rack.startswith("data:image/jpeg;base64,")


def test_compact_vision_response_becomes_full_board_state() -> None:
    compact = CompactVisionState.model_validate({
        "rows": ["." * 15 for _ in range(7)] + ["." * 7 + "A" + "." * 7] + ["." * 15 for _ in range(7)],
        "rack": ["T", "E", "S", "T"],
        "blanks": [(7, 7)],
        "confidence": 97,
    })
    bonuses = [["NORMAL" for _ in range(15)] for _ in range(15)]
    bonuses[0][0] = "TW"
    state = _to_board_state(compact, bonuses)
    assert state.grid[0][0].bonus == "TW"
    assert state.grid[7][7].letter == "A"
    assert state.grid[7][7].bonus == "NORMAL"
    assert state.grid[7][7].is_blank


def test_detect_visible_bonuses_reads_each_coordinate_without_a_board_pattern(tmp_path: Path) -> None:
    screenshot = tmp_path / "screenshot.png"
    width, height, board_top = 588, 1275, round(1275 * 0.296)
    image = Image.new("RGB", (width, height), (42, 45, 52))
    draw = ImageDraw.Draw(image)
    colours = {"DL": (106, 142, 78), "TL": (29, 99, 150), "DW": (206, 113, 10), "TW": (152, 39, 45)}
    for (row, col), bonus in { (0, 0): "DL", (1, 13): "TL", (9, 4): "DW", (14, 14): "TW" }.items():
        draw.rectangle((int(col * width / 15), board_top + int(row * width / 15),
                        int((col + 1) * width / 15), board_top + int((row + 1) * width / 15)), fill=colours[bonus])
    image.save(screenshot)
    detected = detect_visible_bonuses(screenshot)
    assert detected[0][0] == "DL"
    assert detected[1][13] == "TL"
    assert detected[9][4] == "DW"
    assert detected[14][14] == "TW"
    assert detected[4][4] == "NORMAL"


def test_board_crop_is_located_from_grid_instead_of_fixed_screen_ratio(tmp_path: Path) -> None:
    screenshot = tmp_path / "variable-header.png"
    width, height, board_top = 600, 1200, 300
    image = Image.new("RGB", (width, height), (60, 60, 60))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, board_top, width - 1, board_top + width - 1), fill=(47, 52, 55))
    for boundary in range(16):
        x = min(width - 1, round(boundary * width / 15))
        draw.line((x, board_top, x, board_top + width - 1), fill=(25, 27, 29), width=3)
    draw.rectangle((1, board_top + 1, width // 15 - 2, board_top + width // 15 - 2),
                   fill=(106, 142, 78))
    tile_row, tile_col = 9, 3
    draw.rectangle((
        int(tile_col * width / 15) + 4,
        board_top + int(tile_row * width / 15) + 4,
        int((tile_col + 1) * width / 15) - 4,
        board_top + int((tile_row + 1) * width / 15) - 4,
    ), fill=(225, 220, 210))
    image.save(screenshot)

    assert _locate_board_top(image) == board_top
    assert detect_visible_bonuses(screenshot)[0][0] == "DL"
    assert (tile_row, tile_col) in detect_visible_tiles(screenshot)


def test_visible_tile_alignment_corrects_a_consistent_vision_row_offset(tmp_path: Path) -> None:
    screenshot = tmp_path / "screenshot.png"
    width, height, board_top = 588, 1275, round(1275 * 0.296)
    image = Image.new("RGB", (width, height), (42, 45, 52))
    draw = ImageDraw.Draw(image)
    for row, col in [(6, 7), (7, 7), (8, 7), (9, 3), (9, 4), (9, 5), (9, 6), (9, 7)]:
        draw.rectangle((int(col * width / 15) + 3, board_top + int(row * width / 15) + 3,
                        int((col + 1) * width / 15) - 3, board_top + int((row + 1) * width / 15) - 3),
                       fill=(225, 220, 210))
    image.save(screenshot)
    compact = CompactVisionState.model_validate({
        "rows": ["." * 15 for _ in range(5)] + ["." * 7 + "B" + "." * 7,
                                                    "." * 7 + "O" + "." * 7,
                                                    "." * 7 + "M" + "." * 7,
                                                    "...ZETJE" + "." * 7] + ["." * 15 for _ in range(6)],
        "rack": ["T"],
        "blanks": [],
        "confidence": 95,
    })
    assert detect_visible_tiles(screenshot) == {(6, 7), (7, 7), (8, 7), (9, 3), (9, 4), (9, 5), (9, 6), (9, 7)}
    aligned = _align_compact_to_visible_tiles(compact, detect_visible_tiles(screenshot))
    assert aligned.rows[6][7] == "B"
    assert aligned.rows[9][3:8] == "ZETJE"


class _FakeResponse:
    def __init__(self, compact: dict[str, Any]) -> None:
        self._compact = compact

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": json.dumps(self._compact)}}]}


def _compact_payload(confidence: float) -> dict[str, Any]:
    return {
        "rows": ["." * 15 for _ in range(7)] + ["." * 7 + "A" + "." * 7] + ["." * 15 for _ in range(7)],
        "rack": ["T", "E", "S", "T"],
        "blanks": [],
        "confidence": confidence,
    }


def _stub_openrouter(monkeypatch: pytest.MonkeyPatch, *payloads: dict[str, Any]) -> list[dict[str, Any]]:
    """Answer each call with the next payload, repeating the last one."""
    calls: list[dict[str, Any]] = []

    def fake_post(*_args: object, **kwargs: Any) -> _FakeResponse:
        calls.append(kwargs)
        return _FakeResponse(payloads[min(len(calls) - 1, len(payloads) - 1)])

    monkeypatch.setattr("wordfeud_analyzer.vision.requests.post", fake_post)
    return calls


def _sent_prompt(call: dict[str, Any]) -> str:
    return str(call["json"]["messages"][0]["content"][0]["text"])


def _blank_screenshot(tmp_path: Path) -> Path:
    screenshot = tmp_path / "screenshot.png"
    Image.new("RGB", (588, 1275), (42, 45, 52)).save(screenshot)
    return screenshot


def test_a_confident_extraction_is_used_without_a_verification_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = _stub_openrouter(monkeypatch, _compact_payload(MINIMUM_CONFIDENCE))
    extraction = extract_board(_blank_screenshot(tmp_path), api_key="test-key")
    assert extraction.confidence == MINIMUM_CONFIDENCE
    assert extraction.state.grid[7][7].letter == "A"
    assert extraction.state.rack == ["T", "E", "S", "T"]


def test_an_uncertain_extraction_is_rejected_instead_of_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_openrouter(monkeypatch, _compact_payload(72))
    with pytest.raises(LowConfidenceError) as raised:
        _ = extract_board(_blank_screenshot(tmp_path), api_key="test-key")
    assert raised.value.confidence == 72
    assert "72%" in str(raised.value)
    assert len(calls) == 1


def test_a_miscounted_empty_row_is_repaired_instead_of_failing_the_extraction() -> None:
    rows = ["." * 15 for _ in range(15)]
    rows[1] = "." * 10  # the exact defect seen in production
    rows[2] = ""
    rows[3] = "." * 17
    rows[9] = "..ZETJE" + "." * 8
    compact = CompactVisionState.model_validate({"rows": rows, "rack": ["T"], "confidence": 95})
    assert compact.rows[1] == "." * 15
    assert compact.rows[2] == "." * 15
    assert compact.rows[3] == "." * 15
    assert compact.rows[9] == "..ZETJE" + "." * 8


def test_a_short_row_holding_letters_is_still_rejected_and_names_the_row() -> None:
    rows = ["." * 15 for _ in range(15)]
    rows[4] = "..ZETJE"
    with pytest.raises(ValidationError, match="row 5 has 7 characters"):
        _ = CompactVisionState.model_validate({"rows": rows, "rack": ["T"], "confidence": 95})


def test_a_rejected_answer_is_retried_with_the_concrete_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = _compact_payload(95)
    broken["rows"] = list(broken["rows"])
    broken["rows"][4] = "..ZETJE"
    calls = _stub_openrouter(monkeypatch, broken, _compact_payload(95))

    extraction = extract_board(_blank_screenshot(tmp_path), api_key="test-key")

    assert extraction.state.grid[7][7].letter == "A"
    assert len(calls) == 2
    assert "rejected" not in _sent_prompt(calls[0])
    assert "row 5 has 7 characters" in _sent_prompt(calls[1])


def test_a_persistent_failure_reports_a_readable_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = _compact_payload(95)
    broken["rows"] = list(broken["rows"])
    broken["rows"][4] = "..ZETJE"
    _ = _stub_openrouter(monkeypatch, broken)

    with pytest.raises(VisionExtractionError) as raised:
        _ = extract_board(_blank_screenshot(tmp_path), api_key="test-key")

    message = str(raised.value)
    assert message.startswith("Het bord kon niet worden uitgelezen")
    assert "row 5 has 7 characters" in message
    assert "pydantic.dev" not in message
