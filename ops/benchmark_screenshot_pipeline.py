#!/usr/bin/env python3
"""Measure the production screenshot-to-solutions Python pipeline.

The benchmark intentionally excludes browser upload/render time: those depend on
the network and client device. It includes trusted image validation, local OCR,
word suggestions, generation of twelve moves, and JSON serialization.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
from statistics import median
import sys
from time import perf_counter


ROOT = Path(
    os.getenv("FEUTSOLVER_BENCHMARK_ROOT", str(Path(__file__).resolve().parents[1]))
).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wordfeud_analyzer.move_generator import (  # noqa: E402
    Gaddag,
    board_words,
    generate_moves,
    load_wordlist,
    suggest_words,
)
from wordfeud_analyzer.vision import extract_board  # noqa: E402


def run_once(screenshot: Path, wordlist: Path, lexicon: Gaddag) -> dict[str, object]:
    started = perf_counter()
    extraction = extract_board(screenshot, backend="local")
    after_ocr = perf_counter()
    suggestions = suggest_words(board_words(extraction.state), wordlist)
    after_suggestions = perf_counter()
    moves = generate_moves(extraction.state, lexicon, limit=12)
    after_solve = perf_counter()
    serialized = json.dumps(
        {
            "state": extraction.state.model_dump(mode="json"),
            "confidence": extraction.confidence,
            "word_suggestions": suggestions,
            "moves": [move.model_dump(mode="json") for move in moves],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    finished = perf_counter()
    return {
        "ocr_seconds": after_ocr - started,
        "suggest_seconds": after_suggestions - after_ocr,
        "solve_seconds": after_solve - after_suggestions,
        "serialize_seconds": finished - after_solve,
        "total_seconds": finished - started,
        "confidence": extraction.confidence,
        "board_tiles": sum(cell.letter is not None for row in extraction.state.grid for cell in row),
        "move_count": len(moves),
        "payload_sha256": hashlib.sha256(serialized).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("screenshots", nargs="+", type=Path)
    parser.add_argument("--wordlist", type=Path, default=ROOT / "data/opentaal-wordlist.txt")
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--repetitions", type=int, default=3)
    arguments = parser.parse_args()
    if arguments.warmups < 0 or arguments.repetitions < 1:
        parser.error("warmups must be >= 0 and repetitions must be >= 1")

    lexicon = load_wordlist(arguments.wordlist)
    report: dict[str, object] = {
        "environment": {
            "FEUTSOLVER_MAX_LOCAL_TILE_OCR_WORKERS": os.getenv(
                "FEUTSOLVER_MAX_LOCAL_TILE_OCR_WORKERS", "unset",
            ),
            "OMP_THREAD_LIMIT": os.getenv("OMP_THREAD_LIMIT", "unset"),
            "project_root": str(ROOT),
            "machine": platform.machine(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "warmups": arguments.warmups,
        "repetitions": arguments.repetitions,
        "screenshots": {},
    }
    screenshots_report = report["screenshots"]
    assert isinstance(screenshots_report, dict)
    for screenshot in arguments.screenshots:
        resolved = screenshot if screenshot.is_absolute() else ROOT / screenshot
        for _ in range(arguments.warmups):
            run_once(resolved, arguments.wordlist, lexicon)
        runs = [run_once(resolved, arguments.wordlist, lexicon) for _ in range(arguments.repetitions)]
        timing_names = (
            "ocr_seconds",
            "suggest_seconds",
            "solve_seconds",
            "serialize_seconds",
            "total_seconds",
        )
        screenshots_report[screenshot.name] = {
            "median": {name: median(float(run[name]) for run in runs) for name in timing_names},
            "runs": runs,
        }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
