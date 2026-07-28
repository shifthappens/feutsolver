"""Deterministic legal-move generation and Wordfeud scoring."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import subprocess
import tempfile
from typing import Iterable

from .models import BoardState, Move, PlacedTile

BOARD_SIZE = 15
LETTER_VALUES = {
    "A": 1, "B": 4, "C": 5, "D": 2, "E": 1, "F": 4, "G": 3, "H": 4,
    "I": 2, "J": 4, "K": 3, "L": 3, "M": 3, "N": 1, "O": 1, "P": 4,
    "Q": 10, "R": 2, "S": 2, "T": 2, "U": 2, "V": 4, "W": 5, "X": 8,
    "Y": 8, "Z": 5,
}
LETTER_MULTIPLIER = {"NORMAL": 1, "DL": 2, "TL": 3, "DW": 1, "TW": 1}
WORD_MULTIPLIER = {"NORMAL": 1, "DL": 1, "TL": 1, "DW": 2, "TW": 3}


class Gaddag:
    """Compact, minimized GADDAG lexicon.

    Each word is represented around every split (ABC -> +ABC, A+BC, BA+C).
    The strings are minimized into a directed acyclic word graph, so a Dutch
    word list remains practical in Python while retaining GADDAG traversal.
    """

    SEPARATOR = "+"

    def __init__(self, words: Iterable[str] = ()) -> None:
        clean_words = {normalise_word(word) for word in words}
        clean_words = {word for word in clean_words if 2 <= len(word) <= BOARD_SIZE}
        sequences = sorted(
            prefix[::-1] + self.SEPARATOR + word[len(prefix):]
            for word in clean_words
            for prefix in (word[:split] for split in range(len(word)))
        )
        self.count = len(clean_words)
        self._states, self.root = self._from_sorted_sequences(sequences)

    @classmethod
    def from_wordlist(cls, path: str | Path) -> "Gaddag":
        """Build a compact GADDAG without holding millions of strings in RAM."""
        instance = cls.__new__(cls)
        instance.count = 0
        with tempfile.TemporaryDirectory(prefix="wordfeud-gaddag-") as directory:
            unsorted_path = Path(directory) / "sequences.txt"
            sorted_path = Path(directory) / "sequences-sorted.txt"
            with Path(path).open(encoding="utf-8") as source, unsorted_path.open("w", encoding="ascii") as target:
                for line in source:
                    if not _is_plain_netherlands_word(line):
                        continue
                    word = normalise_word(line)
                    if not 2 <= len(word) <= BOARD_SIZE:
                        continue
                    instance.count += 1
                    for split in range(len(word)):
                        target.write(word[:split][::-1] + cls.SEPARATOR + word[split:] + "\n")
            # External sorting keeps peak memory bounded for the complete OpenTaal list.
            subprocess.run(["sort", str(unsorted_path), "-o", str(sorted_path)], check=True)
            with sorted_path.open(encoding="ascii") as sequences:
                instance._states, instance.root = cls._from_sorted_sequences(line.rstrip("\n") for line in sequences)
        return instance

    @staticmethod
    def _from_sorted_sequences(sequences: Iterable[str]) -> tuple[list[tuple[dict[str, int], bool]], int]:
        """Incrementally minimize lexicographically sorted strings into a DAFSA."""
        nodes: list[dict[str, object]] = [{"children": {}, "terminal": False}]
        register: dict[tuple[bool, tuple[tuple[str, int], ...]], int] = {}
        previous = ""
        path = [0]

        def minimise(down_to: int) -> None:
            nonlocal path
            for index in range(len(previous), down_to, -1):
                state_id = path[index]
                node = nodes[state_id]
                children = node["children"]
                assert isinstance(children, dict)
                signature = (bool(node["terminal"]), tuple(sorted(children.items())))
                canonical = register.get(signature)
                if canonical is None:
                    canonical = state_id
                    register[signature] = canonical
                parent = nodes[path[index - 1]]
                parent_children = parent["children"]
                assert isinstance(parent_children, dict)
                parent_children[previous[index - 1]] = canonical
            path = path[: down_to + 1]

        for sequence in sequences:
            if sequence == previous:
                continue
            common = 0
            upper = min(len(sequence), len(previous))
            while common < upper and sequence[common] == previous[common]:
                common += 1
            minimise(common)
            current = path[-1]
            for char in sequence[common:]:
                next_id = len(nodes)
                nodes.append({"children": {}, "terminal": False})
                children = nodes[current]["children"]
                assert isinstance(children, dict)
                children[char] = next_id
                current = next_id
                path.append(current)
            nodes[current]["terminal"] = True
            previous = sequence
        minimise(0)

        # During construction, superseded suffix nodes remain in ``nodes``.
        # Re-index only the reachable canonical graph: this is the difference
        # between a practical Dutch GADDAG and a multi-gigabyte build graph.
        compact: list[tuple[dict[str, int], bool] | None] = []
        reindexed: dict[int, int] = {}

        def copy_state(old_id: int) -> int:
            if old_id in reindexed:
                return reindexed[old_id]
            new_id = len(compact)
            reindexed[old_id] = new_id
            compact.append(None)
            old = nodes[old_id]
            old_children = old["children"]
            assert isinstance(old_children, dict)
            compact[new_id] = (
                {char: copy_state(child_id) for char, child_id in old_children.items()},
                bool(old["terminal"]),
            )
            return new_id

        root = copy_state(0)
        return [state for state in compact if state is not None], root

    def transition(self, state_id: int, char: str) -> int | None:
        return self._states[state_id][0].get(char)

    def children(self, state_id: int) -> Iterable[tuple[str, int]]:
        return self._states[state_id][0].items()

    def terminal(self, state_id: int) -> bool:
        return self._states[state_id][1]

    def contains(self, word: str) -> bool:
        state_id = self.transition(self.root, self.SEPARATOR)
        if state_id is None:
            return False
        for char in normalise_word(word):
            state_id = self.transition(state_id, char)
            if state_id is None:
                return False
        return self.terminal(state_id)


def normalise_word(word: str) -> str:
    word = word.strip().upper()
    return word if word and all("A" <= char <= "Z" for char in word) else ""


def _is_plain_netherlands_word(word: str) -> bool:
    """Exclude OpenTaal entries such as 06-nummers, t/m and capitalised names."""
    word = word.strip()
    return 2 <= len(word) <= BOARD_SIZE and word.isascii() and word.isalpha() and word == word.lower()


def load_wordlist(path: str | Path) -> Gaddag:
    return Gaddag.from_wordlist(path)


def _in_bounds(row: int, col: int) -> bool:
    return 0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE


def _letter(state: BoardState, row: int, col: int) -> str | None:
    return state.grid[row][col].letter


def _has_tiles(state: BoardState) -> bool:
    return any(cell.letter for row in state.grid for cell in row)


def _word_at(state: BoardState, row: int, col: int, dr: int, dc: int, center: str) -> str:
    """Return the full cross word through (row, col), using center as the new tile."""
    before: list[str] = []
    r, c = row - dr, col - dc
    while _in_bounds(r, c) and _letter(state, r, c):
        before.append(_letter(state, r, c) or "")
        r, c = r - dr, c - dc
    after: list[str] = []
    r, c = row + dr, col + dc
    while _in_bounds(r, c) and _letter(state, r, c):
        after.append(_letter(state, r, c) or "")
        r, c = r + dr, c + dc
    return "".join(reversed(before)) + center + "".join(after)


def _touches_board(state: BoardState, tiles: list[PlacedTile]) -> bool:
    for tile in tiles:
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            r, c = tile.row + dr, tile.col + dc
            if _in_bounds(r, c) and _letter(state, r, c):
                return True
    return False


def _score_move(state: BoardState, word: str, row: int, col: int, direction: str, tiles: list[PlacedTile]) -> tuple[int, list[str]]:
    dr, dc = (0, 1) if direction == "H" else (1, 0)
    newly = {(tile.row, tile.col): tile for tile in tiles}
    main_sum, main_multiplier = 0, 1
    for index, char in enumerate(word):
        r, c = row + dr * index, col + dc * index
        existing = state.grid[r][c]
        tile = newly.get((r, c))
        if tile:
            value = 0 if tile.is_blank else LETTER_VALUES[char]
            main_sum += value * LETTER_MULTIPLIER[existing.bonus]
            main_multiplier *= WORD_MULTIPLIER[existing.bonus]
        else:
            main_sum += 0 if existing.is_blank else LETTER_VALUES[char]
    score = main_sum * main_multiplier
    cross_words: list[str] = []
    for tile in tiles:
        pr, pc = (1, 0) if direction == "H" else (0, 1)
        cross = _word_at(state, tile.row, tile.col, pr, pc, tile.letter)
        if len(cross) > 1:
            cross_words.append(cross)
            cell = state.grid[tile.row][tile.col]
            # Existing cross letters were previously placed, so their bonuses never apply.
            old_sum = sum(
                0 if state.grid[r][c].is_blank else LETTER_VALUES[state.grid[r][c].letter or "A"]
                for r, c in _cross_existing_positions(state, tile.row, tile.col, pr, pc)
            )
            new_value = 0 if tile.is_blank else LETTER_VALUES[tile.letter] * LETTER_MULTIPLIER[cell.bonus]
            score += (old_sum + new_value) * WORD_MULTIPLIER[cell.bonus]
    if len(tiles) == 7:
        score += 40
    return score, cross_words


def _cross_existing_positions(state: BoardState, row: int, col: int, dr: int, dc: int) -> list[tuple[int, int]]:
    positions: list[tuple[int, int]] = []
    r, c = row - dr, col - dc
    while _in_bounds(r, c) and _letter(state, r, c):
        positions.append((r, c))
        r, c = r - dr, c - dc
    r, c = row + dr, col + dc
    while _in_bounds(r, c) and _letter(state, r, c):
        positions.append((r, c))
        r, c = r + dr, c + dc
    return positions


def _anchors(state: BoardState) -> list[tuple[int, int]]:
    """Empty squares that a legal turn can use to connect to the board."""
    if not _has_tiles(state):
        return [(7, 7)]
    anchors: list[tuple[int, int]] = []
    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            if _letter(state, row, col):
                continue
            if any(_in_bounds(row + dr, col + dc) and _letter(state, row + dr, col + dc)
                   for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                anchors.append((row, col))
    return anchors


def _cross_checks(state: BoardState, lexicon: Gaddag, direction: str) -> dict[tuple[int, int], set[str]]:
    """Allowed letters per empty square, based on the perpendicular word."""
    pr, pc = (1, 0) if direction == "H" else (0, 1)
    allowed: dict[tuple[int, int], set[str]] = {}
    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            if _letter(state, row, col):
                continue
            probe = _word_at(state, row, col, pr, pc, "A")
            if len(probe) == 1:
                allowed[(row, col)] = set(LETTER_VALUES)
            else:
                allowed[(row, col)] = {
                    char for char in LETTER_VALUES
                    if lexicon.contains(_word_at(state, row, col, pr, pc, char))
                }
    return allowed


def _materialise_move(state: BoardState, anchor: tuple[int, int], direction: str,
                      tiles: list[PlacedTile]) -> tuple[str, int, int]:
    """Read the completed main word from the board plus the proposed tiles."""
    dr, dc = (0, 1) if direction == "H" else (1, 0)
    new = {(tile.row, tile.col): tile.letter for tile in tiles}
    row, col = anchor
    while _in_bounds(row - dr, col - dc) and (
        _letter(state, row - dr, col - dc) or (row - dr, col - dc) in new
    ):
        row, col = row - dr, col - dc
    start_row, start_col = row, col
    letters: list[str] = []
    while _in_bounds(row, col) and (_letter(state, row, col) or (row, col) in new):
        letters.append(new.get((row, col)) or _letter(state, row, col) or "")
        row, col = row + dr, col + dc
    return "".join(letters), start_row, start_col


def generate_moves(state: BoardState, lexicon: Gaddag, limit: int = 6) -> list[Move]:
    """Generate legal moves with an anchored GADDAG traversal and cross checks."""
    rack = Counter(state.rack)
    candidates: dict[tuple[str, int, int, str], Move] = {}
    for direction, (dr, dc) in (("H", (0, 1)), ("V", (1, 0))):
        checks = _cross_checks(state, lexicon, direction)
        for anchor in _anchors(state):
            _generate_left(
                state, lexicon, rack, anchor, anchor[0] - dr, anchor[1] - dc,
                direction, dr, dc, checks, lexicon.root, [], candidates,
            )
    return sorted(candidates.values(), key=lambda move: (-move.score, move.word, move.row, move.col))[:limit]


def _generate_left(state: BoardState, lexicon: Gaddag, rack: Counter[str], anchor: tuple[int, int],
                   row: int, col: int, direction: str, dr: int, dc: int,
                   checks: dict[tuple[int, int], set[str]], state_id: int,
                   tiles: list[PlacedTile], candidates: dict[tuple[str, int, int, str], Move]) -> None:
    """Walk left from an anchor; the GADDAG's separator starts the right half."""
    existing = _letter(state, row, col) if _in_bounds(row, col) else None
    if not existing:
        separator = lexicon.transition(state_id, lexicon.SEPARATOR)
        if separator is not None:
            _generate_right(state, lexicon, rack, anchor, anchor[0], anchor[1], direction, dr, dc,
                            checks, separator, tiles, candidates)
    if not _in_bounds(row, col):
        return
    if existing:
        child = lexicon.transition(state_id, existing)
        if child is not None:
            _generate_left(state, lexicon, rack, anchor, row - dr, col - dc, direction, dr, dc,
                           checks, child, tiles, candidates)
        return
    for char, child in lexicon.children(state_id):
        if char == lexicon.SEPARATOR or char not in checks[(row, col)]:
            continue
        _place_left(state, lexicon, rack, anchor, row, col, direction, dr, dc, checks,
                    char, child, tiles, candidates)


def _place_left(state: BoardState, lexicon: Gaddag, rack: Counter[str], anchor: tuple[int, int],
                row: int, col: int, direction: str, dr: int, dc: int,
                checks: dict[tuple[int, int], set[str]], char: str, child: int,
                tiles: list[PlacedTile], candidates: dict[tuple[str, int, int, str], Move]) -> None:
    """Use a rack tile on the reversed (left) half of a GADDAG word."""
    if rack[char] > 0:
        rack[char] -= 1
        tiles.append(PlacedTile(row=row, col=col, letter=char))
        _generate_left(state, lexicon, rack, anchor, row - dr, col - dc, direction, dr, dc,
                       checks, child, tiles, candidates)
        tiles.pop()
        rack[char] += 1
    if rack["?"] > 0:
        rack["?"] -= 1
        tiles.append(PlacedTile(row=row, col=col, letter=char, is_blank=True))
        _generate_left(state, lexicon, rack, anchor, row - dr, col - dc, direction, dr, dc,
                       checks, child, tiles, candidates)
        tiles.pop()
        rack["?"] += 1


def _generate_right(state: BoardState, lexicon: Gaddag, rack: Counter[str], anchor: tuple[int, int],
                    row: int, col: int, direction: str, dr: int, dc: int,
                    checks: dict[tuple[int, int], set[str]], state_id: int,
                    tiles: list[PlacedTile], candidates: dict[tuple[str, int, int, str], Move]) -> None:
    """Consume/place the right half and emit only complete board words."""
    if lexicon.terminal(state_id) and (not _in_bounds(row, col) or not _letter(state, row, col)):
        word, start_row, start_col = _materialise_move(state, anchor, direction, tiles)
        if len(word) >= 2 and tiles:
            score, cross_words = _score_move(state, word, start_row, start_col, direction, tiles)
            key = (word, start_row, start_col, direction)
            candidates[key] = Move(word=word, row=start_row, col=start_col, direction=direction,
                                   score=score, tiles=tiles.copy(), cross_words=cross_words,
                                   bingo=len(tiles) == 7)
    if not _in_bounds(row, col):
        return
    existing = _letter(state, row, col)
    if existing:
        child = lexicon.transition(state_id, existing)
        if child is not None:
            _generate_right(state, lexicon, rack, anchor, row + dr, col + dc, direction, dr, dc,
                            checks, child, tiles, candidates)
        return
    for char, child in lexicon.children(state_id):
        if char == lexicon.SEPARATOR or char not in checks[(row, col)]:
            continue
        if rack[char] > 0:
            rack[char] -= 1
            tiles.append(PlacedTile(row=row, col=col, letter=char))
            _generate_right(state, lexicon, rack, anchor, row + dr, col + dc, direction, dr, dc,
                            checks, child, tiles, candidates)
            tiles.pop()
            rack[char] += 1
        if rack["?"] > 0:
            rack["?"] -= 1
            tiles.append(PlacedTile(row=row, col=col, letter=char, is_blank=True))
            _generate_right(state, lexicon, rack, anchor, row + dr, col + dc, direction, dr, dc,
                            checks, child, tiles, candidates)
            tiles.pop()
            rack["?"] += 1
