# Session Protocol

How an assistant-driven work session on this project should be
structured so that hitting a tool-call or context limit mid-task costs
a "continue" rather than a rewrite.

This document is about *process*, not architecture. Architectural
decisions belong in `docs/engineering_decisions.md`; what happened on a
given day belongs in `docs/development_log_<date>.md`; outstanding
annotation work belongs in `docs/annotation_progress.md`.

## The actual failure mode

The risk is not running out of budget. The risk is running out of
budget **with finished work that exists only inside the assistant's
sandbox**.

The assistant works in an ephemeral Linux container. A cloned repo at
`/tmp/repo`, edits made there, and tests run there all disappear when
the session ends. Work is only durable once it has been written to
`C:\git\ComicAutomation` or exported as a patch the operator has
downloaded.

Stopping *after* delivering a chunk costs nothing. Stopping *before*
delivering costs everything since the last delivery. Every rule below
follows from that asymmetry.

## Rule 1 — Write to the repository, not to the sandbox

Use the Filesystem tools to write validated files straight to
`C:\git\ComicAutomation`. Work becomes durable the moment it is
correct, with no packaging step to run out of budget before.

```text
validate in the container  ->  write via Filesystem MCP  ->  operator commits
```

The container is still the right place to *run* things: install
dependencies, execute pytest, compile-check, experiment. It is the
wrong place to leave anything of value.

Git operations stay with the operator. The assistant does not run
`git add`, `git commit`, or `git push` against the real checkout.

Patch export (`git format-patch` to `/mnt/user-data/outputs/`) remains
a valid fallback when a change genuinely needs to arrive as a commit
series with authored messages. It is not the default, because it
concentrates all delivery risk at the end of the session. If used,
generate the bundle incrementally rather than once at the end.

## Rule 2 — A chunk is one file plus its tests plus its documentation

Not one checklist item. Not one audit. One coherent unit that can be
committed on its own and is useful on its own.

```text
good chunk:  perceptual_hashing.py + its 3 new tests + its audit annotation
bad chunk:   "the archive I/O audit's section 3"   (that is three chunks)
```

A chunk is finished when all of the following are true:

- the code change is written to the repository;
- its tests are written and passing;
- the relevant documentation reflects it;
- nothing about it is still only in the assistant's head.

If a chunk cannot be described in one sentence naming one file, it is
too big and should be split before starting.

The operator can help here: a request scoped to a single file makes the
chunk boundary explicit and lets delivery happen sooner than a request
scoped to a checklist item.

## Rule 3 — Reserve the last portion of the session for delivery

Stop *starting* new work at roughly 70% of the available budget and
spend the remainder writing files out, updating documentation, and
summarizing state.

Two finished, delivered chunks beat three-and-a-half undelivered ones.
When in doubt, deliver.

## Session start

1. Read `docs/production_handoff_<latest>.md` for the current
   authoritative state.
2. Read the most recent `docs/development_log_<date>.md`.
3. Read this file.
4. Read `docs/annotation_progress.md` if the work is documentation or
   annotation.
5. **Verify the tree before trusting any document.** Documents describe
   intent and history; they can lag the code. Confirm what is actually
   implemented with `git log --oneline`, `git diff --stat`, and by
   reading the source. A roadmap item marked outstanding may already be
   shipped.
6. Confirm `HEAD`, `origin/master`, and working-tree cleanliness before
   changing anything.

Point 5 is not hypothetical. An entire planned work item — the job
enqueue duplicate-row fix — was found already implemented in full,
including its production migration and all 72 required tests, while
three audit documents still read as though it were outstanding.

## Session end, or when the budget runs low

Leave the project in a state a fresh session can resume from:

- every finished chunk written to the repository;
- documentation updated to match;
- a summary stating what landed, what was deliberately not done and on
  what authority, and what remains open;
- any environment discovery worth keeping recorded here or in the
  development log.

Never end mid-chunk with the work only in the container.

## Budget efficiency

Habits that waste tool calls, observed rather than theorized:

- **Repeated full test-suite runs.** The suite takes ~35s and one call.
  Run the targeted test file while iterating; run the full suite once
  before delivery, and once more at the end if later chunks touched
  shared code.
- **Piecemeal file reading.** A `grep` then a `sed` then a `view` of the
  same file is three calls that one consolidated read would have
  covered. Read the whole file when it is a few hundred lines.
- **Git history rewriting.** `git commit --amend` and interactive rebase
  invite rework when a command lands somewhere unexpected. Prefer a new,
  clearly-labeled follow-up commit over amending an earlier one.
- **Speculating instead of checking.** One command that answers a
  question definitively is cheaper than three messages of reasoning
  about what the answer probably is.

Batch independent shell work into a single call where it is safe to do
so. Keep destructive or state-changing steps in their own call so a
failure is unambiguous.

## Environment facts worth not rediscovering

Recorded so future sessions do not spend calls relearning them.

**`git am` requires `--keep-cr` for this repository.** `scripts/` files
are CRLF. Without the flag, `git mailinfo` normalizes CRLF to LF inside
the patch body and any patch touching those files fails to apply with
"patch does not apply". Confirmed by reproduction.

**Line-ending conventions differ by directory.** `comic_automation/` is
LF. `scripts/cbz_sanitizer.py` and `scripts/cbz_library_maintenance.py`
are CRLF. New lines must match the file being edited, which means
byte-exact edits rather than line-oriented ones in the `scripts/` tree.

**`git diff --check` is meaningless in the Linux container.** It flags
every CRLF line as trailing whitespace. The gate is only informative on
the Windows checkout with `core.autocrlf` configured. Container output
must not be reported as a real finding.

**Two tests fail in the container for environmental reasons.**
`tests/test_series_detection.py` asserts on `\\tower\media\comics\...`
UNC paths that do not parse equivalently under Linux `pathlib`. They
fail identically on unmodified `master`, so they are a constant, not a
regression signal.

**Container test baseline is branch-dependent.** Record the count at
session start rather than assuming one. For reference:

```text
master @ fe8897b                      530 passed, 2 failed
+ archive I/O hardening branch        542 passed, 2 failed
```

The 2 failures are the UNC-path tests above in both cases. Always
reconcile the delta against the number of tests actually added.

**`tests/test_workflows.py` cannot be collected in the container.** It
imports `apps/cbz_gui.py`, which imports `tkinter`, which is not
installed. Run the suite with `--ignore=tests/test_workflows.py` and
note that the file is unverified.

**Container setup requires two installs.** `pip install
--break-system-packages -r requirements.txt` and a separate
`pip install --break-system-packages pytest`, which is not in
`requirements.txt`.

## Verification standards

The project's operating rule — clean reviewed code, read-only
preflight, protected backup, reconciled postflight — applies to
production database work. For code work the equivalent minimum is:

```text
python3 -m py_compile $(find comic_automation scripts -name "*.py")
python3 -m pytest -q --ignore=tests/test_workflows.py
```

Additionally:

- state the test count before and after, and reconcile the delta
  against the number of tests actually added;
- distinguish pre-existing failures from new ones explicitly, and prove
  it by checking the same failure against unmodified `master` rather
  than asserting it;
- when exporting patches, re-apply them to a fresh checkout and confirm
  the resulting tree is byte-identical;
- flag clearly what could not be verified in the container so the
  operator knows what to re-run on Windows.

A test that passes is not automatically a test that works. Two tests
written in one session passed for the wrong reason and had to be fixed:
one built a "malformed" archive that `zipfile` silently corrected at
write time, and one used `id(object())` for uniqueness where memory
reuse let consecutive values coincide. Confirm a new test fails when the
behavior it guards is absent.

## Scope discipline

Audits classify their own findings by risk. Implement the low-risk tier;
leave anything the audit itself marked as requiring benchmarking,
schema design, or environment-specific validation.

When deliberately not doing something an audit recommends, record that
choice and its authority — in a code comment at the relevant site, in
the audit's resolution log, or both. Silence is indistinguishable from
oversight.

Audit documents are preserved unedited as the evidence record of the
code at the time of audit. Mark closed findings inline and add a
resolution log; never rewrite an original finding to describe current
code.
