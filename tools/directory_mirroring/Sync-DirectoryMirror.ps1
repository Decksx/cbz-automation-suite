#Requires -Version 7.0
<#
.SYNOPSIS
    Mirrors a primary directory structure into a secondary directory.

.DESCRIPTION
    Compares two directory trees, then makes the secondary mirror the primary by:
      1. Replicating the folder architecture from primary into secondary.
      2. Resolving each primary file against existing files in secondary.
         - Default  : match by Name + Size  (fast, good for large libraries)
         - -UseHash : match by SHA256 hash  (slower but exact; catches renamed
                      or same-size-different-content files)
      3. Moving matched secondary files into their correct mirrored locations.
         If a different file already occupies the target path it is displaced
         to _extraneous before the correct file is moved in.
      4. Copying any unmatched files from primary into secondary.
      5. Moving leftover secondary files (not present in primary) into
         <SecondaryRoot>\_extraneous\<original relative path> for manual review.
         Filename collisions inside _extraneous are resolved with (1), (2), etc.

    Performance and reliability features (PS 7):
      - Parallel hashing with configurable throttle (-HashThrottle, default 8)
      - Size pre-filter: only hash files with a same-size counterpart in the other tree
      - Parallel copy of missing files (-CopyThrottle, default 4)
      - Incremental hash cache: written to disk after every batch of files so a
        kill/crash at any point preserves work already done (-CacheFile)
      - Stage checkpointing: completed stages are saved so a restart skips them
      - Pause/resume: create <LogFile>.pause to pause workers; delete to resume
      - Live progress: log updated every -ProgressInterval files during hashing
      - Per-stage elapsed time and throughput in the log

.PARAMETER Primary
    Path to the authoritative source directory.

.PARAMETER Secondary
    Path to the directory that will be made to mirror Primary.

.PARAMETER UseHash
    Use SHA256 content hashing for file matching instead of Name+Size.
    More accurate but significantly slower on first run against large libraries.
    Combine with -CacheFile to make subsequent runs fast.

.PARAMETER HashThrottle
    Maximum number of files to hash in parallel during inventory.
    Default: 8. Raise on fast local storage; lower on slow network links.

.PARAMETER CopyThrottle
    Maximum number of files to copy from primary in parallel.
    Default: 4.

.PARAMETER CacheFile
    Path to a JSONL hash-cache file. Hashes are written incrementally so a
    kill at any point preserves work done. On resume, cached files are skipped.
    Strongly recommended for large libraries.

.PARAMETER ProgressInterval
    How often (in files) to write a progress line to the log during hashing.
    Default: 250.

.PARAMETER WhatIf
    Simulate all operations without touching the filesystem.

.PARAMETER LogFile
    Optional path to a log file. Defaults to .\SyncMirror_<timestamp>.log

.EXAMPLE
    # First run - name+size match, fast
    .\Sync-DirectoryMirror.ps1 -Primary "\\tower\media\comics\comix" -Secondary "X:\Comix" `
        -LogFile "C:\temp\SyncMirror.log"

.EXAMPLE
    # Hash match with incremental cache (resume-safe)
    .\Sync-DirectoryMirror.ps1 -Primary "\\tower\media\comics\comix" -Secondary "X:\Comix" `
        -UseHash -CacheFile "C:\temp\SyncCache.jsonl" -LogFile "C:\temp\SyncMirror.log"

.EXAMPLE
    # Dry run
    .\Sync-DirectoryMirror.ps1 -Primary "\\tower\media\comics\comix" -Secondary "X:\Comix" `
        -UseHash -CacheFile "C:\temp\SyncCache.jsonl" -LogFile "C:\temp\SyncMirror.log" -WhatIf
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)] [string] $Primary,
    [Parameter(Mandatory)] [string] $Secondary,
    [Parameter()] [switch] $UseHash,
    [Parameter()] [int]    $HashThrottle      = 8,
    [Parameter()] [int]    $CopyThrottle      = 4,
    [Parameter()] [string] $CacheFile         = "",
    [Parameter()] [int]    $ProgressInterval  = 250,
    [Parameter()] [string] $LogFile           = "",

    # Pre-built cache files from the orchestrator (skips all hashing when both supplied)
    # PrimaryCache   : JSONL from Build-HashCache.sh (Tower-side)
    # SecondaryCache : JSONL from Build-HashCache.ps1 (Windows-side)
    [Parameter()] [string] $PrimaryCache      = "",
    [Parameter()] [string] $SecondaryCache    = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ===========================================================================
# LOGGING
# ===========================================================================
$script:LogPath = if ($LogFile) { $LogFile } else {
    Join-Path (Get-Location) ("SyncMirror_{0}.log" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
}
$script:PauseFile = "$($script:LogPath).pause"

function Write-Log {
    param(
        [string]$Message,
        [ValidateSet('INFO','WARN','ERROR','ACTION','DRY','PROGRESS')]
        [string]$Level = 'INFO'
    )
    $ts   = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = "[$ts][$Level] $Message"
    Add-Content -Path $script:LogPath -Value $line -Encoding UTF8
    $color = switch ($Level) {
        'INFO'     { 'Cyan'    }
        'WARN'     { 'Yellow'  }
        'ERROR'    { 'Red'     }
        'ACTION'   { 'Green'   }
        'DRY'      { 'Magenta' }
        'PROGRESS' { 'White'   }
    }
    Write-Host $line -ForegroundColor $color
}

function Flush-LogQueue {
    param([System.Collections.Concurrent.ConcurrentQueue[string]]$Queue)
    $item = $null
    while ($Queue.TryDequeue([ref]$item)) {
        $parts = $item -split '\|', 2
        Write-Log $parts[1] $parts[0]
    }
}

# ===========================================================================
# STAGE CHECKPOINTING
# ===========================================================================
# Checkpoint file lives alongside the log: <logpath>.checkpoint
# Format: one stage name per line e.g. "HASH_PRIMARY", "HASH_SECONDARY"
$script:CheckpointFile = "$($script:LogPath).checkpoint"

function Get-CompletedStages {
    if (-not (Test-Path $script:CheckpointFile)) { return @{} }
    $result = @{}
    Get-Content $script:CheckpointFile | ForEach-Object { $result[$_.Trim()] = $true }
    return $result
}

function Save-StageComplete {
    param([string]$Stage)
    Add-Content -Path $script:CheckpointFile -Value $Stage -Encoding UTF8
    Write-Log "Checkpoint saved: $Stage" 'INFO'
}

# ===========================================================================
# PAUSE/RESUME
# ===========================================================================
function Wait-IfPaused {
    if (Test-Path $script:PauseFile) {
        Write-Log "PAUSED -- delete '$($script:PauseFile)' to resume" 'WARN'
        while (Test-Path $script:PauseFile) { Start-Sleep -Seconds 2 }
        Write-Log "RESUMED" 'INFO'
    }
}

# ===========================================================================
# COUNTERS
# ===========================================================================
$stats = [ordered]@{
    DirsCreated     = 0
    FilesMoved      = 0
    FilesCopied     = 0
    FilesExtraneous = 0
    HashMismatches  = 0
    CacheHits       = 0
    Errors          = 0
}

# ===========================================================================
# INCREMENTAL HASH CACHE (JSONL)
# ===========================================================================
# Format: one JSON object per line: {"K":"path","S":size,"T":ticks,"H":"hash"}
# Appended atomically per batch; safe to kill at any time.
$script:HashCache    = @{}   # key=path -> {Size,Ticks,Hash}
$script:CacheChanged = $false

function Load-HashCache {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return }
    $loaded = 0
    $skipped = 0
    foreach ($line in [System.IO.File]::ReadLines($Path)) {
        $line = $line.Trim()
        if (-not $line) { continue }
        try {
            $e = $line | ConvertFrom-Json
            # Last write wins (handles duplicate keys from incremental appends)
            $script:HashCache[$e.K.ToLowerInvariant()] = @{ Size = $e.S; Ticks = $e.T; Hash = $e.H }
            $loaded++
        } catch { $skipped++ }
    }
    Write-Log "Hash cache loaded: $loaded entries ($skipped malformed skipped) from $Path" 'INFO'
}

function Flush-CacheBatch {
    param(
        [string]$Path,
        [System.Collections.Concurrent.ConcurrentBag[object]]$Batch
    )
    if (-not $Path -or $Batch.Count -eq 0) { return }
    $lines = [System.Collections.Generic.List[string]]::new()
    $item  = $null
    # Drain the bag
    $items = @($Batch.ToArray())
    foreach ($e in $items) {
        $lines.Add(($e | ConvertTo-Json -Compress))
        $script:HashCache[$e.K] = @{ Size = $e.S; Ticks = $e.T; Hash = $e.H }
    }
    # Clear the bag by rebuilding (ConcurrentBag has no Clear in all PS7 versions)
    try { $Batch.Clear() } catch {}
    if ($lines.Count -gt 0) {
        [System.IO.File]::AppendAllLines($Path, $lines)
        $script:CacheChanged = $true
    }
}

function Compact-HashCache {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return }
    # Rewrite deduplicated (last-wins already in $script:HashCache)
    $lines = [System.Collections.Generic.List[string]]::new()
    foreach ($kvp in $script:HashCache.GetEnumerator()) {
        $lines.Add(([PSCustomObject]@{ K=$kvp.Key; S=$kvp.Value.Size; T=$kvp.Value.Ticks; H=$kvp.Value.Hash } | ConvertTo-Json -Compress))
    }
    [System.IO.File]::WriteAllLines($Path, $lines)
    Write-Log "Hash cache compacted: $($lines.Count) unique entries -> $Path" 'INFO'
}

# ===========================================================================
# PATH HELPERS
# ===========================================================================
function Resolve-FullPath {
    param([Parameter(Mandatory)][string]$Path)
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    if ($resolved -match '^[^:]+::[\\\/]{2}') {
        $resolved = $resolved -replace '^[^:]+::', ''
    }
    return [System.IO.Path]::GetFullPath($resolved).TrimEnd('\','/')
}

function Get-RelativePath {
    param([Parameter(Mandatory)][string]$BasePath,
          [Parameter(Mandatory)][string]$ChildPath)
    $sep     = [System.IO.Path]::DirectorySeparatorChar
    $baseUri = [System.Uri]::new(($BasePath.TrimEnd('\','/') + $sep))
    $childUri= [System.Uri]::new($ChildPath)
    return [System.Uri]::UnescapeDataString(
        $baseUri.MakeRelativeUri($childUri).ToString()
    ).Replace('/', $sep)
}

function Test-IsInsidePath {
    param([Parameter(Mandatory)][string]$CandidatePath,
          [Parameter(Mandatory)][string]$ParentPath)
    $sep = [System.IO.Path]::DirectorySeparatorChar
    $c   = [System.IO.Path]::GetFullPath($CandidatePath).TrimEnd('\','/')
    $p   = [System.IO.Path]::GetFullPath($ParentPath).TrimEnd('\','/')
    return $c.Equals($p, [System.StringComparison]::OrdinalIgnoreCase) -or
           $c.StartsWith($p + $sep, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-UniqueDestinationPath {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $Path }
    $dir   = Split-Path -Parent $Path
    $base  = [System.IO.Path]::GetFileNameWithoutExtension($Path)
    $ext   = [System.IO.Path]::GetExtension($Path)
    $index = 1
    do {
        $candidate = Join-Path $dir ('{0} ({1}){2}' -f $base, $index, $ext)
        $index++
    } while (Test-Path -LiteralPath $candidate)
    return $candidate
}

# ===========================================================================
# FILESYSTEM ACTION HELPERS
# ===========================================================================
function Ensure-Dir {
    param([string]$Path)
    if (-not (Test-Path $Path -PathType Container)) {
        if ($PSCmdlet.ShouldProcess($Path, 'Create directory')) {
            New-Item -ItemType Directory -Path $Path -Force | Out-Null
            Write-Log "Created dir: $Path" 'ACTION'
        } else {
            Write-Log "DRY: Would create dir: $Path" 'DRY'
        }
        $stats.DirsCreated++
    }
}

function Move-FileItem {
    param([string]$Source, [string]$Destination)
    Ensure-Dir (Split-Path $Destination -Parent)
    if ($PSCmdlet.ShouldProcess($Source, "Move -> $Destination")) {
        Move-Item -LiteralPath $Source -Destination $Destination -Force
        Write-Log "MOVED: `"$Source`" -> `"$Destination`"" 'ACTION'
    } else {
        Write-Log "DRY: Would MOVE `"$Source`" -> `"$Destination`"" 'DRY'
    }
    $stats.FilesMoved++
}

function Move-ToExtraneous {
    param([string]$SourcePath, [string]$RelPath, [string]$ExtraRoot)
    $relParent = Split-Path -Parent $RelPath
    $destDir   = if ([string]::IsNullOrWhiteSpace($relParent)) { $ExtraRoot }
                 else { Join-Path $ExtraRoot $relParent }
    Ensure-Dir $destDir
    $destPath = Get-UniqueDestinationPath -Path (Join-Path $destDir (Split-Path -Leaf $SourcePath))
    if ($PSCmdlet.ShouldProcess($SourcePath, "Move to _extraneous -> $destPath")) {
        Move-Item -LiteralPath $SourcePath -Destination $destPath -Force
        Write-Log "EXTRANEOUS: `"$RelPath`" -> `"$destPath`"" 'WARN'
    } else {
        Write-Log "DRY: Would move extraneous `"$RelPath`" -> `"$destPath`"" 'DRY'
    }
    $stats.FilesExtraneous++
}

function Write-Progress-Simple {
    param([string]$Activity, [int]$Current, [int]$Total, [string]$Status = "")
    $pct = if ($Total -gt 0) { [int](($Current / $Total) * 100) } else { 100 }
    $s   = if ($Status) { $Status } else { "$Current / $Total" }
    Write-Progress -Activity $Activity -Status $s -PercentComplete $pct
}

# ===========================================================================
# PARALLEL HASH PASS  (with live background monitor)
# ===========================================================================
function Invoke-ParallelHash {
    param(
        [object[]]$Files,
        [string]$RootPath,
        [System.Collections.Generic.HashSet[long]]$SizeFilter,
        [bool]$UseHash,
        [string]$CacheFilePath,
        [int]$Throttle,
        [int]$ProgressInterval,
        [string]$Label,
        [bool]$IsClaimed
    )

    $total         = $Files.Count
    $logQueue      = [System.Collections.Concurrent.ConcurrentQueue[string]]::new()
    $newEntries    = [System.Collections.Concurrent.ConcurrentBag[object]]::new()
    $progressBag   = [System.Collections.Concurrent.ConcurrentBag[int]]::new()
    $pauseFile     = $script:PauseFile

    # Build a case-insensitive Dictionary for the parallel workers.
    # Plain [hashtable] passed via $using: is deserialized with ordinal case-sensitive
    # comparison, causing cache misses even when keys match after lowercasing.
    # A Generic Dictionary with OrdinalIgnoreCase survives $using: serialization correctly.
    $cacheSnapshot = [System.Collections.Generic.Dictionary[string,object]]::new(
        [System.StringComparer]::OrdinalIgnoreCase)
    foreach ($kvp in $script:HashCache.GetEnumerator()) {
        $cacheSnapshot[$kvp.Key] = $kvp.Value
    }
    $stageStart    = [datetime]::UtcNow

    # Shared flag box so the monitor thread knows when hashing is done.
    # We use a single-element array since PS parallel/threadjob share by ref.
    $doneFlag = [int[]]@(0)

    # --- Background monitor: runs every 5 seconds, drains the log queue and
    #     writes a PROGRESS line to the log file while hashing is in flight. ---
    $monitorJob = Start-ThreadJob -ScriptBlock {
        param($logPath, $queue, $progBag, $total, $cacheFile, $newEntries,
              $doneFlag, $label, $pauseFile, $hashCache)

        $start = [datetime]::UtcNow

        while ($doneFlag[0] -eq 0) {
            Start-Sleep -Seconds 5

            # Drain log queue -> log file
            $item = $null
            while ($queue.TryDequeue([ref]$item)) {
                $parts = $item -split '\|', 2
                $ts    = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
                $line  = "[$ts][$($parts[0])] $($parts[1])"
                Add-Content -Path $logPath -Value $line -Encoding UTF8
            }

            # Flush new cache entries incrementally
            if ($cacheFile -and $newEntries.Count -gt 0) {
                $lines = [System.Collections.Generic.List[string]]::new()
                $items = $newEntries.ToArray()
                foreach ($e in $items) {
                    $lines.Add(($e | ConvertTo-Json -Compress))
                    $hashCache[$e.K] = @{ Size=$e.S; Ticks=$e.T; Hash=$e.H }
                }
                try { $newEntries.Clear() } catch {}
                if ($lines.Count -gt 0) {
                    [System.IO.File]::AppendAllLines($cacheFile, $lines)
                }
            }

            # Write progress line
            $done    = $progBag.Count
            $elapsed = [int]([datetime]::UtcNow - $start).TotalSeconds
            $rate    = if ($elapsed -gt 0) { [math]::Round($done / $elapsed, 1) } else { 0 }
            $pct     = if ($total  -gt 0) { [int]($done / $total * 100) } else { 0 }
            $eta     = if ($rate   -gt 0) { [int](($total - $done) / $rate) } else { 0 }
            $etaStr  = if ($eta -gt 3600) { "{0:D2}h {1:D2}m" -f [int]($eta/3600), [int](($eta%3600)/60) }
                       elseif ($eta -gt 60) { "{0:D2}m {1:D2}s" -f [int]($eta/60), ($eta%60) }
                       else { "${eta}s" }
            $ts      = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
            $line    = "[$ts][PROGRESS] Hash $label | $done / $total ($pct%) | $rate files/sec | ETA $etaStr"
            Add-Content -Path $logPath -Value $line -Encoding UTF8
            Write-Host $line -ForegroundColor White

            # Pause detection
            if (Test-Path $pauseFile) {
                $ts   = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
                $line = "[$ts][WARN] PAUSED -- delete '$pauseFile' to resume"
                Add-Content -Path $logPath -Value $line -Encoding UTF8
                Write-Host $line -ForegroundColor Yellow
                while (Test-Path $pauseFile) { Start-Sleep -Seconds 2 }
                $ts   = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
                $line = "[$ts][INFO] RESUMED"
                Add-Content -Path $logPath -Value $line -Encoding UTF8
                Write-Host $line -ForegroundColor Cyan
            }
        }

        # Final drain after hash completes
        $item = $null
        while ($queue.TryDequeue([ref]$item)) {
            $parts = $item -split '\|', 2
            $ts    = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
            Add-Content -Path $logPath -Value "[$ts][$($parts[0])] $($parts[1])" -Encoding UTF8
        }

    } -ArgumentList $script:LogPath, $logQueue, $progressBag, $total,
                    $CacheFilePath, $newEntries, $doneFlag, $Label,
                    $pauseFile, $script:HashCache

    # --- Run the parallel hash workers ---
    $results = $Files | ForEach-Object -ThrottleLimit $Throttle -Parallel {
        $f            = $_
        $useHash      = $using:UseHash
        $sizeFilter   = $using:SizeFilter
        $cache        = $using:cacheSnapshot
        $queue        = $using:logQueue
        $newEntries   = $using:newEntries
        $progBag      = $using:progressBag
        $rootPath     = $using:RootPath
        $pauseFile    = $using:pauseFile

        # Pause: workers also check (monitor handles logging; workers just wait)
        while (Test-Path $pauseFile) { Start-Sleep -Milliseconds 500 }

        $sep     = [System.IO.Path]::DirectorySeparatorChar
        $baseUri = [System.Uri]::new(($rootPath.TrimEnd('\','/') + $sep))
        $childUri= [System.Uri]::new($f.FullName)
        $relPath = [System.Uri]::UnescapeDataString(
            $baseUri.MakeRelativeUri($childUri).ToString()
        ).Replace('/', $sep)

        $hash = $null
        if ($useHash) {
            if ($sizeFilter.Contains($f.Length)) {
                $key = $f.FullName.ToLowerInvariant()
                if ($cache.ContainsKey($key)) {
                    $e = $cache[$key]
                    if ($e.Size -eq $f.Length -and $e.Ticks -eq $f.LastWriteTimeUtc.Ticks) {
                        $hash = $e.Hash
                        # Don't flood the queue with cache hits - sample only
                    }
                }
                if (-not $hash) {
                    try {
                        $hash = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash
                        $newEntries.Add([PSCustomObject]@{
                            K = $f.FullName.ToLowerInvariant()
                            S = $f.Length
                            T = $f.LastWriteTimeUtc.Ticks
                            H = $hash
                        })
                    } catch {
                        $queue.Enqueue("ERROR|Hash failed: $($f.FullName): $_")
                    }
                }
            }
        }

        $progBag.Add(1)

        [PSCustomObject]@{
            FullPath = $f.FullName
            RelPath  = $relPath
            Name     = $f.Name
            Size     = $f.Length
            Hash     = $hash
        }
    }

    # Signal monitor to stop and wait for it to finish its final drain
    $doneFlag[0] = 1
    $monitorJob | Wait-Job | Remove-Job -Force

    # Final cache flush for anything the monitor missed in its last cycle
    if ($CacheFilePath -and $newEntries.Count -gt 0) {
        Flush-CacheBatch -Path $CacheFilePath -Batch $newEntries
    }

    # Summary line
    $elapsed = [int]([datetime]::UtcNow - $stageStart).TotalSeconds
    $rate    = if ($elapsed -gt 0) { [int]($total / $elapsed) } else { 0 }
    Write-Log ("Hash {0} complete | {1:N0} files | {2:N0}s elapsed | {3} files/sec" -f `
        $Label, $total, $elapsed, $rate) 'PROGRESS'

    return $results
}

# ===========================================================================
# PRE-FLIGHT
# ===========================================================================
foreach ($p in @($Primary, $Secondary)) {
    if (-not (Test-Path -LiteralPath $p -PathType Container)) {
        throw "Directory not found: $p"
    }
}

$primaryRoot    = Resolve-FullPath $Primary
$secondaryRoot  = Resolve-FullPath $Secondary
$extraneousRoot = Join-Path $secondaryRoot '_extraneous'

if ($primaryRoot.Equals($secondaryRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Primary and Secondary must be different directories."
}
if (Test-IsInsidePath -CandidatePath $secondaryRoot -ParentPath $primaryRoot) {
    throw "Secondary cannot be nested inside Primary."
}
if (Test-IsInsidePath -CandidatePath $primaryRoot -ParentPath $secondaryRoot) {
    throw "Primary cannot be nested inside Secondary."
}

# Load cache and checkpoints
if ($UseHash -and $CacheFile) { Load-HashCache -Path $CacheFile }
$completedStages = Get-CompletedStages

# ===========================================================================
# STARTUP LOG
# ===========================================================================
$scriptStart = [datetime]::UtcNow

Write-Log "========================================" 'INFO'
Write-Log "Sync-DirectoryMirror started" 'INFO'
Write-Log "  Primary          : $primaryRoot" 'INFO'
Write-Log "  Secondary        : $secondaryRoot" 'INFO'
Write-Log "  UseHash          : $($UseHash.IsPresent)" 'INFO'
Write-Log "  HashThrottle     : $HashThrottle" 'INFO'
Write-Log "  CopyThrottle     : $CopyThrottle" 'INFO'
Write-Log "  CacheFile        : $(if ($CacheFile) { $CacheFile } else { '(none)' })" 'INFO'
Write-Log "  PrimaryCache     : $(if ($PrimaryCache) { $PrimaryCache } else { '(none)' })" 'INFO'
Write-Log "  SecondaryCache   : $(if ($SecondaryCache) { $SecondaryCache } else { '(none)' })" 'INFO'
Write-Log "  ProgressInterval : $ProgressInterval files" 'INFO'
Write-Log "  PauseFile        : $($script:PauseFile)" 'INFO'
Write-Log "  WhatIf           : $($PSBoundParameters.ContainsKey('WhatIf'))" 'INFO'
Write-Log "  Log              : $($script:LogPath)" 'INFO'
Write-Log "  Checkpoint       : $($script:CheckpointFile)" 'INFO'
Write-Log "========================================" 'INFO'
Write-Log "To PAUSE : create file '$($script:PauseFile)'" 'INFO'
Write-Log "To RESUME: delete file '$($script:PauseFile)'" 'INFO'
Write-Log "========================================" 'INFO'

if ($completedStages.Count -gt 0) {
    Write-Log "Resuming -- completed stages: $($completedStages.Keys -join ', ')" 'WARN'
}

# ===========================================================================
# STEP 1 -- INVENTORY & HASH
# ===========================================================================
$stepStart = [datetime]::UtcNow
Write-Log "STEP 1: Inventorying directories..." 'INFO'

$primaryRaw = @(Get-ChildItem -LiteralPath $primaryRoot -Recurse -File -Force)
Write-Log "  Primary files found  : $($primaryRaw.Count)" 'INFO'

$secondaryRaw = @(Get-ChildItem -LiteralPath $secondaryRoot -Recurse -File -Force |
    Where-Object { -not (Test-IsInsidePath -CandidatePath $_.FullName -ParentPath $extraneousRoot) })
Write-Log "  Secondary files found: $($secondaryRaw.Count)" 'INFO'

# Size sets for pre-filter
$primarySizes   = [System.Collections.Generic.HashSet[long]]::new(
    [long[]]($primaryRaw   | ForEach-Object { $_.Length }))
$secondarySizes = [System.Collections.Generic.HashSet[long]]::new(
    [long[]]($secondaryRaw | ForEach-Object { $_.Length }))

# ===========================================================================
# PRE-CACHE BYPASS: if both -PrimaryCache and -SecondaryCache are supplied,
# load hashes directly from those files and skip all hashing entirely.
# The orchestrator uses this path for zero-network-hash syncs.
# ===========================================================================
$usePreCache = $PrimaryCache -and $SecondaryCache -and `
               (Test-Path -LiteralPath $PrimaryCache) -and `
               (Test-Path -LiteralPath $SecondaryCache)

if ($usePreCache) {
    Write-Log "PRE-CACHE MODE: Loading hashes from supplied cache files (skipping all hashing)" 'WARN'
    $UseHash = [switch]::Present   # force hash-mode matching

    # Load both caches into HashCache
    foreach ($cacheFile in @($PrimaryCache, $SecondaryCache)) {
        $loaded = 0
        foreach ($line in [System.IO.File]::ReadLines($cacheFile)) {
            $line = $line.Trim(); if (-not $line) { continue }
            try {
                $e = $line | ConvertFrom-Json
                $script:HashCache[$e.K.ToLowerInvariant()] = @{ Size=$e.S; Ticks=$e.T; Hash=$e.H }
                $loaded++
            } catch {}
        }
        Write-Log "  Loaded $loaded entries from $cacheFile" 'INFO'
    }

    # Build file records from inventory using cached hashes (no network reads)
    Write-Log "Building file records from pre-cache..." 'INFO'

    $primaryFiles = $primaryRaw | ForEach-Object -ThrottleLimit $HashThrottle -Parallel {
        $f = $_; $root = $using:primaryRoot; $cache = $using:script:HashCache
        $sep = [System.IO.Path]::DirectorySeparatorChar
        $relPath = [System.Uri]::UnescapeDataString(
            ([System.Uri]::new($root.TrimEnd('\','/') + $sep)).MakeRelativeUri(
             [System.Uri]::new($f.FullName)).ToString()).Replace('/', $sep)
        $key  = $f.FullName.ToLowerInvariant()
        $hash = if ($cache.ContainsKey($key)) { $cache[$key].Hash } else { $null }
        [PSCustomObject]@{ FullPath=$f.FullName; RelPath=$relPath; Name=$f.Name; Size=$f.Length; Hash=$hash }
    }

    $secondaryFiles = [System.Collections.Generic.List[object]]::new()
    foreach ($f in $secondaryRaw) {
        $sep = [System.IO.Path]::DirectorySeparatorChar
        $relPath = [System.Uri]::UnescapeDataString(
            ([System.Uri]::new($secondaryRoot.TrimEnd('\','/') + $sep)).MakeRelativeUri(
             [System.Uri]::new($f.FullName)).ToString()).Replace('/', $sep)
        $key  = $f.FullName.ToLowerInvariant()
        $hash = if ($script:HashCache.ContainsKey($key)) { $script:HashCache[$key].Hash } else { $null }
        $secondaryFiles.Add([PSCustomObject]@{
            FullPath=$f.FullName; RelPath=$relPath; Name=$f.Name
            Size=$f.Length; Hash=$hash; Claimed=$false
        })
    }

    $elapsed = [int]([datetime]::UtcNow - $stepStart).TotalSeconds
    Write-Log ("STEP 1 complete (pre-cache) | {0:N0}s elapsed" -f $elapsed) 'PROGRESS'

} else {

# ---- Hash primary ----
if ($completedStages.ContainsKey('HASH_PRIMARY')) {
    Write-Log "STEP 1: Primary hash -- SKIPPED (checkpoint found)" 'WARN'
    # Rebuild records from cache without rehashing
    $primaryFiles = $primaryRaw | ForEach-Object -ThrottleLimit $HashThrottle -Parallel {
        $f = $_; $root = $using:primaryRoot; $cache = $using:script:HashCache
        $sep     = [System.IO.Path]::DirectorySeparatorChar
        $baseUri = [System.Uri]::new(($root.TrimEnd('\','/') + $sep))
        $relPath = [System.Uri]::UnescapeDataString(
            ([System.Uri]::new($root.TrimEnd('\','/') + $sep)).MakeRelativeUri([System.Uri]::new($f.FullName)).ToString()
        ).Replace('/', $sep)
        [PSCustomObject]@{
            FullPath = $f.FullName; RelPath = $relPath; Name = $f.Name
            Size = $f.Length
            Hash = if ($cache.ContainsKey($f.FullName)) { $cache[$f.FullName].Hash } else { $null }
        }
    }
} else {
    Write-Log "STEP 1: Hashing primary files (parallel, throttle=$HashThrottle)..." 'INFO'
    $primaryFiles = Invoke-ParallelHash `
        -Files $primaryRaw `
        -RootPath $primaryRoot `
        -SizeFilter $secondarySizes `
        -UseHash $UseHash.IsPresent `
        -CacheFilePath $CacheFile `
        -Throttle $HashThrottle `
        -ProgressInterval $ProgressInterval `
        -Label "primary" `
        -IsClaimed $false
    Save-StageComplete 'HASH_PRIMARY'
}

# ---- Hash secondary ----
if ($completedStages.ContainsKey('HASH_SECONDARY')) {
    Write-Log "STEP 1: Secondary hash -- SKIPPED (checkpoint found)" 'WARN'
    $secondaryFiles = [System.Collections.Generic.List[object]]::new()
    foreach ($f in $secondaryRaw) {
        $sep     = [System.IO.Path]::DirectorySeparatorChar
        $baseUri = [System.Uri]::new(($secondaryRoot.TrimEnd('\','/') + $sep))
        $relPath = [System.Uri]::UnescapeDataString(
            $baseUri.MakeRelativeUri([System.Uri]::new($f.FullName)).ToString()
        ).Replace('/', $sep)
        $secondaryFiles.Add([PSCustomObject]@{
            FullPath = $f.FullName; RelPath = $relPath; Name = $f.Name
            Size = $f.Length
            Hash    = if ($script:HashCache.ContainsKey($f.FullName)) { $script:HashCache[$f.FullName].Hash } else { $null }
            Claimed = $false
        })
    }
} else {
    Write-Log "STEP 1: Hashing secondary files (parallel, throttle=$HashThrottle)..." 'INFO'
    $secondaryResults = Invoke-ParallelHash `
        -Files $secondaryRaw `
        -RootPath $secondaryRoot `
        -SizeFilter $primarySizes `
        -UseHash $UseHash.IsPresent `
        -CacheFilePath $CacheFile `
        -Throttle $HashThrottle `
        -ProgressInterval $ProgressInterval `
        -Label "secondary" `
        -IsClaimed $false

    $secondaryFiles = [System.Collections.Generic.List[object]]::new()
    foreach ($r in $secondaryResults) {
        $secondaryFiles.Add([PSCustomObject]@{
            FullPath = $r.FullPath; RelPath = $r.RelPath; Name = $r.Name
            Size     = $r.Size;     Hash    = $r.Hash;    Claimed = $false
        })
    }
    Save-StageComplete 'HASH_SECONDARY'
}

# Compact cache after both passes
if ($UseHash -and $CacheFile -and $script:CacheChanged) {
    Compact-HashCache -Path $CacheFile
}

$elapsed = [int]([datetime]::UtcNow - $stepStart).TotalSeconds
Write-Log ("STEP 1 complete | {0:N0}s elapsed" -f $elapsed) 'PROGRESS'

} # end of pre-cache bypass else block

# Build indexes
$secondaryIndex  = @{}
$secondaryByPath = @{}
foreach ($sf in $secondaryFiles) {
    $key = if ($UseHash -and $sf.Hash) { "HASH|$($sf.Hash)" } else { "NS|$($sf.Name)|$($sf.Size)" }
    if (-not $secondaryIndex.ContainsKey($key)) {
        $secondaryIndex[$key] = [System.Collections.Generic.List[object]]::new()
    }
    $secondaryIndex[$key].Add($sf)
    $secondaryByPath[$sf.FullPath.ToLowerInvariant()] = $sf
}

# ===========================================================================
# STEP 2 -- MIRROR DIRECTORY ARCHITECTURE
# ===========================================================================
$stepStart = [datetime]::UtcNow
Write-Log "STEP 2: Mirroring directory architecture..." 'INFO'
Wait-IfPaused

$primaryDirs = Get-ChildItem -LiteralPath $primaryRoot -Recurse -Directory -Force
foreach ($dir in $primaryDirs) {
    $relDir    = Get-RelativePath -BasePath $primaryRoot -ChildPath $dir.FullName
    $targetDir = Join-Path $secondaryRoot $relDir
    Ensure-Dir $targetDir
}

$elapsed = [int]([datetime]::UtcNow - $stepStart).TotalSeconds
Write-Log ("STEP 2 complete | {0} dirs processed | {1:N0}s elapsed" -f $primaryDirs.Count, $elapsed) 'PROGRESS'

# ===========================================================================
# STEP 3 -- RESOLVE FILES
# ===========================================================================
$stepStart = [datetime]::UtcNow
Write-Log "STEP 3: Resolving files..." 'INFO'

$toCopy = [System.Collections.Generic.List[object]]::new()
$total  = $primaryFiles.Count
$i      = 0

foreach ($pf in $primaryFiles) {
    $i++
    if ($i % $ProgressInterval -eq 0) {
        $pct  = [int]($i / $total * 100)
        $rate = if (([datetime]::UtcNow - $stepStart).TotalSeconds -gt 0) {
            [int]($i / ([datetime]::UtcNow - $stepStart).TotalSeconds)
        } else { 0 }
        Write-Log ("STEP 3 progress | {0:N0} / {1:N0} ({2}%) | {3} files/sec" -f $i, $total, $pct, $rate) 'PROGRESS'
        Wait-IfPaused
    }
    Write-Progress-Simple "Resolving files" $i $total

    $targetPath = Join-Path $secondaryRoot $pf.RelPath
    $lookupKey  = if ($UseHash -and $pf.Hash) { "HASH|$($pf.Hash)" } else { "NS|$($pf.Name)|$($pf.Size)" }

    # Already at correct path?
    if (Test-Path -LiteralPath $targetPath) {
        $existing     = Get-Item -LiteralPath $targetPath
        $sizeMatch    = ($existing.Length -eq $pf.Size)
        $contentMatch = $false

        if ($sizeMatch) {
            if ($UseHash -and $pf.Hash) {
                $secRec       = $secondaryByPath[$targetPath.ToLowerInvariant()]
                $existingHash = if ($secRec -and $secRec.Hash) { $secRec.Hash }
                                else { (Get-FileHash -LiteralPath $targetPath -Algorithm SHA256).Hash }
                $contentMatch = ($existingHash -eq $pf.Hash)
                if (-not $contentMatch) {
                    Write-Log "HASH MISMATCH (will overwrite): $($pf.RelPath)" 'WARN'
                    $stats.HashMismatches++
                }
            } else {
                $contentMatch = $true
            }
        }

        if ($contentMatch) {
            Write-Log "SKIP (already correct): $($pf.RelPath)" 'INFO'
            $inPlace = $secondaryByPath[$targetPath.ToLowerInvariant()]
            if ($inPlace) { $inPlace.Claimed = $true }
            continue
        }

        # Wrong content -- displace occupant
        $occupant = $secondaryByPath[$targetPath.ToLowerInvariant()]
        if ($occupant -and -not $occupant.Claimed) {
            Write-Log "DISPLACING wrong-content occupant: $($pf.RelPath)" 'WARN'
            try {
                Move-ToExtraneous -SourcePath $occupant.FullPath `
                                  -RelPath    $occupant.RelPath `
                                  -ExtraRoot  $extraneousRoot
                $occupant.Claimed = $true
            } catch {
                Write-Log "ERROR displacing occupant `"$($occupant.FullPath)`": $_" 'ERROR'
                $stats.Errors++
                continue
            }
        }
    }

    # Search secondary for a match
    $candidates = $secondaryIndex[$lookupKey]
    $matched    = $null
    if ($candidates -and $candidates.Count -gt 0) {
        $matched = $candidates | Where-Object { -not $_.Claimed } | Select-Object -First 1
    }

    if ($matched) {
        $matched.Claimed = $true

        if ($matched.FullPath.Equals($targetPath, [System.StringComparison]::OrdinalIgnoreCase)) {
            Write-Log "SKIP (match already at target): $($pf.RelPath)" 'INFO'
            continue
        }

        # Displace blocker if needed
        if (Test-Path -LiteralPath $targetPath) {
            $blocker = $secondaryByPath[$targetPath.ToLowerInvariant()]
            if ($blocker -and -not $blocker.Claimed) {
                try {
                    Move-ToExtraneous -SourcePath $blocker.FullPath `
                                      -RelPath    $blocker.RelPath `
                                      -ExtraRoot  $extraneousRoot
                    $blocker.Claimed = $true
                } catch {
                    Write-Log "ERROR displacing blocker `"$($blocker.FullPath)`": $_" 'ERROR'
                    $stats.Errors++
                    continue
                }
            }
        }

        try {
            Move-FileItem -Source $matched.FullPath -Destination $targetPath
        } catch {
            Write-Log "ERROR moving `"$($matched.FullPath)`": $_" 'ERROR'
            $stats.Errors++
        }
    } else {
        $toCopy.Add($pf)
    }
}
Write-Progress -Activity "Resolving files" -Completed

# Parallel copy pass
if ($toCopy.Count -gt 0) {
    Write-Log "STEP 3: Copying $($toCopy.Count) missing files from primary (parallel, throttle=$CopyThrottle)..." 'INFO'
    Wait-IfPaused

    $isDryRun   = $PSBoundParameters.ContainsKey('WhatIf')
    $logQueue   = [System.Collections.Concurrent.ConcurrentQueue[string]]::new()
    $errorQueue = [System.Collections.Concurrent.ConcurrentQueue[string]]::new()
    $copyCount  = [System.Collections.Concurrent.ConcurrentBag[int]]::new()
    $pauseFile  = $script:PauseFile
    $copyTotal  = $toCopy.Count
    $copyInterval = $ProgressInterval

    $toCopy | ForEach-Object -ThrottleLimit $CopyThrottle -Parallel {
        $pf        = $_
        $secRoot   = $using:secondaryRoot
        $isDryRun  = $using:isDryRun
        $queue     = $using:logQueue
        $errQueue  = $using:errorQueue
        $counter   = $using:copyCount
        $pauseFile = $using:pauseFile

        if (Test-Path $pauseFile) {
            while (Test-Path $pauseFile) { Start-Sleep -Milliseconds 500 }
        }

        $targetPath = Join-Path $secRoot $pf.RelPath
        $targetDir  = Split-Path $targetPath -Parent
        try {
            if (-not (Test-Path -LiteralPath $targetDir -PathType Container)) {
                New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
            }
            if ($isDryRun) {
                $queue.Enqueue("DRY|DRY: Would COPY `"$($pf.FullPath)`" -> `"$targetPath`"")
            } else {
                Copy-Item -LiteralPath $pf.FullPath -Destination $targetPath -Force
                $queue.Enqueue("ACTION|COPIED: `"$($pf.FullPath)`" -> `"$targetPath`"")
            }
            $counter.Add(1)
        } catch {
            $errQueue.Enqueue("ERROR|ERROR copying `"$($pf.FullPath)`": $_")
        }
    }

    Flush-LogQueue $logQueue
    $item = $null
    while ($errorQueue.TryDequeue([ref]$item)) {
        $parts = $item -split '\|', 2
        Write-Log $parts[1] $parts[0]
        $stats.Errors++
    }
    $stats.FilesCopied = $copyCount.Count
}

$elapsed = [int]([datetime]::UtcNow - $stepStart).TotalSeconds
$rate    = if ($elapsed -gt 0) { [int]($total / $elapsed) } else { 0 }
Write-Log ("STEP 3 complete | {0:N0} moved, {1:N0} copied | {2:N0}s elapsed | {3} files/sec" -f `
    $stats.FilesMoved, $stats.FilesCopied, $elapsed, $rate) 'PROGRESS'

# ===========================================================================
# STEP 4 -- EXTRANEOUS FILES
# ===========================================================================
$stepStart = [datetime]::UtcNow
Write-Log "STEP 4: Moving extraneous secondary files to _extraneous..." 'INFO'
Wait-IfPaused

foreach ($ef in ($secondaryFiles | Where-Object { -not $_.Claimed })) {
    if (-not (Test-Path -LiteralPath $ef.FullPath)) { continue }
    try {
        Move-ToExtraneous -SourcePath $ef.FullPath `
                          -RelPath    $ef.RelPath `
                          -ExtraRoot  $extraneousRoot
    } catch {
        Write-Log "ERROR moving extraneous `"$($ef.FullPath)`": $_" 'ERROR'
        $stats.Errors++
    }
}

$elapsed = [int]([datetime]::UtcNow - $stepStart).TotalSeconds
Write-Log ("STEP 4 complete | {0:N0} extraneous | {1:N0}s elapsed" -f $stats.FilesExtraneous, $elapsed) 'PROGRESS'

# ===========================================================================
# STEP 5 -- PRUNE EMPTY DIRECTORIES
# ===========================================================================
$stepStart = [datetime]::UtcNow
Write-Log "STEP 5: Pruning empty directories in secondary..." 'INFO'

$emptyDirs = Get-ChildItem -LiteralPath $secondaryRoot -Recurse -Directory -Force |
    Where-Object { -not (Test-IsInsidePath -CandidatePath $_.FullName -ParentPath $extraneousRoot) } |
    Sort-Object { $_.FullName.Length } -Descending

$pruned = 0
foreach ($ed in $emptyDirs) {
    if (Get-ChildItem -LiteralPath $ed.FullName -Force | Select-Object -First 1) { continue }
    try {
        if ($PSCmdlet.ShouldProcess($ed.FullName, 'Remove empty directory')) {
            Remove-Item -LiteralPath $ed.FullName -Force
            Write-Log "Removed empty dir: $($ed.FullName)" 'ACTION'
            $pruned++
        } else {
            Write-Log "DRY: Would remove empty dir: $($ed.FullName)" 'DRY'
        }
    } catch {
        Write-Log "WARN: Could not remove `"$($ed.FullName)`": $_" 'WARN'
    }
}

$elapsed = [int]([datetime]::UtcNow - $stepStart).TotalSeconds
Write-Log ("STEP 5 complete | {0} dirs pruned | {1:N0}s elapsed" -f $pruned, $elapsed) 'PROGRESS'

# ===========================================================================
# CLEANUP CHECKPOINT (on clean completion)
# ===========================================================================
if (Test-Path $script:CheckpointFile) {
    Remove-Item $script:CheckpointFile -Force
    Write-Log "Checkpoint file removed (clean completion)" 'INFO'
}

# ===========================================================================
# SUMMARY
# ===========================================================================
$totalElapsed = [int]([datetime]::UtcNow - $scriptStart).TotalSeconds
$hh = [int]($totalElapsed / 3600)
$mm = [int](($totalElapsed % 3600) / 60)
$ss = $totalElapsed % 60

Write-Log "========================================" 'INFO'
Write-Log "COMPLETE" 'INFO'
Write-Log ("  Total elapsed       : {0:D2}h {1:D2}m {2:D2}s" -f $hh, $mm, $ss) 'INFO'
Write-Log "  Match mode          : $(if ($UseHash) { 'SHA256 hash' } else { 'Name + Size' })" 'INFO'
Write-Log "  Directories created : $($stats.DirsCreated)" 'INFO'
Write-Log "  Files moved         : $($stats.FilesMoved)" 'INFO'
Write-Log "  Files copied        : $($stats.FilesCopied)" 'INFO'
Write-Log "  Files extraneous    : $($stats.FilesExtraneous)" 'INFO'
if ($UseHash) {
    Write-Log "  Hash mismatches     : $($stats.HashMismatches)" 'INFO'
    Write-Log "  Cache hits          : $($stats.CacheHits)" 'INFO'
}
Write-Log "  Errors              : $($stats.Errors)" 'INFO'
Write-Log "  Log                 : $($script:LogPath)" 'INFO'
Write-Log "========================================" 'INFO'

if ($stats.FilesExtraneous -gt 0) {
    Write-Log "Review extraneous files in: $extraneousRoot" 'WARN'
}
if ($UseHash -and $stats.HashMismatches -gt 0) {
    Write-Log "Hash mismatches: $($stats.HashMismatches) file(s) replaced from primary." 'WARN'
}

# SIG # Begin signature block
# MIIFngYJKoZIhvcNAQcCoIIFjzCCBYsCAQExDzANBglghkgBZQMEAgEFADB5Bgor
# BgEEAYI3AgEEoGswaTA0BgorBgEEAYI3AgEeMCYCAwEAAAQQH8w7YFlLCE63JNLG
# KX7zUQIBAAIBAAIBAAIBAAIBADAxMA0GCWCGSAFlAwQCAQUABCDJqB1PncSWF7Y9
# zVhzxAox2pYjbAlfDBU74vQEd7ya+6CCAxAwggMMMIIB9KADAgECAhAhiJkQjcYV
# vkSjiEdp9RGMMA0GCSqGSIb3DQEBCwUAMB4xHDAaBgNVBAMME0RhdmlkIExvY2Fs
# IFNjcmlwdHMwHhcNMjYwNTI2MjIyMDA4WhcNMjcwNTI2MjI0MDA4WjAeMRwwGgYD
# VQQDDBNEYXZpZCBMb2NhbCBTY3JpcHRzMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A
# MIIBCgKCAQEArOHEX2VZUSiL3F4HnlRvN5WQQBtd1mxE3FhIDttricuMlUcztQSn
# 6mN6A+JFyuWbkp3Hne4ML/oTozCTPIYvcQkqD+oBPfgIft3pWys5MsL3ftEyqhfx
# Yp1Ty9lad4a4Cmkh+wPUeUTRKF6d4srs4s5FGPKVHfQ6CQYrnArbM6lffFOasotw
# P1cTbLDSDib9otHyAtN4oVi03WbcbfcIuwks0kpQILRrMHLQxAV/ZheKMs2jUxDx
# PtRpZLXzg8F/X6iPEhiEYQePLlc2sf38tmWDk4Hy7bDg49BES3C9HD1WXHMBnUqy
# 0mG3FIih1f0qe2YAE4yQfALBDdPVnQmxrQIDAQABo0YwRDAOBgNVHQ8BAf8EBAMC
# B4AwEwYDVR0lBAwwCgYIKwYBBQUHAwMwHQYDVR0OBBYEFFGHu73uR4ri7DsqywXe
# /Seo8p6IMA0GCSqGSIb3DQEBCwUAA4IBAQB4VLHtsiYDvF0vJHJFYKKprio8JFhu
# wK6sMx9UVBD248ecYbldBoWah/ZpRxSDYOuzHUIbfNxOZewr/P1YbJHg+LFIPsm4
# vRLu1K7JzqjuXL3xNcy6cyeKJeRtUjRcf96CbDUPsfMk3Gjvn1FbEogPzdo9v11+
# plsvCB++HGSmioTu9MyPGWNC79DGwjHi/Ml7/LvOA/GPghmCtsPHw1dsNMGJvnCc
# bKBLi+vDkSQNVN3lrcwl9NzdRHdCoB9xNTyNyrbbi5SVv1haVlO0xCbQ7wlOaut/
# Hg6HJPGaVKuafiXDfn5/fPoTiqIIfo/wXUsd6Wlogp/y7PXVlJfJPrWBMYIB5DCC
# AeACAQEwMjAeMRwwGgYDVQQDDBNEYXZpZCBMb2NhbCBTY3JpcHRzAhAhiJkQjcYV
# vkSjiEdp9RGMMA0GCWCGSAFlAwQCAQUAoIGEMBgGCisGAQQBgjcCAQwxCjAIoAKA
# AKECgAAwGQYJKoZIhvcNAQkDMQwGCisGAQQBgjcCAQQwHAYKKwYBBAGCNwIBCzEO
# MAwGCisGAQQBgjcCARUwLwYJKoZIhvcNAQkEMSIEIJkUoXFkse7zKr9BmymT0HMa
# dg2obtmB5F3ldqaD6Gu6MA0GCSqGSIb3DQEBAQUABIIBAJ2R7qg66igxkI3Ju21K
# jdqXPn5m2IOkGxgKWcHELkIZShBturM7tcmTztd4gk20c2KUceKMTa2EuEFmTBRg
# oIZysnLEbYybiqoQOKFq9WEePezC0U/3IgGbon2NMRaeNhJiOZjUgN+gvTC1KsDa
# kgGlWsdmaRgsN5BvKJODOLuUyd7UGX9wPs2NejfpzBE3nX8CxOrcgoQkZnJN3o4a
# ywU9XMR9DU9i5AYQ1erDebhQY3iBpQ+v224uw4VyBPHFk9WCk5kLVDTPOgTsiNod
# himIhWS+t1LoNC3wwphXZeAzC5UU53ocyFHCjKf+thLKkeV5RKvXqlUph5ObdSL7
# dAU=
# SIG # End signature block
