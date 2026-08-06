"""Deterministic legal-move generation and Wordfeud scoring."""
from __future__ import annotations

from array import array
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from threading import RLock
import unicodedata
from typing import Literal, NamedTuple, TypeAlias, TypedDict, TypeGuard, cast

from .models import BoardState, Move, PlacedTile
from .security import (
    audit_wordlist_mutation,
    file_version,
    log_security_event,
    may_mutate_production_wordlist,
    new_correlation_id,
)

BOARD_SIZE = 15
LETTER_VALUES = {
    "A": 1, "B": 4, "C": 5, "D": 2, "E": 1, "F": 4, "G": 3, "H": 4,
    "I": 2, "J": 4, "K": 3, "L": 3, "M": 3, "N": 1, "O": 1, "P": 4,
    "Q": 10, "R": 2, "S": 2, "T": 2, "U": 2, "V": 4, "W": 5, "X": 8,
    "Y": 8, "Z": 5,
}
ALL_LETTERS = frozenset(LETTER_VALUES)
LETTER_MULTIPLIER = {"NORMAL": 1, "DL": 2, "TL": 3, "DW": 1, "TW": 1}
WORD_MULTIPLIER = {"NORMAL": 1, "DL": 1, "TL": 1, "DW": 2, "TW": 3}
GADDAG_CACHE_VERSION = 6
CACHE_SCHEMA_VERSION = 1
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_CACHE_NODES = 10_000_000
MAX_CACHE_WORDS = 2_000_000
WORDLIST_LOCK_MODE = 0o664
RUNTIME_CACHE_WRITE = os.getenv(
    "FEUTSOLVER_RUNTIME_CACHE_WRITE",
    "0" if os.getenv("APP_ENV", "production").strip().lower() == "production" else "1",
).strip().lower() in {"1", "true", "yes"}
Direction = Literal["H", "V"]
# `array` only became subscriptable at runtime in Python 3.12; the element types
# are quoted so this alias also evaluates on 3.11 without losing type information.
GraphData: TypeAlias = tuple["array[int]", "array[int]", bytearray, bytearray, "array[int]", int]


class _InternalTile(NamedTuple):
    """Lightweight tile data used while exploring the search tree."""

    row: int
    col: int
    letter: str
    is_blank: bool


class _Candidate(NamedTuple):
    """A scored candidate before its public Pydantic model is created."""

    word: str
    row: int
    col: int
    direction: Direction
    score: int
    tiles: tuple[_InternalTile, ...]
    cross_words: tuple[str, ...]


_WORDLIST_WRITE_LOCK = RLock()


@contextmanager
def _open_wordlist_lock(path: Path) -> Iterator[object]:
    """Open the shared lock with group write permission regardless of umask."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, WORDLIST_LOCK_MODE)
    try:
        try:
            os.fchmod(descriptor, WORDLIST_LOCK_MODE)
        except PermissionError:
            # Deployment may own a correctly-created lock while the app only
            # has group access.  Ownership is not needed if the mode is right.
            if os.fstat(descriptor).st_mode & 0o777 != WORDLIST_LOCK_MODE:
                raise
        with os.fdopen(descriptor, "a+", encoding="ascii") as handle:
            descriptor = -1
            yield handle
    finally:
        if descriptor != -1:
            os.close(descriptor)


@contextmanager
def _wordlist_file_lock(path: Path) -> Iterator[None]:
    """Coordinate app writes with the deployment-side wordlist merge."""
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve(strict=False)
    if resolved.name == "opentaal-wordlist.txt" and resolved.parent.name == "data":
        # The app may reach the mutable production list through
        # releases/<sha>/data/opentaal-wordlist.txt, while deployment operates
        # on <deploy>/data/opentaal-wordlist.txt. Lock their shared parent.
        lock_path = resolved.parent.parent / ".wordlist.lock"
    else:
        lock_path = path.with_name(path.name + ".lock")
    with _open_wordlist_lock(lock_path) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
                            target.write(word + "\n")
            # External sorting keeps peak memory bounded for the complete OpenTaal
            # list; -u collapses the duplicates that folding diacritics creates, and
            # any overlap between the two lists.
            subprocess.run(["sort", "-u", str(unsorted_path), "-o", str(sorted_path)], check=True)
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
            starts.append(len(labels))
            terminals.append(bool(old["terminal"]))
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
    # Deliberately use a new name: an older object cache is never opened, even
    # as a migration source.
    return path.with_name(path.name + ".gaddag-cache-v3.json")


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


def _as_bounded_int_list(value: object, *, maximum: int) -> list[int] | None:
    if not isinstance(value, list) or len(value) > maximum:
        return None
    if not all(isinstance(item, int) and not isinstance(item, bool) for item in value):
        return None
    return value


def _read_cached_data(cache: Path) -> tuple[int, tuple[int, ...], str, int, GraphData] | None:
    """Read and strictly validate the non-executable JSON cache format."""
    try:
        if cache.stat().st_size > MAX_CACHE_BYTES:
            return None
        with cache.open("rb") as handle:
            raw = handle.read(MAX_CACHE_BYTES + 1)
        if len(raw) > MAX_CACHE_BYTES:
            return None
        loaded = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    required = {
        "magic", "version", "schema_version", "source_sha256", "source_signature",
        "payload_length", "payload_sha256", "payload",
    }
    if set(loaded) != required or loaded.get("magic") != "FEUTSOLVER-GADDAG":
        return None
    if loaded.get("version") != GADDAG_CACHE_VERSION or loaded.get("schema_version") != CACHE_SCHEMA_VERSION:
        return None
    source_sha256 = loaded.get("source_sha256")
    if not isinstance(source_sha256, str) or len(source_sha256) != 64:
        return None
    source_signature = _as_bounded_int_list(loaded.get("source_signature"), maximum=16)
    if not source_signature:
        return None
    payload = loaded.get("payload")
    if not isinstance(payload, dict):
        return None
    if set(payload) != {"count", "starts", "counts", "terminals", "labels", "targets", "root"}:
        return None
    encoded_payload = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload_length = loaded.get("payload_length")
    payload_sha256 = loaded.get("payload_sha256")
    if (
        not isinstance(payload_length, int)
        or isinstance(payload_length, bool)
        or payload_length != len(encoded_payload)
        or not isinstance(payload_sha256, str)
        or len(payload_sha256) != 64
        or hashlib.sha256(encoded_payload).hexdigest() != payload_sha256
    ):
        return None

    count = payload.get("count")
    root = payload.get("root")
    starts = _as_bounded_int_list(payload.get("starts"), maximum=MAX_CACHE_NODES)
    counts = _as_bounded_int_list(payload.get("counts"), maximum=MAX_CACHE_NODES)
    terminals = _as_bounded_int_list(payload.get("terminals"), maximum=MAX_CACHE_NODES)
    labels = _as_bounded_int_list(payload.get("labels"), maximum=MAX_CACHE_BYTES)
    targets = _as_bounded_int_list(payload.get("targets"), maximum=MAX_CACHE_BYTES)
    if (
        not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= MAX_CACHE_WORDS
        or not isinstance(root, int) or isinstance(root, bool)
        or starts is None or counts is None or terminals is None or labels is None or targets is None
        or len(starts) != len(counts) or len(starts) != len(terminals)
        or len(labels) != len(targets) or not starts or not 0 <= root < len(starts)
        or len(starts) > MAX_CACHE_NODES
    ):
        return None
    node_count = len(starts)
    edge_count = len(labels)
    if any(start < 0 or size < 0 or size > 255 or start + size > edge_count for start, size in zip(starts, counts)):
        return None
    if any(value not in (0, 1) for value in terminals):
        return None
    if any(value < 65 or value > 90 for value in labels):
        return None
    if any(value < 0 or value >= node_count for value in targets):
        return None
    graph: GraphData = (
        array("I", starts), array("B", counts), bytearray(terminals),
        bytearray(labels), array("I", targets), root,
    )
    return GADDAG_CACHE_VERSION, tuple(source_signature), source_sha256, count, graph


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
    source_hash = file_version(source) or hashlib.sha256(b"").hexdigest()
    cache = _cache_path(source)
    cache_reason = "missing" if not cache.exists() else "invalid_or_stale"
    cached = _read_cached_data(cache)
    if cached is not None:
        version, cached_signature, cached_source_hash, count, graph = cached
        if version == GADDAG_CACHE_VERSION and cached_signature == signature and cached_source_hash == source_hash:
            return Gaddag.from_cached_graph(count, graph)
        cache_reason = "invalid_or_stale"

    instance = Gaddag.from_wordlist(source)
    payload = {
        "count": instance.count,
        "starts": list(instance._starts),
        "counts": list(instance._counts),
        "terminals": list(instance._terminals),
        "labels": list(instance._labels),
        "targets": list(instance._targets),
        "root": instance.root,
    }
    encoded_payload = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    document = {
        "magic": "FEUTSOLVER-GADDAG",
        "version": GADDAG_CACHE_VERSION,
        "schema_version": CACHE_SCHEMA_VERSION,
        "source_sha256": source_hash,
        "source_signature": list(signature),
        "payload_length": len(encoded_payload),
        "payload_sha256": hashlib.sha256(encoded_payload).hexdigest(),
        "payload": payload,
    }
    encoded_document = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    cache_written = False
    if RUNTIME_CACHE_WRITE:
        try:
            temporary_cache: Path | None = None
            with tempfile.NamedTemporaryFile(mode="wb", dir=cache.parent, prefix=cache.name + ".", delete=False) as handle:
                temporary_cache = Path(handle.name)
                handle.write(encoded_document)
                handle.flush()
                os.fsync(handle.fileno())
            if temporary_cache is not None:
                temporary_cache.replace(cache)
                try:
                    directory_fd = os.open(cache.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
                cache_written = True
        except OSError:
            # A read-only local development word list still works; it simply is not cached.
            if 'temporary_cache' in locals() and temporary_cache is not None:
                temporary_cache.unlink(missing_ok=True)
    log_security_event(
        event="cache_rebuild",
        correlation_id=new_correlation_id(),
        action="gaddag",
        result="success" if cache_written else "rebuilt_uncached",
        error_category=cache_reason,
        word_count=instance.count,
    )
    return instance


def suggest_words(words: Iterable[str], path: str | Path) -> list[str]:
    """Return normalised words not yet present in the configured word list.

    The returned words are only suggestions. This helper deliberately has no
    write side effect: OCR output must never change the dictionary implicitly.
    """
    target = Path(path)
    try:
        known = {normalise_word(line) for line in target.read_text(encoding="utf-8").split()}
    except FileNotFoundError:
        known = set()
    return sorted({normalise_word(word) for word in words} - known - {""})


def _mutation_words(words: Iterable[str], *, maximum: int = 1000) -> list[str]:
    if isinstance(words, str):
        words = (words,)
    result: list[str] = []
    for word in words:
        if len(result) >= maximum:
            break
        if not isinstance(word, str) or len(word) > 64:
            continue
        normalised = normalise_word(word)
        if normalised and normalised not in result:
            result.append(normalised)
    return result


def _deny_mutation(*, actor: str | None, action: str, path: Path, word_count: int, correlation_id: str) -> None:
    version = file_version(path)
    audit_wordlist_mutation(
        actor=actor, action=action, word_count=word_count, result="denied",
        correlation_id=correlation_id, version_before=version, version_after=version,
    )
    raise PermissionError("WORDLIST-DENIED")


def _atomic_text_replace(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    version_before: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def add_words_to_wordlist(
    words: Iterable[str], path: str | Path, *, actor: str | None = None, correlation_id: str | None = None,
) -> list[str]:
    """Atomically add explicitly confirmed words after server-side authorization."""
    # Production releases expose the mutable list through a symlink. Resolve
    # it before an atomic replace so the replace updates the shared data file,
    # rather than replacing the release-local symlink itself.
    target = Path(path).resolve(strict=False)
    correlation_id = correlation_id or new_correlation_id()
    requested = _mutation_words(words)
    if not may_mutate_production_wordlist(actor):
        _deny_mutation(actor=actor, action="add", path=target, word_count=len(requested), correlation_id=correlation_id)
    with _WORDLIST_WRITE_LOCK, _wordlist_file_lock(target):
        version_before = file_version(target)
        fresh = suggest_words(requested, target)
        if not fresh:
            audit_wordlist_mutation(
                actor=actor, action="add", word_count=0, result="no_change",
                correlation_id=correlation_id, version_before=version_before, version_after=version_before,
            )
            return []
        try:
            current = target.read_text(encoding="utf-8") if target.exists() else ""
            if current and not current.endswith("\n"):
                current += "\n"
            _atomic_text_replace(target, current + "".join(word.lower() + "\n" for word in fresh))
        except OSError:
            version_after = file_version(target)
            audit_wordlist_mutation(
                actor=actor, action="add", word_count=len(fresh), result="error",
                correlation_id=correlation_id, version_before=version_before, version_after=version_after,
            )
            raise
        audit_wordlist_mutation(
            actor=actor, action="add", word_count=len(fresh), result="success",
            correlation_id=correlation_id, version_before=version_before, version_after=file_version(target),
        )
        return fresh


def remove_words_from_wordlist(
    words: Iterable[str], path: str | Path, *, actor: str | None = None, correlation_id: str | None = None,
) -> list[str]:
    """Remove many words in one atomic pass after server-side authorization."""
    # See add_words_to_wordlist: never atomically replace the deployment
    # symlink that points at the shared production wordlist.
    source = Path(path).resolve(strict=False)
    correlation_id = correlation_id or new_correlation_id()
    target_words = _mutation_words(words)
    if not may_mutate_production_wordlist(actor):
        _deny_mutation(actor=actor, action="remove", path=source, word_count=len(target_words), correlation_id=correlation_id)
    if not target_words:
        version = file_version(source)
        audit_wordlist_mutation(
            actor=actor, action="remove", word_count=0, result="no_change",
            correlation_id=correlation_id, version_before=version, version_after=version,
        )
        return []

    temporary_path: Path | None = None
    removed: set[str] = set()
    try:
        with _WORDLIST_WRITE_LOCK, _wordlist_file_lock(source):
            version_before = file_version(source)
            try:
                source_handle = source.open(encoding="utf-8")
            except FileNotFoundError:
                audit_wordlist_mutation(
                    actor=actor, action="remove", word_count=0, result="no_change",
                    correlation_id=correlation_id, version_before=version_before, version_after=version_before,
                )
                return []
            with source_handle:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=source.parent,
                    prefix=source.name + ".",
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    target_set = set(target_words)
                    for line in source_handle:
                        normalised = normalise_word(line)
                        if normalised in target_set:
                            removed.add(normalised)
                            continue
                        temporary.write(line)
                    temporary.flush()
                    os.fsync(temporary.fileno())
            if removed and temporary_path is not None:
                temporary_path.replace(source)
                directory_fd = os.open(source.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                temporary_path = None
            result = [word for word in target_words if word in removed]
            audit_wordlist_mutation(
                actor=actor, action="remove", word_count=len(result),
                result="success" if result else "no_change", correlation_id=correlation_id,
                version_before=version_before, version_after=file_version(source),
            )
        return result
    except OSError:
        version_after = file_version(source)
        audit_wordlist_mutation(
            actor=actor, action="remove", word_count=len(removed), result="error",
                correlation_id=correlation_id, version_before=version_before, version_after=version_after,
        )
        raise
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def remove_word_from_wordlist(
    word: str, path: str | Path, *, actor: str | None = None, correlation_id: str | None = None,
) -> bool:
    """Permanently remove every spelling of one word from a source list."""
    return bool(remove_words_from_wordlist((word,), path, actor=actor, correlation_id=correlation_id))


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
    while _in_bounds(r, c):
        letter = _letter(state, r, c)
        if letter is None:
            break
        before.append(letter)
        r, c = r - dr, c - dc
    after: list[str] = []
    r, c = row + dr, col + dc
    while _in_bounds(r, c):
        letter = _letter(state, r, c)
        if letter is None:
            break
        after.append(letter)
        r, c = r + dr, c + dc
    return "".join(reversed(before)) + center + "".join(after)


def _score_move(state: BoardState, word: str, row: int, col: int, direction: Direction,
                tiles: tuple[_InternalTile, ...]) -> tuple[int, list[str]]:
    dr, dc = (0, 1) if direction == "H" else (1, 0)
    newly = {(tile.row, tile.col): tile for tile in tiles}
    main_sum, main_multiplier = 0, 1
    for index, char in enumerate(word):
        r, c = row + dr * index, col + dc * index
        existing = state.grid[r][c]
        tile = newly.get((r, c))
        if tile is not None:
            value = 0 if tile.is_blank else LETTER_VALUES[char]
            bonus = state.effective_bonus(r, c)
            main_sum += value * LETTER_MULTIPLIER[bonus]
            main_multiplier *= WORD_MULTIPLIER[bonus]
        else:
            main_sum += 0 if existing.is_blank else LETTER_VALUES[char]
    score = main_sum * main_multiplier
    cross_words: list[str] = []
    cross_dr, cross_dc = (1, 0) if direction == "H" else (0, 1)
    for tile in tiles:
        cross = _word_at(state, tile.row, tile.col, cross_dr, cross_dc, tile.letter)
        if len(cross) > 1:
            cross_words.append(cross)
            bonus = state.effective_bonus(tile.row, tile.col)
            # Existing cross letters were previously placed, so their bonuses never apply.
            old_sum = sum(0 if state.grid[r][c].is_blank else LETTER_VALUES[state.grid[r][c].letter or "A"]
                          for r, c in _cross_existing_positions(state, tile.row, tile.col, cross_dr, cross_dc))
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


def _cross_checks(
    state: BoardState,
    lexicon: Gaddag,
    direction: Direction,
    excluded_words: set[str],
) -> dict[tuple[int, int], set[str]]:
    """Allowed letters per empty square, based on the perpendicular word."""
    pr, pc = (1, 0) if direction == "H" else (0, 1)
    allowed: dict[tuple[int, int], set[str]] = {}
    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            if _letter(state, row, col):
                continue
            probe = _word_at(state, row, col, pr, pc, "A")
            if len(probe) == 1:
                allowed[(row, col)] = set(ALL_LETTERS)
            else:
                letters: set[str] = set()
                for char in LETTER_VALUES:
                    cross_word = _word_at(state, row, col, pr, pc, char)
                    if cross_word not in excluded_words and lexicon.contains(cross_word):
                        letters.add(char)
                allowed[(row, col)] = letters
    return allowed


def _candidate_starts(state: BoardState, direction: Direction, rack_size: int,
                      anchors: set[tuple[int, int]] | None = None) -> list[tuple[int, int]]:
    """Line starts that can reach an existing tile or perpendicular anchor."""
    dr, dc = (0, 1) if direction == "H" else (1, 0)
    if not _has_tiles(state):
        return ([(7, col) for col in range(max(0, 8 - rack_size), 8)] if direction == "H"
                else [(row, 7) for row in range(max(0, 8 - rack_size), 8)])
    anchors = anchors if anchors is not None else set(_anchors(state))
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


def generate_moves(
    state: BoardState,
    lexicon: Gaddag,
    limit: int = 12,
    *,
    excluded_words: Iterable[str] = (),
) -> list[Move]:
    """Generate moves, optionally omitting words without rebuilding the lexicon."""
    if limit <= 0:
        return []
    excluded = {normalise_word(word) for word in excluded_words}
    excluded.discard("")
    initial_rack = Counter(state.rack)
    rack_size = sum(initial_rack.values())
    best: list[_Candidate] = []
    connection_squares = set(_anchors(state))

    def rank(move: _Candidate) -> tuple[int, str, int, int, str]:
        return (-move.score, move.word, move.row, move.col, move.direction)

    def consider(word: str, row: int, col: int, direction: Direction, score: int,
                 tiles: list[_InternalTile], cross_words: list[str]) -> None:
        candidate = _Candidate(word, row, col, direction, score, tuple(tiles), tuple(cross_words))
        if any(
            (current.word, current.row, current.col, current.direction)
            == (candidate.word, candidate.row, candidate.col, candidate.direction)
            for current in best
        ):
            return
        if len(best) < limit or rank(candidate) < rank(best[-1]):
            best.append(candidate)
            best.sort(key=rank)
            del best[limit:]

    directions: tuple[Direction, ...] = ("H", "V")
    for direction in directions:
        dr, dc = (0, 1) if direction == "H" else (1, 0)
        checks = _cross_checks(state, lexicon, direction, excluded)
        for start_row, start_col in _candidate_starts(state, direction, rack_size, connection_squares):
            rack = initial_rack.copy()

            def walk(row: int, col: int, state_id: int, tiles: list[_InternalTile],
                     word_letters: list[str], connected: bool) -> None:
                # A word can end only before an empty square or at the board edge;
                # an adjacent existing tile must be consumed as part of the word.
                if (
                    lexicon.terminal(state_id)
                    and (not _in_bounds(row, col) or not _letter(state, row, col))
                ):
                    word = "".join(word_letters)
                    if word not in excluded and len(word) >= 2 and tiles and connected:
                        score, cross_words = _score_move(state, word, start_row, start_col, direction, tuple(tiles))
                        consider(word, start_row, start_col, direction, score, tiles, cross_words)
                if not _in_bounds(row, col):
                    return
                existing = _letter(state, row, col)
                if existing:
                    child = lexicon.transition(state_id, existing)
                    if child is not None:
                        word_letters.append(existing)
                        walk(row + dr, col + dc, child, tiles, word_letters, connected)
                        word_letters.pop()
                    return
                if len(tiles) >= rack_size:
                    return
                for char, child in lexicon.children(state_id):
                    if char not in checks[(row, col)]:
                        continue
                    if rack[char] > 0:
                        rack[char] -= 1
                        tiles.append(_InternalTile(row, col, char, False))
                        word_letters.append(char)
                        walk(row + dr, col + dc, child, tiles, word_letters,
                             connected or (row, col) in connection_squares)
                        word_letters.pop()
                        tiles.pop()
                        rack[char] += 1
                    if rack["?"] > 0:
                        rack["?"] -= 1
                        tiles.append(_InternalTile(row, col, char, True))
                        word_letters.append(char)
                        walk(row + dr, col + dc, child, tiles, word_letters,
                             connected or (row, col) in connection_squares)
                        word_letters.pop()
                        tiles.pop()
                        rack["?"] += 1

            walk(start_row, start_col, lexicon.root, [], [], False)
    return [
        Move(
            word=candidate.word, row=candidate.row, col=candidate.col, direction=candidate.direction,
            score=candidate.score,
            tiles=[PlacedTile(row=tile.row, col=tile.col, letter=tile.letter, is_blank=tile.is_blank)
                   for tile in candidate.tiles],
            cross_words=list(candidate.cross_words), bingo=len(candidate.tiles) == 7,
        )
        for candidate in best
    ]
