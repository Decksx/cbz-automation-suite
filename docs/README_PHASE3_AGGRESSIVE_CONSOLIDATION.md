# Phase 3 Aggressive Consolidation Package

Copy these files into your repo root:

```text
scripts/cbz_library_maintenance.py
cbz_gui.py
docs/phase3_aggressive_consolidation.md
```

Then test:

```powershell
python scripts\cbz_library_maintenance.py --help
python scripts\cbz_library_maintenance.py archive-clean "\\tower\media\comics\Comix" --dry-run
python scripts\cbz_library_maintenance.py organize-series "\\tower\media\comics\Comix" --dry-run
python scripts\cbz_library_maintenance.py metadata "\\tower\media\comics\Comix" --dry-run
python apps\cbz_gui.py
```

Commit:

```powershell
git add scripts\cbz_library_maintenance.py apps\cbz_gui.py docs\phase3_aggressive_consolidation.md
git commit -m "refactor(tools): consolidate maintenance workflows and update GUI"
```
