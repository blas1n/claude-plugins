---
description: Attack the current change with a separate verifier and report only the defects a harness reproduced (after the change, not before it).
argument-hint: "[base-ref] [issue-number | request-file]"
allowed-tools: Bash, Read, Write, Task
---

Run an adversarial verification of the change in this working tree. Arguments: `$ARGUMENTS`
(first: base ref; second: an issue number or a file holding the request — both optional).

The harness is `${CLAUDE_PLUGIN_ROOT}/scripts/judge.py` (Python 3, standard library only).
Throughout, `SCRATCH=.adversarial_scratch` at the repository root.

1. **Base.** Use the first argument if given. Otherwise the merge base with the default branch:
   `git merge-base HEAD origin/HEAD` (fall back to `origin/main`, then `main`). Say which base you
   used. If the change is empty (`git diff <base>` and no untracked files), stop and say so.

2. **Request.** The verifier and the intent check need the text the change was asked to satisfy.
   - an issue number → `gh issue view <n> --json title,body` (title + body);
   - a file → its content;
   - otherwise the open PR's body (`gh pr view --json title,body`) if there is one, else the task
     the user gave in this conversation, written out faithfully. If none of these exist, ask the
     user for one sentence describing what the change should do.
   Write it to `$SCRATCH/request.md`. If the request has numbered or checklist requirements, also
   write `$SCRATCH/requirements.json` as `[{"id": "R1", "text": "..."}]`.

3. **Snapshot and diff.**
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/judge.py" snapshot --base <base>
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/judge.py" diff --base <base> > .adversarial_scratch/change.diff
   ```

4. **Attack.** Launch the `adversarial-verifier` agent (Task tool) with: the base ref, the scratch
   directory `.adversarial_scratch`, the paths of `request.md` / `requirements.json` /
   `change.diff`, the surface (`code`, or `artifact` when the change is a document, report or
   generated text), and the output path `.adversarial_scratch/findings.json`. Do not give it your
   own opinion of the change.

5. **Judge.** Run the harness — never decide a finding yourself:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/judge.py" run --base <base> \
     --findings .adversarial_scratch/findings.json --request .adversarial_scratch/request.md \
     [--requirements .adversarial_scratch/requirements.json] > .adversarial_scratch/results.json
   ```
   Exit 3 means the verifier changed files outside the scratch directory: report that, show
   `git status`, and stop — do not restore anything without asking the user.

6. **Intent check.** For each result with `"status": "candidate"`, launch the `intent-judge`
   agent once, with the full request, the requirements, the claim, the repro and its
   `expected_from`. `conflict: true`, an unreadable answer, or a failed call → the finding is
   **unconfirmed** ("conflicts with the requested change"). Otherwise it is **confirmed**.

7. **Report**, most severe first:
   - **Confirmed** — claim, file:line, the repro (test path or command), the harness's reason and a
     short excerpt of the output. These are defects a test proved, not opinions.
   - **Unconfirmed** — one line each with the harness's reason. Present them as suspicions only.
   - If there were no findings, say the verifier found nothing it could reproduce — not that the
     change is correct.
   Do not fix anything unless the user asks. Leave `.adversarial_scratch/` in place (it is excluded
   from git) so the user can re-run a repro; mention `rm -rf .adversarial_scratch` to clean up.
