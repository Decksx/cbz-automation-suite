# CBZ Documentation Update Package

## Apply automatically

From PowerShell:

```powershell
cd "C:\Users\David.Johnson\ComicAutomation"
powershell -ExecutionPolicy Bypass -File "<unzipped-package>\tools\apply_doc_updates.ps1" -RepoRoot "."
```

The script creates a timestamped backup of your current `docs/` folder before overwriting files.

## Or copy manually

Copy the files in `docs/` into your repository's `docs/` folder.

## Review and commit

```powershell
git diff docs
git add docs
git commit -m "docs: update shared core architecture documentation"
```
