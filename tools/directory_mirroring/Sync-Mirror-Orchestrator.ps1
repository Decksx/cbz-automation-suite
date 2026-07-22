
#Requires -Version 7.0
<#
.SYNOPSIS
    Orchestrates parallel pre-hashing of both primary (NAS) and secondary
    (local) directories, then runs the directory mirror sync.

.DESCRIPTION
    Kicks off two hash jobs simultaneously:
      1. SSH into the Tower NAS and runs Build-HashCache.sh locally
         (full disk speed, no SMB overhead)
      2. Runs Build-HashCache.ps1 locally against the secondary directory
         (full local disk speed)

    Monitors both jobs with live combined progress. When both finish, pulls
    the primary cache from the NAS via SMB, merges both caches, then runs
    Sync-DirectoryMirror.ps1 with pre-built hashes -- zero network hashing
    at sync time.

.PARAMETER Primary
    UNC path to the primary (NAS) directory. e.g. \\tower\media\comics\comix

.PARAMETER Secondary
    Local path to the secondary directory. e.g. X:\Comix

.PARAMETER TowerHost
    Hostname or IP of the Tower NAS. e.g. tower.local or 192.168.1.100

.PARAMETER TowerUser
    SSH username for the Tower. e.g. root

.PARAMETER TowerLocalPath
    Local path on the Tower matching Primary. e.g. /mnt/user/media/comics/Comix

.PARAMETER TowerSharePath
    SMB path to pull the primary cache from. e.g. \\tower\media\comics
    The cache file will be written to TowerSharePath\PrimaryCache.jsonl

.PARAMETER ScriptDir
    Directory containing Build-HashCache.ps1 and Sync-DirectoryMirror.ps1.
    If Build-HashCache.sh is also present, it is uploaded to the Tower;
    otherwise the script expects /tmp/Build-HashCache.sh to already exist there.
    Defaults to the directory of this script.

.PARAMETER WorkDir
    Working directory for cache files and logs. Default: C:\temp\SyncMirror

.PARAMETER HashThrottle
    Parallel hash workers for the local (secondary) hash job. Default: 8

.PARAMETER CopyThrottle
    Parallel copy workers for the sync stage. Default: 4

.PARAMETER TowerHashThreads
    Hash threads for the Tower bash script. Default: 2

.PARAMETER SkipHashing
    Skip the hashing stage and go straight to sync using existing cache files.
    Useful if hashing already completed and you just want to re-run the sync.

.PARAMETER WhatIf
    Pass -WhatIf through to the sync script (dry run).

.EXAMPLE
    # Full run - hash both sides then sync
    .\Sync-Mirror-Orchestrator.ps1 `
        -Primary "\\tower\media\comics\comix" `
        -Secondary "X:\Comix" `
        -TowerHost "tower.local" `
        -TowerUser "root" `
        -TowerLocalPath "/mnt/user/media/comics/Comix" `
        -TowerSharePath "\\tower\media\comics"

.EXAMPLE
    # Skip hashing, just sync with existing caches
    .\Sync-Mirror-Orchestrator.ps1 `
        -Primary "\\tower\media\comics\comix" `
        -Secondary "X:\Comix" `
        -TowerHost "tower.local" `
        -TowerUser "root" `
        -TowerLocalPath "/mnt/user/media/comics/Comix" `
        -TowerSharePath "\\tower\media\comics" `
        -SkipHashing
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Primary,
    [Parameter(Mandatory)] [string] $Secondary,
    [Parameter(Mandatory)] [string] $TowerHost,
    [Parameter(Mandatory)] [string] $TowerUser,
    [Parameter(Mandatory)] [string] $TowerLocalPath,
    [Parameter(Mandatory)] [string] $TowerSharePath,
    [Parameter()] [string] $ScriptDir        = "",
    [Parameter()] [string] $WorkDir          = "C:\temp\SyncMirror",
    [Parameter()] [int]    $HashThrottle     = 8,
    [Parameter()] [int]    $CopyThrottle     = 4,
    [Parameter()] [int]    $TowerHashThreads = 2,
    [Parameter()] [switch] $SkipHashing,
    [Parameter()] [switch] $WhatIf
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
if (-not $ScriptDir) {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}

# Ensure work directory exists
if (-not (Test-Path $WorkDir)) { New-Item -ItemType Directory -Path $WorkDir -Force | Out-Null }

$timestamp        = Get-Date -Format 'yyyyMMdd_HHmmss'
$LogFile          = Join-Path $WorkDir "Orchestrator_$timestamp.log"
$PrimaryCacheLocal = Join-Path $WorkDir "PrimaryCache.jsonl"
$PrimaryCacheShare = Join-Path $TowerSharePath "PrimaryCache.jsonl"
$SecondaryCacheFile= Join-Path $WorkDir "SecondaryCache.jsonl"
$MergedCacheFile  = Join-Path $WorkDir "MergedCache.jsonl"
$SyncLogFile      = Join-Path $WorkDir "Sync_$timestamp.log"

$BuildHashCacheSh = Join-Path $ScriptDir "Build-HashCache.sh"
$BuildHashCachePs = Join-Path $ScriptDir "Build-HashCache.ps1"
$SyncScript       = Join-Path $ScriptDir "Sync-DirectoryMirror.ps1"

# Validate required scripts exist
foreach ($f in @($BuildHashCachePs, $SyncScript)) {
    if (-not (Test-Path $f)) { throw "Required script not found: $f" }
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $ts   = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = "[$ts][$Level] $Message"
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    $color = switch ($Level) {
        'INFO'     { 'Cyan'    }
        'WARN'     { 'Yellow'  }
        'ERROR'    { 'Red'     }
        'ACTION'   { 'Green'   }
        'PROGRESS' { 'White'   }
        default    { 'Cyan'    }
    }
    Write-Host $line -ForegroundColor $color
}

# ---------------------------------------------------------------------------
# SSH helper - runs a command on Tower and returns stdout
# Uses native ssh.exe (built into Windows 10/11)
# Prompts for password interactively (ssh handles this)
# ---------------------------------------------------------------------------
function Invoke-TowerSSH {
    param(
        [string]$Command,
        [switch]$Background  # fire and forget via nohup
    )
    if ($Background) {
        $Command = "nohup $Command > /tmp/orchestrator_primary.log 2>&1 &"
    }
    $result = ssh -o StrictHostKeyChecking=no `
                  -i "$env:USERPROFILE\.ssh\tower_key" `
                  "${TowerUser}@${TowerHost}" $Command 2>&1
    return $result
}

function Get-TowerLog {
    # Tail the remote hash log via SSH
    $lines = ssh -o StrictHostKeyChecking=no `
                 -i "$env:USERPROFILE\.ssh\tower_key" `
                 "${TowerUser}@${TowerHost}" `
                 "tail -5 /tmp/orchestrator_primary.log 2>/dev/null" 2>&1
    return $lines
}

function Test-TowerHashRunning {
    $result = ssh -o StrictHostKeyChecking=no `
                  -i "$env:USERPROFILE\.ssh\tower_key" `
                  "${TowerUser}@${TowerHost}" `
                  "pgrep -f 'build_hash_cache.py' > /dev/null 2>&1 && echo RUNNING || echo DONE" 2>&1
    return ($result -join '') -match 'RUNNING'
}

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
Write-Log "========================================" 'INFO'
Write-Log "Sync-Mirror-Orchestrator started" 'INFO'
Write-Log "  Primary          : $Primary" 'INFO'
Write-Log "  Secondary        : $Secondary" 'INFO'
Write-Log "  TowerHost        : $TowerHost" 'INFO'
Write-Log "  TowerLocalPath   : $TowerLocalPath" 'INFO'
Write-Log "  TowerSharePath   : $TowerSharePath" 'INFO'
Write-Log "  WorkDir          : $WorkDir" 'INFO'
Write-Log "  SkipHashing      : $($SkipHashing.IsPresent)" 'INFO'
Write-Log "  WhatIf           : $($WhatIf.IsPresent)" 'INFO'
Write-Log "  Log              : $LogFile" 'INFO'
Write-Log "========================================" 'INFO'

$orchestratorStart = [datetime]::UtcNow

# ===========================================================================
# STAGE 1 -- PARALLEL HASHING
# ===========================================================================
if (-not $SkipHashing) {

    Write-Log "STAGE 1: Starting parallel hash jobs..." 'INFO'
    Write-Log "  Primary hash   : SSH -> Tower -> Build-HashCache.sh (local NAS disk)" 'INFO'
    Write-Log "  Secondary hash : Local -> Build-HashCache.ps1 ($Secondary)" 'INFO'

    # ---- Upload Build-HashCache.sh to Tower when the local copy is available ----
    Write-Log "Uploading Build-HashCache.sh to Tower..." 'INFO'
    try {
        $towerTempScript = Join-Path $TowerSharePath ".." | Join-Path -ChildPath "Build-HashCache.sh"
        # Try to write via SMB to a writable share location
        # Fall back to echo-based upload via SSH if SMB write fails
        if (Test-Path $BuildHashCacheSh) {
            $scriptContent = Get-Content $BuildHashCacheSh -Raw
            # Upload via SSH heredoc
            $uploadCmd = "cat > /tmp/Build-HashCache.sh << 'ENDSSH'`n$scriptContent`nENDSSH`nchmod +x /tmp/Build-HashCache.sh"
            # Use a temp file approach for large scripts
            $encodedScript = [Convert]::ToBase64String(
                [System.Text.Encoding]::UTF8.GetBytes($scriptContent))
            $result = ssh -o StrictHostKeyChecking=no `
                          -o BatchMode=no `
                          "${TowerUser}@${TowerHost}" `
                          "echo '$encodedScript' | base64 -d > /tmp/Build-HashCache.sh && chmod +x /tmp/Build-HashCache.sh && echo OK" 2>&1
            if ($result -match 'OK') {
                Write-Log "Build-HashCache.sh uploaded to Tower" 'INFO'
            } else {
                Write-Log "WARN: Could not upload script, assuming /tmp/Build-HashCache.sh exists on Tower" 'WARN'
            }
        }
    } catch {
        Write-Log "WARN: Script upload failed: $_. Assuming /tmp/Build-HashCache.sh exists on Tower." 'WARN'
    }

    # Build the UNC prefix - use the TowerLocalPath basename to preserve case
    # e.g. /mnt/user/media/comics/Comix -> \\tower\media\comics\Comix
    $uncPrefix = $Primary.TrimEnd('\')

    # ---- Launch Tower hash job (SSH background) ----
    Write-Log "Launching primary hash on Tower (background SSH)..." 'INFO'
    $pythonScript = "$TowerSharePath/build_hash_cache.py".Replace('\', '/')
    $towerCmd = "python3 -u $pythonScript '$TowerLocalPath' '$uncPrefix' '$TowerSharePath/PrimaryCache.jsonl' $TowerHashThreads"

    # Launch via SSH, detached so it survives connection drop
    $towerSshArgs = @(
        '-o', 'StrictHostKeyChecking=no',
        '-i', "$env:USERPROFILE\.ssh\tower_key",
        "${TowerUser}@${TowerHost}",
        "nohup $towerCmd > /tmp/orchestrator_primary.log 2>&1 & echo `$!"
    )
    $towerPid = & ssh @towerSshArgs 2>&1
    Write-Log "Tower hash job launched (PID: $($towerPid -join ''))" 'INFO'

    # ---- Launch local secondary hash job ----
    Write-Log "Launching secondary hash locally..." 'INFO'
    $localHashJob = Start-Job -ScriptBlock {
        param($script, $path, $output, $throttle, $logFile)
        & pwsh -File $script `
               -Path $path `
               -OutputFile $output `
               -HashThrottle $throttle `
               -LogFile $logFile
    } -ArgumentList $BuildHashCachePs, $Secondary, $SecondaryCacheFile,
                    $HashThrottle, (Join-Path $WorkDir "SecondaryHash.log")

    Write-Log "Both hash jobs running. Monitoring progress..." 'INFO'
    Write-Log "  Primary log  : $TowerSharePath\PrimaryCache.jsonl (building)" 'INFO'
    Write-Log "  Secondary log: $(Join-Path $WorkDir 'SecondaryHash.log')" 'INFO'
    Write-Log "----------------------------------------" 'INFO'

    # ---- Monitor both jobs ----
    $secondaryLog = Join-Path $WorkDir "SecondaryHash.log"
    $pollInterval = 15  # seconds

    while ($true) {
        Start-Sleep -Seconds $pollInterval

        # Check Tower status
        $towerRunning = Test-TowerHashRunning
        $towerLines   = Get-TowerLog
        $towerProgress = ($towerLines | Select-String '\[PROGRESS\]' | Select-Object -Last 1)?.Line
        if (-not $towerProgress) {
            $towerProgress = ($towerLines | Select-Object -Last 1)
        }

        # Check local job status
        $localRunning = ($localHashJob.State -eq 'Running')
        $localProgress = $null
        if (Test-Path $secondaryLog) {
            $localProgress = (Get-Content $secondaryLog -Tail 1)
        }

        # Log combined status
        $towerStatus = if ($towerRunning) { "RUNNING" } else { "DONE" }
        $localStatus = if ($localRunning) { "RUNNING" } else { "DONE ($($localHashJob.State))" }

        Write-Log "PRIMARY  [$towerStatus] : $towerProgress" 'PROGRESS'
        Write-Log "SECONDARY[$localStatus] : $localProgress" 'PROGRESS'
        Write-Log "----------------------------------------" 'INFO'

        # Exit when both done
        if (-not $towerRunning -and -not $localRunning) {
            Write-Log "Both hash jobs completed." 'INFO'
            break
        }

        # If only local is done, keep waiting for Tower
        if (-not $localRunning -and $towerRunning) {
            Write-Log "Secondary done, waiting for primary (Tower)..." 'INFO'
        }

        # If only Tower is done, keep waiting for local
        if ($towerRunning -and -not $localRunning) {
            Write-Log "Primary (Tower) done, waiting for secondary..." 'INFO'
        }
    }

    # Cleanup local job
    $localHashJob | Remove-Job -Force

    Write-Log "STAGE 1 complete." 'ACTION'

} else {
    Write-Log "STAGE 1: Skipped (--SkipHashing specified)" 'WARN'
}

# ===========================================================================
# STAGE 2 -- PULL PRIMARY CACHE FROM TOWER VIA SMB
# ===========================================================================
Write-Log "STAGE 2: Pulling primary cache from Tower via SMB..." 'INFO'

if (-not (Test-Path $PrimaryCacheShare)) {
    throw "Primary cache not found at $PrimaryCacheShare -- did the Tower hash job complete?"
}

Copy-Item -LiteralPath $PrimaryCacheShare -Destination $PrimaryCacheLocal -Force
$primaryCount = (Get-Content $PrimaryCacheLocal | Measure-Object -Line).Lines
Write-Log "Primary cache pulled: $primaryCount entries -> $PrimaryCacheLocal" 'ACTION'

# ===========================================================================
# STAGE 3 -- MERGE CACHES
# ===========================================================================
Write-Log "STAGE 3: Merging primary and secondary caches..." 'INFO'

$mergedCache = [System.Collections.Generic.Dictionary[string,string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase)

$loadCount = 0
foreach ($cacheFile in @($PrimaryCacheLocal, $SecondaryCacheFile)) {
    if (-not (Test-Path $cacheFile)) {
        Write-Log "WARN: Cache file not found, skipping: $cacheFile" 'WARN'
        continue
    }
    foreach ($line in [System.IO.File]::ReadLines($cacheFile)) {
        $line = $line.Trim()
        if (-not $line) { continue }
        try {
            $e = $line | ConvertFrom-Json
            $mergedCache[$e.K] = $line
            $loadCount++
        } catch {}
    }
}

$mergedLines = [System.Collections.Generic.List[string]]::new()
foreach ($line in $mergedCache.Values) { $mergedLines.Add($line) }
[System.IO.File]::WriteAllLines($MergedCacheFile, $mergedLines)

Write-Log "Merged cache: $($mergedLines.Count) unique entries -> $MergedCacheFile" 'ACTION'

# ===========================================================================
# STAGE 4 -- SYNC
# ===========================================================================
Write-Log "STAGE 4: Running directory mirror sync..." 'INFO'
Write-Log "  Script    : $SyncScript" 'INFO'
Write-Log "  Primary   : $Primary" 'INFO'
Write-Log "  Secondary : $Secondary" 'INFO'
Write-Log "  Cache     : $MergedCacheFile" 'INFO'
Write-Log "  SyncLog   : $SyncLogFile" 'INFO'

$syncArgs = @(
    '-File', $SyncScript,
    '-Primary', $Primary,
    '-Secondary', $Secondary,
    '-UseHash',
    '-CacheFile', $MergedCacheFile,
    '-PrimaryCache', $PrimaryCacheLocal,
    '-SecondaryCache', $SecondaryCacheFile,
    '-LogFile', $SyncLogFile,
    '-HashThrottle', $HashThrottle,
    '-CopyThrottle', $CopyThrottle
)
if ($WhatIf) { $syncArgs += '-WhatIf' }

& pwsh @syncArgs

# ===========================================================================
# SUMMARY
# ===========================================================================
$totalElapsed = [int]([datetime]::UtcNow - $orchestratorStart).TotalSeconds
$hh = [int]($totalElapsed / 3600)
$mm = [int](($totalElapsed % 3600) / 60)
$ss = $totalElapsed % 60

Write-Log "========================================" 'INFO'
Write-Log "ORCHESTRATOR COMPLETE" 'INFO'
Write-Log ("  Total elapsed : {0:D2}h {1:D2}m {2:D2}s" -f $hh, $mm, $ss) 'INFO'
Write-Log "  Work dir      : $WorkDir" 'INFO'
Write-Log "  Sync log      : $SyncLogFile" 'INFO'
Write-Log "  Merged cache  : $MergedCacheFile ($($mergedLines.Count) entries)" 'INFO'
Write-Log "========================================" 'INFO'

# SIG # Begin signature block
# MIIFngYJKoZIhvcNAQcCoIIFjzCCBYsCAQExDzANBglghkgBZQMEAgEFADB5Bgor
# BgEEAYI3AgEEoGswaTA0BgorBgEEAYI3AgEeMCYCAwEAAAQQH8w7YFlLCE63JNLG
# KX7zUQIBAAIBAAIBAAIBAAIBADAxMA0GCWCGSAFlAwQCAQUABCCqJFb3Z/QjvEzP
# zA8UPRzmjsXbqDMN0DahkLDnN0iZaqCCAxAwggMMMIIB9KADAgECAhAhiJkQjcYV
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
# MAwGCisGAQQBgjcCARUwLwYJKoZIhvcNAQkEMSIEIL90m7Hc8SxQkP6m//eOhf1W
# LAjWp6M59r9njUAWx8+5MA0GCSqGSIb3DQEBAQUABIIBAH2WgL0rwHMoxtJBJRZo
# RIL0gb4fGPh3XWt/BOQLjDoYdn+iRB3TeMPdmN4YTC9TdhJ1Y51BS8hysGPIeR8M
# RmpLRHsjmOvhHm4CaopdYSB1zdQP8pnCIB0nlUbZu3vAvLzqqhKcl7xJnkffWe6o
# b1YuxBZAH/bwqDeMn93ip7T1D7MkI3wEjyoK++TtMuDikzAJz+NrT5aN3IHzGhlw
# 2yTvtW+U4iZ+xwZciCT3hjZllskBNYFfwCW0et3EolsX5n2kdu1DW6c3B2VTfBbX
# MSD7gkIvFYxXZvK5/TUN3a7RM4MrAKwmLFIwmdr8GlHIAIr1J3eBVmNqmdWglSZN
# /0U=
# SIG # End signature block
