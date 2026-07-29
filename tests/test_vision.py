import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from wordfeud_analyzer.vision import (
    BOARD_SIZE,
    MINIMUM_CONFIDENCE,
    LowConfidenceError,
    PendingMoveError,
    TileReading,
    VisionExtractionError,
    _implausible_tiles,  # pyright: ignore[reportPrivateUsage]
    _locate_board_top,  # pyright: ignore[reportPrivateUsage]
    _to_board_state,  # pyright: ignore[reportPrivateUsage]
    _wordfeud_crop_images,  # pyright: ignore[reportPrivateUsage]
    detect_pending_move,
    detect_visible_bonuses,
    detect_visible_tiles,
    extract_board,
    tile_strip,
)

Rgb = tuple[int, int, int]

# The two skins Wordfeud ships. Every colour below was sampled from real screenshots;
# the point of keeping both is that no test may pass by knowing one theme's palette.
DARK_THEME: dict[str, Rgb] = {
    "empty": (42, 46, 51), "grid": (20, 22, 25), "outside": (30, 32, 36), "tile": (240, 237, 228),
    "DL": (106, 142, 78), "TL": (29, 99, 150), "DW": (206, 113, 10), "TW": (152, 39, 45),
}
LIGHT_THEME: dict[str, Rgb] = {
    "empty": (197, 209, 214), "grid": (255, 255, 255), "outside": (222, 226, 232), "tile": (245, 242, 235),
    "DL": (76, 161, 24), "TL": (28, 126, 178), "DW": (226, 128, 11), "TW": (181, 24, 34),
}
THEMES = {"donker": DARK_THEME, "licht": LIGHT_THEME}

WIDTH, HEIGHT, BOARD_TOP = 600, 1300, 300  # a board top that is not the old fixed ratio


def _screenshot(
    path: Path,
    theme: dict[str, Rgb],
    tiles: set[tuple[int, int]] = frozenset(),
    bonuses: dict[tuple[int, int], str] | None = None,
) -> Path:
    """Draw a Wordfeud-shaped screenshot in the given theme."""
    image = Image.new("RGB", (WIDTH, HEIGHT), theme["outside"])
    draw = ImageDraw.Draw(image)
    cell = WIDTH / BOARD_SIZE
    draw.rectangle((0, BOARD_TOP, WIDTH - 1, BOARD_TOP + WIDTH - 1), fill=theme["grid"])
    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            colour = theme["empty"]
            if (row, col) in tiles:
                colour = theme["tile"]
            elif bonuses and (row, col) in bonuses:
                colour = theme[bonuses[(row, col)]]
            draw.rectangle(
                (int(col * cell) + 2, BOARD_TOP + int(row * cell) + 2,
                 int((col + 1) * cell) - 3, BOARD_TOP + int((row + 1) * cell) - 3),
                fill=colour,
            )
    image.save(path)
    return path


@pytest.mark.parametrize("theme_name", list(THEMES))
def test_the_board_is_located_from_its_grid_in_both_themes(theme_name: str, tmp_path: Path) -> None:
    """The dark theme draws grid lines darker than the cells, the light theme lighter."""
    path = _screenshot(tmp_path / f"{theme_name}.png", THEMES[theme_name], tiles={(7, 7), (7, 8)})
    with Image.open(path) as image:
        located = _locate_board_top(image.convert("RGB"))
    # Within the cell border this fixture draws; what matters is that the grid was
    # found at all instead of falling back to the fixed screen ratio.
    assert abs(located - BOARD_TOP) <= 3
    assert abs(located - round(HEIGHT * 0.296)) > 20
    board, _ = _wordfeud_crop_images(path)
    assert board.size == (WIDTH, WIDTH)


@pytest.mark.parametrize("theme_name", list(THEMES))
def test_tiles_are_detected_in_both_themes_without_mistaking_bonuses(theme_name: str, tmp_path: Path) -> None:
    tiles = {(7, 7), (7, 8), (7, 9), (8, 7), (9, 7)}
    bonuses = {(0, 0): "TW", (0, 4): "DL", (5, 5): "TL", (14, 14): "DW", (3, 11): "DL"}
    path = _screenshot(tmp_path / f"{theme_name}.png", THEMES[theme_name], tiles, bonuses)
    assert detect_visible_tiles(path) == tiles


@pytest.mark.parametrize("theme_name", list(THEMES))
def test_bonus_squares_are_read_by_hue_in_both_themes(theme_name: str, tmp_path: Path) -> None:
    bonuses = {(0, 0): "TW", (1, 13): "TL", (9, 4): "DW", (14, 14): "DL"}
    path = _screenshot(tmp_path / f"{theme_name}.png", THEMES[theme_name], {(7, 7), (7, 8)}, bonuses)
    detected = detect_visible_bonuses(path)
    for (row, col), expected in bonuses.items():
        assert detected[row][col] == expected, f"{theme_name}: {row},{col}"
    assert detected[4][4] == "NORMAL"


@pytest.mark.parametrize("theme_name", list(THEMES))
def test_a_bonus_under_a_tile_counts_as_consumed(theme_name: str, tmp_path: Path) -> None:
    path = _screenshot(tmp_path / f"{theme_name}.png", THEMES[theme_name], {(7, 7), (7, 8)}, {(0, 0): "TW"})
    assert detect_visible_bonuses(path)[7][7] == "NORMAL"


def test_a_tile_without_any_neighbour_is_reported_as_implausible() -> None:
    """Wordfeud cannot produce a lone tile, so one means the detector misread."""
    assert _implausible_tiles({(7, 7), (7, 8)}) == set()
    assert _implausible_tiles({(7, 7), (7, 8), (2, 2)}) == {(2, 2)}


def test_the_strip_holds_the_tiles_in_reading_order(tmp_path: Path) -> None:
    """Order is imposed here, which is why the model never returns a coordinate."""
    tiles = [(7, 7), (7, 8), (8, 8)]
    path = _screenshot(tmp_path / "board.png", DARK_THEME, set(tiles))
    board, _ = _wordfeud_crop_images(path)
    strip = tile_strip(board, tiles)
    cell = board.size[0] // BOARD_SIZE
    for index, (row, col) in enumerate(tiles):
        x = 6 + index * (cell + 6) + cell // 2
        y = 6 + cell // 2
        source = board.getpixel((int((col + 0.5) * board.size[0] / BOARD_SIZE),
                                 int((row + 0.5) * board.size[1] / BOARD_SIZE)))
        assert strip.getpixel((x, y)) == source


def test_letters_are_written_back_to_the_squares_they_were_cut_from() -> None:
    tiles = [(7, 7), (7, 8), (8, 8)]
    bonuses = [["NORMAL"] * BOARD_SIZE for _ in range(BOARD_SIZE)]
    bonuses[0][0] = "TW"
    bonuses[7][7] = "DL"  # consumed: a tile sits on it
    state = _to_board_state(["K", "a", "T"], tiles, ["R", "?"], bonuses)
    assert state.grid[7][7].letter == "K"
    assert state.grid[7][7].bonus == "NORMAL"
    assert state.grid[7][8].letter == "A"
    assert state.grid[7][8].is_blank
    assert not state.grid[8][8].is_blank
    assert state.grid[0][0].bonus == "TW"
    assert state.grid[6][6].letter is None
    assert state.rack == ["R", "?"]


def test_a_letter_list_must_hold_single_letters() -> None:
    with pytest.raises(ValidationError):
        _ = TileReading.model_validate({"letters": ["AB"], "rack": ["A"], "confidence": 95})
    with pytest.raises(ValidationError):
        _ = TileReading.model_validate({"letters": ["4"], "rack": ["A"], "confidence": 95})
    reading = TileReading.model_validate({"letters": ["A", "b"], "rack": ["a", "?"], "confidence": 95})
    assert reading.letters == ["A", "b"]
    assert reading.rack == ["A", "?"]


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": json.dumps(self._payload)}}]}


def _stub_openrouter(monkeypatch: pytest.MonkeyPatch, *payloads: dict[str, Any]) -> list[dict[str, Any]]:
    """Answer each call with the next payload, repeating the last one. Never hits the network."""
    calls: list[dict[str, Any]] = []

    def fake_post(*_args: object, **kwargs: Any) -> _FakeResponse:
        calls.append(kwargs)
        return _FakeResponse(payloads[min(len(calls) - 1, len(payloads) - 1)])

    monkeypatch.setattr("wordfeud_analyzer.vision.requests.post", fake_post)
    return calls


def _sent_prompt(call: dict[str, Any]) -> str:
    return str(call["json"]["messages"][0]["content"][0]["text"])


def _sent_images(call: dict[str, Any]) -> int:
    return sum(1 for part in call["json"]["messages"][0]["content"] if part["type"] == "image_url")


def test_a_confident_reading_becomes_a_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _screenshot(tmp_path / "board.png", LIGHT_THEME, {(7, 7), (7, 8), (7, 9)})
    calls = _stub_openrouter(monkeypatch, {"letters": ["K", "A", "t"], "rack": ["R", "E"], "confidence": 96})

    extraction = extract_board(path, api_key="test-key")

    assert extraction.confidence == 96
    assert [extraction.state.grid[7][col].letter for col in (7, 8, 9)] == ["K", "A", "T"]
    assert extraction.state.grid[7][9].is_blank
    assert extraction.state.rack == ["R", "E"]
    assert len(calls) == 1
    assert _sent_images(calls[0]) == 2


def test_an_uncertain_reading_is_rejected_instead_of_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _screenshot(tmp_path / "board.png", DARK_THEME, {(7, 7), (7, 8)})
    calls = _stub_openrouter(monkeypatch, {"letters": ["K", "A"], "rack": ["R"], "confidence": 72})

    with pytest.raises(LowConfidenceError) as raised:
        _ = extract_board(path, api_key="test-key")

    assert raised.value.confidence == 72
    assert "72%" in str(raised.value)
    assert len(calls) == 1


def test_a_wrong_number_of_letters_is_retried_with_the_expected_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single failure mode left: we know how many tiles we cut out."""
    path = _screenshot(tmp_path / "board.png", DARK_THEME, {(7, 7), (7, 8), (7, 9)})
    calls = _stub_openrouter(
        monkeypatch,
        {"letters": ["K", "A"], "rack": ["R"], "confidence": 96},
        {"letters": ["K", "A", "T"], "rack": ["R"], "confidence": 96},
    )

    extraction = extract_board(path, api_key="test-key")

    assert extraction.state.grid[7][9].letter == "T"
    assert len(calls) == 2
    assert "rejected" not in _sent_prompt(calls[0])
    assert "expected exactly 3 letters" in _sent_prompt(calls[1])


def test_a_persistent_mismatch_reports_a_readable_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _screenshot(tmp_path / "board.png", DARK_THEME, {(7, 7), (7, 8), (7, 9)})
    _ = _stub_openrouter(monkeypatch, {"letters": ["K"], "rack": ["R"], "confidence": 96})

    with pytest.raises(VisionExtractionError) as raised:
        _ = extract_board(path, api_key="test-key")

    message = str(raised.value)
    assert message.startswith("Het bord kon niet worden uitgelezen")
    assert "expected exactly 3 letters" in message
    assert "pydantic.dev" not in message


def test_an_empty_board_asks_only_for_the_rack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An opening move is a legal position, and needs no tile strip at all."""
    path = _screenshot(tmp_path / "board.png", LIGHT_THEME)
    calls = _stub_openrouter(monkeypatch, {"letters": [], "rack": ["R", "E", "?"], "confidence": 97})

    extraction = extract_board(path, api_key="test-key")

    assert extraction.state.rack == ["R", "E", "?"]
    assert all(cell.letter is None for row in extraction.state.grid for cell in row)
    assert _sent_images(calls[0]) == 1
    assert "board is empty" in _sent_prompt(calls[0])


def _yellow_bubble(
    path: Path, theme: dict[str, Rgb], centre: tuple[float, float] = (0.5, 0.5), size: int = 60
) -> Path:
    """A screenshot with Wordfeud's pending-score bubble somewhere on the board.

    The bubble is drawn without any digits in it: what identifies a move in progress
    is the saturated yellow blob itself, not what it says or where it sits.
    """
    _screenshot(path, theme, {(7, 7), (7, 8)})
    with Image.open(path) as opened:
        image = opened.convert("RGB")
    x, y = centre[0] * WIDTH, BOARD_TOP + centre[1] * WIDTH
    draw = ImageDraw.Draw(image)
    draw.ellipse((x - size / 2, y - size / 3, x + size / 2, y + size / 3), fill=(255, 214, 0))
    image.save(path)
    return path


@pytest.mark.parametrize("centre", [(0.5, 0.5), (0.04, 0.02), (0.97, 0.98), (0.5, 0.0)])
@pytest.mark.parametrize("size", [40, 90])
def test_the_bubble_is_found_anywhere_on_the_board(
    centre: tuple[float, float], size: int, tmp_path: Path
) -> None:
    """Nothing about the position or the tiles matters, only that the bubble is there."""
    path = _yellow_bubble(tmp_path / "pending.png", DARK_THEME, centre, size)
    assert detect_pending_move(path)


@pytest.mark.parametrize("theme_name", list(THEMES))
def test_a_move_that_is_not_played_yet_is_refused(
    theme_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wordfeud has not approved those words, so the board may not be used or learned from."""
    path = _yellow_bubble(tmp_path / f"{theme_name}.png", THEMES[theme_name])
    assert detect_pending_move(path)
    calls = _stub_openrouter(monkeypatch, {"letters": ["K", "A"], "rack": ["R"], "confidence": 99})

    with pytest.raises(PendingMoveError, match="nog niet gespeeld"):
        _ = extract_board(path, api_key="test-key")

    assert calls == []


def test_tiles_alone_never_make_a_board_pending(tmp_path: Path) -> None:
    """Tiles lying anywhere are just letters; without the bubble nothing is pending."""
    crowded = {(row, col) for row in range(6, 12) for col in range(4, 11)}
    path = _screenshot(tmp_path / "vol.png", DARK_THEME, crowded)
    assert not detect_pending_move(path)


@pytest.mark.parametrize("theme_name", list(THEMES))
def test_a_played_board_is_not_mistaken_for_a_pending_move(theme_name: str, tmp_path: Path) -> None:
    """The pale yellow of a highlighted last move must not trigger the refusal."""
    theme = dict(THEMES[theme_name])
    highlighted = {(7, 7), (7, 8)}
    path = _screenshot(tmp_path / f"{theme_name}.png", theme, highlighted)
    with Image.open(path) as opened:
        image = opened.convert("RGB")
    draw = ImageDraw.Draw(image)
    cell = WIDTH / BOARD_SIZE
    for row, col in highlighted:  # Wordfeud's "last move" tint
        draw.rectangle(
            (int(col * cell) + 2, BOARD_TOP + int(row * cell) + 2,
             int((col + 1) * cell) - 3, BOARD_TOP + int((row + 1) * cell) - 3),
            fill=(240, 220, 130),
        )
    image.save(path)
    assert not detect_pending_move(path)


def test_a_stray_tile_stops_the_extraction_before_any_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _screenshot(tmp_path / "board.png", DARK_THEME, {(7, 7), (7, 8), (2, 2)})
    calls = _stub_openrouter(monkeypatch, {"letters": ["K", "A", "T"], "rack": ["R"], "confidence": 96})

    with pytest.raises(VisionExtractionError, match="losse vakje"):
        _ = extract_board(path, api_key="test-key")

    assert calls == []
