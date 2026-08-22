"""Golden OCR regression tests for the confirmed Wordfeud screenshots.

These tests deliberately call the same local extraction entrypoint as the app. They
do not mock OCR, tile geometry, rack detection, or bonus detection: a change in any
of those production steps must show up here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from wordfeud_analyzer.vision import BOARD_SIZE, MINIMUM_CONFIDENCE, extract_board


ROOT = Path(__file__).resolve().parents[1]
SCREENSHOTS = ROOT / "tests" / "screenshots"


# Coordinates are 1-based (row, column), matching the review transcript and the
# coordinates visible on a Wordfeud board. These are independent golden values; do
# not derive them from STANDARD_BONUS_LAYOUT in production code.
BONUS_LAYOUTS: dict[str, dict[str, tuple[tuple[int, int], ...]]] = {
    "A": {
        "TW": ((2, 2), (3, 15), (4, 10), (5, 5), (11, 12), (12, 1), (14, 1)),
        "DW": ((2, 15), (4, 3), (5, 3), (5, 12), (9, 2), (11, 14), (13, 2), (14, 9), (15, 3)),
        "TL": ((2, 3), (2, 14), (3, 5), (3, 6), (4, 1), (6, 10), (6, 12),
               (7, 7), (7, 11), (7, 12), (7, 13), (10, 14), (12, 4), (12, 9),
               (15, 6), (15, 7)),
        "DL": ((1, 1), (1, 7), (3, 4), (4, 4), (4, 8), (4, 12), (4, 14),
               (5, 4), (8, 3), (8, 4), (9, 11), (9, 15), (10, 15), (11, 4),
               (11, 13), (11, 15), (14, 8), (15, 12)),
    },
    "B": {
        "TW": ((1, 5), (1, 11), (5, 1), (5, 15), (11, 1), (11, 15), (15, 5), (15, 11)),
        "DW": ((3, 3), (3, 13), (4, 8), (5, 5), (5, 11), (8, 4), (8, 12),
               (11, 5), (11, 11), (12, 8), (13, 3), (13, 13)),
        "TL": ((1, 1), (1, 15), (2, 6), (2, 10), (4, 4), (4, 12), (6, 2),
               (6, 6), (6, 10), (6, 14), (10, 2), (10, 6), (10, 10), (10, 14),
               (12, 4), (12, 12), (14, 6), (14, 10), (15, 1), (15, 15)),
        "DL": ((1, 8), (2, 2), (2, 14), (3, 7), (3, 9), (5, 7), (5, 9),
               (7, 3), (7, 5), (7, 11), (7, 13), (8, 1), (8, 15), (9, 3),
               (9, 5), (9, 11), (9, 13), (11, 7), (11, 9), (13, 7), (13, 9),
               (14, 2), (14, 14), (15, 8)),
    },
}


# A dot is an empty board square. Every non-dot is a confirmed board letter.
EXPECTED: dict[str, dict[str, object]] = {
    "IMG_5912.PNG": {
        "rack": "WPHSOEO",
        "layout": "A",
        "rows": (
            "...............", "...............", "...............", "...............", "...............",
            "...............", ".....P.B.......", ".....L.O.......", ".....E.M.......", "...ZETJE.......",
            ".....T.NUL.....", "....HE.........", "....A..........", "....A..........", "....G..........",
        ),
    },
    "IMG_5913.PNG": {
        "rack": "OQOESJN",
        "layout": "B",
        "rows": (
            "..........DAMES", ".........ZO.AH.", "........JE..M.V", ".W.....NEUT.A.I",
            "GI.KEN...LID..D", ".SOEBAT.RECENTE", ".S.V..AZEN.U..S", "KEGEL.BOX..K...",
            ".N.R..E........", ".....GEDUW.....", ".......AH......", ".......T.......",
            "......YEN......", ".....NON.......", "...............",
        ),
    },
    "IMG_5915.PNG": {
        "rack": "DBXEDRT",
        "layout": "B",
        "rows": (
            "...............", "...............", "...............", "...............", ".........KLEVER",
            "..........O..D.", "...H....P.T.YIN", "..KA...BESJE.T.", ".GIL..ZET.E..I.",
            "GIN..JOZEF..WE.", "END.VETER.KLAS.", "W..MAN.M..O.N..", "AQUA......M.D..",
            "S..C.....RE....", "...HOREN.EN....",
        ),
    },
    "IMG_5916.PNG": {
        "rack": "NJNJDME",
        "layout": "B",
        "rows": (
            "...............", "...............", "...............", "...............", "...............",
            "...............", "...............", ".......Q...Z...", "......FACADE...", ".....HUT.L.PECH",
            "B...POT.YOGI..E", "A....LOK...G..G", "N.WONEN.VLOER..", "KEI..N....M....", ".NERD.....E....",
        ),
    },
    "IMG_5917.PNG": {
        "rack": "WEOAIVK",
        "layout": "A",
        "rows": (
            "...............", "...............", "...............", "...............", "...............",
            "...............", ".....P.B.......", ".....L.O.......", ".....E.M.......", "...ZETJE.......",
            ".....T.NUL.....", "....HE.........", "....ARME.......", "HOPSA..........", "....G..........",
        ),
    },
    "IMG_5921 2.PNG": {
        "rack": "XFE?UPN",
        "layout": "B",
        "rows": (
            "...............", "...............", "......GA.......", ".......DEIST...", ".........KLEVER",
            "....BACO..O..D.", "...HE...P.T.YIN", "..KAR..BESJE.T.", ".GILD.ZET.E..I.",
            "GIN..JOZEF..WE.", "END.VETER.KLAS.", "W..MAN.M..O.N..", "AQUA......M.D..",
            "S..C.....RE....", "...HOREN.EN....",
        ),
    },
    "IMG_5932.PNG": {
        "rack": "OQMPERE",
        "layout": "B",
        "rows": (
            "..........DAMES", ".........ZO.AH.", "........JE..M.V", ".W.....NEUT.A.I",
            "GI.KEN...LID..D", ".SOEBAT.RECENTE", ".S.V..AZEN.U..S", "KEGEL.BOX..K...",
            ".N.R..E........", ".....GEDUW.....", "DONSJE.AH......", "I......T.......",
            "E.....YEN......", "F....NON.......", "...............",
        ),
    },
    "IMG_5942.PNG": {
        "rack": "OAVSWSC",
        "layout": "A",
        "rows": (
            "...............", "...............", "...............", "...............", "...............",
            "...............", ".....P.B.......", ".....L.O.......", ".....E.M...F...", "...ZETJE...U...",
            ".....T.NUL.I...", "....HE..WIEK...", "....ARME.......", "HOPSA..........", "....G..........",
        ),
    },
    "IMG_6091.png": {
        "rack": "C",
        "layout": "B",
        "rows": (
            "....G..........", "...MA...K......", ".NOODWEER.ZEND.", "...R...AU.ER.U.",
            ".H..BEDUIDT.FIT", ".IJZEN..TOE.AF.", ".P..N....EL.N..", ".S.SEC.YAMS....",
            ".T..N...Q...J..", "....D...U..PETS", "...MENG.AXEL..A", "......ES...O..R",
            "..N...V...WOL.O", "..E..GE..HAI..N", ".VERBENE..D....",
        ),
    },
}


def _expected_bonus_matrix(layout_name: str, rows: tuple[str, ...]) -> list[list[str]]:
    matrix = [["NORMAL"] * BOARD_SIZE for _ in range(BOARD_SIZE)]
    for bonus, coordinates in BONUS_LAYOUTS[layout_name].items():
        for row, col in coordinates:
            matrix[row - 1][col - 1] = bonus
    for row, text in enumerate(rows):
        for col, letter in enumerate(text):
            if letter != ".":
                matrix[row][col] = "NORMAL"
    return matrix


def _expected_grid(rows: tuple[str, ...], bonuses: list[list[str]]) -> list[list[dict[str, object]]]:
    return [
        [
            {
                "letter": None if letter == "." else letter,
                "bonus": bonuses[row][col],
                "is_blank": False,
            }
            for col, letter in enumerate(text)
        ]
        for row, text in enumerate(rows)
    ]


@pytest.mark.parametrize("filename", tuple(EXPECTED))
def test_confirmed_screenshot_matches_production_ocr(filename: str) -> None:
    """Run the real local production route and compare the complete board state."""
    expected = EXPECTED[filename]
    rows = expected["rows"]
    rack = expected["rack"]
    layout = expected["layout"]
    assert isinstance(rows, tuple)
    assert isinstance(rack, str)
    assert isinstance(layout, str)

    # This is the same extraction entrypoint selected by app.process_upload when
    # WORDFEUD_OCR_BACKEND is local. No OCR or geometry step is mocked here.
    extraction = extract_board(SCREENSHOTS / filename, backend="local")
    bonuses = _expected_bonus_matrix(layout, rows)

    assert extraction.confidence >= MINIMUM_CONFIDENCE, filename
    assert extraction.state.rack == list(rack), filename
    assert extraction.state.effective_bonuses == bonuses, filename
    assert [
        [cell.model_dump() for cell in row]
        for row in extraction.state.grid
    ] == _expected_grid(rows, bonuses), filename
