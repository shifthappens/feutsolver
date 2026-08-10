from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from wordfeud_analyzer.models import Move, PlacedTile, standard_board
from wordfeud_analyzer.state import (
    InvalidSolveRequest,
    StaleSolveRequest,
    apply_move,
    apply_place_request,
    consume_rack,
    is_current_board_version,
    make_solve_result,
    replaceable_words,
    replace_from_upload,
    is_current_solve_result,
    snapshot_hash,
)


def test_standard_board_has_the_complete_symmetric_layout() -> None:
    state = standard_board()
    assert state.rack == []
    assert state.grid[0][0].bonus == "TL"
    assert state.grid[0][4].bonus == "TW"
    assert state.grid[7][7].bonus == "NORMAL"
    assert state.grid[7][3].bonus == "DW"
    assert state.grid[7][0].bonus == "DL"
    assert state.grid[3][7].bonus == "DW"
    assert state.grid[11][7].bonus == "DW"
    # The standard Wordfeud layout has 24 DL and 12 DW squares.
    assert sum(cell.bonus == "DL" for row in state.grid for cell in row) == 24
    assert sum(cell.bonus == "TL" for row in state.grid for cell in row) == 20
    assert sum(cell.bonus == "DW" for row in state.grid for cell in row) == 12
    assert sum(cell.bonus == "TW" for row in state.grid for cell in row) == 8


def test_component_events_from_an_older_board_version_are_ignored() -> None:
    assert is_current_board_version(0, 0)
    assert is_current_board_version(None, 0)
    assert is_current_board_version(2, 2)
    assert not is_current_board_version(1, 2)
    assert not is_current_board_version(None, 2)
    assert not is_current_board_version("invalid", 2)


def test_consume_rack_uses_each_blank_once_and_preserves_order() -> None:
    assert consume_rack(["A", "?", "R"], [("E", True), ("A", False)]) == ["R"]
    with pytest.raises(ValueError, match="rack"):
        consume_rack(["A"], [("E", True)])


def test_apply_move_consumes_blank_and_effective_bonus() -> None:
    state = standard_board()
    state.rack = ["?", "A"]
    move = Move(
        word="BA",
        row=7,
        col=3,
        direction="H",
        score=0,
        tiles=[
            PlacedTile(row=7, col=3, letter="B", is_blank=True),
            PlacedTile(row=7, col=4, letter="A"),
        ],
    )
    committed = apply_move(state, move)
    assert committed.rack == []
    assert committed.grid[7][3].letter == "B"
    assert committed.grid[7][3].is_blank
    assert committed.grid[7][3].bonus == "NORMAL"
    assert committed.effective_bonuses is not None
    assert committed.effective_bonuses[7][3] == "NORMAL"


def test_solve_result_is_stale_after_the_state_changes() -> None:
    state = standard_board()
    token = "solve-1"
    state_hash = snapshot_hash(state)
    assert is_current_solve_result(token, state_hash, token, state)
    assert not is_current_solve_result("other-token", state_hash, token, state)
    assert not is_current_solve_result(token, "other-hash", token, state)
    state.rack = ["A"]
    assert not is_current_solve_result(token, state_hash, token, state)


def test_solve_and_place_request_is_bound_to_the_exact_snapshot_and_move() -> None:
    state = standard_board()
    state.rack = ["A"]
    move = Move(
        word="A",
        row=7,
        col=7,
        direction="H",
        score=1,
        tiles=[PlacedTile(row=7, col=7, letter="A")],
    )
    result = make_solve_result(state, [move] * 13, "solve-1")
    assert len(result["moves"]) == 12

    payload = {"solveToken": "solve-1", "stateHash": result["state_hash"], "selectedMove": result["moves"][0]}
    committed = apply_place_request(state, result, payload)
    assert committed.grid[7][7].letter == "A"
    assert committed.rack == []

    with pytest.raises(StaleSolveRequest):
        apply_place_request(state, result, {**payload, "stateHash": "wrong"})
    with pytest.raises(InvalidSolveRequest):
        apply_place_request(state, result, {**payload, "selectedMove": {"word": "Z"}})


def test_replaceable_words_include_suggestion_cross_words() -> None:
    state = standard_board()
    move = Move(
        word="FLUX",
        row=7,
        col=7,
        direction="H",
        score=20,
        tiles=[PlacedTile(row=7, col=7, letter="F")],
        cross_words=["FTE"],
    )
    result = make_solve_result(state, [move], "solve-1")

    assert replaceable_words(result) == {"FLUX", "FTE"}


def test_upload_replacement_validates_before_the_working_state_is_swapped() -> None:
    current = standard_board()
    current.rack = ["Z"]
    uploaded = standard_board()
    uploaded.rack = ["A", "?"]
    committed = replace_from_upload(current, SimpleNamespace(state=uploaded))
    assert committed.rack == ["A", "?"]
    assert current.rack == ["Z"]

    invalid = {"grid": [], "rack": []}
    with pytest.raises(ValidationError):
        replace_from_upload(current, invalid)
    assert current.rack == ["Z"]
