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

**The suite is expected to pass completely on Windows.**

```text
commit  : 801614590e3ebbed728dfc4d42b84ba438c45836
command : python -m pytest -q
result  : 911 passed, 0 skipped, ~71s
python  : 3.11.3
measured: 2026-08-04, clean tree, Windows checkout
```

`CLAUDE.md` carries the same record and the rule behind its shape.
`tests/test_series_detection.py` asserts on `\\tower\media\comics\...`
UNC paths and `tests/test_workflows.py` imports `tkinter` through
`apps/cbz_gui.py`; both are fine here and are the first things to fail
if the suite is ever run somewhere that is not Windows. Reconcile any
delta against the number of tests actually added.

The previous entry here read "549 passed in about 40s at `a84831e`" and
had drifted by 362 tests before anyone noticed, because nothing about a
bare count goes stale visibly. Re-measure deliberately, on a clean tree,
naming the commit — and never as a side effect of unrelated work, since
a number corrected in passing is a number nobody reviewed.

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

### Test the guaranteed property, not the mechanics that achieve it

Three tests written on 2026-08-04 were flaky, and all three failed the
same way: the assertion depended on incidental implementation behavior
rather than on the property under test. Each passed locally several
times before failing in CI, and two had already failed once and been
dismissed as environmental. "It passed five times here" measures this
machine's timing, not determinism.

- **Trigger mutations with an explicit event or an injected operation.**
  To prove a payload changing mid-read is rejected, really rewrite the
  file and force the mtime; do not arrange for a `stat` to report
  something different. Drive the real condition the guard exists for.
- **Never use call counts as synchronization.** One test counted
  `os.stat` calls to decide when to simulate a change. `Path.is_file()`
  also stats, and whether `rglob` serves that from a cached `scandir`
  entry varies by platform, so the counter landed differently on CI
  than locally. A call count is an implementation detail, not a clock.
- **Never rely on thread acquisition order.** A test asserting that
  configured destination priority survives a race built a `SeriesIndex`
  with no priority, so every destination ranked equally and the winner
  was whichever thread wrote first. It asserted nothing and passed by
  luck. Use barriers and events to establish order you need, and supply
  the real configuration the assertion depends on.
- **Assert the intended interleaving actually occurred.** A concurrency
  test that silently fails to interleave passes for the wrong reason.
  Set a flag at the injection point and assert it afterwards, so
  "nothing raced" cannot masquerade as "the guard held".
- **Prove a guard is load-bearing by bypassing that exact guard.**
  Remove the specific block and confirm the test fails; restore it and
  confirm it passes. Reverting a whole module usually breaks the import
  instead, which proves nothing about the behavior.

Timestamp- and filesystem-dependent assertions deserve particular
suspicion: the window they exercise is a property of the volume, not of
the code (see `docs/archive_io_resource_audit.md`, Finding 2).

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

## Recording measurements and their provenance

`CLAUDE.md` carries the two standing rules — a measured figure travels
with what it measures, and provenance that lives only in a session
transcript is not recorded. Both were learned the same way, from the
2026-08-03 evidence census, and the mechanism is worth keeping.

### How a number outlives its qualifier

Issue #31 recorded the adult signal as "97.8% coverage against 0.46%
false positives ... (2 confirmed false positives)". Three separate
things had collapsed into one sentence:

```text
negative FP (raw)        : 3/646 (0.46%)   <- quoted in the issue
negative FP (adjudicated): 2/646 (0.31%)   <- what the signal was accepted at
```

Three negative-pool series matched. One, `ERIKA`, was adjudicated a true
positive — a mixed folder holding 3 adult archives among 9 horror ones —
and is therefore not counted in the confirmed false-positive numerator.
The denominator stays 646 for both rates: adjudication changes what
counts as an error, not the size of the population measured against.
`derive_adult.py` prints `len(fp_rows)/len(neg)` with `len(neg)` fixed
at 646, and `derive.txt` reads `2/646`. The acceptance threshold is
*adjudicated* precision ≥ 0.99, so the issue quoted the one rate that
did not correspond to its own criterion, and paired it with a count from
the other.

This paragraph said "removed from the negative denominator" until
2026-08-14, which is the very failure it exists to warn about: the
correct fractions were printed two lines above it, and the prose beside
them still drifted. It is hard to catch by eye because a 645 denominator
rounds to the same 0.31% — so the fraction is the check, and the
percentage is not.

What made this more than a typo: the acceptance criteria *required* the
figure be recorded in code alongside the signal. Implementing against
the issue as written would have moved the conflation into a comment, and
from there into the permanent record, correctly cited to an issue and no
longer checkable against anything. Nobody would have had to make a
mistake for it to become true.

The census artifacts had it right all along — raw and adjudicated
printed one line apart, the adjudication codified as `ADJUDICATED_TP` /
`CONFIRMED_FP` with its rationale in the module docstring. The loss
happened in transcription, at the point where a measurement became a
headline.

So: when writing a figure into an issue, a comment, or a document, carry
the denominator and name the population. When a human judgment sits
between the raw measurement and the quoted one, name the judgment too —
adjudication is an input, not a result, and a rate that depends on it is
not reproducible from the data alone.

### Watch for numeric coincidences

The same issue reports `AgeRating` present on 3 of 646 negative-pool
series — also 0.46%, same numerator and denominator, entirely unrelated
to the false-positive rate. Two identical numbers meaning different
things in one document is a trap for the next reader. Annotate the
collision rather than trusting them to be told apart.

### Identical artifacts prove less than they appear to

Five `rerun*.json` files from that census, named as if they were
before/after comparisons around a specific PR, are byte-identical to one
another. `rerun.py` writes a fixed filename, so the variants were copied
by hand and nothing outside the cleared transcript records which run
produced which.

Byte-identity is consistent with two different histories: runs that
genuinely produced identical output, and copies of a single run that was
never repeated. The files cannot distinguish them, so they do not
establish that any change left classification output unchanged — which
is exactly the conclusion their filenames invite. Record what such
artifacts *cannot* support, explicitly, at the point someone would reach
for them.
