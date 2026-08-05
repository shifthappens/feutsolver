"""Merge the repository and production Wordfeud word lists during deployment.

The production list is mutable because users can remove suggestions and add
confirmed OCR words.  A plain file copy would either lose those changes or
re-introduce exclusions.  This module keeps the last repository input and the
last merged production result on the server and applies both sides' changes
as a three-way merge.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import tempfile
import unicodedata
from contextlib import contextmanager
from collections.abc import Iterator


WORDLIST_RELATIVE_PATH = Path("data/opentaal-wordlist.txt")
SYNC_DIRECTORY = ".wordlist-sync"
INCOMING_REPOSITORY_NAME = "incoming-repository.txt"
INITIAL_BASE_NAME = "initial-repository.txt"
PREVIOUS_REPOSITORY_NAME = "previous-repository.txt"
PREVIOUS_MERGED_NAME = "previous-merged.txt"
MIN_WORD_LENGTH = 2
MAX_WORD_LENGTH = 15
WORDLIST_LOCK_MODE = 0o664


@dataclass(frozen=True)
class MergeReport:
    """Small, serialisable summary used by the deploy log and tests."""

    production_before: int
    repository: int
    merged: int
    repository_additions: int
    repository_removals: int
    production_additions: int
    production_removals: int


def _normalise_word(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.strip())
    folded = "".join(char for char in decomposed if not unicodedata.combining(char))
    word = folded.casefold()
    if (
        MIN_WORD_LENGTH <= len(word) <= MAX_WORD_LENGTH
        and word.isascii()
        and word.isalpha()
    ):
        return word
    return ""


def read_words(path: Path) -> set[str]:
    """Read a word list in the same plain A-Z shape used by the app."""
    try:
        with path.open(encoding="utf-8") as source:
            return {
                normalised
                for line in source
                if (normalised := _normalise_word(line))
            }
    except FileNotFoundError:
        return set()


def merge_word_sets(
    previous_repository: set[str],
    previous_merged: set[str],
    repository: set[str],
    production: set[str],
) -> tuple[set[str], MergeReport]:
    """Merge two changed copies of the last merged word list.

    Additions from either side are included.  Removals from either side are
    applied last, so a production exclusion cannot be reintroduced by a repo
    deploy (and a local exclusion cannot be undone by a production addition).
    """
    repository_additions = repository - previous_repository
    repository_removals = previous_repository - repository
    production_additions = production - previous_merged
    production_removals = previous_merged - production
    merged = (
        previous_merged
        | repository_additions
        | production_additions
    ) - repository_removals - production_removals
    return merged, MergeReport(
        production_before=len(production),
        repository=len(repository),
        merged=len(merged),
        repository_additions=len(repository_additions),
        repository_removals=len(repository_removals),
        production_additions=len(production_additions),
        production_removals=len(production_removals),
    )


def _atomic_write(path: Path, words: set[str], mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = path.stat().st_mode & 0o777
        except FileNotFoundError:
            mode = 0o664
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="ascii",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            for word in sorted(words):
                _ = temporary.write(f"{word}\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_path, mode)
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


@contextmanager
def _open_wordlist_lock(path: Path) -> Iterator[object]:
    """Open the shared lock with group write permission regardless of umask."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, WORDLIST_LOCK_MODE)
    try:
        try:
            os.fchmod(descriptor, WORDLIST_LOCK_MODE)
        except PermissionError:
            # The app may own a correctly-created lock while the deploy user
            # only has group access.  Do not require ownership to re-apply an
            # already-correct mode, but reject a mode that cannot be shared.
            if os.fstat(descriptor).st_mode & 0o777 != WORDLIST_LOCK_MODE:
                raise
        with os.fdopen(descriptor, "a+", encoding="ascii") as handle:
            descriptor = -1
            yield handle
    finally:
        if descriptor != -1:
            os.close(descriptor)


def merge_wordlist(deploy_path: str | Path) -> MergeReport:
    """Merge the staged repository list into the live list atomically.

    ``initial-repository.txt`` is used only for the first run, when no prior
    sync state exists.  The deploy workflow stages that file from the last
    known production revision, so changes made on either side since that
    revision are included in the first merge as well.
    """
    root = Path(deploy_path)
    wordlist = root / WORDLIST_RELATIVE_PATH
    sync_directory = root / SYNC_DIRECTORY
    incoming_repository = sync_directory / INCOMING_REPOSITORY_NAME
    initial_base = sync_directory / INITIAL_BASE_NAME
    previous_repository_path = sync_directory / PREVIOUS_REPOSITORY_NAME
    previous_merged_path = sync_directory / PREVIOUS_MERGED_NAME
    # Keep this path identical to move_generator._wordlist_file_lock(), even
    # when the app accesses the production list through a release symlink.
    lock_path = root / ".wordlist.lock"

    if not incoming_repository.is_file():
        raise FileNotFoundError(f"staged repository list is missing: {incoming_repository}")
    if not initial_base.is_file() and not (
        previous_repository_path.is_file() and previous_merged_path.is_file()
    ):
        raise FileNotFoundError(
            "wordlist sync state is missing; stage initial-repository.txt for the first merge"
        )

    sync_directory.mkdir(parents=True, exist_ok=True)
    wordlist.parent.mkdir(parents=True, exist_ok=True)
    with _open_wordlist_lock(lock_path) as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        production = read_words(wordlist)
        repository = read_words(incoming_repository)
        if previous_repository_path.is_file() and previous_merged_path.is_file():
            previous_repository = read_words(previous_repository_path)
            previous_merged = read_words(previous_merged_path)
        else:
            previous_repository = read_words(initial_base)
            previous_merged = set(previous_repository)

        merged, report = merge_word_sets(
            previous_repository,
            previous_merged,
            repository,
            production,
        )
        if merged != production:
            _atomic_write(wordlist, merged)
        _atomic_write(previous_repository_path, repository, mode=0o664)
        _atomic_write(previous_merged_path, merged, mode=0o664)
        incoming_repository.unlink()
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("deploy_path", type=Path)
    args = parser.parse_args()
    report = merge_wordlist(args.deploy_path)
    print(
        "wordlist merge: "
        f"production={report.production_before} repository={report.repository} "
        f"merged={report.merged} "
        f"repo(+{report.repository_additions}/-{report.repository_removals}) "
        f"prod(+{report.production_additions}/-{report.production_removals})"
    )


if __name__ == "__main__":
    main()
