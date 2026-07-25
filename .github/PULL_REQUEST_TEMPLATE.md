<!--
Delete whatever does not apply. A one-line typo fix does not need this whole
form; a change to the write protocol needs all of it.
-->

## What this changes and why

<!-- The failure mode, if this is a fix. What it enables, if it is a feature. -->

## Consistency impact

<!--
Skip only if the change cannot touch the write path.

- Does reconcile() still do no I/O?
- Is every new sink action idempotent, and safe under prev_may_be_missing and
  multiple prev_possible_records?
- If a sync crashes partway through this change, does the next one converge?
-->

## Evidence

<!--
Which tier covers it, and how you know the test would fail without the fix.
"Reverted the change and the new test failed" is the answer that carries
weight; a test that passes both ways is not evidence.

Real-stack behaviour needs `make test-integration`, not the fake.
-->

## Checklist

- [ ] `make ci` passes
- [ ] Docs and ADRs updated in this PR if it changed behaviour they describe
- [ ] No credentials, URLs or document content added to target keys, tracking
      records or logs
- [ ] CHANGELOG entry, unless this is internal-only
