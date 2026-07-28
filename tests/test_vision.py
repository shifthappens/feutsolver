from pathlib import Path

from PIL import Image, ImageDraw

from wordfeud_analyzer.vision import (
    CompactVisionState,
    _align_compact_to_visible_tiles,
    _locate_board_top,
    _to_board_state,
    detect_visible_bonuses,
    detect_visible_tiles,
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
