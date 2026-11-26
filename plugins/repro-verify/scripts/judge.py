#!/usr/bin/env python3
"""Deterministic judge for adversarial findings: a finding counts only when its repro,
run by THIS script (never by the model), shows the defect after the change and NOT before it.

Subcommands
  snapshot  record a fingerprint of the change so `run` can refuse a verifier that edited
            anything outside the scratch directory
  diff      print the change under review (tracked diff vs base + untracked files)
  run       judge a findings file; prints JSON results to stdout

Statuses in the output
  candidate    reproduced after the change, not before, with a checkable basis.
               Still needs the intent check (does the expected behaviour contradict
               what the request asked to change?) before it is reported as confirmed.
  unconfirmed  anything else, with the reason.

Standard library only. Runs every repro in a throwaway copy of the working tree; the real
tree is only read.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_SCRATCH = ".adversarial_scratch"
MAX_FINDINGS = 5
MAX_TEST_CHARS = 100_000
OUTPUT_CHARS = 1500
SEVERITIES = ("high", "medium", "low")
EXPECTED_KINDS = ("request", "existing_test", "unchanged_contract")
MIN_QUOTE_CHARS = 20
MIN_QUOTE_WORDS = 3
DEFAULT_TIMEOUT_S = 300

_TEST_FILE_PATTERNS = ("test_*.py", "*_test.py", "*.test.*", "*.spec.*")
_PYTEST_FAILED = 1
_PYTEST_NOT_RUN = (2, 3, 4, 5)
_JS_FAILED = 1

REASON_NO_BASIS = "no basis: the finding does not say where the expected behaviour comes from"
REASON_BASIS_MISMATCH = "basis does not check out"
REASON_SAME_BEFORE = "same result before the change (the repro does not depend on the change)"
REASON_EXPECT_MISMATCH = "the command's result did not match `expect`"
REASON_TEST_PASSED = "the repro test passed"
REASON_REPRO_MISSING = "the repro test file was never written"
REASON_TIMEOUT = "timed out"

# ------------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Repro:
    kind: str  # "test" | "command"
    path: str | None
    command: str | None
    expect: dict[str, Any]
    content: str | None = None


@dataclass(frozen=True)
class ExpectedFrom:
    kind: str
    quote: str = ""
    req_id: str = ""
    test: str = ""
    location: str = ""


@dataclass(frozen=True)
class Finding:
    severity: str
    claim: str
    file: str | None
    line: int | None
    repro: Repro
    expected_from: ExpectedFrom | None = None


@dataclass
class Result:
    claim: str
    severity: str
    file: str | None
    line: int | None
    repro: dict[str, Any]
    expected_from: dict[str, Any] | None
    status: str = "unconfirmed"
    reason: str = ""
    output: str = ""
    baseline_not_run: bool = False
    not_run: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------------------- parsing


def _text(raw: Any, limit: int = 600) -> str:
    return raw.strip()[:limit] if isinstance(raw, str) else ""


def parse_expect(raw: Any) -> dict[str, Any] | None:
    """``{"exit_code": int|"nonzero", "contains": str, "not_contains": str}``; a bare string
    means 'the output contains this'."""
    if isinstance(raw, str) and raw.strip():
        return {"contains": raw}
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    code = raw.get("exit_code")
    if code == "nonzero" or (isinstance(code, int) and not isinstance(code, bool)):
        out["exit_code"] = code
    for key in ("contains", "not_contains"):
        if isinstance(raw.get(key), str) and raw[key]:
            out[key] = raw[key]
    return out or None


def parse_expected_from(raw: Any) -> ExpectedFrom | None:
    if not isinstance(raw, dict) or raw.get("kind") not in EXPECTED_KINDS:
        return None
    ef = ExpectedFrom(
        kind=raw["kind"],
        quote=_text(raw.get("quote"), 1000),
        req_id=_text(raw.get("req_id") or raw.get("id"), 80),
        test=_text(raw.get("test")),
        location=_text(raw.get("location")),
    )
    needed = {"request": ef.quote, "existing_test": ef.test, "unchanged_contract": ef.location}
    return ef if needed[ef.kind] else None


def _parse_repro(raw: Any) -> Repro | None:
    if not isinstance(raw, dict):
        return None
    if raw.get("kind") == "test":
        path = raw.get("path")
        if not isinstance(path, str) or not path.strip():
            return None
        content = raw.get("content")
        return Repro("test", path.strip(), None, {}, content if isinstance(content, str) else None)
    if raw.get("kind") == "command":
        command, expect = raw.get("command"), parse_expect(raw.get("expect"))
        if not isinstance(command, str) or not command.strip() or expect is None:
            return None
        return Repro("command", None, command.strip(), expect)
    return None


def _load_json(raw: str) -> Any:
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except ValueError:
            return None


def parse_findings(raw: str) -> list[Finding] | None:
    """Findings without a runnable repro are dropped. ``None`` = not the expected JSON."""
    data = _load_json(raw)
    items = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    out: list[Finding] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        repro, claim = _parse_repro(item.get("repro")), item.get("claim")
        if repro is None or not isinstance(claim, str) or not claim.strip():
            continue
        line = item.get("line")
        out.append(
            Finding(
                severity=item.get("severity") if item.get("severity") in SEVERITIES else "medium",
                claim=claim.strip()[:400],
                file=item["file"] if isinstance(item.get("file"), str) else None,
                line=line if isinstance(line, int) and not isinstance(line, bool) else None,
                repro=repro,
                expected_from=parse_expected_from(item.get("expected_from")),
            )
        )
        if len(out) == MAX_FINDINGS:
            break
    return out


# ------------------------------------------------------------------------------- rules


def write_refusal(rel_path: str, scratch_dir: str, content: str | None = None) -> str | None:
    """Why a repro test may not live at ``rel_path`` (``None`` = allowed)."""
    if not rel_path or rel_path.startswith("/") or "\x00" in rel_path:
        return "absolute or empty path"
    clean = posixpath.normpath(rel_path)
    if clean == ".." or clean.startswith("../"):
        return "path escapes the repository"
    if not clean.startswith(scratch_dir.rstrip("/") + "/"):
        return f"repro tests must live in the scratch directory ({scratch_dir})"
    if not any(fnmatch.fnmatch(posixpath.basename(clean), p) for p in _TEST_FILE_PATTERNS):
        return "not a test file name"
    if content is not None and len(content) > MAX_TEST_CHARS:
        return f"test file too large (max {MAX_TEST_CHARS} chars)"
    return None


def normalize_ws(text: str) -> str:
    return " ".join((text or "").split())


_LOCATION = re.compile(r"^[^\s:]+:\d+(?:-\d+)?$")


def split_test_id(test: str) -> tuple[str, str] | None:
    path, sep, name = test.partition("::")
    if not sep or not path.strip() or not name.strip() or path.startswith("/") or ".." in path:
        return None
    return path.strip(), name.strip()


def check_expected_basis(ef: ExpectedFrom | None, requests: list[tuple[str, str]]) -> str | None:
    """The part of the basis check that needs no run. ``requests`` = (id, text) pairs: the
    request body (id ``""``) and each requirement item."""
    if ef is None:
        return REASON_NO_BASIS
    if ef.kind == "request":
        quote = normalize_ws(ef.quote)
        if len(quote) < MIN_QUOTE_CHARS or len(quote.split()) < MIN_QUOTE_WORDS:
            return (
                f"{REASON_BASIS_MISMATCH}: quote too short (needs {MIN_QUOTE_CHARS}+ chars and "
                f"{MIN_QUOTE_WORDS}+ words after whitespace normalisation)"
            )
        pool = [t for i, t in requests if not ef.req_id or i in ("", ef.req_id)]
        if any(quote in normalize_ws(t) for t in pool):
            return None
        return f"{REASON_BASIS_MISMATCH}: quote not found in the request"
    if ef.kind == "existing_test":
        if split_test_id(ef.test) is None:
            return f"{REASON_BASIS_MISMATCH}: existing test must be path::name"
        return None
    if not _LOCATION.match(ef.location) or ef.location.startswith("/"):
        return f"{REASON_BASIS_MISMATCH}: location must be file:line"
    return None


def judge_command(expect: dict[str, Any], *, exit_code: int | None, output: str, timed_out: bool) -> bool:
    if timed_out or exit_code is None:
        return False
    want = expect.get("exit_code")
    if want == "nonzero" and exit_code == 0:
        return False
    if isinstance(want, int) and exit_code != want:
        return False
    if "contains" in expect and expect["contains"] not in output:
        return False
    return not ("not_contains" in expect and expect["not_contains"] in output)


def judge_test(*, exit_code: int | None, failed_code: int, timed_out: bool) -> bool:
    """A repro test confirms only by FAILING as a test, not by erroring out."""
    return not timed_out and exit_code == failed_code


_EXPECTED_ERROR_MARKERS = ("no module named", "modulenotfounderror", "importerror", "not found")
_COMMAND_NOT_RUN = re.compile(
    r"^ModuleNotFoundError: No module named .+$"
    r"|^(?:(?:sh|bash|zsh|dash)(?:: line \d+|: \d+)?: )?[^\s:]+: (?:command )?not found[ \t]*$",
    re.MULTILINE,
)


def _expects_the_error(expect: dict[str, Any]) -> bool:
    wanted = str(expect.get("contains", "")).lower()
    if any(m in wanted for m in _EXPECTED_ERROR_MARKERS):
        return True
    code = expect.get("exit_code")
    return isinstance(code, int) and not isinstance(code, bool) and code in (126, 127)


def command_did_not_run(expect: dict[str, Any], *, exit_code: int | None, output: str, timed_out: bool) -> bool:
    """The command died before showing anything (shell 'not found', missing module) — unless
    the claim itself expects that error."""
    if timed_out or exit_code is None or exit_code == 0 or _expects_the_error(expect):
        return False
    if exit_code in (126, 127):
        return True
    return bool(_COMMAND_NOT_RUN.search(output or ""))


_SHELL = r"(?:(?:sh|bash|zsh|dash)(?:: line \d+)?: )?"
_RUNNER_MISSING = re.compile(
    r"^[^\s:]*python[\d.]*: No module named '?(?:_?pytest|vitest|jest)'?[ \t]*$"
    r"|^error: Failed to spawn: .?(?:pytest|vitest|jest)\b.*$"
    rf"|^{_SHELL}command not found: (?:pytest|vitest|jest|uv|npx)[ \t]*$"
    rf"|^{_SHELL}(?:pytest|vitest|jest|uv|npx): (?:command )?not found[ \t]*$",
    re.MULTILINE,
)
_RUNNER_RAN = re.compile(
    r"^=+ test session starts =+|^collected \d+ items?\b"
    r"|^=+ .*\b(?:passed|failed|errors?)\b.* in [\d.]+s.*=+\s*$|^\d+ (?:passed|failed|errors?)\b",
    re.MULTILINE,
)


def runner_is_missing(output: str) -> bool:
    text = output or ""
    return not _RUNNER_RAN.search(text) and bool(_RUNNER_MISSING.search(text))


def repro_test_did_not_run(*, exit_code: int | None, timed_out: bool, output: str = "") -> bool:
    if timed_out:
        return False
    return exit_code in _PYTEST_NOT_RUN or runner_is_missing(output)


def clip(text: str, limit: int = OUTPUT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"…[+{len(text) - limit} chars]"


# ------------------------------------------------------------------------------- environment


@dataclass(frozen=True)
class Env:
    """How repros run. ``python`` is a command (``uv run python`` or a path)."""

    python: list[str]
    shell_prefix: list[str]  # prefix for `sh -c` (e.g. ["uv", "run"]) so `python` resolves
    path_shim: str | None  # directory put first on PATH so `python` is the chosen interpreter
    timeout_s: int


def detect_env(repo: Path, python: str | None, timeout_s: int) -> Env:
    if python:
        shim = tempfile.mkdtemp(prefix="repro-shim-")
        target = shutil.which(python) or python
        for name in ("python", "python3"):
            os.symlink(target, os.path.join(shim, name))
        return Env([target], [], shim, timeout_s)
    if (repo / "uv.lock").exists() and shutil.which("uv"):
        return Env(["uv", "run", "python"], ["uv", "run"], None, timeout_s)
    return Env([sys.executable], [], None, timeout_s)


def test_runner(path: str, env: Env, repo: Path) -> tuple[list[str], int] | None:
    """(argv, exit code meaning 'a test failed') for a repro test file; ``None`` = unknown."""
    if path.endswith(".py"):
        return [*env.python, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", path], _PYTEST_FAILED
    if re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", path):
        pkg = repo / "package.json"
        tool = "vitest" if pkg.exists() and "vitest" in pkg.read_text(errors="ignore") else "jest"
        argv = ["npx", "--no-install", "vitest", "run", path] if tool == "vitest" else ["npx", "--no-install", "jest", path]
        return argv, _JS_FAILED
    return None


@dataclass
class Ran:
    exit_code: int | None
    output: str
    timed_out: bool


def _run(argv: list[str], cwd: Path, env: Env) -> Ran:
    e = dict(os.environ)
    if env.path_shim:
        e["PATH"] = env.path_shim + os.pathsep + e.get("PATH", "")
    e.setdefault("CI", "true")
    e.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    try:
        p = subprocess.run(argv, cwd=cwd, env=e, capture_output=True, text=True, timeout=env.timeout_s, check=False)
        return Ran(p.returncode, (p.stdout or "") + (p.stderr or ""), False)
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return Ran(None, out if isinstance(out, str) else out.decode(errors="replace"), True)
    except FileNotFoundError as exc:
        return Ran(127, f"{argv[0]}: command not found ({exc})", False)


def _command_argv(command: str, env: Env) -> list[str]:
    return [*env.shell_prefix, "sh", "-c", command]


# ------------------------------------------------------------------------------- git + copies


def _git(repo: Path, *args: str, text: bool = True) -> Any:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=text).stdout


def _exclude_scratch(repo: Path, scratch: str) -> None:
    """Keep the scratch dir out of `git status` and of the change being judged."""
    info = Path(_git(repo, "rev-parse", "--git-path", "info/exclude").strip())
    info = info if info.is_absolute() else repo / info
    info.parent.mkdir(parents=True, exist_ok=True)
    line = f"/{scratch}/"
    existing = info.read_text() if info.exists() else ""
    if line not in existing.splitlines():
        info.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + line + "\n")


#: Byproducts of merely RUNNING code or tools (not edits): a verifier that imports a module in a
#: repo with no .gitignore creates __pycache__/, and that is not tampering.
_CACHE_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis", ".tox", ".nox"}
_CACHE_SUFFIXES = (".pyc", ".pyo")


def _is_cache(rel: str) -> bool:
    parts = rel.split("/")
    return any(p in _CACHE_PARTS for p in parts[:-1]) or rel.endswith(_CACHE_SUFFIXES) or parts[-1] == ".coverage"


def fingerprint(repo: Path, base: str, scratch: str) -> str:
    """Hash of the change outside the scratch dir: tracked diff vs base + untracked files
    (tool caches and bytecode excluded)."""
    h = hashlib.sha256()
    h.update(_git(repo, "diff", "--binary", base, "--", ".", f":(exclude){scratch}", text=False))
    for rel in sorted(_git(repo, "ls-files", "-z", "-o", "--exclude-standard").split("\0")):
        if not rel or rel.startswith(scratch + "/") or _is_cache(rel):
            continue
        h.update(rel.encode())
        p = repo / rel
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def change_text(repo: Path, base: str, scratch: str) -> str:
    diff = _git(repo, "diff", base, "--", ".", f":(exclude){scratch}")
    untracked = [
        r for r in _git(repo, "ls-files", "-o", "--exclude-standard").splitlines() if not r.startswith(scratch + "/")
    ]
    if untracked:
        diff += "\n# untracked files (new, not yet added):\n" + "\n".join(f"#   {u}" for u in untracked) + "\n"
    return diff


def make_copy(repo: Path) -> Path:
    """A throwaway copy of the working tree, ignored files included (venvs, node_modules)."""
    dst = Path(tempfile.mkdtemp(prefix="repro-copy-")) / "tree"
    if sys.platform == "darwin":
        # APFS clone: near-free even for large trees.
        r = subprocess.run(["cp", "-cR", str(repo), str(dst)], capture_output=True, check=False)
        if r.returncode == 0:
            return dst
    shutil.copytree(repo, dst, symlinks=True)
    return dst


def drop_copy(copy: Path) -> None:
    shutil.rmtree(copy.parent, ignore_errors=True)


def to_baseline(repo: Path, copy: Path, base: str) -> None:
    """Turn a copy of the changed tree into the tree BEFORE the change: delete files the change
    added (tracked-added and untracked), then put every tracked file back as it was at base.
    Ignored files stay — the repro still needs its environment."""
    added = _git(repo, "diff", "-z", "--no-renames", "--name-only", "--diff-filter=A", base).split("\0")
    untracked = _git(repo, "ls-files", "-z", "-o", "--exclude-standard").split("\0")
    for rel in added + untracked:
        if rel:
            (copy / rel).unlink(missing_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tf:
        tar = tf.name
    try:
        _git(repo, "archive", base, "-o", tar)
        subprocess.run(["tar", "-xf", tar, "-C", str(copy)], check=True, capture_output=True)
    finally:
        os.unlink(tar)


def _carry(repo: Path, copy: Path, rel: str) -> None:
    src, dst = repo / rel, copy / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


# ------------------------------------------------------------------------------- judging


class Judge:
    def __init__(self, repo: Path, base: str, scratch: str, env: Env, requests: list[tuple[str, str]]):
        self.repo, self.base, self.scratch, self.env, self.requests = repo, base, scratch, env, requests

    def _before(self, argv: list[str], test_path: str | None) -> Ran:
        old = make_copy(self.repo)
        try:
            to_baseline(self.repo, old, self.base)
            if test_path:
                _carry(self.repo, old, test_path)
            return _run(argv, old, self.env)
        finally:
            drop_copy(old)

    def judge(self, f: Finding) -> Result:
        res = Result(
            claim=f.claim, severity=f.severity, file=f.file, line=f.line,
            repro=asdict(f.repro), expected_from=asdict(f.expected_from) if f.expected_from else None,
        )
        res.repro.pop("content", None)
        fresh = make_copy(self.repo)
        try:
            ok, reason = (self._judge_test if f.repro.kind == "test" else self._judge_command)(f, fresh, res)
            if ok:
                ok, reason = self._vet_basis(f, fresh)
        except subprocess.CalledProcessError as exc:
            ok, reason = False, f"could not build the before-change copy ({clip(str(exc.stderr or exc), 200)})"
        finally:
            drop_copy(fresh)
        res.status = "candidate" if ok else "unconfirmed"
        res.reason = reason
        return res

    def _judge_test(self, f: Finding, fresh: Path, res: Result) -> tuple[bool, str]:
        path = f.repro.path or ""
        refusal = write_refusal(path, self.scratch, f.repro.content)
        if refusal:
            return False, refusal
        if f.repro.content is not None:
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.repro.content)
        if not (self.repo / path).is_file():
            res.extra["repro_missing"] = True
            return False, REASON_REPRO_MISSING
        runner = test_runner(path, self.env, self.repo)
        if runner is None:
            return False, "no known test runner for this file type"
        argv, failed_code = runner
        _carry(self.repo, fresh, path)
        after = _run(argv, fresh, self.env)
        res.output = clip(after.output)
        if after.timed_out:
            return False, REASON_TIMEOUT
        if repro_test_did_not_run(exit_code=after.exit_code, timed_out=False, output=after.output):
            res.not_run = True
            return False, f"the repro did not run (collection/import error or no runner, exit {after.exit_code})"
        if not judge_test(exit_code=after.exit_code, failed_code=failed_code, timed_out=False):
            return False, REASON_TEST_PASSED
        before = self._before(argv, path)
        if before.timed_out:
            return False, "before-change control timed out"
        if runner_is_missing(before.output):
            res.baseline_not_run = True
            return False, "before-change control could not run (no test runner)"
        if judge_test(exit_code=before.exit_code, failed_code=failed_code, timed_out=False):
            return False, REASON_SAME_BEFORE
        if before.exit_code == 0:
            return True, "fails only after the change (passes before)"
        if repro_test_did_not_run(exit_code=before.exit_code, timed_out=False):
            # It imports what the change adds: the failure after is all there is to go on.
            res.baseline_not_run = True
            return True, f"fails after the change (cannot be collected before it, exit {before.exit_code})"
        return False, f"before-change control inconclusive (exit {before.exit_code})"

    def _judge_command(self, f: Finding, fresh: Path, res: Result) -> tuple[bool, str]:
        expect, argv = f.repro.expect, _command_argv(f.repro.command or "", self.env)
        after = _run(argv, fresh, self.env)
        res.output = clip(after.output)
        if after.timed_out:
            return False, REASON_TIMEOUT
        if command_did_not_run(expect, exit_code=after.exit_code, output=after.output, timed_out=False):
            res.not_run = True
            return False, f"the repro did not run (command error, exit {after.exit_code})"
        if not judge_command(expect, exit_code=after.exit_code, output=after.output, timed_out=False):
            return False, REASON_EXPECT_MISMATCH
        before = self._before(argv, None)
        if before.timed_out:
            return False, "before-change control timed out"
        if command_did_not_run(expect, exit_code=before.exit_code, output=before.output, timed_out=False):
            res.baseline_not_run = True
            return False, "before-change control could not run — a command repro is never confirmed without it"
        if judge_command(expect, exit_code=before.exit_code, output=before.output, timed_out=False):
            return False, REASON_SAME_BEFORE
        return True, "matches `expect` only after the change"

    def _vet_basis(self, f: Finding, fresh: Path) -> tuple[bool, str]:
        problem = check_expected_basis(f.expected_from, self.requests)
        if problem:
            return False, problem
        ef = f.expected_from
        assert ef is not None
        if ef.kind == "existing_test":
            path, _ = split_test_id(ef.test) or ("", "")
            if not path.endswith(".py"):
                return False, f"{REASON_BASIS_MISMATCH}: only pytest ids are supported for existing_test"
            argv = [*self.env.python, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", ef.test]
            before = self._before(argv, None)
            if before.timed_out or before.exit_code != 0:
                return False, f"{REASON_BASIS_MISMATCH}: the existing test does not pass before the change"
            if f.repro.path != path:
                after = _run(argv, fresh, self.env)
                if not judge_test(exit_code=after.exit_code, failed_code=_PYTEST_FAILED, timed_out=after.timed_out):
                    return False, f"{REASON_BASIS_MISMATCH}: the existing test does not fail after the change"
        return True, "reproduced; basis checks out — intent check pending"


# ------------------------------------------------------------------------------- cli


def _requests(request_file: Path, requirements_file: Path | None) -> list[tuple[str, str]]:
    out = [("", request_file.read_text())]
    if requirements_file:
        data = json.loads(requirements_file.read_text())
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                out.append((str(item.get("id", "")), item["text"]))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("snapshot", "diff", "run"):
        p = sub.add_parser(name)
        p.add_argument("--repo", default=".")
        p.add_argument("--base", required=True, help="git ref of the tree BEFORE the change")
        p.add_argument("--scratch", default=DEFAULT_SCRATCH)
        if name == "run":
            p.add_argument("--findings", required=True)
            p.add_argument("--request", required=True, help="file with the request / issue text")
            p.add_argument("--requirements", help='optional JSON [{"id": "R1", "text": "..."}]')
            p.add_argument("--python", help="interpreter for repros (default: uv run if uv.lock, else this python)")
            p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    a = ap.parse_args(argv)
    repo = Path(a.repo).resolve()
    repo = Path(_git(repo, "rev-parse", "--show-toplevel").strip())
    _exclude_scratch(repo, a.scratch)
    fp_file = repo / a.scratch / ".fingerprint"

    if a.cmd == "snapshot":
        fp_file.parent.mkdir(parents=True, exist_ok=True)
        fp_file.write_text(fingerprint(repo, a.base, a.scratch))
        print(f"snapshot written: {fp_file}")
        return 0
    if a.cmd == "diff":
        sys.stdout.write(change_text(repo, a.base, a.scratch))
        return 0

    if fp_file.exists() and fp_file.read_text().strip() != fingerprint(repo, a.base, a.scratch):
        print(
            "refused: the working tree changed outside the scratch directory since `snapshot` — "
            "the verifier may only write repro tests into the scratch directory. Restore the tree "
            "and re-run.",
            file=sys.stderr,
        )
        return 3
    findings = parse_findings(Path(a.findings).read_text())
    if findings is None:
        print("findings file is not the expected JSON ({\"findings\": [...]})", file=sys.stderr)
        return 2
    env = detect_env(repo, a.python, a.timeout)
    judge = Judge(repo, a.base, a.scratch, env, _requests(Path(a.request), Path(a.requirements) if a.requirements else None))
    results = [asdict(judge.judge(f)) for f in findings]
    print(json.dumps({"base": a.base, "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
