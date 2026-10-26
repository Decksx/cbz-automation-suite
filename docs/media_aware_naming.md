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
and ambiguity guards. `Full` without a number defaults to volume 1 under David's #2991 decision;
its operator-policy evidence remains bound to the original archive facts.

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
An existing scope also refuses a registered root nested physically beneath
it, including one reached through an outside junction: one parent scan cannot
silently apply its style to a differently configured sub-library.
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

A confirmed operator-chosen series folder wins series identity and display
even when ComicInfo uses a substantive alias or typo. The narrow comparison folds case, spaces and punctuation,
retaining every letter/digit/combining mark and all meaningful words. Thus
`ICECREAMMAN` becomes `Ice Cream Man`, and `68` can use folder `'68`.
`HalloweeNight` in a confirmed `Halloween Night` series folder uses the
chosen folder spelling. Number, volume, decensor and content guards remain
independent; a folder decision cannot override those facts. The chosen spelling retains
Windows-legal punctuation; the shared formatter replaces illegal characters
with spaces and retains its reserved-name/length/ambiguity guards.

The planner uses the same style and folder display in its second resolution,
including when the scan root itself is the series subfolder. A Manga volume
whose new `vNN` spelling cannot re-establish its volume role remains held; for
example, a single Vol-labelled book with only ComicInfo Number and no Volume
can fail this guard. The policy does not certify a new filename as authority.
Explicit number style also retains the folder comparison at a scan root;
choosing syntax cannot manufacture series-folder authority. Use
`--series-folder-root` when an unregistered scan root is itself the chosen
series folder. A matched registry category is a flat root, contributing no
series identity; registered subfolder scans inherit series-folder context.

Planner version 2 retains `canonical-naming-plan-v1` executor compatibility.
Style selection and exact registry hash are in provenance, hence in the plan
digest. Changing configuration bytes or choosing an explicit style produces
a new digest even when the proposed destination names happen to match.

## Watcher rollout and parity

This is a deliberate live naming change: after deployment and configuration,
new Western imports use `#003`, and collected editions use `v01 (TPB)`.
The watcher enables media naming only with a nonempty shared registry. With
an unset/empty registry it runs the complete legacy naming path, including
when an explicit number-style flag was supplied; deploying code alone does
not stall imports. Explicit style overrides syntax once a registry is configured.
The planner independently keeps refusing auto without a matching registry.
No caller guesses style from a directory's basename. With a configured registry,
unknown/overlapping destinations stay in intake for review. Staging cleanup can already
have run before the final destination/style decision; no library landing is
authorized by a failed media decision.

The watcher binds original ComicInfo before its existing per-file rewrite,
then selects style from the final destination under the settled series lock,
after any resolver category override. The shared field mapper preserves
year, collection and edition facts. Tag-sensitive deferred landing and
no-overwrite refusals still apply. Placeholder fallback gets the same selected
number style. A CLI session releases its policy on every exit, so it cannot
silently configure a later embedded caller in the same process.

The watcher never formats away unconsumed filename numbers. When the original
stem has number text outside its decided series and consumed number/display
tokens, it retains the existing legacy arrival/deferred display instead of
guessing a chapter or issue. `Naruto 700` and `Naruto 701` therefore retain
distinct names and can both land; this supplies no bulk-planner authority.

A same-file case-only rename uses two steps through a unique temporary sibling,
with Windows no-replace rename on both legs. An occupied unrelated destination
refuses. A failed final step restores the original spelling; if rollback is
also blocked, the archive remains at the logged temporary path and the caller
tracks that retained path as refused. It never overwrites the interloper.

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
Configured-registry end-to-end regressions cover bare numbers, successive and
same-batch arrivals, Windows case renames, empty-registry legacy imports,
explicit-style folder conflicts, nested scopes and retained-file recovery.

No live library, database, watcher process or configuration is changed by
this implementation. Claude owns exact-head review and deliberate rollout;
the later Tower dry run must produce a fresh digest before any batch apply.


## Final operator decisions (#2991, corrected by #2996)

A reboot suffix is a separate `series_year` field in the pure grammar:
`Batman (2016) #003` and `Batman (2026) #003` have distinct identity. The
series year is the only filename year for these series. An issue year of
2020 stays in ComicInfo `<Year>`; it is not appended to the 2016 name.
Plain series without a reboot suffix retain their existing year display.

Confirmed series-folder authority is explicit in field resolution, with the
original ComicInfo Series retained as evidence. It applies to series only,
not Number/Volume, protected identity or supersession. The planner reuses
that authority through proposed-name resolution; the watcher preserves the
reboot suffix through intake-directory cleanup, selected destination and
ComicInfo rewrite. Flat roots cannot acquire a series identity from their
category basename. An unregistered series-root scan declares
`--series-folder-root`, which is sealed in provenance.

An unnumbered Full is `v01 (TPB)`. Its default volume is an operator-policy
fact derived from the original archive, not authority invented from its
proposed filename; that same source-bound default is used in re-resolution.
Literal titles such as Full Metal Panic retain their words. Explicit number
or volume evidence still refuses conflicts. Bare Western volumes and the
year-before-TPB ordering otherwise remain approved as implemented.

## Series years after review 6424897336 (David #3003)

A parenthesised filename year is never discarded. It becomes the decided
series year, the issue year (in the name, or in ComicInfo `<Year>` beside a
reboot series year), or the watcher keeps the legacy arrival name and the
planner holds. Both callers share the decision:

- A year-qualified chosen series folder decides. In `Batman (2016)`, the
  scene name `Batman (2016) #003 (2019).cbz` reads 2016 as the series year
  and checks only 2019 against ComicInfo Year. The result is
  `Batman (2016) #003.cbz`, and `<Year>2019</Year>` is written when ComicInfo
  lacked it. A different filename, ComicInfo Series or Volume year refuses;
  it never overrides the folder.
- Without a folder year, a filename series year (`Batman (2016) #003`) is
  kept only when ComicInfo confirms it: `<Series>Batman (2016)</Series>`
  (David #3003) or the ComicVine/ComicTagger `<Volume>2016</Volume>`.
  Unconfirmed, the watcher keeps the arrival name, so `Batman (2011) #003`
  and `Batman (2016) #003` arriving together both land, and the planner holds.
- A four-digit Volume in 1900-2100 is always the series start year, never a
  collected volume, even without a filename year: `Batman/Batman #003.cbz`
  with `<Volume>2016</Volume>` becomes `Batman (2016) #003.cbz`. This matches
  ComicVine-tagged libraries. A year on a different ComicInfo title (an
  alias) never decides the chosen folder's identity.
- A title-adjacent year counts as a series year only when numbering follows
  it. In an unnumbered `Series Full (2019)` it remains the issue year.

The watcher also uses the media name when a lone bare filename number equals
authoritative ComicInfo Number (`Series 3` with Number 3 becomes
`Series #003`), matching the planner. An unconfirmed bare number is still
never guessed: `Batman (2016) 003 (2019) (Digital) (Zone-Empire).cbz` with
ComicInfo Number 3 becomes `Batman (2016) #003.cbz`, but without ComicInfo it
lands under its legacy arrival name.

An applied unnumbered-Full default leaves `Series v01 (TPB).cbz`. Re-planning
recognises exactly that canonical `v01 (TPB)` name, with no ComicInfo Number
or Volume, as the same operator-policy volume, so the next plan is a no-op.
No other volume gains filename authority.

The never-drop rule is enforced once in the shared formatter: any `(YYYY)`
left after the series year and issue year are consumed refuses, including
after an unmatched title or one the folder replaced (`Batman/Other (2016)
#003` is held, not renamed to `Batman #003`). The planner, which only
renames, also refuses a series-year name unless its issue year is already
in ComicInfo Year. The watcher writes and reads back `<Year>` before it
renames away the year-bearing name. If the writer gives up, the archive
stays in intake under that name and a later pass or restart lands it.
