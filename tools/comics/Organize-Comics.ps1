[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$Path = ".",

    [Parameter(Mandatory = $false)]
    [switch]$Recurse
)

$resolvedPath = Resolve-Path -LiteralPath $Path -ErrorAction Stop

$getChildParams = @{
    LiteralPath = $resolvedPath
    File        = $true
    Include     = @("*.cbr", "*.cbz")
}
if ($Recurse) {
    $getChildParams["Recurse"] = $true
}

$files = Get-ChildItem @getChildParams

if ($files.Count -eq 0) {
    Write-Host "No .cbr or .cbz files found in '$resolvedPath'."
    exit 0
}

Write-Host "Found $($files.Count) file(s). Processing..."

$moved  = 0
$failed = 0

foreach ($file in $files) {
    $baseName  = $file.BaseName          # filename without extension
    $targetDir = Join-Path $file.DirectoryName $baseName

    try {
        # Create directory only if it doesn't already exist
        if (-not (Test-Path -LiteralPath $targetDir)) {
            New-Item -ItemType Directory -Path $targetDir -ErrorAction Stop | Out-Null
        }

        $destination = Join-Path $targetDir $file.Name

        # Skip if file already lives in its target folder
        if ($file.FullName -eq $destination) {
            Write-Verbose "Skipping '$($file.Name)' — already in target folder."
            continue
        }

        Move-Item -LiteralPath $file.FullName -Destination $targetDir -ErrorAction Stop
        Write-Verbose "Moved '$($file.Name)' -> '$targetDir'"
        $moved++
    }
    catch {
        Write-Warning "Failed to process '$($file.Name)': $_"
        $failed++
    }
}

Write-Host "Done. Moved: $moved  |  Failed: $failed"
