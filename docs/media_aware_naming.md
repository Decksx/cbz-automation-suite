# Media-aware canonical names (#140)

The approved policy is recorded in issue #140, review 6422344608. Codex
implements; Claude reviews and owns deliberate watcher rollout. Batch apply
still needs a fresh read-only plan, verified backup and David's exact digest
approval. No old Tower plan is approved for this new naming policy.

## Shared grammar

Western issues use `Series #003 (2019).cbz` when a year is resolved. Manga,
manhwa and webtoon chapters use `Series ch. 3.cbz`. Manga volumes retain
`v01` without a TPB marker. A collected Western edition uses
`Series v01 (TPB).cbz`; the collection marker is separate from the existing
edition slot, so `Series v01 (TPB) (Colored).cbz` preserves both facts.

The pure formatter requires a TPB to have a volume and no issue/chapter.
Its parser and formatter enforce the same fixpoint, length, reserved-name
and ambiguity guards. `Full` without a number remains held for review;
David has not confirmed a default volume for it.

## Style registry

`config/number-styles.json` is the shared default registry. It is empty on
purpose: neither a basename nor the development machine's drive letters
establish media identity. Operators can supply a shared JSON file with
`{"version":1,"roots":[{"path":"<absolute library path>","style":"issue"}]}`.
Use `chapter` for Manga/manhwa/webtoons. Configure both callers to use the
same file. Do not edit live configuration during development.

Auto selection compares `(st_dev, st_ino)` against both access-path and
resolved-path ancestors. A root registered through UNC therefore matches a
drive-letter subfolder scan. Exactly one registry entry must match; unknown,
overlapping, duplicate, inaccessible or zero-identity roots refuse auto.
An explicit issue/chapter override wins and does not probe registered roots.
Every selection retains registry path, exact content SHA256, selected scope,
style, source and matched root for digest provenance.

## Implementation checkpoint

The collection grammar and registry primitives are implemented and tested.
Caller wiring, resolved collection evidence, folder-display selection,
watcher/planner parity and the final mutation/full-suite evidence remain to
be completed before #140 can be reviewed or deployed. The primitives alone
change neither caller's live naming behavior.
