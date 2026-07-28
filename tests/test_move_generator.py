from wordfeud_analyzer.models import BoardState
from wordfeud_analyzer.move_generator import Gaddag, generate_moves, load_wordlist


def empty_board(rack: list[str]) -> BoardState:
    return BoardState.model_validate({
        "grid": [[{"letter": None, "bonus": "NORMAL", "is_blank": False} for _ in range(15)] for _ in range(15)],
        "rack": rack,
    })


def test_first_move_must_cross_center_and_scores_bingo() -> None:
    state = empty_board(list("ABCDEFG"))
    moves = generate_moves(state, Gaddag(["ABCDEFG"]), limit=20)
    assert moves
    assert all(any(tile.row == 7 and tile.col == 7 for tile in move.tiles) for move in moves)
    assert moves[0].score == 60  # A(1)+B(4)+C(5)+D(2)+E(1)+F(4)+G(3) + 40


def test_cross_word_must_exist() -> None:
    state = empty_board(["A"])
    state.grid[7][7].letter = "B"
    state.grid[6][8].letter = "C"
    # BA would create the invalid cross CA when laid horizontally beside B.
    moves = generate_moves(state, Gaddag(["BA"]), limit=20)
    assert not any(move.direction == "H" and move.row == 7 and move.col == 7 for move in moves)


def test_score_uses_bonus_at_its_extracted_random_coordinate() -> None:
    state = empty_board(["A"])
    state.grid[7][7].letter = "B"
    # This bonus is deliberately not at a standard-board location.
    state.grid[7][8].bonus = "TL"
    moves = generate_moves(state, Gaddag(["BA"]), limit=20)
    horizontal = next(move for move in moves if move.direction == "H" and move.row == 7 and move.col == 7)
    assert horizontal.score == 7  # existing B (4) + new A on 3L (3)


def test_existing_anchor_does_not_reuse_hidden_bonus() -> None:
    state = empty_board(list("WEEPT"))
    state.grid[9][3].letter = "Z"
    state.grid[9][3].bonus = "TW"
    state.grid[10][3].bonus = "DL"
    state.grid[11][3].bonus = "TL"
    moves = generate_moves(state, Gaddag(["ZWEEPT"]), limit=20)
    vertical = next(move for move in moves if move.word == "ZWEEPT")
    assert vertical.score == 25


def test_opentaal_loader_excludes_names_and_punctuation(tmp_path) -> None:
    wordlist = tmp_path / "words.txt"
    wordlist.write_text("juweel\nWetzel\nt/m\n06-nummer\n", encoding="utf-8")
    gaddag = load_wordlist(wordlist)
    assert gaddag.contains("JUWEEL")
    assert not gaddag.contains("WETZEL")
    assert not gaddag.contains("TM")


def test_gaddag_contains_words_from_both_sides_of_anchor() -> None:
    gaddag = Gaddag(["KAT", "KATER"])
    assert gaddag.contains("KAT")
    assert gaddag.contains("KATER")
    assert not gaddag.contains("KAS")
