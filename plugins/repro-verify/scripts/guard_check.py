#!/usr/bin/env python3
"""Counterfactual guard check: do the tests a change added actually guard the change?

For every test file the change added or modified, run it twice in throwaway copies:
  head            the tree as it is now
  without_change  the same tree with every NON-test change reverted to base
                  (the tests themselves are kept)

A test that still passes without the change does not depend on it. That is fine for a
regression test of existing behaviour, and a smell for a test written to prove a fix —
so this is a report, never a gate. Counts come from pytest's own summary, so a file that
silently stopped collecting shows up as "not run", not as green.

    guard_check.py --base origin/main [--repo .] [--python PATH] [--json]
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import posixpath
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import judge  # noqa: E402

_TEST_PATTERNS = ("test_*.py", "*_test.py")
_COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped)")


def _is_test(rel: str) -> bool:
    return any(fnmatch.fnmatch(posixpath.basename(rel), p) for p in _TEST_PATTERNS)


def _counts(output: str) -> dict[str, int]:
    out = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    lines = [ln for ln in output.splitlines() if re.search(r"\b(passed|failed|error)", ln)]
    for n, kind in _COUNT.findall(lines[-1] if lines else ""):
        out["errors" if kind.startswith("error") else kind] += int(n)
    return out


def _changed(repo: Path, base: str) -> tuple[list[str], list[str], list[str]]:
    """(changed test files, modified non-test files, added non-test files)."""
    status = judge._git(repo, "diff", "--no-renames", "--name-status", base).splitlines()
    tests, modified, added = [], [], []
    for line in status:
        code, _, rel = line.partition("\t")
        if not rel:
            continue
        if _is_test(rel):
            if code != "D":
                tests.append(rel)
        elif code == "A":
            added.append(rel)
        elif code != "D":
            modified.append(rel)
    for rel in judge._git(repo, "ls-files", "-o", "--exclude-standard").splitlines():
        if rel.startswith(judge.DEFAULT_SCRATCH + "/"):
            continue
        (tests if _is_test(rel) else added).append(rel)
    return tests, modified, added


def _without_change(repo: Path, copy: Path, base: str, modified: list[str], added: list[str]) -> None:
    for rel in added:
        (copy / rel).unlink(missing_ok=True)
    for rel in modified:
        data = judge._git(repo, "show", f"{base}:{rel}", text=False)
        (copy / rel).write_bytes(data)


def check(repo: Path, base: str, python: str | None = None, timeout_s: int = judge.DEFAULT_TIMEOUT_S) -> dict:
    repo = Path(judge._git(repo, "rev-parse", "--show-toplevel").strip())
    env = judge.detect_env(repo, python, timeout_s)
    tests, modified, added = _changed(repo, base)
    report: dict = {"base": base, "reverted": {"modified": modified, "added": added}, "tests": []}
    if not (modified or added):
        report["note"] = "the change touches only tests; nothing to revert"
        return report
    for rel in tests:
        argv = [*env.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", rel]
        entry: dict = {"file": rel}
        head_copy = judge.make_copy(repo)
        try:
            head = judge._run(argv, head_copy, env)
        finally:
            judge.drop_copy(head_copy)
        entry["head"] = {"exit": head.exit_code, **_counts(head.output)}
        if head.exit_code != 0:
            entry["verdict"] = "not_green_at_head"
            entry["output"] = judge.clip(head.output, 800)
            report["tests"].append(entry)
            continue
        cut = judge.make_copy(repo)
        try:
            _without_change(repo, cut, base, modified, added)
            res = judge._run(argv, cut, env)
        finally:
            judge.drop_copy(cut)
        counts = _counts(res.output)
        entry["without_change"] = {"exit": res.exit_code, **counts}
        if res.exit_code in (2, 3, 4, 5) and not counts["passed"] and not counts["failed"]:
            entry["verdict"] = "cannot_import_without_change"  # guards by import, says nothing finer
        elif counts["failed"] == 0 and counts["errors"] == 0:
            entry["verdict"] = "vacuous"  # every test passes without the change
        elif counts["passed"] > 0:
            entry["verdict"] = "partly_vacuous"
        else:
            entry["verdict"] = "guards"
        entry["output"] = judge.clip(res.output, 800)
        report["tests"].append(entry)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", required=True)
    ap.add_argument("--python")
    ap.add_argument("--timeout", type=int, default=judge.DEFAULT_TIMEOUT_S)
    a = ap.parse_args(argv)
    print(json.dumps(check(Path(a.repo).resolve(), a.base, a.python, a.timeout), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
