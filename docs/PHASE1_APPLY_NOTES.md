# CBZ Automation Suite Phase 1 Starter Patch

This starter patch adds:

- `scripts/cbz_core.py`
- `scripts/__init__.py`
- `pytest.ini`
- regression tests for normalization, series inference, and ComicInfo updates
- Windows GitHub Actions test workflow

## Apply

Copy these files into the repository root, then run:

```powershell
python -m pip install -U pytest
python -m pytest
```

## Next step

After tests pass, refactor `scripts/cbz_watcher.py` first to import from `scripts.cbz_core`
with a direct-script fallback:

```python
try:
    from scripts.cbz_core import ...
except ModuleNotFoundError:
    from cbz_core import ...
```

Then remove watcher-local duplicates for:

- `sanitize`
- `clean_filename`
- `clean_directory_name`
- `clean_xml_field`
- `is_generic`
- `normalise_number_tokens`
- duplicated title/chapter/volume regexes where practical
