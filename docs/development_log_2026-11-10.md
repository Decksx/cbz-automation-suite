# Development log: 2026-11-10

## Watcher: a downloader's group prefix no longer names the series

### Incident

David stopped the watcher (live at `6e5c3a8`). Chapter files that arrive as
`<Group>_<Chapter|Episode|Ep.> N[ - title]_<hex6>.cbz` kept the scanlation
group as the series. From `Logs\cbz_watcher.log`, 2026-11-10 08:49, folder
"Life of a Demon Hunter":

- `Alpha_Episode 42_2a8cb9.cbz` -> `Alpha Episode 42.cbz`
- `Asura_Chapter 42_d1a897.cbz` -> `Asura Chapter 42.cbz`
- `Myung_Chapter 49_51801c.cbz` -> `Myung Chapter 49.cbz` (The Toy of 100 Baseball Players)

while `Chapter 17_028c98.cbz` became `Survival Supremacy Ch. 17.cbz`.

Cause: `clean_filename` turns underscores into spaces before
`_stem_pipeline` runs, so `normalize_stem` sees `Alpha Episode 42`, which
does not start with a chapter token, and never puts the folder series in
front. The log's prefixes are groups, not series: `The Hours Between`
arrives in SHADOW SYSTEM, My Girlfriend, Bloodbath of the Fallen and more;
`gladys` in From Sandbox to Bed, Obedient Pregnancy, Palace Slave and more;
`Literary Curator` in Lookism (974) and Life of a Demon Hunter (332).

### Changes

- `scripts/cbz_core.py`: `_GROUP_CHAPTER_RE` / `_without_group_prefix`.
  Before cleaning, `<Group>_<Chapter|Ch.|Episode|Ep.> N[ - title]_<hex6>`
  becomes `Chapter N[ - title]_<hex6>`, which the existing pipeline names
  `<Series> Ch. N`, exactly as a group-free arrival. The episode number is
  still read from the arrival's own words (ComicInfo Notes keep
  `Episode: 42`). Only the downloader's layout is matched: the trailing hash
  and an underscore right before the chapter token are required, so
  `Berserk_Chapter 5.cbz` is unchanged; a prefix carrying a volume or
  season (`Vol. 3_Chapter 12_...`) is information and the name is kept. A
  prefix that is the folder series gives the same name as any group.
- `scripts/cbz_watcher.py`: two translations of one chapter now share one
  name. The second's rename was skipped because its sibling already had the
  name, and it landed under its raw arrival name beside the first (a raw
  duplicate, not an overwrite). Such an archive now goes to review
  ("another archive of this arrival has its name"). A name kept because it
  carries a distinguishing tag (`(s2)`, `dropped_round_tags`) still lands as
  before: the tag tells the two apart.

### Deliberately not done

- Titles stay in the existing group-free form, `Ch. 45 Friend`, not
  `Ch. 45 - Friend`; the bracket cleaning still drops `(7)` from
  `Friend (7)`.
- `gladys_Prologue_c3b658.cbz` has no chapter number and still becomes
  `gladys Prologue.cbz`.
- The library (Z:) is not touched; another agent handles existing files.
- Media naming's "unconsumed filename number" (below) is reported, not
  changed.

### Media naming: "kept legacy arrival ... unconsumed filename number"

Confirmed. `_apply_media_naming` reads the facts captured before
`process_cbz_file` renamed the archive, so `watcher_naming` sees
`fact.filename == 'Chapter 17_028c98.cbz'`. The title does not match, the
chapter number 17 is consumed, and the hash's digits (`028`, `98`) remain,
so it raises `KeepArrivalName`. Reproduced directly: `Chapter 17_028c98`
and `Chapter 18_a1b2c3` are kept; `Chapter 19_abcdef` (a hash without
digits) is media-named; `Survival Supremacy Ch. 17.cbz` given directly is
media-named. A fix would be small (drop an underscore-delimited hex6 hash
that contains a letter before the number check), but it is not clearly
right: every hashed arrival would switch from legacy to media naming,
which spells `Survival Supremacy ch. 17.cbz` instead of today's `Ch. 17`.

**Decision (David, 2026-11-10): keep the uppercase `Ch.` as it is.**
`watcher_naming` and its hash handling are deliberately not changed; the
log line stays for hashed arrivals, and they keep the legacy name.

### Tests

New `tests/test_group_prefixed_chapter_names.py` (34): the live names and
the log's main groups, plain and display parsing; a series-named prefix;
names that must not change; the episode number; and an end-to-end watcher
pass in which Alpha's Episode 42 and Asura's Chapter 42 meet (one lands as
`Life of a Demon Hunter Ch. 42.cbz`, the other goes to review, both books
kept). At `6e5c3a8` the 22 behaviour tests fail and the 12 "unchanged" ones
pass.

Guards proven by bypass (failing tests): a group prefix is dropped before
cleaning 22; Episode/Ep. count as chapter tokens 10; only with the
downloader hash 2; a volume or season prefix is kept 4; the episode number
survives the rewrite 1; a same-named sibling goes to review 1; a
tag-distinguished name is not a same-name review 2.

Watcher edits byte-exact: no unchanged line changed its ending; the 22 added
lines are CRLF like their surroundings; bare LF stays 863.

Full suite: **5,178 passed / 10 skipped / 0 failed** (5,188 collected =
5,154 on master + 34 new).

### PR #178 review round 1 (Codex, at `aa4c248`)

**P2:** `_without_group_prefix` protected Vol./Season prefixes but took
`Part A`, `Part B`, `Part 1` and `Part 2` for groups. `Part A_Chapter 5_ab12cd`
and `Part B_Chapter 5_ab12cd` both became `Berserk Ch. 5.cbz`: one landed and
the other went to review as a second copy, and the landed name lost its
part. Codex's `test_review178_part_prefixes.py` (SHA256 `6b020e53...8c49`)
fails 6/6 at `aa4c248` and passes 6/6 on base.

Fix: `_is_group_prefix`, conservative. A prefix is not a group when it holds
a digit (`Part 2`, `S2`, `Vol. 3`, `Book 1`, `Arc 3`) or a structural word
(part, pt, arc, book, story, side, extra(s), special(s), bonus, omake,
oneshot, prologue, epilogue, afterword, interlude, act, season, vol(ume),
chapter, ch, episode, ep): `Part A`, `Side Story`, `Extra`, `Act II`. Such an
arrival keeps the name it had on master; numeric parts keep master's older
spelling (`Berserk Ch. 1 Chapter 5.cbz`), not redesigned here. This replaces
the Vol./Season-only guard. Of the 70 group prefixes in the live log, 69 are
still dropped; `Amin3` (one file) holds a digit and keeps its old name.

Codex's probe passes 6/6 at this head. New tests: 13 structural prefixes in
plain and display parsing (26 cases, each the master name) and an
end-to-end pass in which `Part 1`/`Part 2` and `Part A`/`Part B` land as two
books with nothing reviewed or quarantined. With `cbz_core.py` at `aa4c248`,
24 of the 26 cases and both end-to-end tests fail; the two that pass are
`S2`, which the old season guard already kept.

Guards proven by bypass (failing tests): a prefix with a digit is not a
group 2; a prefix with a structural word is not a group 15; the group test
decides 32; the earlier guards still fail as before (prefix dropped 22,
Episode/Ep. 10, hash required 2, episode number 1, same-named sibling 1,
tag-distinguished name 2).

`scripts/cbz_watcher.py` is not changed in this round.

Full suite: **5,206 passed / 10 skipped / 0 failed** (5,216 collected =
5,188 + 28 new).
