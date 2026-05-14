# Possible Same-Series Grouping Update

Copy these files into your repo root:

```text
scripts/cbz_library_maintenance.py
docs/possible_same_series_grouping.md
```

Test:

```powershell
python scripts\cbz_library_maintenance.py organize-series "\\tower\media\comics\Comix" --dry-run --possible-series-check
```

Commit:

```powershell
git add scripts\cbz_library_maintenance.py docs\possible_same_series_grouping.md
git commit -m "feat(organizer): group likely same-series folders for manual review"
```
