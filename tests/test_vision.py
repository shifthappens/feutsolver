import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from wordfeud_analyzer.vision import (
    BOARD_SIZE,
    LOCAL_PROFILE_MAX_DISTANCE,
    LOCAL_PROFILE_MIN_MARGIN,
    LOCAL_PROFILE_SIZE,
    BoardExtraction,
    MINIMUM_CONFIDENCE,
    LooseTilesError,
    LocalOCRFailure,
    LowConfidenceError,
    PendingMoveError,
    TileReading,
    VisionExtractionError,
    _disconnected_tiles,  # pyright: ignore[reportPrivateUsage]
    _implausible_tiles,  # pyright: ignore[reportPrivateUsage]
    _decoded_wordfeud_profiles,  # pyright: ignore[reportPrivateUsage]
    _profile_is_decisive,  # pyright: ignore[reportPrivateUsage]
    _profile_confidence,  # pyright: ignore[reportPrivateUsage]
    _profile_letter_candidates,  # pyright: ignore[reportPrivateUsage]
    _reconcile_local_letter,  # pyright: ignore[reportPrivateUsage]
    _locate_board_top,  # pyright: ignore[reportPrivateUsage]
    _max_response_tokens,  # pyright: ignore[reportPrivateUsage]
    _outside_in,  # pyright: ignore[reportPrivateUsage]
    _rack_boxes,  # pyright: ignore[reportPrivateUsage]
    _parse_content,  # pyright: ignore[reportPrivateUsage]
    _to_board_state,  # pyright: ignore[reportPrivateUsage]
    _wordfeud_crop_images,  # pyright: ignore[reportPrivateUsage]
    detect_pending_move,
    detect_visible_bonuses,
    detect_visible_tiles,
    extract_board,
    tile_contact_sheet,
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
    play_button: bool = False,
) -> Path:
    """Draw a Wordfeud-shaped screenshot in the given theme.

    `play_button` paints the filled blue Speel button that replaces the neutral
    Pas/Hussel buttons while a move is being composed.
    """
    image = Image.new("RGB", (WIDTH, HEIGHT), theme["outside"])
    draw = ImageDraw.Draw(image)
    if play_button:
        draw.rounded_rectangle((30, HEIGHT - 120, 250, HEIGHT - 40), radius=40, fill=(0, 122, 255))
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


def test_local_rack_geometry_finds_all_tiles_without_reading_the_letters() -> None:
    rack = Image.new("RGB", (588, 315), DARK_THEME["empty"])
    draw = ImageDraw.Draw(rack)
    for index in range(7):
        left = 15 + index * 80
        draw.rectangle((left, 95, left + 76, 170), fill=DARK_THEME["tile"])
        # A scanline through the glyph would otherwise split every tile into two
        # bright runs and look like fourteen rack tiles.
        draw.rectangle((left + 35, 105, left + 40, 130), fill=(0, 0, 0))

    boxes = _rack_boxes(rack, DARK_THEME["empty"])

    assert boxes == [(15 + index * 80, 95, 15 + index * 80 + 77, 171) for index in range(7)]


def test_local_rack_geometry_does_not_split_a_single_tile_on_a_glyph_scanline() -> None:
    rack = Image.new("RGB", (588, 315), DARK_THEME["empty"])
    draw = ImageDraw.Draw(rack)
    draw.rectangle((15, 95, 92, 250), fill=DARK_THEME["tile"])
    # This mirrors the supplied screenshot: the last candidate scanline crosses
    # the lower stroke of the only rack glyph and creates two light runs.
    draw.rectangle((50, 219, 55, 219), fill=DARK_THEME["empty"])

    assert _rack_boxes(rack, DARK_THEME["empty"]) == [(15, 95, 93, 251)]


def _glyph_from_profile(letter: str) -> Image.Image:
    """Build a real, versioned Wordfeud glyph fixture without a host font."""
    mask = _decoded_wordfeud_profiles()[letter][0]
    pixels = bytes(
        255 if value & (1 << (7 - offset)) else 0
        for value in mask
        for offset in range(8)
    )
    return Image.frombytes("L", LOCAL_PROFILE_SIZE, pixels)


def test_versioned_glyph_profiles_have_complete_fixed_size_masks() -> None:
    assert {
        len(mask)
        for masks in _decoded_wordfeud_profiles().values()
        for mask in masks
    } == {LOCAL_PROFILE_SIZE[0] * LOCAL_PROFILE_SIZE[1] // 8}


def test_real_three_point_m_profile_is_not_read_as_o() -> None:
    """Regression from the supplied iPhone screenshot: M has value three, O one."""
    candidates = _profile_letter_candidates(_glyph_from_profile("M"))

    assert candidates[0] == (0.0, "M")
    assert _profile_is_decisive(candidates)


def test_decisive_profile_confidence_is_measured_and_above_the_ocr_threshold() -> None:
    """Accepted local glyphs must be measured as usable, not merely displayed."""
    exact = _profile_confidence([(0.0, "M"), (0.2, "O")])
    boundary = _profile_confidence([
        (LOCAL_PROFILE_MAX_DISTANCE, "M"),
        (LOCAL_PROFILE_MAX_DISTANCE + LOCAL_PROFILE_MIN_MARGIN, "O"),
    ])

    assert exact == 99.0
    assert MINIMUM_CONFIDENCE <= boundary < exact


def test_a_moderate_profile_cannot_be_confirmed_by_the_same_ocr_path() -> None:
    """E/3 must fail closed instead of yielding a confident but contradictory tile."""
    with pytest.raises(LocalOCRFailure, match="glyphprofiel"):
        _ = _reconcile_local_letter("E", 0.12, 3)


def test_a_strong_profile_can_reject_a_bad_tiny_point_reading() -> None:
    assert _reconcile_local_letter("L", 0.08, 5) == "L"


def test_local_backend_does_not_require_an_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _screenshot(tmp_path / "local.png", DARK_THEME)
    expected = BoardExtraction(_to_board_state([], [], [], [["NORMAL"] * BOARD_SIZE for _ in range(BOARD_SIZE)]), 98.0)
    monkeypatch.setattr("wordfeud_analyzer.vision._extract_board_local", lambda _, budget=None: expected)

    assert extract_board(path, backend="local") is expected


def test_external_backend_requires_a_real_requester_identity(tmp_path: Path) -> None:
    with pytest.raises(VisionExtractionError, match="OCR-IDENTITY"):
        extract_board(
            tmp_path / "missing.png",
            backend="openrouter",
            allow_external=True,
            api_key="test-key",  # pragma: allowlist secret
        )


def test_recovery_sheet_enlarges_and_labels_each_tile(tmp_path: Path) -> None:
    tiles = [(7, 7), (7, 8), (8, 8)]
    path = _screenshot(tmp_path / "board.png", DARK_THEME, set(tiles))
    board, _ = _wordfeud_crop_images(path)

    sheet = tile_contact_sheet(board, tiles, indexes=[7, 19, 42], columns=2)

    assert sheet.size == (264, 336)
    for position in range(len(tiles)):
        column = position % 2
        row = position // 2
        left = 8 + column * 128
        top = 8 + row * 164
        label = sheet.crop((left, top, left + 120, top + 36))
        assert any(
            label.getpixel((x, y)) != (255, 255, 255)
            for x in range(label.width)
            for y in range(label.height)
        )


def test_deciding_crop_order_is_neither_forward_nor_reverse() -> None:
    assert _outside_in([1, 2, 3, 4, 5, 6]) == [1, 6, 2, 5, 3, 4]


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


def test_a_short_letter_list_cannot_silently_shift_the_rest_of_the_board() -> None:
    bonuses = [["NORMAL"] * BOARD_SIZE for _ in range(BOARD_SIZE)]
    tiles = [(7, 7), (7, 8), (7, 9)]

    with pytest.raises(ValueError, match="expected exactly 3 letters"):
        _ = _to_board_state(["K", "A"], tiles, ["R"], bonuses)


def test_indexed_tile_readings_must_hold_single_letters() -> None:
    with pytest.raises(ValidationError):
        _ = TileReading.model_validate(
            {"tiles": [{"index": 1, "letter": "AB"}], "rack": ["A"], "confidence": 95}
        )
    with pytest.raises(ValidationError):
        _ = TileReading.model_validate(
            {"tiles": [{"index": 1, "letter": "4"}], "rack": ["A"], "confidence": 95}
        )
    reading = TileReading.model_validate(
        {
            "tiles": [
                {"index": 1, "letter": "A"},
                {"index": 2, "letter": "b"},
            ],
            "rack": ["a", "?"],
            "confidence": 95,
        }
    )
    assert reading.letters == ["A", "b"]
    assert reading.rack == ["A", "?"]


def test_indexed_tile_readings_are_sorted_by_their_printed_index() -> None:
    reading = TileReading.model_validate(
        {
            "tiles": [
                {"index": 2, "letter": "A"},
                {"index": 1, "letter": "K"},
                {"index": 3, "letter": "T"},
            ],
            "rack": [],
            "confidence": 95,
        }
    )
    assert reading.letters == ["K", "A", "T"]


def test_model_json_with_fences_and_trailing_commas_is_recovered() -> None:
    reading = _parse_content(
        """Here is the result:
```json
{"tiles": [{"index": 1, "letter": "K",},], "rack": ["R",], "confidence": 95,}
```
"""
    )

    assert reading.letters == ["K"]
    assert reading.rack == ["R"]
    assert reading.confidence == 95


def test_response_budget_grows_for_a_full_board() -> None:
    assert _max_response_tokens(3) == 1_024
    assert _max_response_tokens(96) == 1_792
    assert _max_response_tokens(225) <= 4_000


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


def _tile_payload(
    letters: str | list[str],
    rack: list[str],
    confidence: float,
    indexes: list[int] | None = None,
) -> dict[str, Any]:
    indexes = indexes or list(range(1, len(letters) + 1))
    return {
        "tiles": [
            {"index": index, "letter": letter}
            for index, letter in zip(indexes, letters)
        ],
        "rack": rack,
        "confidence": confidence,
    }


def test_a_confident_reading_becomes_a_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _screenshot(tmp_path / "board.png", LIGHT_THEME, {(7, 7), (7, 8), (7, 9)})
    calls = _stub_openrouter(monkeypatch, _tile_payload("KAt", ["R", "E"], 96))

    extraction = extract_board(path, api_key="test-key", backend="openrouter", allow_external=True, requester=str(path))

    assert extraction.confidence == 96
    assert [extraction.state.grid[7][col].letter for col in (7, 8, 9)] == ["K", "A", "T"]
    assert extraction.state.grid[7][9].is_blank
    assert extraction.state.rack == ["R", "E"]
    assert len(calls) == 2
    assert all(_sent_images(call) == 2 for call in calls)
    assert all("never `0.97` or `1` to mean 100%" in _sent_prompt(call) for call in calls)


def test_an_uncertain_reading_is_rejected_instead_of_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _screenshot(tmp_path / "board.png", DARK_THEME, {(7, 7), (7, 8)})
    calls = _stub_openrouter(monkeypatch, _tile_payload("KA", ["R"], 72))

    with pytest.raises(LowConfidenceError) as raised:
        _ = extract_board(path, api_key="test-key", backend="openrouter", allow_external=True, requester=str(path))

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
        _tile_payload("KA", ["R"], 96),
        _tile_payload("KAT", ["R"], 96),
    )

    extraction = extract_board(path, api_key="test-key", backend="openrouter", allow_external=True, requester=str(path))

    assert extraction.state.grid[7][9].letter == "T"
    assert len(calls) == 3
    assert "deliberately shuffled" in _sent_prompt(calls[1])
    assert "exact printed IDs" in _sent_prompt(calls[2])
    for call in calls:
        tiles_schema = call["json"]["response_format"]["json_schema"]["schema"]["properties"]["tiles"]
        assert tiles_schema["minItems"] == tiles_schema["maxItems"] == 3


def test_a_missing_tile_index_is_retried_instead_of_shifting_following_letters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _screenshot(tmp_path / "board.png", DARK_THEME, {(7, 7), (7, 8), (7, 9)})
    calls = _stub_openrouter(
        monkeypatch,
        {
            "tiles": [
                {"index": 1, "letter": "K"},
                {"index": 2, "letter": "A"},
                {"index": 4, "letter": "T"},
            ],
            "rack": ["R"],
            "confidence": 96,
        },
        _tile_payload("KAT", ["R"], 96),
    )

    extraction = extract_board(path, api_key="test-key", backend="openrouter", allow_external=True, requester=str(path))

    assert extraction.state.grid[7][9].letter == "T"
    assert len(calls) == 3
    assert "1, 2, 3" in _sent_prompt(calls[2])


def test_disagreeing_crop_orders_are_resolved_without_shifting_the_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full, confidently numbered but shifted answer was the production regression."""
    path = _screenshot(tmp_path / "board.png", DARK_THEME, {(7, 7), (7, 8), (7, 9)})
    calls = _stub_openrouter(
        monkeypatch,
        _tile_payload("ATK", ["R"], 97),  # every index exists, but glyphs sit on the wrong IDs
        _tile_payload("KAT", ["R"], 96),  # independent reverse-order reading
        _tile_payload("KAT", ["R"], 98),  # enlarged deciding crops
    )

    extraction = extract_board(path, api_key="test-key", backend="openrouter", allow_external=True, requester=str(path))

    assert [extraction.state.grid[7][col].letter for col in (7, 8, 9)] == ["K", "A", "T"]
    assert extraction.confidence == 96
    assert len(calls) == 3
    assert "exact printed IDs" in _sent_prompt(calls[2])


def test_only_disputed_non_contiguous_ids_are_read_a_third_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _screenshot(tmp_path / "board.png", DARK_THEME, {(7, 7), (7, 8), (7, 9)})
    calls = _stub_openrouter(
        monkeypatch,
        _tile_payload("KET", ["R"], 97),
        _tile_payload("KAT", ["R"], 96),
        _tile_payload("A", ["R"], 98, indexes=[2]),
    )

    extraction = extract_board(path, api_key="test-key", backend="openrouter", allow_external=True, requester=str(path))

    assert [extraction.state.grid[7][col].letter for col in (7, 8, 9)] == ["K", "A", "T"]
    assert "exact printed IDs to return are: 2" in _sent_prompt(calls[2])
    tiles_schema = calls[2]["json"]["response_format"]["json_schema"]["schema"]["properties"]["tiles"]
    assert tiles_schema["minItems"] == tiles_schema["maxItems"] == 1


def test_tile_point_values_are_not_requested_or_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point OCR is not board state and is deliberately absent from the API contract."""
    path = _screenshot(tmp_path / "board.png", DARK_THEME, {(7, 7), (7, 8), (7, 9)})
    first = _tile_payload("KIT", ["R"], 99)
    second = _tile_payload("KIT", ["R"], 98)
    calls = _stub_openrouter(
        monkeypatch,
        first,
        second,
    )

    extraction = extract_board(path, api_key="test-key", backend="openrouter", allow_external=True, requester=str(path))

    assert [extraction.state.grid[7][col].letter for col in (7, 8, 9)] == ["K", "I", "T"]
    assert extraction.confidence == 98
    assert len(calls) == 2
    assert all("points" not in _sent_prompt(call) for call in calls)


def test_a_persistent_mismatch_reports_a_readable_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _screenshot(tmp_path / "board.png", DARK_THEME, {(7, 7), (7, 8), (7, 9)})
    _ = _stub_openrouter(monkeypatch, _tile_payload("K", ["R"], 96))

    with pytest.raises(VisionExtractionError) as raised:
        _ = extract_board(path, api_key="test-key", backend="openrouter", allow_external=True, requester=str(path))

    message = str(raised.value)
    assert message.startswith("Het bord kon niet worden uitgelezen")
    assert "expected exactly 3 indexed tile readings" in message
    assert "pydantic.dev" not in message


def test_an_empty_board_asks_only_for_the_rack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An opening move is a legal position, and needs no tile strip at all."""
    path = _screenshot(tmp_path / "board.png", LIGHT_THEME)
    calls = _stub_openrouter(monkeypatch, _tile_payload([], ["R", "E", "?"], 97))

    extraction = extract_board(path, api_key="test-key", backend="openrouter", allow_external=True, requester=str(path))

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
    calls = _stub_openrouter(monkeypatch, _tile_payload("KA", ["R"], 99))

    with pytest.raises(PendingMoveError, match="nog niet gespeeld"):
        _ = extract_board(path, api_key="test-key", backend="openrouter", allow_external=True, requester=str(path))

    assert calls == []


@pytest.mark.parametrize("theme_name", list(THEMES))
def test_the_play_button_marks_a_move_that_is_still_being_composed(
    theme_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The surest signal: an invalid placement shows no score bubble, but this button."""
    path = _screenshot(tmp_path / f"{theme_name}.png", THEMES[theme_name], {(7, 7), (7, 8)}, play_button=True)
    assert detect_pending_move(path)
    calls = _stub_openrouter(monkeypatch, _tile_payload("KA", ["R"], 99))

    with pytest.raises(PendingMoveError):
        _ = extract_board(path, api_key="test-key", backend="openrouter", allow_external=True, requester=str(path))

    assert calls == []


@pytest.mark.parametrize("theme_name", list(THEMES))
def test_the_neutral_buttons_of_a_played_board_are_not_a_pending_move(
    theme_name: str, tmp_path: Path
) -> None:
    path = _screenshot(tmp_path / f"{theme_name}.png", THEMES[theme_name], {(7, 7), (7, 8)})
    assert not detect_pending_move(path)


def test_tiles_that_do_not_reach_the_centre_are_loose() -> None:
    """A real word dropped loose on the board is still an impossible position."""
    played = {(7, 7), (7, 8), (7, 9)}
    loose = {(2, 3), (2, 4), (2, 5), (2, 6)}  # JLOE, spelling something, connected to nothing
    assert _disconnected_tiles(played) == set()
    assert _disconnected_tiles(played | loose) == loose
    assert _disconnected_tiles({(0, 0), (0, 1)}) == {(0, 0), (0, 1)}  # nothing on the centre


def test_a_loose_word_stops_the_extraction_before_any_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _screenshot(
        tmp_path / "los.png", DARK_THEME, {(7, 7), (7, 8), (2, 3), (2, 4), (2, 5), (2, 6)}
    )
    calls = _stub_openrouter(monkeypatch, _tile_payload("KAJLOE", ["R"], 99))

    with pytest.raises(LooseTilesError, match="los van de rest"):
        _ = extract_board(path, api_key="test-key", backend="openrouter", allow_external=True, requester=str(path))

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
    calls = _stub_openrouter(monkeypatch, _tile_payload("KAT", ["R"], 96))

    with pytest.raises(LooseTilesError, match="los van de rest"):
        _ = extract_board(path, api_key="test-key")

    assert calls == []
