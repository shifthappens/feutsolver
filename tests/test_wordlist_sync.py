from pathlib import Path

from ops.wordlist_sync import merge_word_sets, merge_wordlist


def test_wordlist_merge_keeps_both_additions_and_both_exclusions() -> None:
    previous = {"basis", "blijft", "verwijderd"}
    repository = {"basis", "blijft", "lokaal"}
    production = {"basis", "blijft", "productie"}

    merged, report = merge_word_sets(previous, previous, repository, production)

    assert merged == {"basis", "blijft", "lokaal", "productie"}
    assert report.repository_additions == 1
    assert report.repository_removals == 1
    assert report.production_additions == 1
    assert report.production_removals == 1


def test_production_exclusion_wins_over_local_readdition() -> None:
    previous_repository = {"basis", "uitgesloten"}
    previous_merged = {"basis", "uitgesloten"}
    repository = {"basis", "uitgesloten"}
    production = {"basis"}

    merged, _ = merge_word_sets(
        previous_repository,
        previous_merged,
        repository,
        production,
    )

    assert merged == {"basis"}


def test_previous_production_additions_survive_a_later_repo_deploy() -> None:
    merged, _ = merge_word_sets(
        previous_repository={"basis"},
        previous_merged={"basis", "prodlearned"},
        repository={"basis", "lokaal"},
        production={"basis", "prodlearned", "prodnieuw"},
    )

    assert merged == {"basis", "lokaal", "prodlearned", "prodnieuw"}


def test_deploy_merge_updates_live_list_and_remembers_both_bases(tmp_path: Path) -> None:
    root = tmp_path / "release"
    wordlist = root / "data" / "opentaal-wordlist.txt"
    sync = root / ".wordlist-sync"
    wordlist.parent.mkdir(parents=True)
    sync.mkdir(parents=True)
    wordlist.write_text("basis\n", encoding="utf-8")
    (sync / "initial-repository.txt").write_text(
        "basis\nlocalex\nprodex\n",
        encoding="utf-8",
    )
    (sync / "incoming-repository.txt").write_text(
        "basis\nlokaal\nprodex\n",
        encoding="utf-8",
    )

    report = merge_wordlist(root)

    assert report.merged == 2
    assert wordlist.read_text(encoding="utf-8") == "basis\nlokaal\n"
    assert (sync / "previous-repository.txt").read_text(encoding="utf-8") == (
        "basis\nlokaal\nprodex\n"
    )
    assert (sync / "previous-merged.txt").read_text(encoding="utf-8") == "basis\nlokaal\n"
    assert not (sync / "incoming-repository.txt").exists()
    assert (root / ".wordlist.lock").stat().st_mode & 0o777 == 0o664


def test_first_merge_restores_wordlist_omitted_by_legacy_deploy(tmp_path: Path) -> None:
    root = tmp_path / "release"
    wordlist = root / "data" / "opentaal-wordlist.txt"
    sync = root / ".wordlist-sync"
    wordlist.parent.mkdir(parents=True)
    sync.mkdir(parents=True)
    wordlist.write_text("nieuw\n", encoding="utf-8")
    (sync / "initial-repository.txt").write_text("ander\nbasis\n", encoding="utf-8")
    (sync / "incoming-repository.txt").write_text(
        "ander\nbasis\nnieuw\n",
        encoding="utf-8",
    )

    report = merge_wordlist(root)

    assert wordlist.read_text(encoding="utf-8") == "ander\nbasis\nnieuw\n"
    assert (sync / "previous-merged.txt").read_text(encoding="utf-8") == (
        "ander\nbasis\nnieuw\n"
    )
    assert report.production_before == 3
    assert report.merged == 3
