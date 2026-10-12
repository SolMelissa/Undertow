#requires -Version 7.0

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Root,

    [switch]$DeleteXmp
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = [IO.Path]::GetFullPath($Root)

if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
    throw "Image folder does not exist: $Root"
}

# Load settings from the root directory
$SettingsPath = Join-Path $PSScriptRoot "settings.json"

if (-not (Test-Path $SettingsPath)) {
    Write-Error "Configuration file not found at $SettingsPath. Please ensure settings.json exists."
    exit 1
}

$Settings = Get-Content -Raw -Path $SettingsPath | ConvertFrom-Json

$ImageExtensions = @(
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
    ".heic",
    ".heif"
)

function Find-ImageForXmp {
    param(
        [Parameter(Mandatory)]
        [System.IO.FileInfo]$XmpFile
    )

    # STAG+ normally creates:
    #
    # image.jpg.xmp
    # image.png.xmp
    #
    # Removing only the final .xmp gives the original image filename.
    $candidate = Join-Path `
        -Path $XmpFile.DirectoryName `
        -ChildPath $XmpFile.BaseName

    if (
        (Test-Path -LiteralPath $candidate -PathType Leaf) -and
        ([IO.Path]::GetExtension($candidate).ToLowerInvariant() -in $ImageExtensions)
    ) {
        return $candidate
    }

    # Compatibility fallback for image.xmp.
    $stem = [IO.Path]::GetFileNameWithoutExtension($XmpFile.Name)

    foreach ($extension in $ImageExtensions) {
        $fallback = Join-Path `
            -Path $XmpFile.DirectoryName `
            -ChildPath ($stem + $extension)

        if (Test-Path -LiteralPath $fallback -PathType Leaf) {
            return $fallback
        }
    }

    return $null
}

function Convert-StagTag {
    <#
    .SYNOPSIS
        Converts raw AI tags into Hydrus-compatible namespace:tag format using 
        a dynamic categorizer mapping.
    
    .PARAMETER Value
        The raw tag string extracted from the XMP file.
    
    .PARAMETER Settings
        The configuration object loaded from settings.json.
    #>
    param(
        [Parameter(Mandatory)]
        [string]$Value,

        [Parameter(Mandatory)]
        [object]$Settings
    )

    $Value = $Value.Trim()

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }

    # --- STEP 1: CORE TAG EXTRACTION ---
    # We remove technical prefixes to get the "Base Tag" for the categorizer.
    $BaseTag = $null

    # Handle content tags (ai|dog, st|dog)
    if ($Value -match "^(ai|st)\|(.+)$") {
        $BaseTag = $Matches[2]
    }
    # Handle metadata tags (iq|high, aes|high)
    elseif ($Value -match "^(iq|aes)\|(.+)$") {
        $NamespacePrefix = $Matches[1] # iq or aes
        $TagValue = $Matches[2]
        $BaseTag = "$($NamespacePrefix)_$TagValue"
    }
    # Handle unprefixed values
    else {
        $BaseTag = $Value
    }

    # Sanitize the BaseTag by replacing remaining pipes with underscores
    $BaseTag = $BaseTag -replace "\|", "_"

    if ([string]::IsNullOrWhiteSpace($BaseTag)) {
        return $null
    }

    # --- STEP 2: CATEGORIZATION LOGIC ---
    $CatSettings = $Settings.Categorization
    $AssignedNamespace = $CatSettings.DefaultNamespace
    $Mapping = $CatSettings.NamespaceMapping

    # Iterate through the mapping dictionary to find a matching category
    # We use .PSObject.Properties to iterate over the keys of the JSON object
    foreach ($Property in $Mapping.PSObject.Properties) {
        $Namespace = $Property.Name
        $TriggerTags = $Property.Value

        # Check if the BaseTag exists within the current category list
        # This performs a case-insensitive comparison
        if ($TriggerTags -contains $BaseTag) {
            $AssignedNamespace = $Namespace
            break # Stop searching once the first match is found
        }
    }

    # --- STEP 3: FINAL FORMATTING ---
    # Construct the Hydrus-style string: "Namespace:Tag"
    $FinalTag = $BaseTag

    if (-not [string]::IsNullOrEmpty($AssignedNamespace)) {
        $Separator = $CatSettings.NamespaceSeparator
        $FinalTag = "$AssignedNamespace$Separator$BaseTag"
    }

    # Apply Case Normalization based on settings
    switch ($CatSettings.CaseNormalization) {
        "lowercase" { $FinalTag = $FinalTag.ToLower() }
        "titlecase" { 
            $TextInfo = (Get-Culture).TextInfo
            $FinalTag = $TextInfo.ToTitleCase($FinalTag.ToLower()) 
        }
        default { # "original" - do nothing
        }
    }

    return $FinalTag
}


$count = 0
$converted = 0
$skipped = 0
$errors = 0

$xmpFiles = Get-ChildItem `
    -LiteralPath $Root `
    -File `
    -Recurse `
    -Filter "*.xmp" `
    -ErrorAction SilentlyContinue

foreach ($xmp in $xmpFiles) {
    $count++

    try {
        $raw = Get-Content `
            -LiteralPath $xmp.FullName `
            -Raw `
            -Encoding utf8

        # Ignore unrelated XMP files.
        if (
            $raw -notmatch "hierarchicalSubject" -and
            $raw -notmatch "subject"
        ) {
            $skipped++
            continue
        }

        $imagePath = Find-ImageForXmp -XmpFile $xmp

        if (-not $imagePath) {
            Write-Warning "No matching image found for: $($xmp.FullName)"
            $skipped++
            continue
        }

        [xml]$xml = $raw

        $nodes = $xml.SelectNodes(
            "//*[local-name()='hierarchicalSubject']//*[local-name()='li']"
        )

        if (-not $nodes -or $nodes.Count -eq 0) {
            $nodes = $xml.SelectNodes(
                "//*[local-name()='subject']//*[local-name()='li']"
            )
        }

        $tags = [System.Collections.Generic.HashSet[string]]::new(
            [StringComparer]::OrdinalIgnoreCase
        )

        foreach ($node in $nodes) {
            $tag = Convert-StagTag -Value $node.InnerText

            if ($tag) {
                [void]$tags.Add($tag)
            }
        }

        if ($tags.Count -eq 0) {
            $skipped++
            continue
        }

        # Important:
        #
        # image.jpg -> image.jpg.txt
        #
        # This is a plain text file, not an XML, JSON, or CSV file.
        $sidecarPath = "$imagePath.txt"
        $temporaryPath = "$sidecarPath.tmp"

        # One Hydrus tag per line.
        $content = (
            $tags |
                Sort-Object |
                ForEach-Object { $_ }
        ) -join "`r`n"

        $content += "`r`n"

        # UTF-8 without a byte-order mark.
        $utf8NoBom = [Text.UTF8Encoding]::new($false)

        [IO.File]::WriteAllText(
            $temporaryPath,
            $content,
            $utf8NoBom
        )

        # Replace the final sidecar atomically.
        Move-Item `
            -LiteralPath $temporaryPath `
            -Destination $sidecarPath `
            -Force

        if ($DeleteXmp) {
            Remove-Item `
                -LiteralPath $xmp.FullName `
                -Force
        }

        $converted++

        if (($converted % 500) -eq 0) {
            Write-Host "Created $converted plaintext sidecars..."
        }
    }
    catch {
        $errors++

        Write-Warning "Failed: $($xmp.FullName)"
        Write-Warning $_.Exception.Message
    }
}

Write-Host ""
Write-Host "XMP files examined: $count"
Write-Host "Plaintext .txt sidecars written: $converted"
Write-Host "Skipped: $skipped"
Write-Host "Errors: $errors"
