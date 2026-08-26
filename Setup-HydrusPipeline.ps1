<#
  Setup-HydrusPipeline.ps1
  ------------------------
  One script to install (idempotent) and then launch the gallery-dl -> hydownloader -> Hydrus
  pipeline on Windows. Safe to re-run: it skips anything already installed/configured and just
  starts the services on subsequent runs.

  WHAT THIS AUTOMATES:
    - Installs Python, Git, FFmpeg, Hydrus Network (via winget)
    - Installs gallery-dl (pip) and Poetry
    - Clones hydownloader from its real upstream (GitLab, not the stale GitHub forks)
    - Runs `poetry install`, initializes the hydownloader database
    - Scaffolds config file templates (gallery-dl, hydownloader)
    - Walks you through the two steps that CANNOT be scripted (Reddit app creation,
      Hydrus Client API key generation) with pauses and clear instructions
    - Finishes by launching Hydrus + the hydownloader daemon + the systray GUI,
      which is your ongoing "main interaction point" for adding subscriptions/URLs

  BEFORE RUNNING:
    - Run this from a REGULAR (non-admin) PowerShell window. Hydrus explicitly should not be
      installed/run with admin rights.
    - Requires winget (built into Windows 10 2004+ / Windows 11). If missing, install
      "App Installer" from the Microsoft Store first.
    - Edit the $InstallRoot variable below if you don't want it under your user profile.

  This script has been reviewed but NOT executed end-to-end on a real Windows machine
  (it was written from an environment with no Windows access). Read through it before running,
  especially the winget package IDs, in case Microsoft has renamed anything since this was written.
#>

# ============================================================
# 0. CONFIG - edit these if you want different locations
# ============================================================
$InstallRoot   = "$env:USERPROFILE\HydrusPipeline"
$HydrusDir     = Join-Path $InstallRoot "hydrus"
$HydownloaderRepoDir = Join-Path $InstallRoot "hydownloader"
$DataDir       = Join-Path $InstallRoot "hydownloader-data"
$DownloadedDir = Join-Path $DataDir "downloaded"
$LogsDir       = Join-Path $DataDir "logs"
$HydrusApiUrl  = "http://localhost:45869"

$ErrorActionPreference = "Stop"

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Write-Step {
    param([string]$Text)
    Write-Host ""
    Write-Host ">>> $Text" -ForegroundColor Cyan
}

function Write-Warn2 {
    param([string]$Text)
    Write-Host "!!! $Text" -ForegroundColor Yellow
}

function Set-Utf8NoBom {
    # Windows PowerShell 5.1's `Set-Content -Encoding UTF8` silently prepends a UTF-8 BOM,
    # which Python's json module (and anything else strict about encoding) chokes on with
    # "Expecting value: line 1 column 1 (char 0)". Every config file this script writes gets
    # read back by Python (gallery-dl, hydownloader) or PowerShell - never by something that
    # needs a BOM - so always write plain UTF-8 without one.
    param([string]$Path, [string]$Content)
    [System.IO.File]::WriteAllText($Path, $Content, (New-Object System.Text.UTF8Encoding($false)))
}

New-Item -ItemType Directory -Force -Path $InstallRoot, $DataDir, $DownloadedDir, $LogsDir | Out-Null

# ============================================================
# 1. PREREQUISITES (winget)
# ============================================================
Write-Step "Checking for winget..."
if (-not (Test-CommandExists "winget")) {
    Write-Warn2 "winget not found. Install 'App Installer' from the Microsoft Store, then re-run this script."
    exit 1
}

$wingetCommonArgs = @("--accept-package-agreements", "--accept-source-agreements", "--silent", "--disable-interactivity")

if (-not (Test-CommandExists "python")) {
    Write-Step "Installing Python..."
    winget install --id Python.Python.3.12 -e --scope user --override "/passive InstallAllUsers=0 PrependPath=1" @wingetCommonArgs
} else {
    Write-Host "Python already installed: $(python --version)"
}

if (-not (Test-CommandExists "git")) {
    Write-Step "Installing Git..."
    winget install --id Git.Git -e --scope user @wingetCommonArgs
} else {
    Write-Host "Git already installed: $(git --version)"
}

if (-not (Test-CommandExists "ffmpeg")) {
    Write-Step "Installing FFmpeg..."
    winget install --id Gyan.FFmpeg -e --scope user @wingetCommonArgs
    Write-Warn2 "If 'ffmpeg -version' doesn't work in a NEW terminal after this, you'll need to add its bin folder to PATH manually."
} else {
    Write-Host "FFmpeg already installed."
}

if (-not (Test-CommandExists "mkvmerge")) {
    Write-Step "Installing MKVToolNix (needed by hydownloader for precise ugoira/video timing)..."
    winget install --id MoritzBunkus.MKVToolNix -e --scope user @wingetCommonArgs
} else {
    Write-Host "MKVToolNix already installed."
}

# Hydrus is our own fork (github.com/SolMelissa/hydrus, "undertow" branch), run from source -
# never the upstream winget/release build, so we can freely modify it.
$HydrusVenvPythonw = Join-Path $HydrusDir "venv\Scripts\pythonw.exe"
$HydrusEntryScript = Join-Path $HydrusDir "hydrus_client.pyw"
if (-not (Test-Path $HydrusVenvPythonw) -or -not (Test-Path $HydrusEntryScript)) {
    if (-not (Test-Path (Join-Path $HydrusDir ".git"))) {
        Write-Step "Cloning Hydrus fork (SolMelissa/hydrus, undertow branch) to $HydrusDir ..."
        git clone --branch undertow https://github.com/SolMelissa/hydrus.git "$HydrusDir"
    } else {
        Write-Step "Hydrus fork already cloned at $HydrusDir - pulling latest undertow branch..."
        git -C "$HydrusDir" pull --ff-only origin undertow
    }

    Write-Step "Building Hydrus's venv (simple install)..."
    Push-Location $HydrusDir
    python setup_venv.py -i s
    Pop-Location
    Write-Warn2 "Running from source also needs mpv/SQLite DLLs dropped into $HydrusDir - see hydrus-client/docs/running_from_source.md if the client fails to start."
} else {
    Write-Host "Hydrus already set up from source at $HydrusDir"
}

# Refresh PATH in this session so newly-installed tools are visible without reopening the shell
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

# ============================================================
# 2. gallery-dl + Poetry
# ============================================================
Write-Step "Installing/upgrading gallery-dl..."
python -m pip install --user --upgrade gallery-dl

if (-not (Test-CommandExists "poetry")) {
    Write-Step "Installing Poetry..."
    python -m pip install --user poetry
} else {
    Write-Host "Poetry already installed: $(poetry --version)"
}

# ============================================================
# 3. hydownloader (real upstream is GitLab, not the GitHub forks)
# ============================================================
if (-not (Test-Path $HydownloaderRepoDir)) {
    Write-Step "Cloning hydownloader from upstream (GitLab)..."
    git clone https://gitgud.io/thatfuckingbird/hydownloader.git $HydownloaderRepoDir
} else {
    Write-Host "hydownloader repo already present at $HydownloaderRepoDir"
}

Push-Location $HydownloaderRepoDir
Write-Step "Running poetry install (this can take a few minutes the first time)..."
python -m poetry install
Pop-Location

# ============================================================
# 4. Initialize the hydownloader database (first run only)
# ============================================================
$HydownloaderConfigFile = Join-Path $DataDir "hydownloader-config.json"
if (-not (Test-Path $HydownloaderConfigFile)) {
    Write-Step "Initializing hydownloader database at $DataDir (init-db)..."
    Push-Location $HydownloaderRepoDir
    python -m poetry run hydownloader-tools init-db --path $DataDir
    Pop-Location
    if (-not (Test-Path $HydownloaderConfigFile)) {
        Write-Warn2 "init-db did not produce hydownloader-config.json - check the output above for errors before continuing."
    }
} else {
    Write-Host "hydownloader database already initialized."
}

# ============================================================
# 4b. Patch in any config keys missing from hydownloader-config.json
#     (known issue: freshly-generated configs from init-db have been observed
#     missing keys like 'shared-db-override', which crashes the daemon on
#     startup with a KeyError. This only ADDS missing keys - it never touches
#     existing values, so it's safe to re-run and won't clobber the real
#     auto-generated 'daemon.access-key' secret.)
# ============================================================
function New-UrlSafeToken {
    # .NET equivalent of Python's secrets.token_urlsafe() - used only to fill in
    # daemon.access-key if it's missing (e.g. it was wiped by an earlier bug in
    # this script). If the real key already exists, we never touch it.
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
}

$DefaultHydownloaderConfigKeys = @{
    "gallery-dl.executable" = "gallery-dl"
    "daemon.port" = 53211
    "daemon.host" = "127.0.0.1"
    "daemon.ssl" = $true
    "daemon.access-key" = New-UrlSafeToken
    "daemon.do-not-check-access-key" = $false
    "daemon.fill-import-queue" = $true
    "daemon.do-not-add-to-import-queue-if-already-pending" = $true
    "daemon.enable-quick-mode" = $false
    "daemon.quick-mode-check-time-limit-seconds" = 480
    "daemon.quick-mode-time-intervals" = @()
    "daemon.quick-mode-subscription-id-blacklist" = @()
    "daemon.quick-mode-downloader-blacklist" = @()
    "daemon.quick-mode-due-time-multiplier" = 10.0
    "gallery-dl.archive-override" = ""
    "gallery-dl.data-override" = ""
    "gallery-dl.do-not-use-cookiestxt" = $false
    "gallery-dl.load-hydownloader-extractors" = $true
    "gallery-dl.additional-extractor-paths" = @()
    "shared-db-override" = ""
    "disable-wal" = $false
    "errored-sub-recheck-min-wait-seconds" = 60
    "daemon.check-free-space" = $true
    "cfg-files-rules" = @()
}

if (Test-Path $HydownloaderConfigFile) {
    Write-Step "Checking hydownloader-config.json for missing default keys..."
    $hdCfgPatch = Get-Content $HydownloaderConfigFile -Raw | ConvertFrom-Json
    $patchedAnyKey = $false
    foreach ($key in $DefaultHydownloaderConfigKeys.Keys) {
        if (-not ($hdCfgPatch.PSObject.Properties.Name -contains $key)) {
            $hdCfgPatch | Add-Member -MemberType NoteProperty -Name $key -Value $DefaultHydownloaderConfigKeys[$key]
            Write-Host "  added missing key: $key"
            $patchedAnyKey = $true
        }
    }
    if ($patchedAnyKey) {
        Set-Utf8NoBom -Path $HydownloaderConfigFile -Content ($hdCfgPatch | ConvertTo-Json -Depth 10)
        Write-Host "Patched $HydownloaderConfigFile."
    } else {
        Write-Host "No missing keys found."
    }
}

# ============================================================
# 5. gallery-dl config template
# ============================================================
$GalleryDlConfigDir = "$env:USERPROFILE\gallery-dl"
$GalleryDlConfigFile = Join-Path $GalleryDlConfigDir "config.json"
New-Item -ItemType Directory -Force -Path $GalleryDlConfigDir | Out-Null

if (-not (Test-Path $GalleryDlConfigFile)) {
    Write-Step "Writing gallery-dl config template..."
    $galleryDlConfig = @{
        extractor = @{
            "base-directory" = ($DownloadedDir -replace '\\','/') + "/"
            directory = @{
                reddit   = @("reddit", "{subreddit}")
                bluesky  = @("bluesky", "{author}")
                mastodon = @("mastodon", "{instance}", "{account}")
                tumblr   = @("tumblr", "{blog}")
                pixiv    = @("pixiv", "{user}")
            }
            reddit = @{
                "client-id"     = "PLACEHOLDER_SET_BELOW"
                "user-agent"    = "PLACEHOLDER_SET_BELOW"
                "refresh-token" = "PLACEHOLDER_SET_BELOW"
                videos          = $true
                submissions     = $true
                comments        = $false
            }
            postprocessors = @(
                @{ name = "metadata"; mode = "json"; filename = "{id}.metadata.json" }
            )
        }
        downloader = @{ http = @{ "sleep-429" = "60" } }
        output = @{
            logfile = @{ path = ($LogsDir -replace '\\','/') + "/gallery-dl.log"; mode = "a" }
        }
    }
    Set-Utf8NoBom -Path $GalleryDlConfigFile -Content ($galleryDlConfig | ConvertTo-Json -Depth 10)
} else {
    Write-Host "gallery-dl config already exists at $GalleryDlConfigFile - leaving it alone."
}

# ============================================================
# 6. Reddit OAuth / Hydrus API key status (no prompts here anymore -
#    credentials are configured on demand via Configure-ApiKeys.ps1,
#    not re-asked on every run. This section just reports status.)
# ============================================================
Write-Step "Credential status"
$redditConfigured = $false
try {
    $existing = Get-Content $GalleryDlConfigFile -Raw | ConvertFrom-Json
    if ($existing.extractor.reddit.'client-id' -and $existing.extractor.reddit.'client-id' -ne "PLACEHOLDER_SET_BELOW") {
        $redditConfigured = $true
    }
} catch {}
Write-Host "Reddit OAuth:    $(if ($redditConfigured) {'configured'} else {'NOT configured (Reddit app creation is down on their side as of writing)'})"

$ImportJobsFile = Join-Path $DataDir "hydownloader-import-jobs.py"
$hydrusKeyConfigured = $false
try {
    $jobsContentCheck = Get-Content $ImportJobsFile -Raw -ErrorAction Stop
    if ($jobsContentCheck -match 'defAPIKey\s*=\s*["'']([0-9a-fA-F]{64})["'']') {
        $hydrusKeyConfigured = $true
    }
} catch {}
Write-Host "Hydrus API key:  $(if ($hydrusKeyConfigured) {'configured'} else {'NOT configured'})"
Write-Host "(Run Configure-ApiKeys.ps1, or use the launcher menu, to set/change either of these.)"

# ============================================================
# 8. LAUNCH - Hydrus, then hydownloader daemon, then the systray GUI
#    (the systray + Hydrus main window are your ongoing interaction point)
# ============================================================
$hydrusAlreadyRunning = [bool](Get-Process -Name "hydrus_client" -ErrorAction SilentlyContinue)
if ($hydrusAlreadyRunning) {
    Write-Step "Hydrus is already running - not launching a second copy."
} elseif (Test-Path $HydrusExe) {
    Write-Step "Launching Hydrus..."
    Start-Process $HydrusExe
    Write-Host "Waiting for Hydrus to finish starting (first launch can take a minute)..."
    Start-Sleep -Seconds 15
} else {
    Write-Warn2 "Could not find $HydrusExe - Hydrus install may have failed or landed elsewhere. Launch it manually."
}

if (-not $hydrusKeyConfigured) {
    Write-Warn2 "Hydrus API key not set yet. Run Configure-ApiKeys.ps1 (or the launcher's 'configure keys' option) to set it - importing won't work until then."
}

function Get-RunningHydownloaderProc {
    param([string]$Match)
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $Match }
}

$existingDaemon = Get-RunningHydownloaderProc -Match "hydownloader-daemon"
if ($existingDaemon) {
    Write-Step "hydownloader daemon is already running (PID $($existingDaemon[0].ProcessId)) - not starting a second copy."
    $daemonProc = [PSCustomObject]@{ HasExited = $false; Id = $existingDaemon[0].ProcessId; ExitCode = $null }
} else {
    Write-Step "Starting the hydownloader daemon..."
    $daemonLog = Join-Path $LogsDir "daemon-launch-stdout.log"
    $daemonErrLog = Join-Path $LogsDir "daemon-launch-stderr.log"
    $daemonProc = Start-Process -FilePath "python" -ArgumentList "-m poetry run hydownloader-daemon start --path `"$DataDir`"" -WorkingDirectory $HydownloaderRepoDir -WindowStyle Minimized -RedirectStandardOutput $daemonLog -RedirectStandardError $daemonErrLog -PassThru
    Start-Sleep -Seconds 6
}


# ------------------------------------------------------------
# hydownloader-systray is a SEPARATE project - a prebuilt native Qt app,
# not a Python/poetry entry point. Download it, configure settings.ini,
# and launch the .exe directly.
# ------------------------------------------------------------
$SystrayDir      = Join-Path $InstallRoot "hydownloader-systray"
$SystrayZipUrl   = "https://gitgud.io/thatfuckingbird/hydownloader-assets/-/raw/master/hydownloader-systray-2053acef01cfd0ff464a3d55536da334dc350366.zip"
$SystrayZipPath  = Join-Path $InstallRoot "hydownloader-systray.zip"
$SystrayExe      = Join-Path $SystrayDir "hydownloader-systray.exe"

# The exe actually lands inside a commit-hash-named subfolder after extraction, not
# directly in $SystrayDir - search for it first so we don't re-download/re-extract
# (and potentially fail on file locks if it's currently running) on every run.
$foundExisting = Get-ChildItem -Path $SystrayDir -Filter "hydownloader-systray.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
if ($foundExisting) {
    $SystrayExe = $foundExisting.FullName
}

if (-not (Test-Path $SystrayExe)) {
    Write-Step "Downloading hydownloader-systray (separate project, prebuilt binary - not installed via poetry)..."
    New-Item -ItemType Directory -Force -Path $SystrayDir | Out-Null
    Invoke-WebRequest -Uri $SystrayZipUrl -OutFile $SystrayZipPath
    Expand-Archive -Path $SystrayZipPath -DestinationPath $SystrayDir -Force
    $foundExe = Get-ChildItem -Path $SystrayDir -Filter "hydownloader-systray.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($foundExe) {
        $SystrayExe = $foundExe.FullName
    } else {
        Write-Warn2 "Could not find hydownloader-systray.exe after extracting the zip - check $SystrayDir manually."
    }
} else {
    Write-Host "hydownloader-systray already downloaded at $SystrayExe"
}

# Write/refresh settings.ini next to the exe so it knows how to reach the hydownloader daemon
# (NOT Hydrus itself - this is a different API, hydownloader's own daemon.* settings).
if ((Test-Path $SystrayExe) -and (Test-Path $HydownloaderConfigFile)) {
    $hdCfgForSystray = Get-Content $HydownloaderConfigFile -Raw | ConvertFrom-Json
    $daemonPort = $hdCfgForSystray.'daemon.port'
    $daemonHostName = $hdCfgForSystray.'daemon.host'
    $daemonSsl  = $hdCfgForSystray.'daemon.ssl'
    $daemonKey  = $hdCfgForSystray.'daemon.access-key'
    # hydownloader only actually serves HTTPS if daemon.ssl is true AND a server.pem
    # file exists in the data dir - otherwise it logs a warning and falls back to
    # plain HTTP, regardless of the config flag. Mirror that logic exactly here so
    # the systray's apiURL scheme always matches what the daemon is really doing.
    $serverPemExists = Test-Path (Join-Path $DataDir "server.pem")
    $scheme = if ($daemonSsl -and $serverPemExists) { "https" } else { "http" }
    $daemonApiUrl = "${scheme}://${daemonHostName}:${daemonPort}"

    $settingsIniPath = Join-Path (Split-Path $SystrayExe -Parent) "settings.ini"
    $settingsIniLines = @(
        "instanceNames=main",
        "accessKey=$daemonKey",
        "apiURL=$daemonApiUrl",
        "defaultTests=environment",
        "defaultSubCheckInterval=48",
        "applyDarkPalette=false",
        "updateInterval=3000",
        "startVisible=true",
        "aggressiveUpdates=true",
        "localConnection=true",
        "disablePreviews=false",
        "forceStyle=",
        "userCss="
    )
    Set-Content -Path $settingsIniPath -Value $settingsIniLines -Encoding UTF8
    Write-Host "Wrote $settingsIniPath (apiURL=$daemonApiUrl)"
} elseif (Test-Path $SystrayExe) {
    Write-Warn2 "Can't write settings.ini yet - $HydownloaderConfigFile not found."
}

$existingSystray = [bool](Get-Process -Name "hydownloader-systray" -ErrorAction SilentlyContinue)
if ($existingSystray) {
    Write-Step "hydownloader systray is already running - not starting a second copy."
    $systrayProc = [PSCustomObject]@{ HasExited = $false; Id = (Get-Process -Name "hydownloader-systray").Id; ExitCode = $null }
} elseif (Test-Path $SystrayExe) {
    Write-Step "Launching the hydownloader systray (your day-to-day control panel)..."
    $systrayLog = Join-Path $LogsDir "systray-launch-stdout.log"
    $systrayErrLog = Join-Path $LogsDir "systray-launch-stderr.log"
    $systrayProc = Start-Process -FilePath $SystrayExe -WorkingDirectory (Split-Path $SystrayExe -Parent) -RedirectStandardOutput $systrayLog -RedirectStandardError $systrayErrLog -PassThru
    Start-Sleep -Seconds 4
} else {
    Write-Warn2 "hydownloader-systray.exe not found - skipping launch. Check the download step above."
    $systrayProc = [PSCustomObject]@{ HasExited = $true; Id = $null; ExitCode = "not found" }
}

# ============================================================
# 9. STATUS REPORT (window stays open so you can read this)
# ============================================================
Write-Host ""
Write-Host "=====================================================" -ForegroundColor Green
Write-Host " STATUS REPORT" -ForegroundColor Green
Write-Host "=====================================================" -ForegroundColor Green

$hydrusRunning = [bool](Get-Process -Name "hydrus_client" -ErrorAction SilentlyContinue)
Write-Host "Hydrus running:               $hydrusRunning"

if ($daemonProc.HasExited) {
    Write-Warn2 "hydownloader daemon EXITED already (exit code $($daemonProc.ExitCode)) - it did not stay running."
    if (Test-Path $daemonErrLog) {
        Write-Warn2 "--- daemon stderr (last 20 lines) ---"
        Get-Content $daemonErrLog -Tail 20 | ForEach-Object { Write-Host "  $_" }
    }
} else {
    Write-Host "hydownloader daemon running:  True (PID $($daemonProc.Id))"
}

if ($systrayProc.HasExited) {
    Write-Warn2 "hydownloader systray EXITED already (exit code $($systrayProc.ExitCode)) - it did not stay running."
    if (Test-Path $systrayErrLog) {
        Write-Warn2 "--- systray stderr (last 20 lines) ---"
        Get-Content $systrayErrLog -Tail 20 | ForEach-Object { Write-Host "  $_" }
    }
} else {
    Write-Host "hydownloader systray running: True (PID $($systrayProc.Id))"
}

Write-Host ""
Write-Host "Key files:"
foreach ($f in @($HydownloaderConfigFile, $ImportJobsFile, (Join-Path $DataDir "gallery-dl-user-config.json"), $GalleryDlConfigFile, $SystrayExe, (Join-Path (Split-Path $SystrayExe -Parent) "settings.ini"))) {
    Write-Host "  [$(Test-Path $f)] $f"
}

if (Test-Path $HydownloaderConfigFile) {
    try {
        $hdCfg = Get-Content $HydownloaderConfigFile -Raw | ConvertFrom-Json
        if ($hdCfg.'daemon.access-key') {
            Write-Host ""
            Write-Host "hydownloader's own daemon access key (this is already written into the systray's settings.ini for you):"
            Write-Host "  $($hdCfg.'daemon.access-key')"
        }
    } catch {
        Write-Warn2 "Could not parse $HydownloaderConfigFile to read its access key."
    }
}

Write-Host ""
Write-Host "Reddit OAuth: $(if ($redditConfigured) {'configured'} else {'NOT configured yet (pending Reddit review) - Bluesky/Mastodon/etc. are unaffected'})"
Write-Host ""
Write-Host "Import folder reminder: in Hydrus, go to file -> import folders and add:"
Write-Host "  $DownloadedDir"
Write-Host "=====================================================" -ForegroundColor Green
Read-Host "Press Enter to close this window"
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          