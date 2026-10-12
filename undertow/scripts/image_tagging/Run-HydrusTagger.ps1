#requires -Version 7.0

[CmdletBinding()]
param(
    [string]$ImageRoot = "",
    [switch]$UseSavedFolder,
    [switch]$Test,
    [switch]$Force,
    [switch]$ForceCpu,
    [switch]$All,
    [switch]$QualityOnly,
    [switch]$AestheticOnly,
    [switch]$KeepXmp
)

$SettingsPath = Join-Path $PSScriptRoot "settings.json"
$Config = @{}

if (Test-Path $SettingsPath) {
    try {
        $Config = Get-Content -Raw -Path $SettingsPath | ConvertFrom-Json
        Write-Host "Configuration loaded from settings.json" -ForegroundColor Gray
    } catch {
        Write-Error "Failed to parse settings.json. Ensure the file is valid JSON."
        exit 1
    }
} else {
    Write-Warning "settings.json not found. Proceeding with manual arguments and defaults."
}

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# STAG+ is a general tagger and lives in <image_and_video_management>\image_tagging\local\stag; this
# wrapper (Hydrus-specific: namespace mapping, .txt sidecars) lives in the Undertow repo. Override the
# image_tagging root with env IMAGE_TAGGING_DIR; otherwise walk up looking for a sibling image_tagging\.
function Find-ImageTaggingDir {
    if ($env:IMAGE_TAGGING_DIR -and (Test-Path -LiteralPath $env:IMAGE_TAGGING_DIR)) { return $env:IMAGE_TAGGING_DIR }
    $dir = $PSScriptRoot
    while ($dir) {
        $candidate = Join-Path $dir "image_tagging\local\stag"
        if (Test-Path -LiteralPath $candidate) { return (Join-Path $dir "image_tagging") }
        $dir = Split-Path -Parent $dir
    }
    throw "image_tagging folder not found. Set IMAGE_TAGGING_DIR."
}
$TaggingDir = Find-ImageTaggingDir
$InstallDir = Join-Path $TaggingDir "local\stag"
$RepoDir = Join-Path $InstallDir "stag-plus"
$VenvDir = Join-Path $InstallDir ".venv"
$Python = Join-Path $VenvDir "Scripts\python.exe"
$Converter = Join-Path $PSScriptRoot "Convert-StagXmpToHydrus.ps1"
$LogDir = Join-Path $TaggingDir "_logs\stag"
$ConfigDir = Join-Path $InstallDir "config"
$SavedFolderPath = Join-Path $ConfigDir "last-image-folder.txt"

function Select-ImageFolder {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = [System.Windows.Forms.FolderBrowserDialog]::new()
    $dialog.Description = "Select the folder that STAG+ should process"
    $dialog.ShowNewFolderButton = $false
    try {
        if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
            throw "No image folder was selected."
        }
        return $dialog.SelectedPath
    }
    finally {
        $dialog.Dispose()
    }
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python environment not found. Run the installer first."
}

$StagPath = Join-Path $RepoDir "stag.py"
if (-not (Test-Path -LiteralPath $StagPath -PathType Leaf)) {
    throw "STAG+ was not found. Run the installer first."
}

if ([string]::IsNullOrWhiteSpace($ImageRoot)) {
    if ($UseSavedFolder -and (Test-Path -LiteralPath $SavedFolderPath -PathType Leaf)) {
        # Priority 2: Use the last saved folder from the config directory
        $ImageRoot = (Get-Content -LiteralPath $SavedFolderPath -Raw -Encoding utf8).Trim()
    }
    elseif ($Config.Paths.ImageRoot -and -not [string]::IsNullOrWhiteSpace($Config.Paths.ImageRoot)) {
        # Priority 3: Use the default path defined in settings.json
        $ImageRoot = $Config.Paths.ImageRoot
    }
    else {
        # Priority 4: Final fallback to the interactive GUI picker
        $ImageRoot = Select-ImageFolder
    }
}

$ImageRoot = [IO.Path]::GetFullPath($ImageRoot)
if (-not (Test-Path -LiteralPath $ImageRoot -PathType Container)) {
    throw "Image folder does not exist: $ImageRoot"
}

New-Item -ItemType Directory -Force -Path $ConfigDir, $LogDir | Out-Null
Set-Content -LiteralPath $SavedFolderPath -Value $ImageRoot -Encoding utf8

if ($ForceCpu) {
    $env:CUDA_VISIBLE_DEVICES = ""
}

# --- UNIFIED ARGUMENT CONSTRUCTION ---
$PythonArgs = @()
$PythonArgs += $StagPath
$PythonArgs += $ImageRoot
$PythonArgs += "--prefix"
$PythonArgs += "ai"
$PythonArgs += "--prefer-exact-filenames"

# Model Settings from JSON (Updated to only use implemented tags)
if ($Config.ModelSettings) {
    if ($Config.ModelSettings.QualityBins) { 
        $PythonArgs += "--bins"
        $PythonArgs += $Config.ModelSettings.QualityBins 
    }
    if ($Config.ModelSettings.WriteStars -eq $false) { 
        $PythonArgs += "--no-stars" 
    }
}

# Execution Flags: CLI Priority > JSON Priority
$Flags = $Config.ExecutionFlags
$isForce = $Force -or ($Flags.Force)
$isTest = $Test -or ($Flags.TestMode)
$isAll = $All -or ($Flags.AllTags)
$isQualOnly = $QualityOnly -or ($Flags.QualityOnly)
$isAesOnly = $AestheticOnly -or ($Flags.AestheticOnly)

if ($isAll) { $PythonArgs += "--all" }
elseif ($isQualOnly) { $PythonArgs += "--no-tags"; $PythonArgs += "--iq" }
elseif ($isAesOnly) { $PythonArgs += "--no-tags"; $PythonArgs += "--aes" }
else {
    # Default behavior: combine JSON toggles if no "Only" flag is set
    if ($Config.ModelSettings.IncludeAesthetic) { $PythonArgs += "--aes" }
    if ($Config.ModelSettings.IncludeQuality) { $PythonArgs += "--iq" }
}

if ($isTest) { $PythonArgs += "--test" }
if ($isForce) { $PythonArgs += "--force" }

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logPath = Join-Path $LogDir "stag-$timestamp.log"

Write-Host "Starting STAG+..." -ForegroundColor Cyan
Write-Host "Folder: $ImageRoot"
Write-Host "Log:    $logPath"
Write-Host "Device: $(if($ForceCpu){'CPU'}else{'Automatic PyTorch Selection'})"
Write-Host ""

Push-Location $RepoDir
try {
    # Execute using the unified PythonArgs array
    & $Python @PythonArgs 2>&1 | Tee-Object -FilePath $logPath

    if ($LASTEXITCODE -ne 0) {
        throw "STAG+ exited with code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}

# --- PIPELINE INTEGRATION ---
# Logic: Convert if (JSON AutoConvert is true AND CLI/JSON TestMode is false)
$ShouldConvert = ($Config.Pipeline.AutoConvert -eq $true) -and (-not $isTest)

if ($ShouldConvert) {
    Write-Host ""
    Write-Host "AutoConvert enabled: Creating plaintext Hydrus .txt sidecars..." -ForegroundColor Cyan

    # Determine if XMP files should be deleted based on JSON config
    $DeleteXmpSwitch = if ($Config.Pipeline.DeleteXmpAfterConversion) { "-DeleteXmp" } else { "" }

    & pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Converter -Root $ImageRoot $DeleteXmpSwitch

    if ($LASTEXITCODE -ne 0) {
        throw "The plaintext sidecar converter exited with code $LASTEXITCODE."
    }
}
elseif ($isTest) {
    Write-Host "`nTestMode active: Skipping sidecar conversion." -ForegroundColor Yellow
}
else {
    Write-Host "`nAutoConvert disabled: XMP files preserved." -ForegroundColor Gray
}

Write-Host "`nFinished." -ForegroundColor Green
