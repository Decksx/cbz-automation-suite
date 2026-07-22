param(
    [string]$RepoRoot = ".",
    [switch]$NoBackup
)

$ErrorActionPreference = "Stop"
$repo = Resolve-Path $RepoRoot
$docs = Join-Path $repo "docs"

if (-not (Test-Path $docs)) {
    throw "Could not find docs folder at: $docs"
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backup = Join-Path $repo "docs_backup_$timestamp"

if (-not $NoBackup) {
    Copy-Item -Path $docs -Destination $backup -Recurse
    Write-Host "Backup created: $backup"
}

$sourceDocs = Join-Path $PSScriptRoot "..\docs"

$files = @(
    "overview.md",
    "shared_pipeline.md",
    "cbz_core.md",
    "cbz_watcher.md",
    "cbz_sanitizer.md",
    "engineering_decisions.md"
)

foreach ($file in $files) {
    $src = Join-Path $sourceDocs $file
    $dst = Join-Path $docs $file

    if (-not (Test-Path $src)) {
        throw "Missing source doc: $src"
    }

    Copy-Item -Path $src -Destination $dst -Force
    Write-Host "Updated docs\$file"
}

Write-Host ""
Write-Host "Documentation update complete."
Write-Host "Review with: git diff docs"
Write-Host "Commit with: git add docs && git commit -m `"docs: update shared core architecture documentation`""
