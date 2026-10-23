param(
    [Parameter(Mandatory)][string]$Path,
    [switch]$Recurse,
    [switch]$WhatIf
)

$files = if ($Recurse) {
    Get-ChildItem -Path $Path -File -Recurse
} else {
    Get-ChildItem -Path $Path -File
}

foreach ($file in $files) {
    $newName = $file.BaseName -replace '\s*\[[0-9A-Fa-f]{6,8}\](\s*)$', '$1'
    
    if ($newName -ne $file.BaseName) {
        $newName = $newName.TrimEnd() + $file.Extension
        if ($WhatIf) {
            Write-Host "WhatIf: '$($file.Name)' -> '$newName'"
        } else {
            Rename-Item -LiteralPath $file.FullName -NewName $newName
            Write-Host "Renamed: '$($file.Name)' -> '$newName'"
        }
    }
}

function Get-FileTypeCounts {
    param(
        [Parameter(Mandatory)][string]$Path,
        [switch]$Recurse
    )

    $files = if ($Recurse) {
        Get-ChildItem -Path $Path -File -Recurse
    } else {
        Get-ChildItem -Path $Path -File
    }

    $files |
        Group-Object Extension |
        Sort-Object Count -Descending |
        Select-Object @{N='Extension'; E={ if ($_.Name) { $_.Name } else { '(no extension)' } }},
                      Count |
        Format-Table -AutoSize
}


Get-FileTypeCounts -Path "Z:\David Anime" -Recurse