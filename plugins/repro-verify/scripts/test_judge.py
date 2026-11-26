"""Tests for judge.py and guard_check.py — run against real throwaway git repos.

    uv run --with pytest python -m pytest -q scripts/
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import judge  # noqa: E402

# ---------------------------------------------------------------- pure rules


def test_expect_accepts_bare_string_as_contains():
    assert judge.parse_expect("boom") == {"contains": "boom"}


def test_expect_rejects_empty():
    assert judge.parse_expect({}) is None
    assert judge.parse_expect(None) is None


def test_judge_command_needs_every_expectation():
    exp = {"exit_code": "nonzero", "contains": "Traceback"}
    assert judge.judge_command(exp, exit_code=1, output="Traceback x", timed_out=False)
    assert not judge.judge_command(exp, exit_code=0, output="Traceback x", timed_out=False)
    assert not judge.judge_command(exp, exit_code=1, output="ok", timed_out=False)
    assert not judge.judge_command(exp, exit_code=1, output="Traceback", timed_out=True)


def test_not_contains():
    exp = {"not_contains": "42"}
    assert judge.judge_command(exp, exit_code=0, output="41", timed_out=False)
    assert not judge.judge_command(exp, exit_code=0, output="42", timed_out=False)


def test_command_not_run_detection():
    exp = {"exit_code": "nonzero"}
    assert judge.command_did_not_run(exp, exit_code=127, output="", timed_out=False)
    assert judge.command_did_not_run(
        exp, exit_code=1, output="ModuleNotFoundError: No module named 'x'", timed_out=False
    )
    # a normal exit is never 'not run', whatever it printed
    assert not judge.command_did_not_run(
        exp, exit_code=0, output="ModuleNotFoundError: No module named 'x'", timed_out=False
    )
    # a claim that EXPECTS the import error is not 'not run'
    exp2 = {"contains": "No module named"}
    assert not judge.command_did_not_run(exp2, exit_code=1, output="No module named y", timed_out=False)
    # a grep hit that merely mentions the error is not 'not run'
    assert not judge.command_did_not_run(
        exp, exit_code=1, output="x.py:12:    except ModuleNotFoundError:", timed_out=False
    )


def test_quote_basis_rules():
    reqs = [("", "Make the parser reject empty input with a clear error message.")]
    ok = judge.ExpectedFrom("request", quote="reject empty input with a clear error")
    assert judge.check_expected_basis(ok, reqs) is None
    short = judge.ExpectedFrom("request", quote="the")
    assert "too short" in judge.check_expected_basis(short, reqs)
    invented = judge.ExpectedFrom("request", quote="must never crash on unicode input at all")
    assert "not found" in judge.check_expected_basis(invented, reqs)
    assert judge.check_expected_basis(None, reqs) == judge.REASON_NO_BASIS


def test_quote_ignores_whitespace_layout():
    reqs = [("", "reject empty\n   input with a\tclear error")]
    ef = judge.ExpectedFrom("request", quote="reject empty input with a clear error")
    assert judge.check_expected_basis(ef, reqs) is None


def test_quote_restricted_to_named_requirement():
    reqs = [("", "body text only here"), ("R1", "alpha beta gamma delta epsilon zeta"), ("R2", "other words entirely here ok")]
    ef = judge.ExpectedFrom("request", quote="alpha beta gamma delta epsilon", req_id="R2")
    assert judge.check_expected_basis(ef, reqs) is not None
    ef1 = judge.ExpectedFrom("request", quote="alpha beta gamma delta epsilon", req_id="R1")
    assert judge.check_expected_basis(ef1, reqs) is None


def test_location_and_test_id_shapes():
    assert judge.check_expected_basis(judge.ExpectedFrom("unchanged_contract", location="a/b.py:12"), []) is None
    assert judge.check_expected_basis(judge.ExpectedFrom("unchanged_contract", location="/abs.py:1"), []) is not None
    assert judge.check_expected_basis(judge.ExpectedFrom("existing_test", test="tests/t.py::test_a"), []) is None
    assert judge.check_expected_basis(judge.ExpectedFrom("existing_test", test="tests/t.py"), []) is not None


def test_write_refusal_scratch_only():
    s = ".adversarial_scratch"
    assert judge.write_refusal(f"{s}/test_x.py", s) is None
    assert judge.write_refusal("src/app.py", s) is not None
    assert judge.write_refusal(f"{s}/../src/test_x.py", s) is not None
    assert judge.write_refusal(f"{s}/helper.py", s) is not None  # not a test file


def test_parse_findings_drops_findings_without_repro():
    raw = json.dumps(
        {
            "findings": [
                {"claim": "no repro", "severity": "high"},
                {"claim": "ok", "repro": {"kind": "command", "command": "true", "expect": "x"}},
            ]
        }
    )
    out = judge.parse_findings(raw)
    assert [f.claim for f in out] == ["ok"]


def test_parse_findings_from_fenced_prose():
    raw = 'Here:\n```json\n{"findings": []}\n```\n'
    assert judge.parse_findings(raw) == []
    assert judge.parse_findings("not json") is None


# ---------------------------------------------------------------- end to end on real repos


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef half(x):\n    return x / 2\n")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "base")
    return r


def _change(repo: Path) -> None:
    """The 'agent's change': adds clamp() (buggy for negatives) and leaves add() alone."""
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef half(x):\n    return x / 2\n\n\n"
        "def clamp(x, lo, hi):\n    return min(x, hi)  # bug: ignores lo\n"
    )


def _run_judge(repo: Path, findings: dict, request: str) -> list[dict]:
    scratch = repo / judge.DEFAULT_SCRATCH
    scratch.mkdir(exist_ok=True)
    (scratch / "findings.json").write_text(json.dumps(findings))
    (scratch / "request.md").write_text(request)
    res = subprocess.run(
        [
            sys.executable,
            str(HERE / "judge.py"),
            "run",
            "--repo", str(repo),
            "--base", "main",
            "--findings", str(scratch / "findings.json"),
            "--request", str(scratch / "request.md"),
            "--python", sys.executable,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)["results"]


REQUEST = "Add clamp(x, lo, hi) that limits x to the closed range from lo to hi inclusive."


def test_real_defect_is_confirmed_and_baseline_controlled(repo: Path):
    _change(repo)
    scratch = repo / judge.DEFAULT_SCRATCH
    scratch.mkdir()
    (scratch / "test_clamp.py").write_text(
        "from calc import clamp\n\ndef test_lower_bound():\n    assert clamp(-5, 0, 10) == 0\n"
    )
    findings = {
        "findings": [
            {
                "severity": "high",
                "claim": "clamp ignores the lower bound",
                "file": "calc.py",
                "line": 10,
                "repro": {"kind": "test", "path": f"{judge.DEFAULT_SCRATCH}/test_clamp.py"},
                "expected_from": {
                    "kind": "request",
                    "quote": "limits x to the closed range from lo to hi inclusive",
                },
            }
        ]
    }
    [r] = _run_judge(repo, findings, REQUEST)
    # Before the change clamp does not exist: collection fails → confirmed with that recorded.
    assert r["status"] == "candidate", r
    assert r["baseline_not_run"] is True


def test_same_result_before_change_is_not_confirmed(repo: Path):
    _change(repo)
    findings = {
        "findings": [
            {
                "claim": "half() of an int is a float",
                "repro": {
                    "kind": "command",
                    "command": "python -c 'import calc; print(type(calc.half(4)).__name__)'",
                    "expect": {"contains": "float"},
                },
                "expected_from": {"kind": "unchanged_contract", "location": "calc.py:5"},
            }
        ]
    }
    [r] = _run_judge(repo, findings, REQUEST)
    assert r["status"] == "unconfirmed"
    assert r["reason"] == judge.REASON_SAME_BEFORE


def test_command_defect_only_after_change_is_candidate(repo: Path):
    _change(repo)
    findings = {
        "findings": [
            {
                "claim": "clamp(-5,0,10) returns -5",
                "repro": {
                    "kind": "command",
                    "command": "python -c 'import calc; print(calc.clamp(-5, 0, 10))'",
                    "expect": {"contains": "-5"},
                },
                "expected_from": {
                    "kind": "request",
                    "quote": "limits x to the closed range from lo to hi inclusive",
                },
            }
        ]
    }
    [r] = _run_judge(repo, findings, REQUEST)
    # Before the change the command ran (AttributeError is an ordinary failure, not a missing
    # module or command) and did not print -5: the result holds only after the change.
    assert r["status"] == "candidate", r
    assert r["baseline_not_run"] is False


def test_command_that_cannot_run_before_is_never_confirmed(repo: Path):
    _change(repo)
    (repo / "newdep.py").write_text("def answer():\n    return -1\n")  # added by the change
    findings = {
        "findings": [
            {
                "claim": "answer() is negative",
                "repro": {
                    "kind": "command",
                    "command": "python -c 'import newdep; print(newdep.answer())'",
                    "expect": {"contains": "-1"},
                },
                "expected_from": {"kind": "unchanged_contract", "location": "calc.py:1"},
            }
        ]
    }
    [r] = _run_judge(repo, findings, REQUEST)
    # Before the change `import newdep` dies with ModuleNotFoundError: no control, no confirm.
    assert r["status"] == "unconfirmed"
    assert r["baseline_not_run"] is True


def test_passing_repro_is_unconfirmed(repo: Path):
    _change(repo)
    findings = {
        "findings": [
            {
                "claim": "add is broken",
                "repro": {
                    "kind": "command",
                    "command": "python -c 'import calc; print(calc.add(1, 2))'",
                    "expect": {"contains": "4"},
                },
                "expected_from": {"kind": "unchanged_contract", "location": "calc.py:1"},
            }
        ]
    }
    [r] = _run_judge(repo, findings, REQUEST)
    assert r["status"] == "unconfirmed"
    assert r["reason"] == judge.REASON_EXPECT_MISMATCH


def test_no_basis_is_unconfirmed(repo: Path):
    _change(repo)
    scratch = repo / judge.DEFAULT_SCRATCH
    scratch.mkdir()
    (scratch / "test_c.py").write_text(
        "from calc import clamp\n\ndef test_lb():\n    assert clamp(-5, 0, 10) == 0\n"
    )
    findings = {
        "findings": [
            {"claim": "lower bound", "repro": {"kind": "test", "path": f"{judge.DEFAULT_SCRATCH}/test_c.py"}}
        ]
    }
    [r] = _run_judge(repo, findings, REQUEST)
    assert r["status"] == "unconfirmed"
    assert r["reason"] == judge.REASON_NO_BASIS


def test_verifier_editing_source_is_refused(repo: Path):
    _change(repo)
    subprocess.run(
        [sys.executable, str(HERE / "judge.py"), "snapshot", "--repo", str(repo), "--base", "main"],
        check=True, capture_output=True, text=True,
    )
    (repo / "calc.py").write_text("tampered\n")  # the verifier 'fixed' something
    scratch = repo / judge.DEFAULT_SCRATCH
    (scratch / "findings.json").write_text('{"findings": []}')
    (scratch / "request.md").write_text(REQUEST)
    res = subprocess.run(
        [
            sys.executable, str(HERE / "judge.py"), "run", "--repo", str(repo), "--base", "main",
            "--findings", str(scratch / "findings.json"), "--request", str(scratch / "request.md"),
            "--python", sys.executable,
        ],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 3
    assert "changed outside" in res.stderr


def test_bytecode_and_tool_caches_are_not_tampering(repo: Path):
    """Found in a live run: the verifier ran `python -c 'import calc'` in a repo with no
    .gitignore, `__pycache__/` appeared, and the judge refused the whole run."""
    _change(repo)
    subprocess.run(
        [sys.executable, str(HERE / "judge.py"), "snapshot", "--repo", str(repo), "--base", "main"],
        check=True, capture_output=True, text=True,
    )
    for junk in ("__pycache__/calc.cpython-312.pyc", ".pytest_cache/v/x", "pkg/__pycache__/a.pyc", ".ruff_cache/x"):
        p = repo / junk
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    results = _run_judge(repo, {"findings": []}, REQUEST)
    assert results == []


def test_real_tree_is_untouched(repo: Path):
    _change(repo)
    before = (repo / "calc.py").read_text()
    findings = {
        "findings": [
            {
                "claim": "x",
                "repro": {"kind": "command", "command": "rm -f calc.py; echo gone", "expect": "gone"},
                "expected_from": {"kind": "unchanged_contract", "location": "calc.py:1"},
            }
        ]
    }
    _run_judge(repo, findings, REQUEST)
    assert (repo / "calc.py").read_text() == before


# ---------------------------------------------------------------- guard_check


def test_guard_check_flags_a_test_that_passes_without_the_change(repo: Path):
    import guard_check  # noqa: PLC0415

    _change(repo)
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_guard.py").write_text(
        textwrap.dedent(
            """
            import sys, pathlib
            sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
            import calc

            def test_guards_clamp():
                assert calc.clamp(20, 0, 10) == 10

            def test_vacuous():
                assert calc.add(1, 2) == 3
            """
        )
    )
    report = guard_check.check(repo, "main", python=sys.executable)
    by_name = {t["file"]: t for t in report["tests"]}
    entry = by_name["tests/test_guard.py"]
    assert entry["head"]["passed"] == 2
    # Without the change: clamp is gone → test_guards_clamp fails, test_vacuous still passes.
    assert entry["without_change"]["failed"] == 1
    assert entry["without_change"]["passed"] == 1
    assert entry["verdict"] == "partly_vacuous"
    assert (repo / "calc.py").read_text().count("clamp") == 1  # real tree untouched
