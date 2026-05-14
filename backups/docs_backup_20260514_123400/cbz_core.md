# cbz_core.py

Shared normalization, parsing, and ComicInfo helper module.

`cbz_core.py` is the suite-wide shared core layer. It prevents watcher, sanitizer, and maintenance scripts from drifting apart by maintaining duplicated regexes and helper functions.

---

## Responsibilities

`cbz_core.py` owns:

- text sanitization
- Windows-safe filename cleanup
- mixed English/original-title shortening
- filename normalization
- directory-name normalization
- root-aware series inference
- chapter and volume extraction
- `ParsedComicName`
- ComicInfo XML update decisions

It intentionally does **not** own:

- file moves
- archive rewrite mechanics
- watcher debounce/settle logic
- routing rules
- logging configuration
- dry-run behavior

---

## Public API

The intended public API is exported through `__all__`:

```python
ALL_RULES
GIBBERISH_RE
IGNORED_SERIES_FOLDERS
NUMBER_PREFIX_RE
TRAILING_JUNK_RE
ParsedComicName
clean_directory_name()
clean_filename()
clean_xml_field()
extract_chapter_number()
extract_volume_number()
infer_series_name()
is_generic()
is_generic_title()
normalise_number_tokens()
normalize_stem()
parse_comic_name()
parse_rules()
sanitize()
shorten_mixed_original_title()
update_comicinfo_xml()
```

---

## ParsedComicName

```python
@dataclass(frozen=True)
class ParsedComicName:
    original_path: Path
    filename: str
    stem: str
    series: str
    chapter: str | None
    volume: str | None
```

This object is the normalized metadata payload used by watcher and ComicInfo update logic.

---

## parse_comic_name()

`parse_comic_name()` is the authoritative normalization pipeline.

It performs:

1. series inference
2. directory-name cleanup
3. filename cleanup
4. leading-number stripping
5. generic stem normalization
6. chapter/volume token normalization
7. trailing-junk stripping
8. chapter extraction
9. volume extraction

Example:

```python
from pathlib import Path
from scripts.cbz_core import parse_comic_name

parsed = parse_comic_name(Path("One Piece/001 - One Piece Ch.005.cbz"))

print(parsed.filename)  # One Piece Ch.5.cbz
print(parsed.series)    # One Piece
print(parsed.chapter)   # 5
```

---

## Mixed-language title shortening

Many source files contain both an English title and the original Japanese/Chinese/Korean title:

```text
One Piece / ワンピース Ch.005.cbz
```

The shared core prefers the English segment when this looks like a duplicate-title pattern, but preserves chapter/volume data from the original-language segment.

```text
One Piece / ワンピース Ch.005.cbz
→ One Piece Ch.5.cbz
```

Non-Latin-only filenames are preserved instead of being erased:

```text
ワンピース Ch.005.cbz
→ ワンピース Ch.5.cbz
```

---

## Root-aware series inference

`infer_series_name()` can skip container folders such as `Issues`, `Chapters`, `Volumes`, `Extras`, and `Specials`.

```python
path = Path(r"\\tower\media\comics\Marvel\Batman\Issues\Batman Ch.5.cbz")
root = Path(r"\\tower\media\comics")

infer_series_name(path, root)
# -> "Batman"
```

---

## ComicInfo XML updates

`update_comicinfo_xml()` accepts existing XML text and a `ParsedComicName`, then returns:

```python
(new_xml, changed)
```

The watcher uses `changed` to avoid unnecessary archive rewrites.

### Title overwrite policy

`<Title>` is replaced only when:

- missing
- blank
- generic
- gibberish
- equal to the series name

Custom titles are preserved.

### Series, Number, Volume

- `<Series>` is always set to the normalized/inferred series.
- `<Number>` is set when a chapter number is detected.
- `<Volume>` is set when a volume number is detected.

---

## Import pattern

Scripts that may run from repo root or directly inside `scripts/` should use the dual import shim:

```python
try:
    from scripts.cbz_core import parse_comic_name, update_comicinfo_xml
except ModuleNotFoundError:
    from cbz_core import parse_comic_name, update_comicinfo_xml
```
