from pathlib import Path

from PIL import Image, ImageDraw

from wordfeud_analyzer.vision import detect_visible_bonuses, wordfeud_crops


def test_wordfeud_crops_produce_square_board_and_rack(tmp_path: Path) -> None:
    screenshot = tmp_path / "screenshot.jpg"
    Image.new("RGB", (588, 1275), "black").save(screenshot)
    board, rack = wordfeud_crops(screenshot)
    assert board.startswith("data:image/png;base64,")
    assert rack.startswith("data:image/png;base64,")


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
