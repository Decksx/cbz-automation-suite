# Engineering Decisions

A record of non-obvious design choices in the suite and the reasoning behind them.

---

## Shared cbz_core.py normalization layer

**Decision:** Shared normalization, parsing, and ComicInfo logic now lives in `scripts/cbz_core.py`. Watcher and batch tools import helpers from the shared module rather than maintaining duplicated copies.

**Why:** The earlier architecture relied on manually syncing duplicated regexes and helper functions between `cbz_sanitizer.py`, `cbz_watcher.py`, and other tools. This repeatedly caused drift bugs where a fix landed in one script but not another.

Moving the logic into `cbz_core.py` creates:

- one authoritative normalization pipeline
- one authoritative ComicInfo update policy
- one authoritative regex set
- reusable structured parsing via `ParsedComicName`
- safer future migrations for dedupe, indexing, and image-aware processing

---

## parse_comic_name() as the authoritative normalization pipeline

**Decision:** Filename generation and metadata extraction now flow through `parse_comic_name()` instead of each script manually chaining helper functions.

**Why:** The old watcher implementation reconstructed the normalization pipeline manually:

```python
clean_filename()
normalize_stem()
normalise_number_tokens()
```

This recreated the exact drift problem the shared-core migration was meant to eliminate.

---

## Mixed-language title shortening instead of destructive Unicode stripping

**Decision:** The suite no longer aggressively strips all non-Latin text during sanitization.

**Why:** Many incoming archives contain both English and original-language titles, such as:

```text
One Piece / ワンピース Ch.005.cbz
```

The new pipeline preserves non-Latin-only titles, prefers English-heavy segments when duplicate-language titles exist, preserves chapter/volume metadata, and removes only Windows-forbidden path characters.

---

## ElementTree-based ComicInfo updates

**Decision:** ComicInfo updates now use `xml.etree.ElementTree` instead of regex substitution.

**Why:** Regex replacement against XML was fragile around multiline formatting, namespaces, malformed XML, and duplicate tags.

---

## Larger file wins on conflict

**Decision:** When two files collide during a merge or move, the larger file is always kept.

**Why:** File size is a practical proxy for scan quality and avoids human prompts during large library merges.

---

## External routing config

**Decision:** Destination routing is driven by `routing.json`.

**Why:** Routing is machine-specific and easier to maintain outside Python code.

---

## Runtime files kept off the repo

**Decision:** Routing files, logs, and progress JSON contents are excluded from git.

**Why:** They are machine-specific runtime state and create noisy diffs.

---

## Dry-run on all batch tools

**Decision:** Every modifying batch tool supports `--dry-run`.

**Why:** Large-library operations need preview mode before applying changes.
