#Requires -Version 7.0
<#
.SYNOPSIS
    Locally hashes all files under a directory and writes a JSONL cache file
    compatible with Sync-DirectoryMirror.ps1 and Sync-Mirror-Orchestrator.ps1.

.DESCRIPTION
    Designed to run on the Windows machine to pre-hash the secondary directory
    (e.g. X:\Comix) in parallel with Build-HashCache.sh running on the NAS.
    Outputs one JSON object per line:
        {"K":"X:\\Comix\\file.cbz","S":1234567,"T":638000000000000000,"H":"ABCDEF..."}

.PARAMETER Path
    Local path to hash, e.g. X:\Comix

.PARAMETER OutputFile
    Path to write the JSONL cache file.

.PARAMETER HashThrottle
    Parallel hash workers. Default: 8.

.PARAMETER LogFile
    Optional log file path.

.EXAMPLE
    .\Build-HashCache.ps1 -Path "X:\Comix" -OutputFile "C:\temp\SecondaryCache.jsonl"
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Path,
    [Parameter(Mandatory)] [string] $OutputFile,
    [Parameter()] [int]    $HashThrottle = 8,
    [Parameter()] [string] $LogFile      = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
$script:LogPath = if ($LogFile) { $LogFile } else {
    Join-Path (Get-Location) ("HashCache_{0}.log" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
}

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $ts   = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = "[$ts][$Level] $Message"
    Add-Content -Path $script:LogPath -Value $line -Encoding UTF8
    $color = switch ($Level) {
        'INFO'     { 'Cyan'   }
        'WARN'     { 'Yellow' }
        'ERROR'    { 'Red'    }
        'PROGRESS' { 'White'  }
        default    { 'Cyan'   }
    }
    Write-Host $line -ForegroundColor $color
}

# ---------------------------------------------------------------------------
# Load existing cache (for incremental updates)
# ---------------------------------------------------------------------------
$existingCache = [System.Collections.Generic.Dictionary[string,object]]::new(
    [System.StringComparer]::OrdinalIgnoreCase)

if (Test-Path -LiteralPath $OutputFile) {
    $loaded = 0
    foreach ($line in [System.IO.File]::ReadLines($OutputFile)) {
        $line = $line.Trim()
        if (-not $line) { continue }
        try {
            $e = $line | ConvertFrom-Json
            $existingCache[$e.K] = @{ Size = $e.S; Ticks = $e.T; Hash = $e.H }
            $loaded++
        } catch {}
    }
    Write-Log "Loaded $loaded existing cache entries from $OutputFile" 'INFO'
}

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
    throw "Directory not found: $Path"
}
$rootPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\','/')

Write-Log "========================================"  'INFO'
Write-Log "Build-HashCache.ps1 started"               'INFO'
Write-Log "  Path         : $rootPath"                'INFO'
Write-Log "  OutputFile   : $OutputFile"              'INFO'
Write-Log "  HashThrottle : $HashThrottle"            'INFO'
Write-Log "  Existing     : $($existingCache.Count) cached entries" 'INFO'
Write-Log "========================================"  'INFO'

# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------
Write-Log "Inventorying $rootPath ..." 'INFO'
$allFiles = @(Get-ChildItem -LiteralPath $rootPath -Recurse -File -Force)
$total    = $allFiles.Count
Write-Log "Found $total files" 'INFO'

# ---------------------------------------------------------------------------
# Parallel hashing with live monitor
# ---------------------------------------------------------------------------
$logQueue    = [System.Collections.Concurrent.ConcurrentQueue[string]]::new()
$newEntries  = [System.Collections.Concurrent.ConcurrentBag[object]]::new()
$progressBag = [System.Collections.Concurrent.ConcurrentBag[int]]::new()
$doneFlag    = [int[]]@(0)
$stageStart  = [datetime]::UtcNow

# Background monitor - writes progress every 5 seconds and flushes cache
$monitorJob = Start-ThreadJob -ScriptBlock {
    param($logPath, $queue, $progBag, $total, $outputFile, $newEntries, $doneFlag)
    $start = [datetime]::UtcNow

    while ($doneFlag[0] -eq 0) {
        Start-Sleep -Seconds 5

        # Drain log queue
        $item = $null
        while ($queue.TryDequeue([ref]$item)) {
            $parts = $item -split '\|', 2
            $ts    = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
            Add-Content -Path $logPath -Value "[$ts][$($parts[0])] $($parts[1])" -Encoding UTF8
        }

        # Flush new cache entries to disk
        if ($newEntries.Count -gt 0) {
            $lines = [System.Collections.Generic.List[string]]::new()
            foreach ($e in $newEntries.ToArray()) {
                $lines.Add(($e | ConvertTo-Json -Compress))
            }
            try { $newEntries.Clear() } catch {}
            if ($lines.Count -gt 0) {
                [System.IO.File]::AppendAllLines($outputFile, $lines)
            }
        }

        # Progress line
        $done    = $progBag.Count
        $elapsed = [int]([datetime]::UtcNow - $start).TotalSeconds
        $pct     = if ($total -gt 0) { [int]($done / $total * 100) } else { 0 }
        $rate    = if ($elapsed -gt 0) { [math]::Round($done / $elapsed, 1) } else { 0 }
        $eta     = if ($rate -gt 0) { [int](($total - $done) / $rate) } else { 0 }
        $etaStr  = if ($eta -gt 3600) { "{0:D2}h {1:D2}m" -f [int]($eta/3600), [int](($eta%3600)/60) }
                   elseif ($eta -gt 60) { "{0:D2}m {1:D2}s" -f [int]($eta/60), ($eta%60) }
                   else { "${eta}s" }
        $ts      = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        $line    = "[$ts][PROGRESS] Secondary | $done / $total ($pct%) | $rate files/sec | ETA $etaStr"
        Add-Content -Path $logPath -Value $line -Encoding UTF8
        Write-Host $line -ForegroundColor White
    }

    # Final drain
    $item = $null
    while ($queue.TryDequeue([ref]$item)) {
        $parts = $item -split '\|', 2
        $ts    = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        Add-Content -Path $logPath -Value "[$ts][$($parts[0])] $($parts[1])" -Encoding UTF8
    }
    if ($newEntries.Count -gt 0) {
        $lines = [System.Collections.Generic.List[string]]::new()
        foreach ($e in $newEntries.ToArray()) { $lines.Add(($e | ConvertTo-Json -Compress)) }
        try { $newEntries.Clear() } catch {}
        if ($lines.Count -gt 0) { [System.IO.File]::AppendAllLines($outputFile, $lines) }
    }
} -ArgumentList $script:LogPath, $logQueue, $progressBag, $total,
                $OutputFile, $newEntries, $doneFlag

Write-Log "Hashing $total files (parallel, throttle=$HashThrottle)..." 'INFO'

# Parallel hash workers
$results = $allFiles | ForEach-Object -ThrottleLimit $HashThrottle -Parallel {
    $f           = $_
    $cache       = $using:existingCache
    $queue       = $using:logQueue
    $newEntries  = $using:newEntries
    $progBag     = $using:progressBag
    $rootPath    = $using:rootPath

    $hash = $null
    $key  = $f.FullName

    # Check cache first (size + ticks match = cache hit)
    if ($cache.ContainsKey($key)) {
        $e = $cache[$key]
        if ($e.Size -eq $f.Length -and $e.Ticks -eq $f.LastWriteTimeUtc.Ticks) {
            $hash = $e.Hash
        }
    }

    # Cache miss - compute hash
    if (-not $hash) {
        try {
            $hash = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash
            $newEntries.Add([PSCustomObject]@{
                K = $f.FullName
                S = $f.Length
                T = $f.LastWriteTimeUtc.Ticks
                H = $hash
            })
        } catch {
            $queue.Enqueue("ERROR|Hash failed: $($f.FullName): $_")
        }
    }

    $progBag.Add(1)

    [PSCustomObject]@{
        FullPath = $f.FullName
        Name     = $f.Name
        Size     = $f.Length
        Hash     = $hash
    }
}

# Signal monitor to stop
$doneFlag[0] = 1
$monitorJob | Wait-Job | Remove-Job -Force

# Final flush of any remaining entries
if ($newEntries.Count -gt 0) {
    $lines = [System.Collections.Generic.List[string]]::new()
    foreach ($e in $newEntries.ToArray()) { $lines.Add(($e | ConvertTo-Json -Compress)) }
    [System.IO.File]::AppendAllLines($OutputFile, $lines)
}

# ---------------------------------------------------------------------------
# Compact cache (deduplicate, last-wins)
# ---------------------------------------------------------------------------
Write-Log "Compacting cache file..." 'INFO'
$finalCache = [System.Collections.Generic.Dictionary[string,string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase)

foreach ($line in [System.IO.File]::ReadLines($OutputFile)) {
    $line = $line.Trim()
    if (-not $line) { continue }
    try {
        $e = $line | ConvertFrom-Json
        $finalCache[$e.K] = $line
    } catch {}
}

$compactLines = [System.Collections.Generic.List[string]]::new()
foreach ($line in $finalCache.Values) { $compactLines.Add($line) }
[System.IO.File]::WriteAllLines($OutputFile, $compactLines)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
$elapsed = [int]([datetime]::UtcNow - $stageStart).TotalSeconds
$hh = [int]($elapsed / 3600)
$mm = [int](($elapsed % 3600) / 60)
$ss = $elapsed % 60

Write-Log "========================================" 'INFO'
Write-Log "COMPLETE" 'INFO'
Write-Log ("  Elapsed    : {0:D2}h {1:D2}m {2:D2}s" -f $hh, $mm, $ss) 'INFO'
Write-Log "  Files      : $total" 'INFO'
Write-Log "  Cache hits : $($existingCache.Count)" 'INFO'
Write-Log "  Output     : $OutputFile ($($compactLines.Count) entries)" 'INFO'
Write-Log "========================================" 'INFO'

# SIG # Begin signature block
# MIIFngYJKoZIhvcNAQcCoIIFjzCCBYsCAQExDzANBglghkgBZQMEAgEFADB5Bgor
# BgEEAYI3AgEEoGswaTA0BgorBgEEAYI3AgEeMCYCAwEAAAQQH8w7YFlLCE63JNLG
# KX7zUQIBAAIBAAIBAAIBAAIBADAxMA0GCWCGSAFlAwQCAQUABCCuLuP8bbZ/Ezfz
# QUB3CW9P3JyTz1es56pXO7SpG7S106CCAxAwggMMMIIB9KADAgECAhAhiJkQjcYV
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
# MAwGCisGAQQBgjcCARUwLwYJKoZIhvcNAQkEMSIEIHskAx3CRnFNJnCT5RMaFxXF
# RfmgeFkF6sEudr6FkZdNMA0GCSqGSIb3DQEBAQUABIIBAEZqsW8lGrj2LbefsOTR
# ExMHwJ0/egvpdcBkPwKpLPHOxwn0JNk04LGGHcqtw5CO6niqnIhIyiii6jLD3hfA
# qzDbjXv4XgwlBfUWMftlutIxR58w1KrqvI6nnftNHaya84Xg8hhP0jHccnQNfLxb
# G0Q09CS0hAYZuS7m0qjqTskE4+a3Zyy9Yk6Hyx8vaY0Ug8AVBaSKavMTJeHuDYd6
# ynZj4REucZ0+PxeSjFP9Ko2fpUs/3MpG0sVjnDsQKUTJ8azhxutArZwgT2Pbnang
# unzRY0Eox/yVYatUNdg/1tvQfrdjG18VLG+CIM9yCG2JTZ8VWCP4z6fJa9VRmAa2
# ck8=
# SIG # End signature block
