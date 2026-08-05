"""Fail CI when detect-secrets reports any finding."""

from __future__ import annotations

import json
from pathlib import Path
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_secrets_scan.py SCAN_JSON")
        return 2
    try:
        report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"SECRETS ERROR: could not read scan report ({type(exc).__name__})")
        return 1
    findings = report.get("results")
    if not isinstance(findings, dict):
        print("SECRETS ERROR: scan report has no results object")
        return 1
    if findings:
        print(f"SECRETS ERROR: detect-secrets reported findings in {len(findings)} file(s)")
        for filename, entries in sorted(findings.items()):
            print(f"  {filename}: {len(entries) if isinstance(entries, list) else 'unknown'} finding(s)")
        return 1
    print("No secrets detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
