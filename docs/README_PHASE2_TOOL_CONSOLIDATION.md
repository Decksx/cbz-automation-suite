# CBZ Tool Consolidation Phase 2

Copy these files into your repo root:

```text
scripts/cbz_archive_cleaner.py
scripts/cbz_library_organizer.py
scripts/cbz_metadata_tools.py
docs/tool_consolidation_phase2.md
```

Test help output:

```powershell
python scripts\cbz_archive_cleaner.py --help
python scripts\cbz_library_organizer.py --help
python scripts\cbz_metadata_tools.py --help
```

Recommended dry runs:

```powershell
python scripts\cbz_archive_cleaner.py dedupe --dry-run
python scripts\cbz_library_organizer.py match-series --dry-run
python scripts\cbz_metadata_tools.py number-tags --dry-run
```
