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

`config/number-styles.json` is the fallback shared registry. It is empty on
purpose: neither a basename nor the development machine's drive letters
establish media identity. Operators can supply a shared JSON file with
`{"version":1,"roots":[{"path":"<absolute library path>","style":"issue"}]}`.
Use `chapter` for Manga/manhwa/webtoons. Configure both callers to use the
same file. `CBZ_NUMBER_STYLE_REGISTRY` selects an external shared file at
process launch (including GUI-launched watchers), or each CLI can take
`--number-style-registry <file>`. Do not edit live configuration during
development. Both CLIs take `--number-style auto|issue|chapter`, default auto.

Auto selection compares `(st_dev, st_ino)` against both access-path and
resolved-path ancestors. A root registered through UNC therefore matches a
drive-letter subfolder scan. Exactly one registry entry must match; unknown,
overlapping, duplicate, inaccessible or zero-identity roots refuse auto.
An explicit issue/chapter override wins and does not probe registered roots.
Every selection retains registry path, exact content SHA256, selected scope,
style, source, matched root and its physical identity for digest provenance.
The planner revalidates the physical selection before sealing its output.

## Evidence and display

TPB/Full/Vol number labels remain filename candidates. A resolved primary
Number agreeing with a single explicit volume label can corroborate its volume
role; the filename alone cannot do that in the batch planner. Conflicting
numbers/volumes refuse. Ordinary issue/chapter tokens remain distinct from a
collected volume. Explicit TPB/Full markers conflict with chapter style.
Different collection markers refuse a version family rather than authorizing
supersession across issue/collection identity.

Only after a series is resolved does a normalized-equal series folder win its
display spelling. The narrow comparison folds case, spaces and punctuation,
retaining every letter/digit/combining mark and all meaningful words. Thus
`ICECREAMMAN` becomes `Ice Cream Man`, and `68` can use folder `'68`.
`HalloweeNight` versus `Halloween Night`, or `Series Uncensored` versus
`Series`, is a substantive conflict and stays held. The chosen spelling retains
Windows-legal punctuation; the shared formatter replaces illegal characters
with spaces and retains its reserved-name/length/ambiguity guards.

The planner uses the same style and folder display in its second resolution,
including when the scan root itself is the series subfolder. A Manga volume
whose new `vNN` spelling cannot re-establish its volume role remains held; for
example, a single Vol-labelled book with only ComicInfo Number and no Volume
can fail this guard. The policy does not certify a new filename as authority.

Planner version 2 retains `canonical-naming-plan-v1` executor compatibility.
Style selection and exact registry hash are in provenance, hence in the plan
digest. Changing configuration bytes or choosing an explicit style produces
a new digest even when the proposed destination names happen to match.

## Watcher rollout and parity

This is a deliberate live naming change: after deployment and configuration,
new Western imports use `#003`, and collected editions use `v01 (TPB)`.
Configure the shared registry or an explicit style before enabling the new
watcher session. The empty fallback registry refuses auto for every root;
it does not guess a style from a directory's basename. Unknown/overlapping
destinations stay in intake for review. Normal staging cleanup can already
have run before the final destination/style decision; no library landing is
authorized by a failed media decision.

The watcher binds original ComicInfo before its existing per-file rewrite,
then selects style from the final destination under the settled series lock,
after any resolver category override. The shared field mapper preserves
year, collection and edition facts. Tag-sensitive deferred landing and
no-overwrite refusals still apply. Placeholder fallback gets the same selected
number style. A CLI session releases its policy on every exit, so it cannot
silently configure a later embedded caller in the same process.

Arrival filename-derived display fields retain the watcher's existing
authority boundary; they never certify a bulk rename or a version family.
The stricter batch planner can therefore hold an uncertain book that the
arrival path can display. Both paths produce identical names for the same
resolved facts, including collected TPBs with independent edition markers.

## Verification boundary

Temporary fixtures cover registry aliases (including a privilege-free
junction and localhost UNC where available), unknown/overlapping roots,
explicit overrides, subfolder scans, sealed policy provenance, TPB1..6,
unnumbered Full variants, legal punctuation, substantive conflicts and
version-family separation. Caller witnesses exercise real planner output,
real watcher renames, a final destination override, and a real watcher
ComicInfo rewrite/import/re-scan. Focused guard mutants run only in memory.
Exact-head full-suite, mutation and CI receipts are posted on the PR.

No live library, database, watcher process or configuration is changed by
this implementation. Claude owns exact-head review and deliberate rollout;
the later Tower dry run must produce a fresh digest before any batch apply.
