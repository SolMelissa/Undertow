<#
.SYNOPSIS
    Relocates the Hydrus Pipeline *app folder* to the iCloud Drive path and repairs every
    link that depends on its location.

.DESCRIPTION
    WHAT THIS MOVES
        C:\0Docs\AI\Claude\The Pipeline
            -> F:\Apple\iCloudDrive\0Docs\AI\Claude\The Pipeline

        That is the app you wrote: the hydrus_pipeline package (web dashboard, TUI,
        watchdog, subscriptions), the legacy .ps1 scripts, run.bat, the guide, and .git.

    WHAT THIS DOES NOT TOUCH - deliberately
        %USERPROFILE%\HydrusPipeline\  stays exactly where it is. All of it:
            hydrus\db\        client.db, client.master.db, client.mappings.db,
                              client.caches.db and client_files\ (your media library)
            hydownloader-data\  hydownloader.db + its -wal/-shm journals, anchor.db,
                              gallery-dl-cache.db, logs, configs, settings json
            hydrus\            the Hydrus install and its registered uninstaller
            hydownloader\      the upstream repo clone
            hydownloader-systray\

        Live SQLite databases must not sit in a cloud-synced folder. iCloud syncs
        file-by-file with no understanding of WAL journals, so it can upload a .db and
        its -wal out of step, and "Optimize Storage" can evict client_files to the cloud
        and leave Hydrus reporting its own media as missing.

        No code change is needed for any of this: hydrus_pipeline/config.py derives
        INSTALL_ROOT from %USERPROFILE%, which does not change. The daemon is launched as
        `poetry run hydownloader-daemon --path <data dir>` with cwd=<repo dir>, both of
        which also stay put.

    WHAT ACTUALLY BREAKS ON A MOVE, AND HOW THIS FIXES IT
        1. The .venv - virtualenvs are not relocatable. pyvenv.cfg and the Scripts\*.exe
           launcher shims embed the absolute build path. It is excluded from the copy and
           rebuilt from scratch.
        2. The Desktop shortcut - "Hydrus Pipeline.lnk" targets the old run.bat. Rebuilt.
        3. PYTHON_PORT_SETUP.md referenced the old path. Already corrected in the source.

.PARAMETER VenvInFolder
    Build the venv as ".venv" inside the project folder (the original layout) instead of
    outside iCloud. run.bat checks the in-folder .venv first, so this works - it just means
    ~4000 small files and native binaries churning through iCloud sync.

.PARAMETER RemoveSource
    Delete the old C: folder after a verified copy. Off by default; the copy is verified
    and the app is confirmed working before you would ever want this.

.PARAMETER DryRun
    Print every step and run the robocopy in list-only mode. Changes nothing.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Move-PipelineToICloud.ps1 -DryRun
    powershell -ExecutionPolicy Bypass -File .\Move-PipelineToICloud.ps1
#>

[CmdletBinding()]
param(
    [string] $Source      = 'C:\0Docs\AI\Claude\The Pipeline',
    [string] $Destination = 'F:\Apple\iCloudDrive\0Docs\AI\Claude\The Pipeline',
    [string] $VenvPath    = "$env:LOCALAPPDATA\HydrusPipeline\venv",
    [switch] $VenvInFolder,
    [switch] $RemoveSource,
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'

function Write-Step { param($m) Write-Host "`n=== $m" -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host "    [ok]   $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "    [warn] $m" -ForegroundColor Yellow }
function Write-Info { param($m) Write-Host "    $m" -ForegroundColor Gray }

# Excluded from the copy. .venv is unrelocatable; __pycache__/*.pyc embed absolute source
# paths and would produce misleading tracebacks; stackdumps are crash litter.
$ExcludeDirs  = @('.venv', '__pycache__')
$ExcludeFiles = @('*.pyc', '*.stackdump')

Write-Host @"

  Hydrus Pipeline - relocate app folder to iCloud Drive
  -----------------------------------------------------
  Moving : $Source
  To     : $Destination
  Venv   : $(if ($VenvInFolder) { "$Destination\.venv (in folder)" } else { "$VenvPath (outside iCloud)" })

  NOT TOUCHED: $env:USERPROFILE\HydrusPipeline
               (Hydrus install + db + client_files, hydownloader repo + data, systray)
"@ -ForegroundColor White

if ($DryRun) { Write-Warn 'DRY RUN - nothing will be changed.' }


# --------------------------------------------------------------------------- 1. preflight
Write-Step '1/7  Preflight checks'

if (-not (Test-Path -LiteralPath $Source)) {
    throw "Source folder not found: $Source"
}
Write-Ok "Source exists: $Source"

$destRoot = Split-Path -Qualifier $Destination
if (-not (Test-Path -LiteralPath "$destRoot\")) {
    throw "Drive $destRoot is not available. Is the F: drive connected and is iCloud Drive signed in?"
}
Write-Ok "Destination drive $destRoot is available"

$destParent = Split-Path -Parent $Destination
if (-not (Test-Path -LiteralPath $destParent)) {
    Write-Warn "Parent folder does not exist yet, will be created: $destParent"
} else {
    Write-Ok "Parent folder exists: $destParent"
}

if (Test-Path -LiteralPath $Destination) {
    $existing = @(Get-ChildItem -LiteralPath $Destination -Force -ErrorAction SilentlyContinue)
    if ($existing.Count -gt 0) {
        Write-Warn "Destination already exists and contains $($existing.Count) item(s)."
        Write-Warn 'Files with matching names will be OVERWRITTEN. Nothing else is deleted.'
        if (-not $DryRun) {
            $ans = Read-Host '    Continue? (y/N)'
            if ($ans -ne 'y') { throw 'Aborted by user.' }
        }
    }
}

# Confirm the install root really is staying put and is intact.
$installRoot = Join-Path $env:USERPROFILE 'HydrusPipeline'
if (Test-Path -LiteralPath $installRoot) {
    Write-Ok "Install root present and staying put: $installRoot"
    foreach ($sub in 'hydrus\db', 'hydownloader-data', 'hydownloader') {
        if (Test-Path -LiteralPath (Join-Path $installRoot $sub)) {
            Write-Info "  - $sub  (untouched)"
        }
    }
} else {
    Write-Warn "Expected install root not found: $installRoot"
    Write-Warn 'The app will still move, but check hydrus_pipeline/config.py afterwards.'
}


# ------------------------------------------------------------------- 2. stop the app only
Write-Step '2/7  Stopping the Pipeline app (Hydrus and the daemon keep running)'

# The app's own process holds a handle on its folder via its working directory, which
# blocks the copy on Windows. It runs as python.exe/pythonw.exe, so it has to be matched on
# command line - the process name is indistinguishable from any other Python.
$pipelineProcs = @(
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*hydrus_pipeline*' }
)

if ($pipelineProcs.Count -eq 0) {
    Write-Ok 'Pipeline app is not running.'
} else {
    foreach ($p in $pipelineProcs) {
        Write-Info "Found PID $($p.ProcessId): $($p.CommandLine)"
        if (-not $DryRun) {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    if (-not $DryRun) {
        Start-Sleep -Seconds 2
        Write-Ok "Stopped $($pipelineProcs.Count) Pipeline process(es)."
        Write-Info 'Hydrus, the hydownloader daemon and the systray were left running -'
        Write-Info 'they run out of the install root, which is not moving.'
    }
}


# ------------------------------------------------------------------------------ 3. copy
Write-Step '3/7  Copying the app folder'

$roboArgs = @(
    $Source, $Destination,
    '/E',            # include subdirectories, including empty ones
    '/COPY:DAT',     # data, attributes, timestamps (no ACLs - they do not survive the move usefully)
    '/DCOPY:DAT',
    '/R:2', '/W:2',  # 2 retries, 2s wait - this is a local copy, not a flaky share
    '/NP', '/NFL', '/NDL'
)
$roboArgs += '/XD'; $roboArgs += $ExcludeDirs
$roboArgs += '/XF'; $roboArgs += $ExcludeFiles
if ($DryRun) { $roboArgs += '/L' }

Write-Info "robocopy $($roboArgs -join ' ')"

# Robocopy signals success with NON-ZERO exit codes (1 = files copied, 2 = extra files
# present, etc; only >=8 is a real failure). PowerShell 7.4+ defaults
# $PSNativeCommandUseErrorActionPreference to $true, which turns any non-zero native exit
# code into a terminating error while $ErrorActionPreference is 'Stop' - so a perfectly
# successful copy would throw. Both preferences are relaxed just around this call.
$prevEAP    = $ErrorActionPreference
$prevNative = $null
if (Test-Path Variable:\PSNativeCommandUseErrorActionPreference) {
    $prevNative = $PSNativeCommandUseErrorActionPreference
    $PSNativeCommandUseErrorActionPreference = $false
}
$ErrorActionPreference = 'Continue'
try {
    & robocopy.exe @roboArgs | Out-Null
    $rc = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $prevEAP
    if ($null -ne $prevNative) { $PSNativeCommandUseErrorActionPreference = $prevNative }
}

# Exit code is a bitmask: <8 is success, >=8 means at least one file failed to copy.
if ($rc -ge 8) {
    throw "robocopy failed with exit code $rc. Nothing was removed; the source is intact."
}
Write-Ok "robocopy completed (exit code $rc - anything under 8 is success)"


# ------------------------------------------------------------------------------ 4. verify
Write-Step '4/7  Verifying the copy'

function Get-PayloadFiles {
    param([string] $Root)
    if (-not (Test-Path -LiteralPath $Root)) { return @() }
    Get-ChildItem -LiteralPath $Root -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object {
            $rel = $_.FullName.Substring($Root.Length).TrimStart('\')
            $parts = $rel -split '\\'
            -not ($parts | Where-Object { $ExcludeDirs -contains $_ }) -and
            -not ($_.Name -like '*.pyc') -and
            -not ($_.Name -like '*.stackdump')
        }
}

if ($DryRun) {
    $srcFiles = @(Get-PayloadFiles -Root $Source)
    $bytes = ($srcFiles | Measure-Object -Property Length -Sum).Sum
    if (-not $bytes) { $bytes = 0 }
    Write-Info "Would copy $($srcFiles.Count) files ($([math]::Round($bytes / 1MB, 2)) MB)"
} else {
    $srcFiles = @(Get-PayloadFiles -Root $Source)
    $dstFiles = @(Get-PayloadFiles -Root $Destination)

    Write-Info "Source: $($srcFiles.Count) files   Destination: $($dstFiles.Count) files"

    if ($dstFiles.Count -eq 0) {
        throw "Destination is empty after robocopy reported success. Check $Destination manually; the source is untouched."
    }

    # @() on both sides - Compare-Object throws on a null argument, which is exactly the
    # case that shows up when something went wrong and is the worst time to get a
    # confusing binding error instead of a real message.
    $srcRel = @($srcFiles | ForEach-Object { $_.FullName.Substring($Source.Length).TrimStart('\') })
    $dstRel = @($dstFiles | ForEach-Object { $_.FullName.Substring($Destination.Length).TrimStart('\') })
    $missing = @(Compare-Object -ReferenceObject $srcRel -DifferenceObject $dstRel |
                    Where-Object { $_.SideIndicator -eq '<=' })

    if ($missing.Count -gt 0) {
        Write-Warn "$($missing.Count) file(s) did not make it across:"
        $missing | Select-Object -First 20 | ForEach-Object { Write-Warn "  $($_.InputObject)" }
        throw 'Copy verification failed. The source folder is untouched - fix the cause and re-run.'
    }
    Write-Ok 'Every source file is present at the destination.'

    foreach ($critical in 'run.bat', 'hydrus_pipeline\config.py', 'hydrus_pipeline\webui.py', 'requirements.txt') {
        if (Test-Path -LiteralPath (Join-Path $Destination $critical)) {
            Write-Ok "  $critical"
        } else {
            throw "Critical file missing at destination: $critical"
        }
    }
}


# ------------------------------------------------------------------------ 5. rebuild venv
Write-Step '5/7  Rebuilding the virtual environment'

$venvTarget = if ($VenvInFolder) { Join-Path $Destination '.venv' } else { $VenvPath }
$venvPy     = Join-Path $venvTarget 'Scripts\python.exe'

Write-Info "Target: $venvTarget"
Write-Info 'A venv cannot be copied - pyvenv.cfg and the Scripts\*.exe shims hardcode the'
Write-Info 'absolute path it was built at. It has to be created fresh.'

if ($DryRun) {
    Write-Info 'Would run: python -m venv <target>; pip install -r requirements.txt'
} else {
    # Find a usable system Python. services.py already warns when several are on PATH.
    $sysPy = $null
    foreach ($cand in 'python.exe', 'py.exe') {
        $found = Get-Command $cand -ErrorAction SilentlyContinue
        if ($found) { $sysPy = $found.Source; break }
    }
    if (-not $sysPy) {
        throw 'No system Python found on PATH. Install Python 3.10+ (or re-run Setup-HydrusPipeline.ps1) and try again.'
    }
    Write-Ok "Using system Python: $sysPy"

    if (Test-Path -LiteralPath $venvTarget) {
        Write-Warn "Removing stale venv at $venvTarget"
        Remove-Item -LiteralPath $venvTarget -Recurse -Force
    }

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $venvTarget) | Out-Null

    if ($sysPy -like '*py.exe') {
        & $sysPy -3 -m venv $venvTarget
    } else {
        & $sysPy -m venv $venvTarget
    }
    if (-not (Test-Path -LiteralPath $venvPy)) { throw "venv creation failed - $venvPy not found." }
    Write-Ok 'venv created'

    & $venvPy -m pip install --upgrade pip --quiet
    & $venvPy -m pip install -r (Join-Path $Destination 'requirements.txt') --quiet
    if ($LASTEXITCODE -ne 0) { throw 'pip install -r requirements.txt failed.' }
    Write-Ok 'requirements.txt installed (requests, psutil, pywin32, rich, flask, textual)'
}


# -------------------------------------------------------------------------- 6. shortcut
Write-Step '6/7  Recreating the Desktop shortcut'

if ($DryRun) {
    Write-Info 'Would rebuild "Hydrus Pipeline.lnk" pointing at the new run.bat'
} else {
    # NOT `python -m hydrus_pipeline.shortcut` - that module's __main__ block ends with
    # input("Press Enter to close"), which would block this script forever on stdin.
    # Calling the function directly skips the prompt.
    Push-Location $Destination
    try {
        & $venvPy -c "from hydrus_pipeline.shortcut import create_desktop_shortcut; create_desktop_shortcut()"
        if ($LASTEXITCODE -ne 0) { throw 'Shortcut creation failed.' }
    } finally {
        Pop-Location
    }

    $lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Hydrus Pipeline.lnk'
    if (Test-Path -LiteralPath $lnk) {
        $shell = New-Object -ComObject WScript.Shell
        $sc = $shell.CreateShortcut($lnk)
        Write-Ok "Shortcut target: $($sc.TargetPath)"
        if ($sc.TargetPath -like "$Destination*") {
            Write-Ok 'Shortcut points at the new location.'
        } else {
            Write-Warn "Shortcut still points at $($sc.TargetPath) - check manually."
        }
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
    } else {
        Write-Warn "Desktop shortcut not found at $lnk"
    }
}


# ------------------------------------------------------------------- 7. post-move sanity
Write-Step '7/7  Post-move sanity check'

if ($DryRun) {
    Write-Info 'Would confirm config paths still resolve to the untouched install root.'
} else {
    # Written to a temp file rather than passed via `python -c` - a multi-line -c argument
    # has to survive PowerShell's native-command argument quoting, which is unreliable
    # across PS 5.1 / 7 on Windows. A file has no quoting to get wrong.
    $probe = Join-Path ([IO.Path]::GetTempPath()) 'hp_probe.py'
    @'
from hydrus_pipeline import config
def show(label, p):
    print("  %-13s %s -> %s" % (label, p, "FOUND" if p.exists() else "MISSING"))
print("  %-13s %s" % ("INSTALL_ROOT", config.INSTALL_ROOT))
show("HYDRUS_EXE", config.HYDRUS_EXE)
show("DATA_DIR", config.DATA_DIR)
show("REPO_DIR", config.HYDOWNLOADER_REPO_DIR)
print("  %-13s %s" % ("SYSTRAY_EXE", config.find_systray_exe()))
'@ | Set-Content -LiteralPath $probe -Encoding ASCII
    # ASCII, not UTF8: on Windows PowerShell 5.1 `-Encoding UTF8` prepends a BOM. That is
    # this project's documented footgun (it is why the PS1 -> Python port happened). The
    # probe is pure ASCII, so this sidesteps it entirely.

    Push-Location $Destination
    try {
        & $venvPy $probe
        if ($LASTEXITCODE -ne 0) {
            Write-Warn 'Config probe failed - the package did not import cleanly. Check the output above.'
        } else {
            Write-Ok 'Config paths still resolve to the untouched install root.'
        }
    } finally {
        Pop-Location
        Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
    }
}

# Leftover references to the old location, anywhere in the copied tree.
if (-not $DryRun) {
    $textFiles = Get-ChildItem -LiteralPath $Destination -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object {
            $_.FullName -notmatch '\\\.git\\' -and
            # This script legitimately contains the old path as its -Source default.
            $_.Name -ne 'Move-PipelineToICloud.ps1' -and
            $_.Extension -in '.py', '.bat', '.ps1', '.md', '.js', '.html', '.json', '.txt', '.cfg'
        }

    $stale = @()
    if ($textFiles) {
        $stale = @(Select-String -LiteralPath $textFiles.FullName -Pattern $Source -SimpleMatch -ErrorAction SilentlyContinue)
    }

    if ($stale.Count -gt 0) {
        Write-Warn 'Files still mentioning the old path (documentation only, harmless but worth tidying):'
        $stale | ForEach-Object { Write-Warn "  $($_.Filename):$($_.LineNumber)" }
    } else {
        Write-Ok 'No references to the old path remain in the copied tree.'
    }
}


# ------------------------------------------------------------------------- source removal
Write-Step 'Old folder'

if ($RemoveSource -and -not $DryRun) {
    Write-Warn "Deleting $Source"
    Remove-Item -LiteralPath $Source -Recurse -Force
    Write-Ok 'Old folder removed.'
} else {
    Write-Info "Left in place: $Source"
    Write-Info 'Confirm the app works from the new location first, then delete it yourself'
    Write-Info 'or re-run this script with -RemoveSource.'
}


Write-Host @"

  Done.

  Next steps
    1. In File Explorer, right-click the new folder and choose
       "Always Keep on This Device" so iCloud cannot evict the code.
    2. Double-click the "Hydrus Pipeline" Desktop shortcut.
    3. The dashboard should open at http://127.0.0.1:8765 with Hydrus, the daemon and
       the systray showing their usual status, and your subscriptions intact - they
       live in the database, which never moved.
    4. Once you are happy, delete: $Source

"@ -ForegroundColor White
