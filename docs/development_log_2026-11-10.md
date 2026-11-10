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

### PR #178 review round 2 (Codex, at `d1bc97b`)

**P2 (same finding, two spellings):** the word splitter missed `PartA` /
`PartB` (which the watcher's own `_PART_RE` reads as parts A and B) and the
canonical `One-shot`. The compact parts still shared one name and one went
to review as a second copy. Codex's `test_review178_known_prefixes.py`
(SHA256 `602a1cb8...8a10`) fails 7/7 at `d1bc97b` and passes 7/7 on base.

Fix: the separate word list is gone; the grammar already in the code
decides.

- Parts: `_PART_RE` moved into `cbz_core` as `PART_MARKER_RE`, and the
  watcher's `_PART_RE` is now that object, so the same-chapter copy check
  and the group check read parts identically.
- Specials: `SPECIAL_WORDS` from `cbz_canonical_name` (Omnibus,
  Compilation, Complete, Special, Extra, Bonus, Omake, One-shot).
- Volume, season, chapter, episode and `pt` have grammars only with a
  number, which the digit rule (kept) covers; their bare words, and arc,
  book, story, side story, prologue, epilogue, afterword, interlude and act,
  which have no grammar here, stay in a small conservative list.
- Every word matches whole, with or without spaces, hyphens or underscores
  between its parts (`One-shot`, `One shot`, `Oneshot`; `Side Story`,
  `Side-Story`, `SideStory`); the part grammar reads `PartA` itself.

The live log's groups are unaffected (69 of 70 dropped, `Amin3` kept as
before). Codex's round-1 and round-2 probes pass 13/13 at this head.

New tests: 10 identity spellings in plain and display parsing (20 cases),
the shared part grammar, and `PartA`/`PartB` landing as two books. With
`cbz_core.py` and `cbz_watcher.py` at `d1bc97b`, 12 of the 20 cases, the
grammar test and the end-to-end test fail: `PartA`, `PartB`, `One-shot`,
`One shot`, `SideStory` and `Complete` were dropped there; `Omake`,
`Oneshot`, `Side-Story` and `Extra2` were already kept.

Guards proven by bypass (failing tests): a part marker is not a group 10;
a canonical special word is not a group 16; another structural word is not
a group 8; words match with or without separators 6; a prefix with a digit
is not a group 2; the group test decides 53; the earlier guards still fail
as before. A split of run-together words at capitals failed nothing under
bypass (the separator-tolerant words and the part grammar already catch
`sidestory` and `parta`), so it was removed. The watcher's `_PART_RE` being
`PART_MARKER_RE` cannot be shown by bypass: an identical pattern recompiled
in the watcher is the same object (the `re` module's cache), so the
behaviour cannot differ. The shared reference stays, to keep one source.

Process note: during the bypass run I copied the two sources aside, and
those copies were the run's temporary (LF-normalised, guard-removed)
versions; restoring from them broke the files. The watcher was rebuilt from
HEAD with the three edits re-applied byte-exactly, and the one changed
`cbz_core.py` line restored; every guard was then proven again on the final
tree, and the full suite ran on it.

Watcher edit byte-exact: no unchanged line changed its ending; the 4 added
lines are CRLF like their surroundings; bare LF stays 863.

Full suite: **5,228 passed / 10 skipped / 0 failed** (5,238 collected =
5,216 + 22 new); both sources hashed before the run and unchanged after.

### PR #178 review round 3 (Codex, at `8b3891f`)

**P2 (a regression of round 2):** replacing the word list with the existing
grammars dropped guards `d1bc97b` had. `Part-A`/`Part-B` (`PART_MARKER_RE`
takes no hyphen), `Side A`/`Side B` (bare "side" was gone) and
`Extras`/`Specials` (plurals are not in `SPECIAL_WORDS`) were read as
groups again, so paired parts and sides shared one name and one went to
review. Codex's `test_review178_preserved_guards.py` (SHA256
`8ca5d18d...d305`) fails 14/14 at `8b3891f` and passes 14/14 at `d1bc97b`.
The direction ("replace the list with the grammar") should have been the
union, as the coordinator noted.

Fix: the grammars are used in addition to the earlier words, never instead
of them. A prefix is not a group when it matches any of: the digit rule,
`PART_MARKER_RE` (unchanged, still the watcher's shared grammar),
`SPECIAL_WORDS`, or `STRUCTURAL_PREFIX_WORDS`, which is `d1bc97b`'s full
set (part, parts, pt, arc, book, story, side, extra, extras, special,
specials, bonus, omake, oneshot, prologue, epilogue, afterword, interlude,
act, season, volume, vol, chapter, ch, episode, ep) plus "side story", each
whole word with or without separators. `Part-A` is read through "part",
`Side B` through "side". The live log's groups are unaffected (69 of 70
dropped, `Amin3` kept). Codex's three probes pass 27/27.

New tests: the 14 probe cases (six prefixes in plain and display parsing,
and the hyphenated-part and lettered-side pairs landing as two books), and
a test that runs every word of `d1bc97b`'s set, written out in the test
rather than read from the code, in five spellings each, asserting the group
rule leaves the arrival untouched. With `cbz_core.py` at `8b3891f`, 18 fail
(the 14, and the word test for part, side, extras and specials). Left as
they are, both nonblocking per Codex: `Partners Scans` (read as a part, so
kept: conservative) and the regex-cache limit on proving the shared part
grammar.

Guards proven by bypass (failing tests): a structural word is not a group
42; a part marker 5; a special word 6; separator-tolerant matching 4; the
digit rule 2; the group test decides 93; a group prefix is dropped 22. Both
sources were hashed before the bypass run and the full suite and were
unchanged after each; nothing was copied or restored during either.

Full suite: **5,268 passed / 10 skipped / 0 failed** (5,278 collected =
5,238 + 40 new).
