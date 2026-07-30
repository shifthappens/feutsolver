from pathlib import Path
import json

from PIL import Image, ImageDraw
from pytest import MonkeyPatch

from wordfeud_analyzer.vision import (
    CompactVisionState,
    _align_compact_to_visible_tiles,  # pyright: ignore[reportPrivateUsage]
    _ordered_tiles_schema,  # pyright: ignore[reportPrivateUsage]
    _repair_compact_with_visible_tiles,  # pyright: ignore[reportPrivateUsage]
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
    })
    assert detect_visible_tiles(screenshot) == {(6, 7), (7, 7), (8, 7), (9, 3), (9, 4), (9, 5), (9, 6), (9, 7)}
    aligned = _align_compact_to_visible_tiles(compact, detect_visible_tiles(screenshot))
    assert aligned.rows[6][7] == "B"
    assert aligned.rows[9][3:8] == "ZETJE"


def test_repair_restores_the_one_visible_tile_a_model_omitted() -> None:
    compact = CompactVisionState.model_validate({
        "rows": ["." * 15 for _ in range(15)],
        "rack": ["Q"],
        "blanks": [],
    })
    rows = list(compact.rows)
    rows[4] = "A" + "." * 14
    compact = CompactVisionState(rows=rows, rack=["Q"], blanks=[])
    repaired = _repair_compact_with_visible_tiles(compact, {(4, 0), (4, 1)}, "z")
    assert repaired.rows[4][:2] == "AZ"
    assert repaired.blanks == [(4, 1)]


def test_ordered_tile_schema_requires_the_local_tile_count() -> None:
    schema = _ordered_tiles_schema(101, include_rack=True)
    letters = schema["properties"]["letters"]
    assert isinstance(letters, dict)
    assert letters["minLength"] == 101
    assert letters["maxLength"] == 101


def test_extract_board_recovers_an_omitted_tile_from_a_contact_sheet(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    screenshot = tmp_path / "screenshot.png"
    width, height, board_top = 588, 1275, round(1275 * 0.296)
    image = Image.new("RGB", (width, height), (42, 45, 52))
    draw = ImageDraw.Draw(image)
    for row, col in [(4, 0), (4, 1)]:
        draw.rectangle((int(col * width / 15) + 3, board_top + int(row * width / 15) + 3,
                        int((col + 1) * width / 15) - 3, board_top + int((row + 1) * width / 15) - 3),
                       fill=(225, 220, 210))
    image.save(screenshot)

    requests: list[dict[str, object]] = []
    responses = iter([
        {"rows": ["." * 15 for _ in range(4)] + ["A" + "." * 14] + ["." * 15 for _ in range(10)],
         "rack": ["Q"], "blanks": []},
        {"letters": "Z"},
    ])

    class FakeResponse:
        def __init__(self, content: object) -> None:
            self.content = content

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": json.dumps(self.content)}}]}

    def fake_post(*args: object, **kwargs: object) -> FakeResponse:
        request_json = kwargs.get("json")
        assert isinstance(request_json, dict)
        requests.append(request_json)
        return FakeResponse(next(responses))

    monkeypatch.setattr("wordfeud_analyzer.vision.requests.post", fake_post)  # type: ignore[attr-defined]
    state = extract_board(screenshot, api_key="test-key", model="test-model", retries=0)
    assert state.grid[4][0].letter == "A"
    assert state.grid[4][1].letter == "Z"
    repair_schema = requests[1]["response_format"]
    assert isinstance(repair_schema, dict)
    json_schema = repair_schema["json_schema"]
    assert isinstance(json_schema, dict)
    schema = json_schema["schema"]
    assert isinstance(schema, dict)
    properties = schema["properties"]
    assert isinstance(properties, dict)
    letters = properties["letters"]
    assert isinstance(letters, dict)
    assert letters["minLength"] == letters["maxLength"] == 1


def test_extract_board_falls_back_after_provider_rejects_the_full_transcription(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    screenshot = tmp_path / "screenshot.png"
    width, height, board_top = 588, 1275, round(1275 * 0.296)
    image = Image.new("RGB", (width, height), (42, 45, 52))
    draw = ImageDraw.Draw(image)
    for row, col in [(4, 0), (4, 1)]:
        draw.rectangle((int(col * width / 15) + 3, board_top + int(row * width / 15) + 3,
                        int((col + 1) * width / 15) - 3, board_top + int((row + 1) * width / 15) - 3),
                       fill=(225, 220, 210))
    image.save(screenshot)

    requests: list[dict[str, object]] = []
    responses = iter(["provider could not produce 101 letters", "same failure", {"letters": "AB", "rack": ["Q"]}])

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": json.dumps(next(responses))}}]}

    def fake_post(*args: object, **kwargs: object) -> FakeResponse:
        request_json = kwargs.get("json")
        assert isinstance(request_json, dict)
        requests.append(request_json)
        return FakeResponse()

    monkeypatch.setattr("wordfeud_analyzer.vision.requests.post", fake_post)
    state = extract_board(screenshot, api_key="test-key", model="test-model")
    assert [state.grid[4][col].letter for col in (0, 1)] == ["A", "B"]
    fallback_format = requests[2]["response_format"]
    assert isinstance(fallback_format, dict)
    json_schema = fallback_format["json_schema"]
    assert isinstance(json_schema, dict)
    schema = json_schema["schema"]
    assert isinstance(schema, dict)
    properties = schema["properties"]
    assert isinstance(properties, dict)
    letters = properties["letters"]
    assert isinstance(letters, dict)
    assert letters["minLength"] == letters["maxLength"] == 2
