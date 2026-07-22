# CBZ SQLite Schema Phase 1

Copy the package contents into the repository root.

Initialize:

```powershell
python scripts\db_cli.py init
```

Test:

```powershell
python -m pytest tests\test_db.py
```

Commit:

```powershell
git add scripts\db.py scripts\db_cli.py migrations tests\test_db.py docs\sqlite_database.md
git commit -m "feat(db): add migration-based SQLite schema foundation"
```
