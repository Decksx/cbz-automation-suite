# Development log 2026-10-20 -- SeriesIndex diagnostic correction (#60)

Implementation lead: Codex. Independent reviewer: Claude. Branch
`fix/60-series-index-diagnostics`, from merged master
`00ee8c8e6c6f76a25ad80f2d6f9a29ff04760fb2`. The ownership and scope were
recorded on issue #60 and in hive messages #1682, #1685 and #1686.

## Change and boundary

`SeriesIndex` retains all distinct lexical source paths for each canonical
series key. Same-destination collisions are no longer silently collapsed;
configured destination priority still determines the legacy routing choice.
Same-root ties use a deterministic path order, with identity still reported
unresolved. Canonical series normalization itself is unchanged.

The explicit exclusion list is Syncthing's `.stfolder` and `.stversions`,
case-insensitively. Other dot titles and UUID-looking directory names remain
candidates. Empty canonical keys are reported separately from collisions.

The immutable diagnostics snapshot is JSON-exportable and feeds
`require_clear()`, the zero-ambiguity branch of the documented gate. Empty
keys, collisions, missing roots, disabled scans and manually constructed
unscanned indexes cannot pass that gate. Watcher snapshots use the same lock
as classification and incremental updates. Startup warnings expose the full
diagnostics without changing the shadow/legacy move policy.

Deliberately not done: reviewed-exception manifest authentication, an atomic
filesystem census, physical link/SMB-alias resolution, live ambiguity
remeasurement, v2 activation, staging wiring, adult-signal changes, library
moves, or database changes. The gate's manifest branch needs its own design;
no list of strings silently waives it. Paths use host lexical case rules and
absolute normalization, without NFC-normalizing filesystem spellings.

## Verification

Fresh full-suite baseline on the named master, Windows / Python 3.11.3:
`pytest -n 4 --dist loadfile -q`: **2373 passed, 8 skipped, 62.67 s**.

The implementation adds 33 collected cases in
`tests/test_series_index_diagnostics.py`. All 33 pass. The existing routing,
watcher and canonical-key focused files pass: **402 passed, 1 skipped**.
Full suite after the behavioral changes: **2406 passed, 8 skipped, 64.43 s**,
exactly 33 additional passes and unchanged skips. After final comment and
priority-error wording corrections, the watcher plus new diagnostics tests
pass again: **71 passed**. CI on the delivered exact head is recorded in the
PR; these are local measurements, not a claim that CI already completed.

Selected guard mutations: **22/22 killed** by the new file. Each row is one
independent mutation; counts are failing test cases, not summed unique tests.

| Removed or broken guard | Failing cases |
| --- | ---: |
| same-root path retention | 8 |
| retention of earlier contributors | 14 |
| empty-key reporting | 7 |
| exclusion of known control directories | 6 |
| narrow policy replaced with all dot names | 1 |
| narrow policy extended to UUID titles | 1 |
| repeated-path deduplication | 5 |
| distinct Unicode path spelling preservation | 1 |
| configured destination priority | 1 |
| two-path collision diagnostics | 11 |
| two-path `is_ambiguous` flag | 7 |
| empty-key gate blocker | 3 |
| collision gate blocker | 3 |
| incomplete-scan gate blocker | 3 |
| gate refusal itself | 8 |
| unbuilt-index gate blocker | 1 |
| disabled-scan gate blocker | 1 |
| missing-root gate blocker | 1 |
| disappearing-root propagation | 1 |
| watcher snapshot lock | 1 |
| watcher startup diagnostic reporting | 1 |
| empty-key JSON export | 2 |

For the three reported defects, the named witnesses include
`test_same_destination_keeps_both_paths_and_reports_the_collision`,
`test_known_syncthing_directories_are_excluded_and_recorded`, and
`test_empty_keys_block_the_gate_without_being_mislabelled_collisions`.
The first two mutations above preserve different failure mechanisms; the
empty-key case does not manufacture or rely on a collision.

The mutation harness restored exact working-file bytes after every run. One
harness substitution initially failed to locate a CRLF-delimited watcher
block after the first 19 completed mutants; it restored the files, then the
remaining three ran successfully with newline-aware matching. This was a
harness interruption, not a surviving guard or a product failure. Raw local
results and the standalone harness are outside the repository at
`C:\Users\david\Documents\Codex\control\cbz-ci-pilot-20261020\issue60-mutation-results.json`
and `issue60-mutations.py` in that folder.

No production paths or database were opened for this work. The older handoff
and roadmap describe historical state; this change makes no claim about
current live ambiguity counts or backfill state.

## Independent review follow-up

Claude reviewed `8ef5ab5d643fd2592cd6121fa5efe39f581867c4` on PR #96
([comment 6301524878](https://github.com/Decksx/cbz-automation-suite/pull/96#issuecomment-6301524878)).
It independently found the three defects fixed, reproduced the full-suite
count, and challenged path identity using real NTFS directories. Its own 19
mutants killed 16 and left three survivors. No product correctness defect
was found, but one survivor exposed a missing behavioral test: removing
`_remember`'s priority comparison made the last destination label win for an
aliased path. The original test observed the labels only in the order that
would pass even with that guard removed.

The test now covers both insertion orders. Another regression loads a real
config with both destination labels pointing at the same synthetic root,
builds its index, then records a lower-priority watcher `note_move` and
requires the configured winner to survive. A third covers the defensive
undefined-destination branch with a directly constructed `RoutingConfig`;
`parse/load` already reject that config. No production code was changed.

Baseline before these additional mutations: **36 diagnostics tests pass**.
Last-write-wins now fails **2** cases, named
`test_two_destination_labels_for_the_same_path_do_not_invent_a_collision[order1]`
and `test_aliased_roots_and_later_lower_priority_move_keep_configured_label`.
Silently ignoring the undefined root fails **1** case,
`test_hand_built_config_with_undefined_destination_cannot_pass_the_gate`.
Exact source bytes were restored afterward. Raw results are
`issue60-review-mutation-results.json` in the same local artifact folder.
These are two additional selected mutations, not a rerun of the earlier 22.

The remaining survivor, removing `_default_lister`'s sort, is deliberately
accepted: directory enumeration order no longer determines selection or
diagnostic order. Sorting that input is not a correctness guard.

Full suite after the review additions: **2409 passed, 8 skipped, 66.17 s**.
The change adds **3** collected passes to the prior 2406, for **36** new cases
over master, without changing the skipped population. The new exact head
still needs Claude's re-review and its own CI results before merge.
