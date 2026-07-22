# cbz_sanitizer.py

Batch sanitizer. Recursively scans a library folder for `.cbz` files and applies the full cleaning and tagging pipeline in-place: filename normalization, directory renaming, and `ComicInfo.xml` creation/repair.

`cbz_sanitizer.py` imports shared normalization and ComicInfo helpers from `scripts/cbz_core.py`, which serves as the suite-wide shared core layer.

---

## Processing Pipeline

For each `.cbz` file found:

1. **Filename parsing** — `parse_comic_name()` runs the shared normalization pipeline and returns structured filename/chapter/volume metadata.
2. **Rename** — renames the `.cbz` file if the parsed filename differs.
3. **ComicInfo.xml** — creates one from the built-in template if absent, or reads the existing one.
4. **Tag update** — delegates metadata decisions to `update_comicinfo_xml()` from `cbz_core.py`, preserving custom titles while normalizing generic metadata.
5. **Archive rewrite** — if any tag or XML changed, rewrites the archive while preserving the original compression type.
6. **Directory rename** — after all files in a subdirectory are processed, renames the directory itself if its cleaned name differs.

See [shared_pipeline.md](shared_pipeline.md) and [cbz_core.md](cbz_core.md).

---

## CLI Usage

```powershell
python scripts\cbz_sanitizer.py
python scripts\cbz_sanitizer.py --dry-run
python scripts\cbz_sanitizer.py --workers 4
python scripts\cbz_sanitizer.py --resume
python scripts\cbz_sanitizer.py --restart
```
