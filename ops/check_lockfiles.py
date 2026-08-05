"""Small, dependency-free CI guard for the checked-in hashed locks."""

from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9_.-]+)==([^\s;]+)")


def direct_requirements(path: Path, seen: set[Path] | None = None) -> tuple[dict[str, str], list[str]]:
    seen = set() if seen is None else seen
    path = path.resolve()
    if path in seen:
        return {}, []
    seen.add(path)
    found: dict[str, str] = {}
    unpinned: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            nested, nested_unpinned = direct_requirements(path.parent / line[3:].strip(), seen)
            found.update(nested)
            unpinned.extend(nested_unpinned)
            continue
        match = REQUIREMENT.match(raw_line)
        if match:
            found[match.group(1).casefold().replace("_", "-")] = match.group(2)
        elif not line.startswith("-"):
            unpinned.append(f"{path.name}: {line}")
    return found, unpinned


def locked_requirements(path: Path) -> tuple[dict[str, str], list[str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    packages: dict[str, str] = {}
    missing_hashes: list[str] = []
    for index, raw_line in enumerate(lines):
        match = REQUIREMENT.match(raw_line)
        if not match:
            continue
        name = match.group(1).casefold().replace("_", "-")
        packages[name] = match.group(2)
        continuation: list[str] = []
        cursor = index + 1
        while cursor < len(lines) and (lines[cursor].startswith(" ") or lines[cursor].startswith("\t")):
            continuation.append(lines[cursor])
            cursor += 1
        if not any("--hash=sha256:" in line for line in continuation):
            missing_hashes.append(name)
    return packages, missing_hashes


def main() -> int:
    failures: list[str] = []
    production, missing = locked_requirements(ROOT / "requirements.txt")
    if missing:
        failures.append(f"requirements.txt entries without hashes: {', '.join(sorted(missing))}")
    for input_name, lock_name in (("requirements-prod.in", "requirements.txt"), ("requirements-dev.in", "requirements-dev.txt")):
        expected, unpinned = direct_requirements(ROOT / input_name)
        failures.extend(f"{input_name} contains an unpinned requirement: {item}" for item in unpinned)
        actual, lock_missing = locked_requirements(ROOT / lock_name)
        if lock_missing:
            failures.append(f"{lock_name} entries without hashes: {', '.join(sorted(lock_missing))}")
        for name, version in expected.items():
            if actual.get(name) != version:
                failures.append(f"{lock_name}: {name} is not pinned to {version}")
    if not production:
        failures.append("requirements.txt is empty")
    if failures:
        for failure in failures:
            print(f"LOCK ERROR: {failure}")
        return 1
    print("Hashed production and development locks are consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
