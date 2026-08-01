# Session Protocol

How an assistant-driven work session on this project should be
structured so that hitting a context or tool-call limit mid-task costs a
"continue" rather than a rewrite.

This document is about *process*. Standing rules — what is off limits,
how commits are shaped, how work is verified — are in `CLAUDE.md`.
Architectural decisions belong in `docs/engineering_decisions.md`; what
happened on a given day belongs in `docs/development_log_<date>.md`.

## The actual failure mode

The assistant works directly in `C:\git\ComicAutomation` and commits as
it goes, so finished work is no longer stranded in a sandbox. What
remains at risk is different and narrower:

- a **half-applied chunk** — code changed, tests not yet written, tree
  in a state no one would want to commit or revert;
- **reasoning that was never written down** — why a fix took the shape
  it did, what was deliberately not done, which finding a change closes.
  That lives only in the session's context until it is committed to a
  file, and the context is the thing that runs out.

Stopping *after* finishing a chunk costs nothing. Stopping *inside* one
costs everything since the last commit, plus the thinking behind it.
Every rule below follows from that asymmetry.

## Rule 1 — A chunk is one file plus its tests plus its documentation

Not one checklist item. Not one audit. One coherent unit that can be
committed on its own and is useful on its own.

```text
good chunk:  perceptual_hashing.py + its 3 new tests + its audit annotation
bad chunk:   "the archive I/O audit's section 3"   (that is three chunks)
```

A chunk is finished when all of the following are true:

- the code change is committed;
- its tests are written, passing, and committed;
- the relevant documentation reflects it;
- nothing about it is still only in the assistant's head.

If a chunk cannot be described in one sentence naming one file, it is
too big and should be split before starting.

The operator can help here: a request scoped to a single file makes the
chunk boundary explicit and lets delivery happen sooner than a request
scoped to a checklist item.

## Rule 2 — Land each chunk before starting the next

Commits are one file each (see `CLAUDE.md`), so a chunk normally lands
as a short series: the source file, then its tests, then the
documentation or audit annotation. That series is the unit of progress —
start it and finish it before opening the next file.

The tension is deliberate. One-file commits keep history reviewable; the
chunk keeps them from being meaningless in isolation. A chunk left
half-committed is worse than either, so do not begin one without the
budget to finish it.

## Rule 3 — Reserve the last portion of the session for closing out

Stop *starting* new work at roughly 70% of the available budget and
spend the remainder committing what is finished, updating documentation,
and summarizing state.

Two finished chunks beat three-and-a-half unfinished ones. When in
doubt, land what you have.

## Session start

1. Read `CLAUDE.md`.
2. Read `docs/production_handoff_<latest>.md` for the current
   authoritative state.
3. Read the most recent `docs/development_log_<date>.md`.
4. Read this file.
5. **Verify the tree before trusting any document.** Documents describe
   intent and history; they can lag the code. Confirm what is actually
   implemented with `git log --oneline`, `git diff --stat`, and by
   reading the source. A roadmap item marked outstanding may already be
   shipped.
6. Confirm `HEAD`, `origin/master`, and working-tree cleanliness before
   changing anything.
7. Record the test baseline by running the suite, rather than assuming
   the number in a document is still current.

Point 5 is not hypothetical. An entire planned work item — the job
enqueue duplicate-row fix — was found already implemented in full,
including its production migration and all 72 required tests, while
three audit documents still read as though it were outstanding.

## Session end, or when the budget runs low

Leave the project in a state a fresh session can resume from:

- every finished chunk committed;
- documentation updated to match;
- a summary stating what landed, what was deliberately not done and on
  what authority, and what remains open;
- any environment discovery worth keeping recorded here or in the
  development log.

Never end mid-chunk with a modified working tree and no record of what
the modification was for.

## Budget efficiency

Habits that waste tool calls, observed rather than theorized:

- **Repeated full test-suite runs.** The suite takes ~40s and one call.
  Run the targeted test file while iterating; run the full suite once
  before delivery, and once more at the end if later chunks touched
  shared code.
- **Piecemeal file reading.** A `grep` then a `sed` then a `view` of the
  same file is three calls that one consolidated read would have
  covered. Read the whole file when it is a few hundred lines.
- **Git history rewriting.** `git commit --amend` and interactive rebase
  invite rework when a command lands somewhere unexpected, and
  interactive git is unavailable in this harness. Prefer a new,
  clearly-labeled follow-up commit over amending an earlier one.
- **Speculating instead of checking.** One command that answers a
  question definitively is cheaper than three messages of reasoning
  about what the answer probably is.

Batch independent shell work into a single call where it is safe to do
so. Keep destructive or state-changing steps in their own call so a
failure is unambiguous.

## Environment facts worth not rediscovering

Recorded so future sessions do not spend calls relearning them.

**`git am` requires `--keep-cr` for this repository.** Two `scripts/`
files are stored with CRLF in the index. Without the flag, `git
mailinfo` normalizes CRLF to LF inside the patch body and any patch
touching them fails to apply with "patch does not apply". Confirmed by
reproduction.

**Line-ending conventions differ by file, at the index level.**
`git ls-files --eol` reports `i/crlf` for `scripts/cbz_sanitizer.py` and
`scripts/cbz_library_maintenance.py`, and `i/lf` for everything else,
with `core.autocrlf=true` and no `.gitattributes`. Edits to those two
files must emit CRLF, which means byte-exact edits rather than
line-oriented ones. `CLAUDE.md` carries the full rule.

**The suite is expected to pass completely on Windows.** 549 passed in
about 40s at `a84831e`. `tests/test_series_detection.py` asserts on
`\\tower\media\comics\...` UNC paths and `tests/test_workflows.py`
imports `tkinter` through `apps/cbz_gui.py`; both are fine here and are
the first things to fail if the suite is ever run somewhere that is not
Windows. Reconcile any delta against the number of tests actually added.

**Dependencies are `requirements.txt` plus `pytest`,** which is not
listed there. CI installs exactly that on `windows-latest` / Python
3.11.

## Verification standards

The project's operating rule — clean reviewed code, read-only preflight,
protected backup, reconciled postflight — applies to production database
work. For code work the equivalent minimum is:

```text
python -m pytest -q
git diff --check
```

Additionally:

- state the test count before and after, and reconcile the delta
  against the number of tests actually added;
- distinguish pre-existing failures from new ones explicitly, and prove
  it by checking the same failure against unmodified `master` rather
  than asserting it;
- flag clearly what could not be verified, and why, so the operator
  knows what is still open.

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
