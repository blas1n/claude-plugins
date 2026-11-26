---
description: Check whether the tests a change added actually depend on the change — revert the non-test changes and see which tests still pass.
argument-hint: "[base-ref]"
allowed-tools: Bash, Read
---

Run the counterfactual guard check on this working tree. Argument: `$ARGUMENTS` (base ref,
optional — default: `git merge-base HEAD origin/HEAD`, falling back to `origin/main`, then `main`).

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/guard_check.py" --base <base>
```

It runs each added or modified Python test file twice, in throwaway copies: once as the tree is,
once with every non-test change reverted to the base (tests kept). Report per test file:

- `guards` — every test fails without the change. Good.
- `partly_vacuous` / `vacuous` — some or all tests still pass without the change. Fine for a
  regression test of behaviour that already existed; a smell for a test meant to prove the fix.
  Name the passing tests from the output and say which reading applies.
- `cannot_import_without_change` — the file cannot even be collected without the change (it
  imports what the change adds). That says the test needs the change, nothing finer.
- `not_green_at_head` — the test does not pass now; nothing to judge.

Also print the head vs. without-change counts: a file whose count drops to zero has stopped
collecting, which is not the same as failing. This is a report, never a gate — do not modify
tests or code unless the user asks.
