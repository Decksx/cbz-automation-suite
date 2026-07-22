#Requires -Version 5.1
<#
.SYNOPSIS
    Organize-PlexMedia.ps1 — Sanitize and organize media files into Plex-ready folder structures.

.DESCRIPTION
    Processes Movies, TV Shows, Anime, Kids Shows, Stand-Up Comedy, and custom libraries.
    Cleans filenames (strips tech tags, release groups, CRC hashes, etc.), infers episode
    numbers, handles Extras/Specials, and moves files to Plex-standard destinations.

.PARAMETER WhatIf
    Dry-run mode. Shows what would happen without moving or renaming anything.

.PARAMETER LogPath
    Path to write the operation log. Defaults to Z:\Plex\Logs\organize_<timestamp>.log

.PARAMETER SkipLibrary
    One or more library names to skip. Valid values:
    Anime, Movies, DavidMovies, TVShows, DavidShows, KidsShows, StandUp, Magique

.EXAMPLE
    .\Organize-PlexMedia.ps1 -WhatIf
    .\Organize-PlexMedia.ps1
    .\Organize-PlexMedia.ps1 -SkipLibrary Movies, Anime
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$LogPath,
    [ValidateSet('Anime','Movies','DavidMovies','TVShows','DavidShows','KidsShows','StandUp','Magique')]
    [string[]]$SkipLibrary = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

# Resolve dry-run mode from CmdletBinding's built-in -WhatIf flag
$IsDryRun = ($WhatIfPreference -ne [System.Management.Automation.ActionPreference]::SilentlyContinue)

# ─────────────────────────────────────────────────────────────────────────────
# LIBRARY DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────
$Libraries = [ordered]@{
    Anime = @{
        Sources     = @('Z:\David Anime')
        Destination = 'Z:\David Anime'
        Type        = 'Anime'
    }
    Movies = @{
        Sources     = @('Z:\Movies')
        Destination = 'Z:\Movies'
        Type        = 'Movie'
    }
    DavidMovies = @{
        Sources     = @('Z:\david movies')
        Destination = 'Z:\David Movies'
        Type        = 'Movie'
    }
    TVShows = @{
        Sources     = @('Z:\TV Shows')
        Destination = 'Z:\TV Shows'
        Type        = 'TVShow'
    }
    DavidShows = @{
        # Kids/StandUp/Magique sub-libraries are carved out automatically below
        Sources     = @('Z:\Davids Shows')
        Destination = 'Z:\David Shows'
        Type        = 'TVShow'
        ExcludeSubfolders = @('Kids Shows','Stand Up','more stand up','Magique')
    }
    KidsShows = @{
        Sources     = @('Z:\Kids Shows')
        Destination = 'Z:\Kids Shows'
        Type        = 'TVShow'
    }
    StandUp = @{
        Sources     = @('Z:\Stand Up','Z:\more stand up')
        Destination = 'Z:\Stand Up'
        Type        = 'Movie'   # Plex treats stand-up specials as movies
    }
    Magique = @{
        Sources     = @('Z:\Magique')
        Destination = 'Z:\Plex\Magique'
        Type        = 'TVShow'
    }
}
# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
if (-not $LogPath) {
    $timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $LogPath   = "Z:\Plex\Logs\organize_$timestamp.log"
}

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    Write-Host $line -ForegroundColor $(switch ($Level) {
        'WARN'  { 'Yellow' }
        'ERROR' { 'Red'    }
        'SKIP'  { 'DarkGray' }
        'DRY'   { 'Cyan'   }
        default { 'White'  }
    })
    if (-not $IsDryRun) {
        $logDir = Split-Path $LogPath
        if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
        Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# SANITIZATION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

# Tech tag patterns to strip from names
$TechTagPattern = [regex]::new(
    '[\(\[]?' +
    '(' +
        # Resolution
        '\b(2160p?|1080p?|720p?|480p?|4K|UHD|HD|SD)\b|' +
        # Source
        '\b(BluRay|Blu-Ray|BDRip|BDRemux|BD|WEB-?DL|WEBRip|WEB|HMAX|DSNP|AMZN|HULU|NF|CR|HDTV|DVDRip|DVD|HDCAM|CAM|REMUX)\b|' +
        # Codec video
        '\b(x264|x265|H\.?264|H\.?265|HEVC|AVC|AV1|XviD|MPEG2|VP9|VC-1)\b|' +
        # Codec audio
        '\b(AAC|AAC2\.0|AAC5\.1|AC3|DDP|DDP2\.0|DDP5\.1|DTS|DTS-HD|DTS-MA|FLAC|FLAC2\.0|MA5\.1|Opus|TrueHD|Atmos|EAC3)\b|' +
        # Bit depth / HDR
        '\b(10[- ]?[Bb]it|10bit|8bit|12bit|HDR|HDR10|HLG|DV|Dolby[- ]?Vision)\b|' +
        # Channels
        '\b(Dual[- ]?Audio|Multi[- ]?Audio|Dual|5\.1|2\.0|7\.1)\b|' +
        # Subtitles / language
        '\b(Eng[lish]*[- ]?Sub[s]?|Multi[- ]?Sub[s]?|Sub[s]?|Dubbed|Dub|English[- ]?Dub|Hindi|Esub)\b|' +
        # Release flags
        '\b(REPACK|PROPER|REAL|EXTENDED|THEATRICAL|DC|UNRATED|UNCENSORED|COMPLETE|FULL|RETAIL|LIMITED|INTERNAL|iNTERNAL)\b|' +
        # Streaming platform abbrevs already handled above; extras
        '\b(HDR10Plus|IMAX|HFR|60FPS|24FPS)\b|' +
        # Release group after hyphen at end of string (e.g. -NTb, -FLUX, -YumYum)
        '(?<=-)[A-Za-z0-9]{2,20}$' +
    ')' +
    '[\)\]]?',
    [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
)

# Bracket-wrapped release group at START of name: [MiniDEX], [Cleo], etc.
$LeadingGroupPattern  = [regex]'^\s*\[[^\]]{1,30}\]\s*'

# CRC hash in brackets: [2D0F2E06], [ABC123], [6240D534] — 5-8 hex chars
$CrcPattern           = [regex]'\[[0-9A-Fa-f]{5,8}\]'

# Trailing noise after a hyphen followed only by tags: " - (Dual Audio_10bit_BD1080p_x265)"
$TrailingParenPattern = [regex]'\s*[\(\[][^\)\]]*[\)\]]\s*$'

# Episode number patterns
$EpPatternSxxExx      = [regex]'(?i)(S\d{1,2})(E\d{2}(?:-?E\d{2})*)'  # S01E01, S01E01-E03
$EpPatternBareNum     = [regex]'(?<![0-9])(\d{2,3})(?![0-9p])'          # bare 01..999, not resolution

$ExtrasKeywords       = @('extras','special','specials','ova','ovas','bonus','trailer','featurette','behind.the.scenes','deleted.scene','interview','short','scene')
$VideoExtensions      = @('.mkv','.mp4','.avi','.m4v','.mov','.wmv','.ts','.m2ts','.iso','.flv')

function Get-CleanName {
    <#
    .SYNOPSIS Sanitize a folder or file base-name, preserving year if present.#>
    param([string]$Raw)

    $name = $Raw

    # 1. Strip leading release group [Tag]
    $name = $LeadingGroupPattern.Replace($name, '')

    # 2. Strip CRC hashes
    $name = $CrcPattern.Replace($name, '')

    # 3. Replace dots and underscores with spaces (common in release names)
    #    BUT protect year patterns like "2021" and "S01E01" from being mangled first
    $name = $name -replace '[_]', ' '
    # Replace dots only when surrounded by word chars (not in extensions)
    $name = [regex]::Replace($name, '(?<=[A-Za-z0-9])\.(?=[A-Za-z0-9])', ' ')

    # 4. Strip tech tags
    $name = $TechTagPattern.Replace($name, ' ')

    # 5. Strip stray trailing/leading hyphens and punctuation left over
    $name = $name -replace '\s*-\s*$', ''
    $name = $name -replace '^\s*-\s*', ''

    # 6. Collapse multiple spaces
    $name = [regex]::Replace($name, ' {2,}', ' ').Trim()

    # 7. Remove any remaining empty bracket pairs
    $name = [regex]::Replace($name, '[\(\[\{]\s*[\)\]\}]', '').Trim()

    # 8. Final trim of trailing punctuation
    $name = $name.TrimEnd('.-_ ')

    return $name
}

function Get-Year {
    param([string]$Raw)
    # Match a 4-digit year in parens or standalone: (2021) or .2021. or _2021_
    $m = [regex]::Match($Raw, '(?<![0-9])((19|20)\d{2})(?![0-9])')
    if ($m.Success) { return $m.Groups[1].Value }
    return $null
}

function Test-IsExtra {
    param([string]$Name)
    $lower = $Name.ToLower()
    foreach ($kw in $ExtrasKeywords) {
        if ($lower -match "\b$([regex]::Escape($kw))\b") { return $true }
    }
    return $false
}

function Get-EpisodeInfo {
    <#
    Returns hashtable: @{ Season='S01'; Episodes='E01'; IsMulti=$false; Clean=<name without ep tag> }
    #>
    param([string]$BaseName)

    # Try SxxExx first
    $m = $EpPatternSxxExx.Match($BaseName)
    if ($m.Success) {
        $season = $m.Groups[1].Value.ToUpper()
        $eps    = $m.Groups[2].Value.ToUpper()
        $clean  = ($BaseName.Substring(0, $m.Index) + $BaseName.Substring($m.Index + $m.Length)).Trim(' -')
        return @{ Season=$season; Episodes=$eps; IsMulti=($eps -match 'E\d+-E\d+'); Clean=$clean }
    }

    # Try bare episode number (anime style): " - 01" or "_01_" or just "01"
    # Look for isolated 2-3 digit number not adjacent to resolution digits
    $m2 = [regex]::Match($BaseName, '(?:[\s_\-]+)(\d{2,3})(?=[\s_\-\.\[]|$)')
    if ($m2.Success) {
        $epNum = $m2.Groups[1].Value.PadLeft(2,'0')
        $clean = ($BaseName.Substring(0, $m2.Index) + $BaseName.Substring($m2.Index + $m2.Length)).Trim(' -')
        return @{ Season='S01'; Episodes="E$epNum"; IsMulti=$false; Clean=$clean }
    }

    return $null
}

function Get-UniqueDestPath {
    <#
    If $Path already exists, append _2, _3, ... to base name until unique.
    Works for both files and directories.
    #>
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) { return $Path }

    $dir  = Split-Path $Path -Parent
    $leaf = Split-Path $Path -Leaf
    $ext  = [System.IO.Path]::GetExtension($leaf)
    $base = [System.IO.Path]::GetFileNameWithoutExtension($leaf)

    $i = 2
    do {
        $candidate = Join-Path $dir ($base + "_$i" + $ext)
        $i++
    } while (Test-Path -LiteralPath $candidate)

    return $candidate
}

# ─────────────────────────────────────────────────────────────────────────────
# MOVE HELPER
# ─────────────────────────────────────────────────────────────────────────────
function Move-MediaFile {
    param(
        [string]$Source,
        [string]$Destination
    )

    $dest = Get-UniqueDestPath -Path $Destination

    if ($IsDryRun) {
        Write-Log "DRY-RUN  MOVE: `"$Source`"  →  `"$dest`"" 'DRY'
        return
    }

    $destDir = Split-Path $dest -Parent
    if (-not (Test-Path -LiteralPath $destDir)) {
        New-Item -ItemType Directory -Path $destDir -Force | Out-Null
    }

    try {
        # Use long-path-safe prefix
        $srcLong  = if ($Source.StartsWith('\\?\'))      { $Source }      else { "\\?\$Source" }
        $destLong = if ($dest.StartsWith('\\?\'))        { $dest }        else { "\\?\$dest"   }
        Move-Item -LiteralPath $srcLong -Destination $destLong -Force
        Write-Log "MOVED: `"$Source`"  →  `"$dest`""
    }
    catch {
        Write-Log "ERROR moving `"$Source`": $_" 'ERROR'
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# PROCESSORS
# ─────────────────────────────────────────────────────────────────────────────

function Process-Movie {
    param(
        [System.IO.FileInfo]$File,
        [string]$Destination
    )

    $rawBase  = $File.BaseName
    $ext      = $File.Extension

    # Clean the name
    $clean    = Get-CleanName -Raw $rawBase
    $year     = Get-Year -Raw $rawBase

    # Build show folder name
    $folderName = if ($year -and ($clean -notmatch [regex]::Escape($year))) {
        "$clean ($year)"
    } elseif ($year) {
        $clean   # year already in clean name
    } else {
        $clean
    }

    # Plex movie structure: Movies\Movie Title (Year)\Movie Title (Year).mkv
    $movieFolder = Join-Path $Destination $folderName
    $destFile    = Join-Path $movieFolder ($folderName + $ext)

    Move-MediaFile -Source $File.FullName -Destination $destFile
}

function Process-TVEpisode {
    param(
        [System.IO.FileInfo]$File,
        [string]$ShowFolderName,
        [string]$Destination
    )

    $rawBase = $File.BaseName
    $ext     = $File.Extension

    # Detect extras first
    if (Test-IsExtra -Name $rawBase) {
        $extraDest = Join-Path $Destination "$ShowFolderName\Specials (Season 00)\$($File.Name)"
        Move-MediaFile -Source $File.FullName -Destination $extraDest
        return
    }

    $epInfo = Get-EpisodeInfo -BaseName $rawBase

    if ($epInfo) {
        $season    = $epInfo.Season   # e.g. S01
        $episodes  = $epInfo.Episodes # e.g. E01 or E01-E03
        $epClean   = Get-CleanName -Raw $epInfo.Clean
        $seasonNum = [int]($season -replace 'S','')
        $seasonFolder = "Season {0:D2}" -f $seasonNum

        # Plex episode filename: Show Name - S01E01 - Episode Title.mkv
        $epTitle = $epClean.Trim(' -')
        if ($epTitle) {
            $fileName = "$ShowFolderName - $season$episodes - $epTitle$ext"
        } else {
            $fileName = "$ShowFolderName - $season$episodes$ext"
        }

        $destFile = Join-Path $Destination "$ShowFolderName\$seasonFolder\$fileName"
    }
    else {
        # No episode info found — put in a review folder
        Write-Log "WARN: Could not detect episode number for `"$($File.FullName)`" — placing in _NeedsReview" 'WARN'
        $destFile = Join-Path $Destination "$ShowFolderName\_NeedsReview\$($File.Name)"
    }

    Move-MediaFile -Source $File.FullName -Destination $destFile
}

function Process-ShowFolder {
    <#
    Process a single show's source folder for TV or Anime.
    $SourceFolder = the raw show folder (e.g. "Trinity Blood [1080]")
    #>
    param(
        [string]$SourceFolder,
        [string]$Destination
    )

    $rawFolderName = Split-Path $SourceFolder -Leaf

    # Clean the show folder name, preserve year
    $cleanShow = Get-CleanName -Raw $rawFolderName
    $year      = Get-Year -Raw $rawFolderName
    $showName  = if ($year -and ($cleanShow -notmatch [regex]::Escape($year))) {
        "$cleanShow ($year)"
    } else {
        $cleanShow
    }

    Write-Log "Processing show: `"$rawFolderName`"  →  `"$showName`""

    # Get all video files recursively inside this show folder
    $files = Get-ChildItem -LiteralPath $SourceFolder -Recurse -File |
             Where-Object { $VideoExtensions -contains $_.Extension.ToLower() }

    foreach ($file in $files) {
        Process-TVEpisode -File $file -ShowFolderName $showName -Destination $Destination
    }
}

function Process-Library {
    param(
        [string]$LibraryName,
        [hashtable]$LibConfig
    )

    if ($SkipLibrary -contains $LibraryName) {
        Write-Log "Skipping library: $LibraryName" 'SKIP'
        return
    }

    Write-Log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    Write-Log "Library: $LibraryName  (Type: $($LibConfig.Type))"
    Write-Log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    $dest = $LibConfig.Destination
    $excludeSubs = if ($LibConfig.ContainsKey('ExcludeSubfolders')) { $LibConfig.ExcludeSubfolders } else { @() }

    foreach ($srcRoot in $LibConfig.Sources) {

        if (-not (Test-Path -LiteralPath $srcRoot)) {
            Write-Log "Source not found, skipping: `"$srcRoot`"" 'WARN'
            continue
        }

        switch ($LibConfig.Type) {

            'Movie' {
                # Each subfolder = one movie, OR loose video files at root
                $subfolders = Get-ChildItem -LiteralPath $srcRoot -Directory
                foreach ($sf in $subfolders) {
                    $videos = Get-ChildItem -LiteralPath $sf.FullName -Recurse -File |
                              Where-Object { $VideoExtensions -contains $_.Extension.ToLower() }
                    foreach ($v in $videos) {
                        Process-Movie -File $v -Destination $dest
                    }
                }
                # Also handle loose files at root level
                $looseFiles = Get-ChildItem -LiteralPath $srcRoot -File |
                              Where-Object { $VideoExtensions -contains $_.Extension.ToLower() }
                foreach ($lf in $looseFiles) {
                    Process-Movie -File $lf -Destination $dest
                }
            }

            { $_ -in 'TVShow','Anime' } {
                # Each subfolder = one show
                $showFolders = Get-ChildItem -LiteralPath $srcRoot -Directory |
                               Where-Object { $excludeSubs -notcontains $_.Name }

                foreach ($sf in $showFolders) {
                    Process-ShowFolder -SourceFolder $sf.FullName -Destination $dest
                }
            }
        }
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

if ($IsDryRun) {
    Write-Log "════════════════════════════════════════════════════"
    Write-Log "DRY-RUN MODE — No files will be moved or renamed."
    Write-Log "════════════════════════════════════════════════════"
} else {
    Write-Log "════════════════════════════════════════════════════"
    Write-Log "Organize-PlexMedia — Starting run"
    Write-Log "Log: $LogPath"
    Write-Log "════════════════════════════════════════════════════"
}

foreach ($libName in $Libraries.Keys) {
    Process-Library -LibraryName $libName -LibConfig $Libraries[$libName]
}

Write-Log "════════════════════════════════════════════════════"
Write-Log "Done."
if (-not $IsDryRun) { Write-Log "Full log saved to: $LogPath" }