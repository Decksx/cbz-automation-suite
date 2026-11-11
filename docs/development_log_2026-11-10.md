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

## Watcher: a chapter range is never merged into one number

### Incident and the gap

A compilation of chapters 1 to 30 (inside: `00 - Chapter 1.cbz -
001__001.jpg` to `29 - Chapter 30.cbz - 010.jpg`) is in the library as
`The Toy of 100 Baseball Players 130.cbz`: the hyphen was lost and the
range became one bogus number. **That collapse could not be reproduced**:
on master `eb32d3e` and on the March 2026 code (`5ca4a3b`), `clean_filename`,
`clean_directory_name`, `sanitize`, `normalise_number_tokens` and
`parse_comic_name` (plain and display) all keep `... 1-30`, `1–30`, `1~30`
and `Chapters 1-30`. The name was already `...130` in the oldest log lines
(`strip_duplicates.log`, 2026-03-13; `cbz_compilation_resolver.log`,
2026-03-15); the watcher logs of that time have rotated away, so neither
the arrival's original name nor the step that dropped the hyphen can be
recovered. Recorded as a gap.

What does still lose a range on master, and is fixed here:

1. `Ch. 1-30`, `#1-30` and `Vol. 1-3` were read as chapter or volume 1
   (`extract_chapter_number` / `extract_volume_number`). With media naming
   on, `watcher_naming` then named `...Ch. 1-30.cbz` `...ch. 1.cbz` (chapter
   style) or `...#001.cbz` (issue style), and ComicInfo `<Number>` became 1.
2. A bracketed range, `(1-30)` or `[1~30]`, was dropped with the brackets:
   `X (1-30).cbz` became `X.cbz`.

### Changes

- `scripts/cbz_core.py`: a number that starts an ascending range
  (`N-M`, `N - M`, `N–M`, `N~M`, `M > N`) is never taken for its first
  number; the field is left unset. A descending or equal second number
  (`Ch. 5 - 3 Kittens`, `Ch. 7 - 7`) is a title, not a range.
  `BRACKETED_RANGE_RE`: a bracket that holds only a number range is kept,
  unbracketed, when brackets are dropped (`X (1-30).cbz` -> `X 1-30.cbz`).
- `comic_automation/library/media_naming.py`: a range number, or a bracketed
  range in the arrival's name, keeps the legacy name (`KeepArrivalName`),
  which spells it as it arrived with the uppercase `Ch.` David keeps; the
  formatter would write `ch. 1-30`, and its own bracket stripping would lose
  a bracketed range. ComicInfo `<Number>` stays unset rather than taking the
  range's first number.

The canonical formatter already accepts ascending chapter ranges
(`ch. 1-30`); the compilation words (`SPECIAL_WORDS`: Compilation,
Omnibus, ...) are a different thing, a word after the series, and are not
changed. Keeping the legacy name is only about the uppercase `Ch.`.

### Tests

New `tests/test_chapter_range_names.py` (36): ranges kept and never read as
their first number (`Ch. 1-30`, `Ch. 1 - 30`, `#1-30`, `Vol. 1-3`,
`(1-30)`, `[1~30]`, `(01–12)`), plain and display; bare ranges
(`1-30`, `1–30`, `1~30`, `Chapters 1-30`) pinned unchanged; single
numbers still read (`Ch. 5`, `Ch. 1.5`, `Ch. 5 - 3 Kittens`, `Ch. 7 - 7`);
media naming keeping the legacy name for a range in both styles and still
naming a single chapter; and an end-to-end landing with media naming on:
`... Ch. 1-30.cbz` with no ComicInfo `<Number>`. At `eb32d3e` 23 fail and
the 13 "unchanged" ones pass.

Guards proven by bypass (failing tests): a chapter range is not its first
number 11; a volume range is not its first number 4; only an ascending
second number makes a range 2; a bracketed range survives bracket cleaning
6; media naming keeps the legacy name for a range 7; and for a bracketed
range 2. The sources were hashed before the bypass run and the full suite
and were unchanged after each.

Full suite: **5,303 passed / 10 skipped / 1 failed** (5,314 collected =
5,278 on master + 36 new). The failure is
`tests/web/test_web_browser_smoke.py::test_a_service_worker_left_on_a_reused_port_cannot_reach_the_console`;
three isolated reruns passed 3/3, and nothing under `web` changes.

### PR #180 review round 1 (Codex, at `2042bfa`)

**P2 (pre-existing, inside this fix's scope):** a range was still cut before
the new guard ran. `shorten_mixed_original_title` keeps the Latin segment of
a mixed-language title and carries the number over from the discarded one
(`_extract_num_suffix`), but copied only the first number: `Series - 漫画
Ch. 1-30.cbz` became `Series Ch. 1.cbz` with ComicInfo `<Number>1</Number>`,
and `Series - 漫画 Vol. 1-3.cbz` became `Series Vol. 1.cbz` and then went to
review. Codex's `test_review180_ranges.py` (SHA256 `22a006db...9446`) fails
6/8 at `2042bfa` and 8/8 on base.

Fix: `_token_end` carries both ends when the number starts an ascending
range (the same `_starts_range` test the extraction uses); a descending
tail is left behind as before. `_extract_num_suffix` has one caller
(`shorten_mixed_original_title`); single numbers come out exactly as
before, including `Series - 漫画 Ch. 5 - 3 Kittens.cbz` -> `3 Kittens Ch.
5.cbz`.

Codex's probe passes 8/8. New tests: the shortened `Ch. 1-30` and
`Vol. 1-3` (plain and display), two single-number cases unchanged, two
end-to-end landings with media naming on (range kept, no `Number`/`Volume`
1, nothing reviewed), and `shorten_mixed_original_title` itself with
ascending and descending tails. At `2042bfa` the 6 range tests and the 2
ascending shortening cases fail; the unchanged cases pass.

Guards proven by bypass (failing tests): a shortened title keeps both ends
of a range 8; only an ascending range keeps its second end there 2. The
earlier six still fail as before (14/7/2/6/9/2).

Full suite: **5,317 passed / 10 skipped / 0 failed** (5,327 collected =
5,314 + 13 new).

### PR #180 review round 2 (Codex, at `ad2e731`)

Codex closed the suffix finding. **P2 (pre-existing):** the CJK fallback read
a range's last end: `Series 第1-30話` landed with Number 30 and `第1-3巻`
with Volume 3. Codex's `test_review180_cjk_ranges.py` (SHA256
`7bbaa4ca...587b`) fails 6/8 at `ad2e731` and on base.

To avoid a third round, every reader of a chapter, volume or issue number in
a name was audited against an ascending range with each separator (`-`,
`–`, `~`, `〜`, `～`, ` - `), each form both after the series name and as the
whole name: 22 forms x 6 separators x 2 = 264 cases. Readers covered:
`extract_chapter_number` (Ch./Chapter/#/Issue/c001), `extract_volume_number`
(Vol./Volume/v01), `extract_trailing_bare_number` (the watcher's folder
fallback), `normalize_stem` (the generic-chapter and generic-title branches),
the leading-index strip, the CJK fallback (第N話/N話/章/回, 第N巻/N巻),
`shorten_mixed_original_title` with `_extract_num_suffix`, Korean 화/권 and
fullwidth digits, and media naming (`field_resolution.parse_name_tokens` via
`watcher_naming`). At `ad2e731` 166 of 264 took an end of the range or lost
it from the name; now none does.

Fixed (all pre-existing on master):

- CJK fallback: a number that ends an ascending range (`_ends_range`) is not
  the chapter or volume.
- `〜` (U+301C) and `～` (U+FF5E) are range separators (`_RANGE_SEP`):
  `Ch. 1〜30` was read as chapter 1.
- `extract_trailing_bare_number`: `Series 1-30` gave the folder chapter 30.
- `shorten_mixed_original_title` split a title at `~` and ` - ` even between
  two numbers: `Series 第1~30話` became `Series 第1.cbz`. An ascending range
  is now held together; a descending pair (`5 - 3 Kittens`) splits as before.
- `normalize_stem`, generic chapter: a whole name `Chapter 1-30` or `#1-30`
  became `Series Ch. 1 30.cbz` with chapter 1, the hyphen lost and the range
  read as two numbers; the closest reproduction found of the historic `130`.
  Now `Series Ch. 1-30.cbz`.
- `normalize_stem`, generic title: a whole name `Chapters 1-30` became
  `Series.cbz`. Now `Series Ch. 1-30.cbz`.
- The leading-index strip removed `1-` from `1-30` (`30.cbz`). A leading
  ascending range is kept. Consequence: a leading index followed by a title
  that starts with a larger number (`01 - 30 Days of Night`) keeps its index
  now; a smaller or equal one (`05 - 3 Kittens`, `12 - 12 Angry Men`) is
  still dropped.

Not fixed (cosmetic, the range survives): fullwidth `Ch. １-３０` becomes
`Ch. 1-３０`, only the first number normalised to ASCII.

Tests: the audit is pinned as `test_no_reader_takes_an_end_of_a_range` (264
cases); whole-name ranges (6); single numbers still read, CJK included
(11); leading indexes still dropped (3); trailing bare numbers still used;
Codex's CJK landing (2). At `ad2e731` 174 fail (166 audit, 6 whole-name, 2
landing) and the single-number, index and trailing pins pass. Codex's two
#180 probes pass 16/16.

Guards proven by bypass (failing tests): CJK chapter marker 50; CJK volume
marker 25; trailing bare number 52; only an ascending pair ends a range 1;
the wave dash and fullwidth tilde 54; a range never split as a title
separator 19; only an ascending pair held together 1; `Chapter 1-30` keeps
the range 7; `Chapters 1-30` keeps the range 7; a leading range is not an
index 11; only an ascending pair is a range there 2. A first bypass run
proved nothing (a script error passed `-X utf8` to pytest, so every run
stopped at argument parsing); it was rerun with the flag on the interpreter.
The sources were hashed before each run and the full suite and were
unchanged after each.

Full suite: **5,604 passed / 10 skipped / 0 failed** (5,614 collected =
5,327 + 287 new).

### PR #180 review round 3 (Codex, at `a1e36f9`)

Codex closed the CJK finding. **P2 (a regression from this PR):** the
translated-title rebuild wrote `<translation> Ch. <chapter>` or
`<translation> Vol. <volume>`, and a range sets neither field any more, so
`漫画 Ch. 1-30` and `漫画 Ch. 31-60` both became `Story.cbz`; one landed and
the other was sent to review. On base they landed as two names (each with
one endpoint). Codex's `test_review180_translated_ranges.py` (SHA256
`7ace1d39...3895`) fails 6/8 at `a1e36f9`.

Fix: the rebuild carries the range as it was written, with its kind
(`_range_suffix`): `Story Ch. 1-30`, `Story Vol. 1-3` (from `Vol. 1-3` or
`第1-3巻`), a bare `Story 1-30`. Single chapters are unchanged (`Story Ch.
30`).

The audit now covers the name **rebuilders** too, not only the readers:

- the translated-title rebuild (`parse_comic_name`): the 264-case matrix
  again with a translator on, plain and display (528 cases);
- the watcher's fallback name for a placeholder (`_apply_fallback_naming`):
  in a range folder (`Berserk Ch. 1-30`) it took the first end on master
  (`Berserk ch. 1.cbz`) and nothing after round 2 (`Berserk.cbz`, which two
  range folders would share). It now carries the range, in the canonical
  formatter's spelling (`1~30` becomes `1-30`, the only range form the
  formatter accepts): `Berserk ch. 1-30.cbz`, chapter field unset. A
  volume-range folder (`Berserk Vol. 1-3`) has no chapter slot to carry its
  range, so its placeholder is reviewed rather than named after the series
  alone (`range_in`).
- the other rebuilders were already covered: `normalize_stem`'s generic
  branches and the mixed-title suffix (rounds 1 and 2), and media naming,
  which keeps the legacy name for any range.

**Placeholder case (David, 2026-11-10):** the fallback name comes from the
canonical formatter, as every fallback name has on master (`Berserk ch. 4`,
pinned by `test_round_group_tags` and `test_watcher_canonical_convergence`),
so a range placeholder is `Berserk ch. 1-30.cbz`, consistent with them,
while the legacy translated rebuild gives `Story Ch. 1-30`. David decided
on uppercase `Ch.` everywhere the watcher names a file; that is a separate
follow-up PR switching the canonical formatter's display to `Ch.`, not part
of this one.

Codex's three #180 probes pass 24/24. New tests: the translated audit
(528), a translated single chapter, two translated compilations landing as
two books, placeholders in range folders (4, the single-number case as
before), a placeholder in a volume-range folder reviewed, and translated
range kinds (4). At `a1e36f9` 192 fail; the single-number cases pass.

Guards proven by bypass (failing tests): the translated rebuild carries the
range 192; a placeholder in a range folder is labelled with it 4; the label
is in the formatter's spelling 2; a volume range is rebuilt as a volume
range 3; a placeholder in a volume-range folder is reviewed 1. The sources
were hashed before the bypass run and the full suite and were unchanged
after each.

Watcher edit byte-exact: no unchanged line changed its ending; the 19 added
lines are CRLF like their surroundings; bare LF stays 863.

Full suite: **6,145 passed / 10 skipped / 0 failed** (6,155 collected =
5,614 + 541 new).

### PR #180 review round 4 (Codex, at `b52a4fd`)

Codex closed the single-range finding. **P2:** compound numbering in the
translated path: `漫画 Vol. 1-3 Ch. 1-30` and `漫画 Vol. 1-3 Ch. 31-60` both
became `Story Vol. 1-3`, and a scalar `Ch. 30` was suppressed by `Vol. 1-3`.
Codex's `test_review180_compound_ranges.py` (SHA256 `46177de1...2390`)
fails 8/10 at `b52a4fd`.

The class is closed rather than the example: the translated-title rebuild
no longer rebuilds the numbering. `_split_numbering` splits the arrival's
name at its first numbering token (a Vol./v/Ch./Chapter/#/Issue/Ep. token,
a 第 or CJK/Korean number marker, or a range); only the title part is
translated, and the numbering span is kept as written apart from the usual
cleaning. If the two cannot be split cleanly (no title before the
numbering, or a digit left in the title, such as `漫画 30`), the arrival
keeps its name. The per-kind carry helper `_range_suffix` is removed.

**David's rule for single numbers (2026-11-10):** a span holding exactly one
number that is a chapter or volume is written as master writes it
(`漫画 第3話` -> `Story Ch. 3`, `漫画 3巻` -> `Story Vol. 3`), his preferred
style and the common case; checked identical to `eb32d3e` on 11 cases. A
range or more than one number is kept verbatim (`Story Vol. 1-3 Ch. 31-60`,
`Story 第1-30話`). One exception, kept verbatim too: a single episode
(`漫画 Ep. 3` -> `Story Ep. 3`). Master writes plain `Story` there (an
episode is no chapter or volume field), which would give `Ep. 3` and
`Ep. 30` one name.

Tests: a property over 168 generated numbering spans (every mix of
Vol./v/巻/Ch./Chapter/#/第話/Ep. markers, scalars and ranges with every
separator, and volume-plus-chapter compounds), each with its exact expected
name; a uniqueness property by meaning (the same number spelled two ways,
`Ch. 30` / `#30` / `第30話`, may share a name; numbering that means different
books never does), plain and display; single numbers pinned to master's
output; the two unsplittable cases; a title without numbering; and Codex's
compound end-to-end. The round-3 test's two range cases now expect the
verbatim span (`Story 第1-3巻`, `Story 第1-30話`). At `b52a4fd` 130 fail; the
master-output pins pass there too. Codex's four #180 witnesses pass 34/34,
unmodified (hashes checked).

Guards proven by bypass (failing tests): a range or compound span is kept
verbatim 294; a title that cannot be split keeps the arrival name 1; the
span starts at the first numbering token 115; a range counts as numbering
13; one chapter number is written as on master 14; one volume number is
written as on master 9; only exactly one number is written as on master 32.
The sources were hashed before the bypass run and the full suite and were
unchanged after each.

Full suite: **6,330 passed / 10 skipped / 0 failed** (6,340 collected =
6,155 + 185 new).

## Watcher: known group prefixes without a downloader hash

### Incident

Live at `eb32d3e` (#178 merged), 2026-11-10 11:58: Comick (Unoriginal)
delivered `Mangakakalot Ch. 53.cbz` into "City of Sins", and it kept that
name. #178 drops a group prefix only with the downloader's trailing hex
hash, because without it the prefix could be a real series. David approved
dropping a prefix without the hash when it is a known group.

### Changes

- `config/known-groups.txt` (new, version-controlled; not the gitignored
  `routing.json`): 212 known scanlation groups and source sites, one per
  line, with the format and how to add a group at the top. 191 come from
  the 2026-11-10 library audit's bucket 1 (`scratchpad/groupnames/
  classify.py`, `GROUPS`) and 21 from the watcher log's hashed arrivals.
- `scripts/cbz_core.py`: `_KNOWN_GROUP_CHAPTER_RE`, `known_groups()`,
  `_group_key()`, `_is_structural_prefix()`. Without a hash,
  `<Group> Ch. N`, `<Group> Chapter N`, `<Group> Episode N` and
  `<Group>_Chapter N` (a title may follow) become `Chapter N`, named
  `<Series> Ch. N` by the existing pipeline (uppercase `Ch.`, as David keeps
  it), when the prefix is exactly a known group: case, spacing and
  punctuation ignored, nothing else. The list is re-read when the file
  changes; a missing file means no known groups. The display-name step now
  receives the group-free name: with the original, `Mangakakalot Chapter 53`
  came out as `Chapter 53.cbz` in display mode.
- **David's decision (2026-11-10): an exact listed match overrides the digit
  rule only.** `_is_structural_prefix` (`PART_MARKER_RE`, `SPECIAL_WORDS`,
  `STRUCTURAL_PREFIX_WORDS`, and the volume, season, episode, part and
  chapter number grammars, so `S2`, `V3`, `E5`, `P2`, `Ch 4`) always wins,
  even over a listed line: `Part 2` or `S2` added to the file can never strip.
  The hashed path keeps its digit rule unchanged.
- `docs/cbz_watcher.md`: "Group prefixes in chapter names", including how to
  add a known group.

### Which candidates were shipped

Of 222 candidates, 212 are listed. Of the 9 the digit rule had dropped, 8
are shipped: `Manhwa18`, `Amin3`, `KM 42`, `1 2 Translations` and
`that one guy 2123` (the digits are part of the group name), and
`Gamma 2`, `Omega 2` and `Manhwa18 2`, which the morning's audit found are
second copies from the same group with the same chapter numbering as the
base group, not seasons; stripped, a copy collides with the primary one and
goes to review, the intended handling for duplicates (noted in the file).
Left out: `Official · sugerrrrr 2.5`, a credit string whose trailing `2.5`
looks like a chapter or version number. Also left out: 5 duplicate
spellings (`LOST_VEYL`, `The Blank`, `UToon`, `Utoon`, `Webtoon`), 3
generic words from the log (`group`, `manga`, `doujinshi`) and one empty
(zero-width) entry. No candidate matched a bucket-3 alternate title or any
series folder in the audit, so none was dropped for that, and none is
rejected by the structural checks.

### Deliberately not done

- A short group name that is also a real series (say `Desire`) would be
  dropped where its files arrive outside their own folder. The audit found
  no such overlap with library series folders.
- Library files on Z: are not touched (another agent handles them).

### Tests

New `tests/test_known_group_prefixes.py` (54): the live Mangakakalot case
and the other forms, plain and display; a prefix equal to the folder
series; unknown, non-exact, unlisted-digit, structural and token-less
prefixes without a hash (unchanged); the data file deciding (a temporary
list; listed `Part A`, `Part 2`, `S2`, `V3`, `Ep4` still blocked; a missing
file); every shipped entry passing the structural checks; the digit
override (`Manhwa18 Chapter 1.cbz` -> `3XLOVE Uncensored Ch. 1.cbz`, and
the other digit groups); the hashed path unchanged; `Manhwa18` and
`Manhwa18 2` copies of one chapter (one lands, the other is reviewed, both
kept); and an end-to-end watcher pass landing `City of Sins Ch. 53.cbz`. At
`eb32d3e` 38 fail and the 16 "unchanged" ones pass.

Guards proven by bypass (failing tests): without a hash only a known group
is dropped 13; the match is exact 6; the display name uses the group-free
name 9; a missing list means no known groups 1; the list is re-read when the
file changes 1; a hashed name still uses the hash rule 4; a listed group
overrides the digit rule 13; nothing overrides the structural checks 1;
number markers are structural 1; the hashed path keeps its digit rule 2.
The sources were hashed before the bypass run and the full suite and were
unchanged after each.

Process note: a failed helper script truncated the new test file once; it
was rewritten in full and re-checked against `eb32d3e` before the bypass
run.

Full suite: **5,322 passed / 10 skipped / 0 failed** (5,332 collected =
5,278 on master + 54 new).

### PR #179 review round 1 (Codex, at `1559d27`)

Rebased onto master `b87b7c8` (#180 merged) after these fixes; only this
log conflicted (both PRs append to it), and was resolved by keeping
master's sections and appending this PR's.

**1. A listed name can also be a title (blocking).** `Desire` and `Violet`
are listed groups but can name books themselves: in a mixed drop,
`Desire Ch. 1.cbz` and `Violet Ch. 1.cbz` became requests for `Mixed Drop
Ch. 1.cbz`, one landed and the other was reviewed. Codex's
`test_review179_identity.py` (SHA256 `fecb9ddd...e612`) fails 12/12 at
`1559d27`. The list only says a text *can* name a group; the arrival's
folder must now confirm it (`_drop_confirms_group`): the folder holds one
series (no subfolders; not a mixed drop point's loose archives, which the
watcher reports per arrival with `known_groups_mixed_drop`), and every
archive in it carries that same listed prefix before a chapter token, or is
already named after the folder's series (a batch is renamed one archive at
a time). An unreadable or missing folder confirms nothing, so a bare path
keeps its name. A prefix that is the folder's own series gives the same
name either way, so no separate check is kept for it.

**2. The structural veto missed normalised forms.** The list match ignores
NFKC, spacing and punctuation, but the veto read only the raw prefix, so a
listed `S2`/`V3`/`P2`/`Ep4` also stripped `S 2`, `V 3`, `P 2`, `E.p.4`,
`Ｓ２`, `Ｖ３`. `_is_structural_prefix` now checks the raw, NFKC,
separator-free and match-key forms.

**3. An undecodable list raised.** A `known-groups.txt` that is not valid
UTF-8 raised `UnicodeDecodeError`; it now means no known groups, with one
warning per version of the file (a missing file stays silent).

Tests: Codex's probe passes 12/12, unmodified. The earlier tests now build
real arrival folders (parsing needs the folder to confirm). New: listed
titles in a mixed folder and without a folder, a folder with subfolders, a
mixed drop's loose archives, siblings already renamed, the six alias
forms plus the literal ones, an undecodable list (warned once), and three
watcher passes (two consecutive Mangakakalot chapters both renamed; the
probe's mixed drop; a lone listed title in a mixed drop). At `1559d27` 26
fail (five of them only because `_without_group_prefix` takes the folder
now).

Guards proven by bypass on the rebased tree (failing tests): the 9 new ones
(folder confirmation 14, mixed drop 3, subfolders 2, every archive 4,
already-renamed siblings 3, normalised veto 6, undecodable list 1, logged
once 1, the watcher's report 1), and the 10 earlier ones re-run there
(known group only 12, exact match 6, group-free display 9, missing list 1,
re-read 1, hashed path 24, digit override 13, structural checks win 11,
number markers structural 7, hashed digit rule 2). The sources were hashed
before the bypass run and the full suite and were unchanged after each.

Watcher edit byte-exact against `b87b7c8`: no unchanged line changed its
ending; the 5 added lines are CRLF; bare LF stays 863.

Full suite on the rebased tree: **6,412 passed / 10 skipped / 0 failed**
(6,422 collected = 6,340 on `b87b7c8` + 82 in `test_known_group_prefixes.py`).
