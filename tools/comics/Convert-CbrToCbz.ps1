[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory = $false)]
    [string]$Path = ".",

    [Parameter(Mandatory = $false)]
    [string]$SevenZipPath = "",

    [Parameter(Mandatory = $false)]
    [switch]$DeleteOriginal
)

# ---------------------------------------------------------------------------
# Locate 7-Zip
# ---------------------------------------------------------------------------
function Find-SevenZip {
    param([string]$UserSupplied)

    if ($UserSupplied -and (Test-Path -LiteralPath $UserSupplied)) {
        return $UserSupplied
    }

    $candidates = @(
        $env:SEVENZIP,
        "$env:ProgramFiles\7-Zip\7z.exe",
        "${env:ProgramFiles(x86)}\7-Zip\7z.exe",
        "$env:ProgramW6432\7-Zip\7z.exe"
    )

    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return $c }
    }

    # Fall back to whatever is on PATH
    $found = Get-Command -Name "7z" -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }

    return $null
}

$7z = Find-SevenZip -UserSupplied $SevenZipPath
if (-not $7z) {
    Write-Error "7-Zip (7z.exe) not found. Install it from https://www.7-zip.org or pass -SevenZipPath."
    exit 1
}
Write-Verbose "Using 7-Zip: $7z"

# ---------------------------------------------------------------------------
# Collect files
# ---------------------------------------------------------------------------
$resolvedPath = Resolve-Path -LiteralPath $Path -ErrorAction Stop

$files = Get-ChildItem -LiteralPath $resolvedPath -Recurse -File -Filter "*.cbr"

if ($files.Count -eq 0) {
    Write-Host "No .cbr files found under '$resolvedPath'."
    exit 0
}

Write-Host "Found $($files.Count) .cbr file(s). Starting conversion..."

$converted = 0
$failed    = 0

# ---------------------------------------------------------------------------
# Process each file
# ---------------------------------------------------------------------------
foreach ($file in $files) {

    $cbzPath = Join-Path $file.DirectoryName ($file.BaseName + ".cbz")
    $tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ([System.Guid]::NewGuid().ToString())

    Write-Host "`nProcessing: $($file.Name)"

    if (-not $PSCmdlet.ShouldProcess($file.FullName, "Convert to CBZ")) {
        continue
    }

    # Skip if a .cbz already exists alongside the .cbr
    if (Test-Path -LiteralPath $cbzPath) {
        Write-Warning "  Skipping — '$($file.BaseName).cbz' already exists in the same folder."
        continue
    }

    try {
        # 1. Create temp extraction directory
        New-Item -ItemType Directory -Path $tempDir -ErrorAction Stop | Out-Null

        # 2. Extract CBR (RAR) to temp directory
        Write-Verbose "  Extracting to temp dir: $tempDir"
        $extractArgs = @("x", $file.FullName, "-o$tempDir", "-y")
        & $7z @extractArgs | Out-Null

        if ($LASTEXITCODE -ne 0) {
            throw "7-Zip extraction failed with exit code $LASTEXITCODE."
        }

        # 3. Compress extracted contents into a ZIP (.cbz)
        Write-Verbose "  Compressing to: $cbzPath"
        $contentsGlob = Join-Path $tempDir "*"
        $compressArgs = @("a", "-tzip", "-r", $cbzPath, $contentsGlob, "-y")
        & $7z @compressArgs | Out-Null

        if ($LASTEXITCODE -ne 0) {
            throw "7-Zip compression failed with exit code $LASTEXITCODE."
        }

        Write-Host "  Converted -> $($file.BaseName).cbz"
        $converted++

        # 4. Optionally remove the original .cbr
        if ($DeleteOriginal) {
            Remove-Item -LiteralPath $file.FullName -Force -ErrorAction Stop
            Write-Verbose "  Deleted original: $($file.Name)"
        }
    }
    catch {
        Write-Warning "  FAILED: $_"
        # Clean up any partial .cbz that may have been written
        if (Test-Path -LiteralPath $cbzPath) {
            Remove-Item -LiteralPath $cbzPath -Force -ErrorAction SilentlyContinue
        }
        $failed++
    }
    finally {
        # Always clean up the temp directory
        if (Test-Path -LiteralPath $tempDir) {
            Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host "`n--- Summary ---"
Write-Host "Converted : $converted"
Write-Host "Failed    : $failed"
if (-not $DeleteOriginal) {
    Write-Host "Original .cbr files were kept. Use -DeleteOriginal to remove them after conversion."
}
