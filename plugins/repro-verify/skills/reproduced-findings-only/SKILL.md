---
name: reproduced-findings-only
description: Verify an agent's change by trying to break it, and count a finding only when a harness reproduces it after the change and not before it. Use after a change passes its own tests and before it is merged — "is this really done?", "review this change", "verify the fix", a PR written by an agent, or a review full of plausible-sounding but unverified findings.
---

# Count only reproduced findings

An LLM reviewer that reads a diff and guesses produces confident false positives; an agent's own
tests share the agent's blind spots. Both looked fine on changes that later broke. What works is
what a careful engineer does: **try to break the change, and treat a finding as real only when a
failing check proves it.**

## The rules (each one closed a real false positive)

1. **No repro, no finding.** Every finding carries a test file or a command plus the output that
   proves the defect. Something other than the model runs it.
2. **Baseline control.** Run the same repro on the tree *before* the change. Same result before
   → the repro does not depend on the change ("a grep that finds some text" is the classic case).
   A test that cannot even be collected before (it imports what the change adds) may still count —
   say so. A *command* that cannot run before is never confirmed.
3. **Expected basis.** "Holds only after the change" proves the change *changed* behaviour, not
   that the new behaviour is *wrong*. Each finding names where the expected behaviour comes from:
   a verbatim quote of the request (≥ 20 chars, ≥ 3 words — a short quote matches anything), an
   existing test that passed before and fails after, or a `file:line` of code the request did not
   touch. Check it mechanically.
4. **Intent check.** A separate judge, given the whole request, asks: does the expected behaviour
   contradict what the request asked to change — including a *different* requirement than the
   one the verifier quoted? If so, it is the intended change, not a regression.
5. **The verifier cannot edit.** It writes only test files into a scratch directory; the tree is
   fingerprinted before and checked after. Repros run in throwaway copies, never the real tree.
6. **"Did not run" is not "wrong".** A repro that dies on an import or collection error is
   unconfirmed with that reason — distinct from "the claim was false".

## How to run it

With the `repro-verify` plugin: `/adversarial-verify [base] [issue#|request-file]`, and
`/guard-check [base]` for "do the new tests depend on the change at all?".

Without the plugin, follow the same rules by hand: write the repro, run it on the change, run it
on `git stash`/a worktree at the base, and only then call it a defect.

## Reading the result

- Confirmed findings are defects a test proved. Fix them, and keep the repro as a regression test.
- Unconfirmed findings are suspicions. Read the reason before acting — "same result before the
  change" and "conflicts with the requested change" usually mean the reviewer was wrong.
- An empty result means nothing reproducible was found, not that the change is correct.
