# blas1n-plugins

Claude Code plugins extracted from [BSVibe](https://github.com/BSVibe/bsvibe-app), an AI agent
platform built in 2026 and retired the same year. These are the parts that held up on their own.

```text
/plugin marketplace add blas1n/claude-plugins
/plugin install repro-verify@blas1n-plugins
```

## repro-verify

An agent's own tests share its blind spots, and an LLM reviewer that reads a diff and guesses
produces confident false positives. `repro-verify` does what a careful engineer does instead:
it tries to break the change, and **counts a finding only when a harness reproduces it after the
change and not before it**.

| | |
|---|---|
| `/adversarial-verify [base] [issue#\|request-file]` | A separate verifier attacks the change and must attach a runnable repro to every finding. A standard-library Python harness (`scripts/judge.py`) runs each repro in a throwaway copy of the changed tree, then again on a copy of the tree before the change, checks the finding's stated basis, and refuses to judge at all if the verifier edited anything outside its scratch directory. A separate judge then asks whether the "defect" is actually what the request asked for. |
| `/guard-check [base]` | Reverts every non-test change in a copy of the tree and re-runs the tests the change added. Tests that still pass do not depend on the change — fine for a regression test, a smell for a test meant to prove a fix. |
| skill `reproduced-findings-only` | The rules, for use with or without the commands. |

### Why each rule exists

Every rule closed a false positive seen in production on BSVibe:

- **Baseline control** — a repro that holds before the change proves nothing about it (a grep that
  finds some text was the common case).
- **Expected basis** — a repro that holds only after the change shows the change *changed* a
  behaviour, not that it is *wrong*; the finding must cite the request verbatim (≥ 20 chars,
  ≥ 3 words — a short quote matches anything), an existing test, or untouched code.
- **Intent check** — a "regression" that another requirement of the same request demanded was
  confirmed twice in a row before this check existed.
- **Did-not-run is not wrong** — repros that die on an import error are reported apart from
  claims that were false.

On BSVibe's own runs the verifier confirmed real defects that had passed every other gate — a test
fake missing a new method, a full clone turned shallow by a fetch, and a branch name that let
`--upload-pack=…` run a command.

### Requirements

- `git`, Python 3.10+.
- Repros run with `uv run` when the repo has `uv.lock`, otherwise with the current Python
  (override with `--python`). Python test repros need `pytest` in that environment; JS repros use
  `npx vitest` or `npx jest` from the project.
- The tree is copied per repro (APFS clones on macOS); very large working trees make it slower.

### Running the harness tests

```bash
cd plugins/repro-verify
uv run --no-project --with pytest python -m pytest -q scripts/
```

## License

MIT
