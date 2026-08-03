# CLAUDE.md

Working agreement for assistant-driven sessions on this repository.
Architecture lives in `docs/architecture.md`; decisions in
`docs/engineering_decisions.md`; process in `docs/session_protocol.md`.

## What this is

Windows-first CBZ automation: a SQLite-backed platform that discovers,
inspects, hashes, compares, quarantines, and routes comic archives.
`scripts/` holds the standalone operator CLIs; `comic_automation/` holds
the SQLite operational core (archive, database, jobs, library) that new
work goes into. `apps/cbz_gui.py` is the Tk launcher.

The active workstream is the Version 1 perceptual-hash backfill. Read
`docs/production_handoff_<latest>.md` for authoritative current state.

## Operator-only actions

**The perceptual-hash backfill is launched by the operator, never by
you.** Do not enqueue a batch, start a worker, or run the guarded batch
runner against production. You may read, propose, and prepare the
preflight; the launch itself is the operator's.

**These are off limits unless the operator explicitly asks in the current
session:**

- `G:\ComicAutomation\` — production database, protected backups, run logs
- `X:\` — live Komga library
- `\\tower\` — SMB comic library

Explicit permission is per-request and does not carry forward. Test
fixtures and tmpdirs are the default target for anything that writes.

## Guarded-operation rules

These apply to every operation that changes state:

- **Dry run first.** Anything with a `--dry-run` flag gets run that way
  first, and the plan is reviewed before apply.
- **Quarantine, don't delete.** Deletion is delayed even though a
  separate library backup exists.
- **Report first.** Recovery and repair CLIs report before they act, and
  take an expected count plus a snapshot digest so they refuse to act on
  a state that has moved.
- **Read-only audits are WAL-aware:** `PRAGMA data_version` before, a
  single deferred read transaction, `data_version` after. A changed pair
  rejects the report. File size, mtime, and WAL/SHM presence are
  diagnostics, never concurrency proof.
- If code, preflight, backup, and postflight disagree, stop.

## Commits

- **One file per commit**, not batched. A commit message describes that
  one file's change.
- Branch off `master`; never commit directly to it.
- Commit only when asked. Never `push` without confirmation.
- Prefer a follow-up commit over `--amend` or interactive rebase.
- `git am` on this repository requires `--keep-cr` (see below), and
  interactive git is unavailable in this harness.

## Line endings

Verified with `git ls-files --eol`, not assumed:

```text
scripts/cbz_sanitizer.py            i/crlf  w/crlf
scripts/cbz_library_maintenance.py  i/crlf  w/crlf
everything else                     i/lf    w/crlf
```

`core.autocrlf=true`, no `.gitattributes`. Those two files are stored
with literal CRLF in the index; git leaves them alone because the index
blob already contains CRLF. **Edits to them must emit CRLF line endings**
— an LF line silently mixes endings and shows as whole-file churn. Edits
elsewhere are normalized on commit and need no special handling.

This is also why `git am` needs `--keep-cr`: without it `git mailinfo`
normalizes CRLF inside the patch body and any patch touching those two
files fails to apply.

## Verifying code work

```text
python -m pytest -q
```

Baseline on this Windows checkout: **549 passed, ~40s** at `a84831e`.
Record the count before and after, and reconcile the delta against the
number of tests actually added. CI (`.github/workflows/tests.yml`) runs
the same suite on `windows-latest` / Python 3.11.

- `git diff --check` is meaningful only for the LF files, where it should
  be clean. It is structurally noisy for the two CRLF files above:
  `--check` inspects *added* lines only, and every added line in a
  CRLF-stored file ends with `\r`, which it reports as trailing
  whitespace. This is not a real defect and predates any current work —
  the merged `7c63bc9` produces 65 such warnings. Scope it to the files
  it can actually speak to:

  ```text
  git diff --check -- . ':(exclude)scripts/cbz_sanitizer.py' \
      ':(exclude)scripts/cbz_library_maintenance.py'
  ```
- A passing test is not a working test. Confirm a new test fails when the
  behavior it guards is removed.
- Distinguish pre-existing failures from new ones by checking the same
  failure against unmodified `master`, not by asserting it.
- Prefer the targeted test file while iterating; full suite before
  delivery.

## Documentation

- Audit documents are evidence records. Never rewrite an original
  finding to describe current code — mark it `[RESOLVED <date>]` inline
  and add a resolution log at the top.
- A resolution log states what landed, what was deliberately **not** done
  and on whose authority, and what remains open. Silence is
  indistinguishable from oversight; record deliberate non-decisions at
  the code site, in the audit, or both.
- Architectural choices go in `docs/engineering_decisions.md`; what
  happened on a day goes in `docs/development_log_<date>.md`.

## Scope

- Verify the tree before trusting any document. Documents lag code — an
  entire planned work item was once found already implemented, tests and
  production migration included, while three audits still called it
  outstanding.
- **Measure the environment; never infer it.** Filesystem, volume, and
  access-path behavior is measured on the actual target before it is
  written down or designed against. On 2026-08-02 three such claims were
  asserted from plausible reasoning and all three were wrong: that the
  library volume was SMB (locally attached), that it was NTFS (exFAT at
  the time), and that a share-mode open would detect a concurrent writer
  (0/16). Reasoning about these produces a hypothesis worth testing, not
  a finding. See `docs/engineering_decisions.md`.
- Implement an audit's low-risk tier; leave anything the audit itself
  marked as needing benchmarking, schema design, or environment-specific
  validation.
- Version 1 perceptual hashes are frozen: no change to decoding, resize,
  DCT, accumulation order, or digest semantics during the backfill.
  Output-preserving optimizations need exact-digest regression tests.
- Deferred until a concrete need justifies them: FastAPI control plane,
  remote/GPU workers, distributed tracing, message brokers,
  microservices, PostgreSQL, an SPA frontend, workflow orchestrators.
  Job-queue leases and fencing tokens are deferred deliberately and must
  not be bundled into other queue work.
