"""Pure state operations shared by the Streamlit bridge and tests."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from .models import BOARD_SIZE, BoardState, Move

MAX_SUGGESTIONS = 12


class StaleSolveRequest(ValueError):
    """The browser is trying to place a result for an older board snapshot."""


class InvalidSolveRequest(ValueError):
    """The browser selected a move that was not returned by this solve."""


def is_current_board_version(event_version: object, current_version: object) -> bool:
    """Reject browser events that belong to a board replaced by an upload."""
    try:
        current = int(current_version)
    except (TypeError, ValueError):
        return False
    if event_version is None:
        return current == 0
    try:
        return int(event_version) == current
    except (TypeError, ValueError):
        return False


def validate_snapshot(snapshot: object) -> BoardState:
    """Validate an untrusted browser snapshot before it reaches the solver."""
    return BoardState.model_validate(snapshot)


def snapshot_hash(state: BoardState) -> str:
    payload = json.dumps(
        state.model_dump(mode="json"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def consume_rack(rack: list[str], tiles: list[tuple[str, bool]]) -> list[str]:
    """Consume each newly placed tile once, preserving the remaining order."""
    remaining = list(rack)
    for letter, is_blank in tiles:
        wanted = "?" if is_blank else letter
        try:
            remaining.remove(wanted)
        except ValueError as error:
            kind = "blanco" if is_blank else f"letter {letter}"
            raise ValueError(f"rack does not contain the required {kind}") from error
    return remaining


def apply_move(state: BoardState, move: Move) -> BoardState:
    """Apply a solver move and consume exactly the rack tiles it used."""
    if not move.tiles:
        raise ValueError("a move must place at least one tile")

    dr, dc = (0, 1) if move.direction == "H" else (1, 0)
    end_row = move.row + dr * (len(move.word) - 1)
    end_col = move.col + dc * (len(move.word) - 1)
    if not (0 <= move.row < BOARD_SIZE and 0 <= move.col < BOARD_SIZE and 0 <= end_row < BOARD_SIZE and 0 <= end_col < BOARD_SIZE):
        raise ValueError("move word is outside the board")
    expected_positions = {
        (move.row + dr * index, move.col + dc * index): letter
        for index, letter in enumerate(move.word)
    }
    seen: set[tuple[int, int]] = set()
    rack_tiles: list[tuple[str, bool]] = []
    for tile in move.tiles:
        position = (tile.row, tile.col)
        if position in seen:
            raise ValueError("a move cannot place two tiles on one square")
        seen.add(position)
        if position not in expected_positions or expected_positions[position] != tile.letter:
            raise ValueError("move tiles do not match the move word")
        if not 0 <= tile.row < BOARD_SIZE or not 0 <= tile.col < BOARD_SIZE:
            raise ValueError("move tile is outside the board")
        if state.grid[tile.row][tile.col].letter is not None:
            raise ValueError("a move cannot overwrite an occupied square")
        rack_tiles.append((tile.letter, tile.is_blank))

    for (row, col), letter in expected_positions.items():
        cell = state.grid[row][col]
        if cell.letter is None and (row, col) not in seen:
            raise ValueError("a move must include every newly placed tile")
        if cell.letter is not None and cell.letter != letter:
            raise ValueError("move word does not match an existing tile")

    next_rack = consume_rack(state.rack, rack_tiles)
    next_state = state.model_copy(deep=True)
    for tile in move.tiles:
        cell = next_state.grid[tile.row][tile.col]
        # Occupied squares no longer expose their premium to later moves.
        cell.letter = tile.letter
        cell.is_blank = tile.is_blank
        cell.bonus = "NORMAL"
        if next_state.effective_bonuses is not None:
            next_state.effective_bonuses[tile.row][tile.col] = "NORMAL"
    next_state.rack = next_rack
    return next_state


def same_snapshot(left: object, right: object) -> bool:
    """Compare snapshots after validation, ignoring dict ordering and aliases."""
    return snapshot_hash(validate_snapshot(left)) == snapshot_hash(validate_snapshot(right))


def is_current_solve_result(
    token: object,
    state_hash: object,
    expected_token: object,
    state: BoardState,
) -> bool:
    """Accept a place request only for the exact solve that produced it."""
    return str(token) == str(expected_token) and str(state_hash) == snapshot_hash(state)


def make_solve_result(state: BoardState, moves: Iterable[Move], token: str) -> dict[str, Any]:
    """Build the browser-bound result from one immutable, validated snapshot."""
    return {
        "token": token,
        "state_hash": snapshot_hash(state),
        "moves": [move.model_dump(mode="json") for move in list(moves)[:MAX_SUGGESTIONS]],
    }


def replaceable_words(solve_result: object) -> set[str]:
    """Return suggestion words and their generated cross words.

    A cross word is also checked by Wordfeud when the suggested move is played,
    so it should be possible to report that word even when it is not one of the
    main suggestions shown to the user.
    """
    if not isinstance(solve_result, dict):
        return set()
    result: set[str] = set()
    moves = solve_result.get("moves", [])
    if not isinstance(moves, list):
        return result
    for stored in moves:
        try:
            move = Move.model_validate(stored)
        except Exception:
            continue
        result.add(move.word)
        result.update(move.cross_words)
    return result


def selected_move(solve_result: dict[str, Any], value: object) -> Move | None:
    """Return a move only when it exactly matches one of the offered moves."""
    try:
        candidate = Move.model_validate(value)
    except Exception:
        return None
    for stored in solve_result.get("moves", []):
        try:
            move = Move.model_validate(stored)
        except Exception:
            continue
        if move.model_dump(mode="json") == candidate.model_dump(mode="json"):
            return move
    return None


def apply_place_request(
    state: BoardState,
    solve_result: object,
    payload: dict[str, object],
) -> BoardState:
    """Validate token/hash and selected move before committing a placement."""
    if not isinstance(solve_result, dict):
        raise StaleSolveRequest("Deze suggestie is verouderd. Kies opnieuw voor ‘Geef oplossingen weer’.")
    requested_hash = str(payload.get("stateHash", ""))
    requested_token = str(payload.get("solveToken", ""))
    if (
        not is_current_solve_result(requested_token, requested_hash, solve_result.get("token"), state)
        or requested_hash != solve_result.get("state_hash")
    ):
        raise StaleSolveRequest("Deze suggestie is verouderd. Kies opnieuw voor ‘Geef oplossingen weer’.")
    move = selected_move(solve_result, payload.get("selectedMove"))
    if move is None:
        raise InvalidSolveRequest("De geselecteerde suggestie was ongeldig.")
    return apply_move(state, move)


def replace_from_upload(_current: BoardState, extracted: object) -> BoardState:
    """Validate an upload replacement before the caller swaps working state."""
    candidate = getattr(extracted, "state", extracted)
    return validate_snapshot(candidate.model_dump(mode="json") if isinstance(candidate, BoardState) else candidate)
