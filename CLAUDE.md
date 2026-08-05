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

Measured with `git ls-files --eol` at `14058d0` on 2026-08-05. **This is a
measurement, not an invariant** — a deliberate future edit may change any
of these counts, and re-measuring is the way to find out rather than
trusting this block.

`core.autocrlf=true`, no `.gitattributes`. **52 of 221 tracked files are
not `i/lf`.** Most are archived docs; these are the ones a session
plausibly edits:

```text
file                                 index    CRLF / bare-LF at 14058d0
scripts/cbz_library_maintenance.py   crlf     2578 / 0
scripts/cbz_sanitizer.py             crlf     1616 / 0
scripts/cbz_gap_checker.py           crlf      334 / 0
scripts/__init__.py                  crlf        1 / 0
scripts/cbz_watcher.py               mixed    1281 / 538
scripts/cbz_compilation_resolver.py  mixed     478 / 13
tests/test_normalization.py          crlf      146 / 0
tests/test_series_detection.py       crlf       20 / 0
tests/test_comicinfo.py              mixed     101 / 61
apps/cbz_gui.py                      -text    1317 / 467   (git treats as binary)
```

A `crlf` file is stored with literal CRLF in the index and git leaves it
alone, because the blob already contains CRLF. **Edits to it must emit
CRLF** — an LF line silently mixes endings and shows as whole-file churn.

A **`mixed` file is worse**, because there is no single correct ending to
emit: it must be edited byte-exactly, matching whatever the neighbouring
lines already use at each site.

The failure mode this produces, observed once on `cbz_watcher.py` on
2026-08-05: a line-wise edit normalized the whole file to CRLF and
produced **618 insertions / 565 deletions against a ~60-line change**,
most of it lines deleted and re-added identically. Restoring from
`master` and splicing byte-exactly with the local ending gave 79/27 and
left the bare-LF count untouched. Nothing about that is specific to the
watcher — it is what a line-wise edit does to any mixed file, and the
watcher is simply where it was hit first.

Check the bare-LF count either side of an edit. If it moved, the edit
normalized something.

Everything else is normalized on commit and needs no special handling.
**Check before editing, do not infer from this table** — it was wrong for
50 files until 2026-08-05, and the file it was wrong about was the
watcher.

This is also why `git am` needs `--keep-cr`: without it `git mailinfo`
normalizes CRLF inside the patch body and any patch touching a CRLF or
mixed file fails to apply.

## Verifying code work

```text
python -m pytest -q
```

Verified full-suite baseline:

```text
commit  : 801614590e3ebbed728dfc4d42b84ba438c45836
command : python -m pytest -q
result  : 911 passed, 0 skipped, ~71s
python  : 3.11.3
measured: 2026-08-04, clean tree, Windows checkout
```

A baseline is a measurement, not a target. It names the commit it was
taken at, so a later count that differs is drift to be reconciled rather
than evidence the record is wrong — and so re-measuring is a deliberate
act with its own commit, never a number quietly edited inside unrelated
work. Record the count before and after your change and reconcile the
delta against the number of tests actually added. CI
(`.github/workflows/tests.yml`) runs the same suite on `windows-latest`
/ Python 3.11.

- `git diff --check` is meaningful only for the LF files, where it should
  be clean. It is structurally noisy for `crlf` and `mixed` files:
  `--check` inspects *added* lines only, and every added line in such a
  file ends with `\r`, which it reports as trailing whitespace. This is
  not a real defect and predates any current work — the merged `7c63bc9`
  produces 65 such warnings.

  Derive the exclusions from git rather than maintaining a list, which is
  how the table above went stale. Only `crlf` and `mixed` are excluded:
  `-text` files are binary to git so `--check` never inspects them, and
  `none` files have no lines to flag, so excluding either would blind the
  check for no reason.

  ```bash
  set -euo pipefail
  tmp=$(mktemp)
  trap 'rm -f "$tmp"' EXIT
  git ls-files --eol -z > "$tmp"
  mapfile -d '' EXCLUDES < <(python - "$tmp" <<'PY'
  import sys
  data = open(sys.argv[1], "rb").read()
  out = [b":(exclude)" + rec.split(b"\t", 1)[1]
         for rec in data.split(b"\0")
         if rec.startswith(b"i/crlf") or rec.startswith(b"i/mixed")]
  sys.stdout.buffer.write(b"\0".join(out))
  PY
  )
  git diff --check -- . "${EXCLUDES[@]}"
  ```

  NUL-separated throughout, so a path containing whitespace cannot split
  into two pathspecs — none does today, which is exactly why a
  whitespace-splitting version would pass review and fail later.
  `set -euo pipefail` makes a failure of `git ls-files` itself an error
  rather than an empty exclusion list that silently checks everything.

  Verified on 2026-08-05 at `14058d0`: excludes 45 files, exits 0 on a
  clean tree, exits 2 on a real trailing space added to an LF file, stays
  quiet for the same defect added to an excluded file, and exits 128 when
  run outside a repository.

  Note `git diff --check` does **not** accept `--pathspec-from-file`; it
  exits 129 with "invalid option". The array form above is the reason.
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
- **A measured figure travels with what it measures.** Record the
  numerator, the denominator, and the population — `2/646 (0.31%)`, not
  `0.31%`. Where a measurement admits more than one defensible rate,
  record every rate, the judgment that separates them, and which one the
  decision was actually made on. A number that outlives its qualifier
  becomes a false claim without anyone editing it.
- **Provenance that lives only in a session transcript is not recorded.**
  Transcripts are cleared. When the artifacts no longer establish how
  something was produced, write the gap down as a gap. A plausible
  reconstruction is worse than an acknowledged hole, because it cannot be
  told apart from a fact.
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
