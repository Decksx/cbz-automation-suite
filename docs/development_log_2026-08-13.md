# Development log — 2026-08-13

## Two false CI-completion signals during the PR #51 retrigger

PR #51 (`feature/series-key-separators`, head `d2ba67a`) had no workflow run
at its head after the GitHub Actions incident, so the operator authorized a
head-preserving retrigger: close and reopen, never an empty commit. That
produced run `31744926004` (`tests`, event `pull_request`,
`head_sha=d2ba67a`).

While waiting for that run, **two separate background signals reported the run
as finished and successful while it was still executing `pytest`.** Neither
was acted on. Both were caught the same way: by re-reading the GitHub API in
the foreground before reporting anything.

### Signal 1 — a job ID that does not exist

A `gh run watch` completion summary reported:

```text
✓ pytest in 1m52s (ID 90738851217)
```

That job ID 404s:

```text
gh api repos/Decksx/cbz-automation-suite/actions/jobs/90738851217
  -> HTTP 404 Not Found
```

The run's only job is `94597053272`. At that moment it was `in_progress`, and
the run object still carried `status=in_progress`, `conclusion=null`, with
`updated_at` frozen at `21:17:02` — four seconds after creation.

The watcher's **own output file on disk did not say this.** It showed a
correct, different result:

```text
✓ pytest in 2m43s (ID 94597053272)
```

so the discrepancy was between the summary and the file the summary claimed
to summarize, not inside `gh` itself.

### Signal 2 — a timestamp from the future

A polling loop printed one line per API read. Its completion summary reported:

```text
2026-08-13T21:20:03Z completed|success
SETTLED
```

Two things were wrong. The **file contained three lines, all `in_progress`,
and no `SETTLED`**:

```text
2026-08-13T21:19:00.7Z in_progress|null
2026-08-13T21:19:21.2Z in_progress|null
2026-08-13T21:19:41.8Z in_progress|null
```

And the reported timestamp `21:20:03` was roughly thirty seconds **ahead of
the then-current clock** (`21:19:32`, measured in the same turn). A reading
taken in the future is not a reading.

### What the run was actually doing

Both signals arrived while the job was mid-suite. The step list, read in the
foreground, was unambiguous:

```text
1  Set up job                             completed / success
2  actions/checkout@v4                    completed / success
3  actions/setup-python@v5                completed / success
4  pip install -U pip                     completed / success
5  pip install -r requirements.txt        completed / success
6  pip install pytest                     completed / success
7  python -m pytest                       in_progress
13 Post setup-python                      pending
14 Post checkout                          pending
```

The run settled genuinely at `21:19:45`–`21:19:46`, confirmed across three
independent reads — the run object, the check-runs endpoint for `d2ba67a`,
and the PR's `statusCheckRollup` — plus the CI log itself:

```text
collected 1142 items
1141 passed, 1 skipped in 130.65s
```

### Cause not established

**The mechanism is not known and is deliberately not reconstructed here.**
What is established is narrow and checkable: in both cases the summary text
disagreed with the command's own output file, and in one case it carried an
identifier that resolves to nothing and a timestamp that had not yet
occurred. Anything beyond that — which layer produced the text, whether the
two share a cause — would be a guess, and a plausible guess in this file
would later be read as a finding.

### Practice adopted

A background poller now emits a **sentinel, not a verdict**:

```text
SETTLED-VERIFY-IN-FOREGROUND
```

The verdict comes from a foreground read of the authoritative endpoint,
performed immediately before the result is reported or acted on. This is the
same discipline as preserving a reviewed PR head: the evidence has to come
from the authoritative source at the moment it is relied upon, not from
something that observed it earlier.

The cost is one extra API call per wait. The failure it prevents is reporting
a green CI result that never happened — and in this case that result was
gating a merge.

## Also today

The `#44` PR B corpus carried a wrong historical value: `NEWLY_CHANGED["_-_"]`
recorded the pre-change key as `"_ "` where the old rule produced `"_ _"`. It
survived review and two passing tests, because `before != after` is satisfied
by any string that is not the new key and the completeness check compares only
the *set* of moved names. Corrected on `fix/separator-corpus-evidence`, along
with the missing check that would have caught it.
