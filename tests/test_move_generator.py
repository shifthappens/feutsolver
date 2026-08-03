from pathlib import Path

from wordfeud_analyzer.models import BoardState
from wordfeud_analyzer.move_generator import (
    Gaddag,
    add_words_to_wordlist,
    board_words,
    generate_moves,
    load_wordlist,
    parse_comma_separated_words,
    remove_word_from_wordlist,
    remove_words_from_wordlist,
    suggest_words,
)


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


def test_opentaal_loader_excludes_names_digits_and_punctuation(tmp_path: Path) -> None:
    wordlist = tmp_path / "words.txt"
    _ = wordlist.write_text("juweel\nWetzel\nt/m\n06-nummer\n", encoding="utf-8")
    gaddag = load_wordlist(wordlist)
    cached_gaddag = load_wordlist(wordlist)
    assert gaddag.contains("JUWEEL")
    assert gaddag.count == 1
    assert cached_gaddag.contains("JUWEEL")
    assert cached_gaddag.count == 1
    assert not gaddag.contains("WETZEL")
    assert not gaddag.contains("TM")


def test_gaddag_contains_words_from_both_sides_of_anchor() -> None:
    gaddag = Gaddag(["KAT", "KATER"])
    assert gaddag.contains("KAT")
    assert gaddag.contains("KATER")
    assert not gaddag.contains("KAS")


def board_with(words: dict[tuple[int, int], str], rack: list[str]) -> BoardState:
    """Place letters at fixed coordinates, so a test can state a board literally."""
    state = empty_board(rack)
    for (row, col), letter in words.items():
        state.grid[row][col].letter = letter
    return state


def test_diacritics_are_folded_instead_of_dropping_the_word(tmp_path: Path) -> None:
    """OpenTaal spells facade with a cedilla; a Wordfeud board never does."""
    source = tmp_path / "lijst.txt"
    _ = source.write_text("façade\nabituriënt\nExloërmond\nt/m\n06-nummer\nkat\n", encoding="utf-8")
    lexicon = load_wordlist(source)
    assert lexicon.contains("FACADE")
    assert lexicon.contains("ABITURIENT")
    assert lexicon.contains("KAT")
    assert not lexicon.contains("EXLOERMOND")  # a capitalised name stays excluded
    assert lexicon.count == 3


def test_board_words_reads_both_directions_and_survives_the_edges() -> None:
    state = board_with({(0, 0): "K", (0, 1): "A", (0, 2): "T", (1, 0): "I", (14, 14): "X"}, ["A"])
    assert sorted(board_words(state)) == ["KAT", "KI"]


def test_words_seen_on_a_board_are_suggestions_until_explicitly_confirmed(tmp_path: Path) -> None:
    source = tmp_path / "lijst.txt"
    _ = source.write_text("kat\n", encoding="utf-8")

    lexicon = load_wordlist(source)
    assert not lexicon.contains("GINS")

    assert suggest_words(["GINS"], source) == ["GINS"]
    assert "gins" not in source.read_text(encoding="utf-8").splitlines()

    assert add_words_to_wordlist(["GINS"], source) == ["GINS"]
    assert suggest_words(["GINS"], source) == []

    relearned = load_wordlist(source)
    assert relearned.contains("GINS")
    assert relearned.contains("KAT")


def test_suggested_words_are_not_written_by_repeated_lookups(tmp_path: Path) -> None:
    source = tmp_path / "lijst.txt"
    _ = source.write_text("kat\n", encoding="utf-8")
    before = source.read_bytes()

    assert suggest_words(["GINS", "ZQX"], source) == ["GINS", "ZQX"]
    assert suggest_words(["GINS", "ZQX"], source) == ["GINS", "ZQX"]

    assert source.read_bytes() == before


def test_remove_word_from_wordlist_removes_diacritic_spelling(tmp_path: Path) -> None:
    source = tmp_path / "lijst.txt"
    _ = source.write_text("kat\nfaçade\nkamer\n", encoding="utf-8")

    assert remove_word_from_wordlist("FACADE", source)
    assert source.read_text(encoding="utf-8") == "kat\nkamer\n"
    assert not remove_word_from_wordlist("FACADE", source)
    assert not load_wordlist(source).contains("FACADE")


def test_remove_words_from_wordlist_updates_a_bulk_selection_atomically(tmp_path: Path) -> None:
    source = tmp_path / "lijst.txt"
    _ = source.write_text("kat\nfaçade\nkamer\nGINS\n", encoding="utf-8")

    removed = remove_words_from_wordlist(["FACADE", "gins", "gins", "niet aanwezig"], source)

    assert removed == ["FACADE", "GINS"]
    assert source.read_text(encoding="utf-8") == "kat\nkamer\n"


def test_generate_moves_can_exclude_main_words_without_rebuilding_lexicon() -> None:
    state = empty_board(list("GINS"))
    lexicon = Gaddag(["gin", "gins", "sing", "sign", "sin"])

    moves = generate_moves(state, lexicon, limit=20, excluded_words=["GINS", "SIN"])

    assert moves
    assert all(move.word not in {"GINS", "SIN"} for move in moves)


def test_generate_moves_can_exclude_cross_words_without_rebuilding_lexicon() -> None:
    state = empty_board(["A"])
    state.grid[7][7].letter = "B"
    state.grid[6][8].letter = "C"
    lexicon = Gaddag(["ba", "ca", "ab"])

    moves = generate_moves(state, lexicon, limit=20, excluded_words=["CA"])

    assert moves
    assert all("CA" not in move.cross_words for move in moves)
    assert not any(move.word == "BA" and move.direction == "H" for move in moves)


def test_parse_comma_separated_words_normalises_and_deduplicates() -> None:
    assert parse_comma_separated_words(" façade, KAT, façade ") == ["FACADE", "KAT"]
    assert parse_comma_separated_words("kat,") == []
    assert parse_comma_separated_words("") == []


def test_removed_words_do_not_return_in_new_suggestions(tmp_path: Path) -> None:
    source = tmp_path / "lijst.txt"
    _ = source.write_text("gin\ngins\n", encoding="utf-8")
    state = board_with({(7, 7): "G", (7, 8): "I", (7, 9): "N"}, ["S"])

    before = generate_moves(state, load_wordlist(source), limit=20)
    assert any(move.word == "GINS" for move in before)

    for word in parse_comma_separated_words("gins, gin"):
        assert remove_word_from_wordlist(word, source)

    after = generate_moves(state, load_wordlist(source), limit=20)
    assert not any(word in {move.word, *move.cross_words} for move in after for word in {"GINS", "GIN"})


def test_a_confirmed_word_makes_a_move_possible_that_was_rejected_before(tmp_path: Path) -> None:
    """A confirmed board word can stop blocking a legal move."""
    source = tmp_path / "lijst.txt"
    _ = source.write_text("gin\n", encoding="utf-8")
    state = board_with({(7, 7): "G", (7, 8): "I", (7, 9): "N"}, ["S"])

    before = generate_moves(state, load_wordlist(source), limit=20)
    assert not any(move.word == "GINS" for move in before)

    assert suggest_words(["ZQX", "GINS"], source) == ["GINS", "ZQX"]
    assert "gins" not in source.read_text(encoding="utf-8").splitlines()
    _ = add_words_to_wordlist(["ZQX", "GINS"], source)
    after = load_wordlist(source)
    assert after.contains("ZQX")
    assert after.contains("GINS")
