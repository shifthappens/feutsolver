"""Deterministic legal-move generation and Wordfeud scoring."""
from __future__ import annotations

from array import array
from collections import Counter
from collections.abc import Iterable
import pickle
from pathlib import Path
import subprocess
import tempfile
import unicodedata
from typing import BinaryIO, Literal, TypeAlias, TypedDict, TypeGuard, cast

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
GADDAG_CACHE_VERSION = 5
Direction = Literal["H", "V"]
# `array` only became subscriptable at runtime in Python 3.12; the element types
# are quoted so this alias also evaluates on 3.11 without losing type information.
GraphData: TypeAlias = tuple["array[int]", "array[int]", bytearray, bytearray, "array[int]", int]


class _Node(TypedDict):
    children: dict[str, int]
    terminal: bool


class Gaddag:
    """Packed, minimized Dutch word automaton.

    The previous full GADDAG expanded every word around every split. That made
    a 5 MB OpenTaal list consume more than 600 MB on a 1 GB VPS. The anchored
    forward traversal below needs only a minimal forward DAWG: it keeps exactly
    the same legality checks, while storing each word once.
    """

    count: int
    _starts: array[int]
    _counts: array[int]
    _terminals: bytearray
    _labels: bytearray
    _targets: array[int]
    root: int

    def __init__(self, words: Iterable[str] = ()) -> None:
        clean_words = {normalise_word(word) for word in words}
        clean_words = {word for word in clean_words if 2 <= len(word) <= BOARD_SIZE}
        sequences = sorted(clean_words)
        self.count = len(clean_words)
        self._set_graph(*self._from_sorted_sequences(sequences))

    @classmethod
    def from_wordlist(cls, *paths: str | Path) -> "Gaddag":
        """Build a packed DAWG from one or more lists, without holding them in RAM."""
        instance = cls.__new__(cls)
        with tempfile.TemporaryDirectory(prefix="wordfeud-gaddag-") as directory:
            unsorted_path = Path(directory) / "sequences.txt"
            sorted_path = Path(directory) / "sequences-sorted.txt"
            with unsorted_path.open("w", encoding="ascii") as target:
                for path in paths:
                    source_path = Path(path)
                    if not source_path.exists():
                        continue
                    with source_path.open(encoding="utf-8") as source:
                        for line in source:
                            if not _is_plain_netherlands_word(line):
                                continue
                            word = normalise_word(line)
                            if not 2 <= len(word) <= BOARD_SIZE:
                                continue
                            _ = target.write(word + "\n")
            # External sorting keeps peak memory bounded for the complete OpenTaal
            # list; -u collapses the duplicates that folding diacritics creates, and
            # any overlap between the two lists.
            _ = subprocess.run(["sort", "-u", str(unsorted_path), "-o", str(sorted_path)], check=True)
            counted = 0

            def sequences_from(handle: Iterable[str]) -> Iterable[str]:
                nonlocal counted
                for line in handle:
                    counted += 1
                    yield line.rstrip("\n")

            with sorted_path.open(encoding="ascii") as sequences:
                instance._set_graph(*cls._from_sorted_sequences(sequences_from(sequences)))
            instance.count = counted
        return instance

    @classmethod
    def from_cached_graph(cls, count: int, graph: GraphData) -> "Gaddag":
        instance = cls.__new__(cls)
        instance.count = count
        instance._set_graph(*graph)
        return instance

    def graph_data(self) -> GraphData:
        return self._starts, self._counts, self._terminals, self._labels, self._targets, self.root

    def _set_graph(
        self,
        starts: array[int],
        counts: array[int],
        terminals: bytearray,
        labels: bytearray,
        targets: array[int],
        root: int,
    ) -> None:
        self._starts = starts
        self._counts = counts
        self._terminals = terminals
        self._labels = labels
        self._targets = targets
        self.root = root

    @staticmethod
    def _from_sorted_sequences(sequences: Iterable[str]) -> GraphData:
        """Incrementally minimize lexicographically sorted strings into a DAFSA."""
        nodes: list[_Node] = [{"children": {}, "terminal": False}]
        register: dict[tuple[bool, tuple[tuple[str, int], ...]], int] = {}
        previous = ""
        path = [0]

        def minimise(down_to: int) -> None:
            nonlocal path
            for index in range(len(previous), down_to, -1):
                state_id = path[index]
                node = nodes[state_id]
                children = node["children"]
                signature = (bool(node["terminal"]), tuple(sorted(children.items())))
                canonical = register.get(signature)
                if canonical is None:
                    canonical = state_id
                    register[signature] = canonical
                parent = nodes[path[index - 1]]
                parent_children = parent["children"]
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
                children[char] = next_id
                current = next_id
                path.append(current)
            nodes[current]["terminal"] = True
            previous = sequence
        minimise(0)

        # During construction, superseded suffix nodes remain in ``nodes``.
        # Re-index only the reachable canonical graph into packed arrays. Python
        # dicts per state used hundreds of MB for OpenTaal; these arrays make the
        # retained lexicon small enough to avoid VPS swap thrashing.
        reindexed: dict[int, int] = {}
        starts: array[int] = array("I")
        counts: array[int] = array("B")
        terminals = bytearray()
        labels = bytearray()
        targets: array[int] = array("I")

        def copy_state(old_id: int) -> int:
            if old_id in reindexed:
                return reindexed[old_id]
            new_id = len(starts)
            reindexed[old_id] = new_id
            old = nodes[old_id]
            old_children = old["children"]
            _ = starts.append(len(labels))
            _ = terminals.append(bool(old["terminal"]))
            ordered_children = sorted(old_children.items())
            counts.append(len(ordered_children))
            # Reserve this state's contiguous edge range before recursively
            # packing children. Otherwise a child's edges would split the
            # parent's labels from its targets.
            edge_start = len(labels)
            labels.extend(ord(char) for char, _ in ordered_children)
            targets.extend([0] * len(ordered_children))
            for offset, (_, child_id) in enumerate(ordered_children):
                targets[edge_start + offset] = copy_state(child_id)
            return new_id

        root = copy_state(0)
        return starts, counts, terminals, labels, targets, root

    def transition(self, state_id: int, char: str) -> int | None:
        code = ord(char)
        start = self._starts[state_id]
        for index in range(start, start + self._counts[state_id]):
            if self._labels[index] == code:
                return self._targets[index]
        return None

    def children(self, state_id: int) -> Iterable[tuple[str, int]]:
        start = self._starts[state_id]
        for index in range(start, start + self._counts[state_id]):
            yield chr(self._labels[index]), self._targets[index]

    def terminal(self, state_id: int) -> bool:
        return bool(self._terminals[state_id])

    def contains(self, word: str) -> bool:
        state_id = self.root
        for char in normalise_word(word):
            state_id = self.transition(state_id, char)
            if state_id is None:
                return False
        return self.terminal(state_id)


def fold_diacritics(word: str) -> str:
    """Write a word the way it is played: façade becomes facade, abituriënt abiturient."""
    decomposed = unicodedata.normalize("NFKD", word)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def normalise_word(word: str) -> str:
    word = fold_diacritics(word).strip().upper()
    return word if word and all("A" <= char <= "Z" for char in word) else ""


def parse_comma_separated_words(value: str) -> list[str]:
    """Parse one or more comma-separated words into unique normalised words."""
    if not value.strip():
        return []
    parts = [part.strip() for part in value.split(",")]
    words = [normalise_word(part) for part in parts]
    if any(not word for word in words):
        return []
    return list(dict.fromkeys(words))


def _is_plain_netherlands_word(word: str) -> bool:
    """Exclude OpenTaal entries such as 06-nummers, t/m and capitalised names.

    Diacritics are folded instead of rejected: OpenTaal spells façade and abituriënt
    with them, while a Wordfeud board only ever holds plain A-Z.
    """
    word = word.strip()
    if not word or word != word.lower():
        return False
    folded = fold_diacritics(word)
    return 2 <= len(folded) <= BOARD_SIZE and folded.isascii() and folded.isalpha()


def _cache_path(path: Path) -> Path:
    return path.with_name(path.name + ".gaddag-cache-v2")


def _is_graph_data(value: object) -> TypeGuard[GraphData]:
    if not isinstance(value, tuple):
        return False
    values = cast(tuple[object, ...], value)
    if len(values) != 6:
        return False
    starts, counts, terminals, labels, targets, root = values
    return (
        isinstance(starts, array)
        and isinstance(counts, array)
        and isinstance(terminals, bytearray)
        and isinstance(labels, bytearray)
        and isinstance(targets, array)
        and isinstance(root, int)
    )


def _read_cached_data(handle: BinaryIO) -> tuple[int, tuple[int, ...], int, GraphData] | None:
    loaded = cast(object, pickle.load(handle))
    if not isinstance(loaded, tuple):
        return None
    values = cast(tuple[object, ...], loaded)
    if len(values) != 4:
        return None
    version, signature, count, graph = values
    if not isinstance(version, int) or not isinstance(count, int):
        return None
    if not isinstance(signature, tuple):
        return None
    signature_values = cast(tuple[object, ...], signature)
    if not signature_values or not all(isinstance(value, int) for value in signature_values):
        return None
    if not _is_graph_data(graph):
        return None
    return version, cast(tuple[int, ...], signature_values), count, graph


def _signature(paths: Iterable[Path]) -> tuple[int, ...]:
    """Rebuild whenever a source list changes."""
    values: list[int] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            values.extend((0, 0))
        else:
            values.extend((stat.st_mtime_ns, stat.st_size))
    return tuple(values)


def load_wordlist(path: str | Path) -> Gaddag:
    """Load a packed, persistent GADDAG; build it only when the word list changed."""
    source = Path(path)
    signature = _signature([source])
    cache = _cache_path(source)
    try:
        with cache.open("rb") as handle:
            cached = _read_cached_data(handle)
        if cached is not None:
            version, cached_signature, count, graph = cached
            if version == GADDAG_CACHE_VERSION and cached_signature == signature:
                return Gaddag.from_cached_graph(count, graph)
    except (OSError, EOFError, pickle.PickleError, ValueError):
        pass

    instance = Gaddag.from_wordlist(source)
    try:
        with tempfile.NamedTemporaryFile(dir=cache.parent, prefix=cache.name + ".", delete=False) as handle:
            pickle.dump((GADDAG_CACHE_VERSION, signature, instance.count, instance.graph_data()),
                        handle, protocol=pickle.HIGHEST_PROTOCOL)
            temporary_cache = Path(handle.name)
        _ = temporary_cache.replace(cache)
    except OSError:
        # A read-only local development word list still works; it simply is not cached.
        pass
    return instance


def learn_words(words: Iterable[str], path: str | Path) -> list[str]:
    """Append newly observed board words directly to the configured word list."""
    target = Path(path)
    try:
        known = {normalise_word(line) for line in target.read_text(encoding="utf-8").split()}
    except FileNotFoundError:
        known = set()
    fresh = sorted({normalise_word(word) for word in words} - known - {""})
    if not fresh:
        return []
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for word in fresh:
            _ = handle.write(word.lower() + "\n")
    return [word.lower() for word in fresh]


def remove_word_from_wordlist(word: str, path: str | Path) -> bool:
    """Permanently remove every spelling of a word from one source list."""
    target_word = normalise_word(word)
    if not target_word:
        return False

    source = Path(path)
    temporary_path: Path | None = None
    removed = False
    try:
        try:
            source_handle = source.open(encoding="utf-8")
        except FileNotFoundError:
            return False
        with source_handle:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=source.parent,
                prefix=source.name + ".",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                for line in source_handle:
                    if normalise_word(line) == target_word:
                        removed = True
                        continue
                    _ = temporary.write(line)
        if removed and temporary_path is not None:
            _ = temporary_path.replace(source)
            temporary_path = None
        return removed
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def board_words(state: BoardState) -> list[str]:
    """Every maximal run of two or more letters on the board, in both directions."""
    def letter_at(row: int, col: int) -> str | None:
        return _letter(state, row, col) if _in_bounds(row, col) else None

    found: list[str] = []
    for dr, dc in ((0, 1), (1, 0)):
        for row in range(BOARD_SIZE):
            for col in range(BOARD_SIZE):
                if letter_at(row, col) is None or letter_at(row - dr, col - dc) is not None:
                    continue  # empty, or not the start of this run
                word, r, c = "", row, col
                while (letter := letter_at(r, c)) is not None:
                    word += letter
                    r, c = r + dr, c + dc
                if len(word) >= 2:
                    found.append(word)
    return found


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


def _score_move(state: BoardState, word: str, row: int, col: int, direction: Direction,
                tiles: list[PlacedTile]) -> tuple[int, list[str]]:
    dr, dc = (0, 1) if direction == "H" else (1, 0)
    newly = {(tile.row, tile.col): tile for tile in tiles}
    main_sum, main_multiplier = 0, 1
    for index, char in enumerate(word):
        r, c = row + dr * index, col + dc * index
        existing = state.grid[r][c]
        tile = newly.get((r, c))
        if tile:
            value = 0 if tile.is_blank else LETTER_VALUES[char]
            bonus = state.effective_bonus(r, c)
            main_sum += value * LETTER_MULTIPLIER[bonus]
            main_multiplier *= WORD_MULTIPLIER[bonus]
        else:
            main_sum += 0 if existing.is_blank else LETTER_VALUES[char]
    score = main_sum * main_multiplier
    cross_words: list[str] = []
    for tile in tiles:
        pr, pc = (1, 0) if direction == "H" else (0, 1)
        cross = _word_at(state, tile.row, tile.col, pr, pc, tile.letter)
        if len(cross) > 1:
            cross_words.append(cross)
            bonus = state.effective_bonus(tile.row, tile.col)
            # Existing cross letters were previously placed, so their bonuses never apply.
            old_sum = sum(
                0 if state.grid[r][c].is_blank else LETTER_VALUES[state.grid[r][c].letter or "A"]
                for r, c in _cross_existing_positions(state, tile.row, tile.col, pr, pc)
            )
            new_value = 0 if tile.is_blank else LETTER_VALUES[tile.letter] * LETTER_MULTIPLIER[bonus]
            score += (old_sum + new_value) * WORD_MULTIPLIER[bonus]
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


def _cross_checks(state: BoardState, lexicon: Gaddag, direction: Direction) -> dict[tuple[int, int], set[str]]:
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


def _materialise_move(state: BoardState, anchor: tuple[int, int], direction: Direction,
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


def _candidate_starts(state: BoardState, direction: Direction, rack_size: int) -> list[tuple[int, int]]:
    """Line starts that can reach an existing tile or perpendicular anchor."""
    dr, dc = (0, 1) if direction == "H" else (1, 0)
    if not _has_tiles(state):
        return ([(7, col) for col in range(max(0, 8 - rack_size), 8)] if direction == "H"
                else [(row, 7) for row in range(max(0, 8 - rack_size), 8)])
    anchors = set(_anchors(state))
    starts: list[tuple[int, int]] = []
    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            before_row, before_col = row - dr, col - dc
            if _in_bounds(before_row, before_col) and _letter(state, before_row, before_col):
                continue
            for offset in range(rack_size + 1):
                current_row, current_col = row + dr * offset, col + dc * offset
                if not _in_bounds(current_row, current_col):
                    break
                if _letter(state, current_row, current_col) or (offset < rack_size and (current_row, current_col) in anchors):
                    starts.append((row, col))
                    break
    return starts


def generate_moves(state: BoardState, lexicon: Gaddag, limit: int = 6) -> list[Move]:
    """Generate from line starts with a compact forward DAWG and cross checks."""
    if limit <= 0:
        return []
    initial_rack = Counter(state.rack)
    board_has_tiles = _has_tiles(state)
    best: list[Move] = []

    def rank(move: Move) -> tuple[int, str, int, int, str]:
        return (-move.score, move.word, move.row, move.col, move.direction)

    def consider(move: Move) -> None:
        key = (move.word, move.row, move.col, move.direction)
        for index, current in enumerate(best):
            if (current.word, current.row, current.col, current.direction) == key:
                if rank(move) < rank(current):
                    best[index] = move
                    best.sort(key=rank)
                return
        if len(best) < limit or rank(move) < rank(best[-1]):
            best.append(move)
            best.sort(key=rank)
            del best[limit:]

    directions: tuple[Direction, ...] = ("H", "V")
    for direction in directions:
        move_direction: Direction = direction
        dr, dc = (0, 1) if move_direction == "H" else (1, 0)
        checks = _cross_checks(state, lexicon, move_direction)
        for start_row, start_col in _candidate_starts(state, move_direction, sum(initial_rack.values())):
            rack = initial_rack.copy()

            def walk(row: int, col: int, state_id: int, tiles: list[PlacedTile]) -> None:
                # A word can end only before an empty square or at the board edge;
                # an adjacent existing tile must be consumed as part of the word.
                if lexicon.terminal(state_id) and (not _in_bounds(row, col) or not _letter(state, row, col)):
                    word, word_row, word_col = _materialise_move(state, (start_row, start_col), move_direction, tiles)
                    connected = (_touches_board(state, tiles) if board_has_tiles
                                 else any(tile.row == 7 and tile.col == 7 for tile in tiles))
                    if len(word) >= 2 and tiles and connected:
                        score, cross_words = _score_move(state, word, word_row, word_col, move_direction, tiles)
                        consider(Move(word=word, row=word_row, col=word_col, direction=move_direction,
                                      score=score, tiles=tiles.copy(), cross_words=cross_words,
                                      bingo=len(tiles) == 7))
                if not _in_bounds(row, col):
                    return
                existing = _letter(state, row, col)
                if existing:
                    child = lexicon.transition(state_id, existing)
                    if child is not None:
                        walk(row + dr, col + dc, child, tiles)
                    return
                if len(tiles) >= sum(initial_rack.values()):
                    return
                for char, child in lexicon.children(state_id):
                    if char not in checks[(row, col)]:
                        continue
                    if rack[char] > 0:
                        rack[char] -= 1
                        tiles.append(PlacedTile(row=row, col=col, letter=char))
                        walk(row + dr, col + dc, child, tiles)
                        _ = tiles.pop()
                        rack[char] += 1
                    if rack["?"] > 0:
                        rack["?"] -= 1
                        tiles.append(PlacedTile(row=row, col=col, letter=char, is_blank=True))
                        walk(row + dr, col + dc, child, tiles)
                        _ = tiles.pop()
                        rack["?"] += 1

            walk(start_row, start_col, lexicon.root, [])
    return best
