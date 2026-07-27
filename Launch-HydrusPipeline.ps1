<#
  Launch-HydrusPipeline.ps1
  --------------------------
  Daily-use launcher: makes sure Hydrus, the hydownloader daemon, and the hydownloader
  systray are all running (starting only whatever isn't already up - never launches a
  second copy of anything), then drops you at a menu to manage everything from the
  keyboard - add downloads/subscriptions, check queue status, manage existing
  subscriptions, and check/restart services - without needing to touch the systray GUI.

  A background watchdog polls every 90 seconds and silently restarts the hydownloader
  daemon or systray if either one crashes. Hydrus itself is NOT auto-restarted (closing
  it is usually deliberate) - use the Health check menu option if you want to bring it
  back up.

  This is the script the Desktop shortcut points to. It does NOT reinstall anything -
  for first-time setup or reinstalling a component, use Setup-HydrusPipeline.ps1.
#>

$ErrorActionPreference = "Stop"

$InstallRoot   = "$env:USERPROFILE\HydrusPipeline"
$HydrusDir     = Join-Path $InstallRoot "hydrus"
$HydownloaderRepoDir = Join-Path $InstallRoot "hydownloader"
$DataDir       = Join-Path $InstallRoot "hydownloader-data"
$LogsDir       = Join-Path $DataDir "logs"
$HydrusExe     = Join-Path $HydrusDir "hydrus_client.exe"
$HydownloaderConfigFile = Join-Path $DataDir "hydownloader-config.json"
$SystrayDir    = Join-Path $InstallRoot "hydownloader-systray"
$ScriptDir     = Split-Path -Parent $MyInvocation.MyCommand.Path

# Resolved once at startup - the systray exe lands inside a commit-hash-named subfolder,
# so we have to search for it. Reused by Start-RequiredServices and the watchdog.
$SystrayExeFound = Get-ChildItem -Path $SystrayDir -Filter "hydownloader-systray.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
$SystrayExe = if ($SystrayExeFound) { $SystrayExeFound.FullName } else { $null }

$WatchdogStatusFile = Join-Path $LogsDir "watchdog-status.json"
$WatchdogIntervalSeconds = 90

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class HydrusPipelineWin32 {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
}
"@

function Show-ProcessWindow {
    param([string]$ProcessName)
    $proc = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
    if ($proc) {
        if ([HydrusPipelineWin32]::IsIconic($proc.MainWindowHandle)) {
            [HydrusPipelineWin32]::ShowWindow($proc.MainWindowHandle, 9) | Out-Null
        }
        [HydrusPipelineWin32]::SetForegroundWindow($proc.MainWindowHandle) | Out-Null
        return $true
    }
    return $false
}

function Get-RunningHydownloaderProc {
    param([string]$Match)
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $Match }
}

# --- Service status + start/restart helpers ---

function Get-ServiceStatus {
    $hydrus = Get-Process -Name "hydrus_client" -ErrorAction SilentlyContinue
    $daemon = Get-RunningHydownloaderProc -Match "hydownloader-daemon"
    $systray = Get-Process -Name "hydownloader-systray" -ErrorAction SilentlyContinue
    return [PSCustomObject]@{
        HydrusRunning  = [bool]$hydrus
        HydrusPid      = if ($hydrus) { $hydrus[0].Id } else { $null }
        DaemonRunning  = [bool]$daemon
        DaemonPid      = if ($daemon) { $daemon[0].ProcessId } else { $null }
        SystrayRunning = [bool]$systray
        SystrayPid     = if ($systray) { $systray[0].Id } else { $null }
    }
}

function Start-RequiredServices {
    $status = Get-ServiceStatus

    if (-not $status.HydrusRunning) {
        if (Test-Path $HydrusExe) {
            Write-Host "  starting Hydrus..."
            Start-Process $HydrusExe
            Start-Sleep -Seconds 12
        } else {
            Write-Host "  Hydrus not found at $HydrusExe - has setup been run?" -ForegroundColor Yellow
        }
    } else {
        Write-Host "  Hydrus already running."
    }

    if (-not $status.DaemonRunning) {
        if (Test-Path $HydownloaderConfigFile) {
            Write-Host "  starting hydownloader daemon..."
            $daemonLog = Join-Path $LogsDir "daemon-launch-stdout.log"
            $daemonErrLog = Join-Path $LogsDir "daemon-launch-stderr.log"
            Start-Process -FilePath "python" -ArgumentList "-m poetry run hydownloader-daemon start --path `"$DataDir`"" -WorkingDirectory $HydownloaderRepoDir -WindowStyle Minimized -RedirectStandardOutput $daemonLog -RedirectStandardError $daemonErrLog | Out-Null
            Start-Sleep -Seconds 5
        } else {
            Write-Host "  hydownloader not set up yet - run Setup-HydrusPipeline.ps1 first." -ForegroundColor Yellow
        }
    } else {
        Write-Host "  hydownloader daemon already running."
    }

    if (-not $status.SystrayRunning) {
        if ($SystrayExe -and (Test-Path $SystrayExe) -and (Test-Path $HydownloaderConfigFile)) {
            Write-Host "  starting hydownloader systray..."
            Start-Process -FilePath $SystrayExe -WorkingDirectory (Split-Path $SystrayExe -Parent) | Out-Null
            Start-Sleep -Seconds 3
        } else {
            Write-Host "  hydownloader-systray not found - run Setup-HydrusPipeline.ps1 first." -ForegroundColor Yellow
        }
    } else {
        Write-Host "  hydownloader systray already running."
    }
}

# --- hydownloader daemon HTTP API ---
# Full endpoint reference: https://gitgud.io/thatfuckingbird/hydownloader/-/raw/master/docs/API.md

function Get-DaemonApiInfo {
    if (-not (Test-Path $HydownloaderConfigFile)) { return $null }
    try {
        $hdCfg = Get-Content $HydownloaderConfigFile -Raw | ConvertFrom-Json
        $port = $hdCfg.'daemon.port'; if (-not $port) { $port = 53211 }
        $hostName = $hdCfg.'daemon.host'; if (-not $hostName) { $hostName = "127.0.0.1" }
        $ssl = $hdCfg.'daemon.ssl'
        $accessKey = $hdCfg.'daemon.access-key'
        $serverPemExists = Test-Path (Join-Path $DataDir "server.pem")
        $scheme = if ($ssl -and $serverPemExists) { "https" } else { "http" }
        if (-not $accessKey) { return $null }
        return [PSCustomObject]@{ BaseUrl = "${scheme}://${hostName}:${port}"; AccessKey = $accessKey; Scheme = $scheme }
    } catch { return $null }
}

function Invoke-DaemonApi {
    # Wraps every call to the hydownloader daemon API. Returns [Success, Data, Error] instead
    # of throwing, so menu functions can show a friendly message instead of crashing the launcher.
    param(
        [Parameter(Mandatory)][string]$Route,
        $Body = $null,
        [int]$TimeoutSec = 8
    )
    $api = Get-DaemonApiInfo
    if (-not $api) {
        return [PSCustomObject]@{ Success = $false; Data = $null; Error = "hydownloader daemon API isn't reachable (no access key found - is the daemon set up/running?)" }
    }
    $irmParams = @{
        Uri = "$($api.BaseUrl)$Route"
        Method = "Post"
        Headers = @{ "HyDownloader-Access-Key" = $api.AccessKey }
        TimeoutSec = $TimeoutSec
        ErrorAction = "Stop"
    }
    if ($null -ne $Body) {
        $bodyJson = $Body | ConvertTo-Json -Depth 10 -Compress
        # ConvertTo-Json quirk (PS 5.1): a PowerShell array with exactly ONE element gets
        # unwrapped to a bare JSON object instead of a one-item JSON array - e.g. @(@{a=1})
        # serializes to {"a":1} instead of [{"a":1}]. hydownloader's batch routes (add/update
        # subscriptions, add URLs, etc.) expect a top-level JSON array, so a single-item add
        # was silently being sent as a bare object. Re-wrap it here whenever the source value
        # was actually a PS array but the JSON came out without the leading '[' - covers 1-item
        # arrays without double-wrapping 2+-item arrays (which already serialize correctly).
        if ($Body -is [array] -and $bodyJson.TrimStart()[0] -ne '[') {
            $bodyJson = "[$bodyJson]"
        }
        $irmParams.Body = $bodyJson
        $irmParams.ContentType = "application/json"
    }
    if ($api.Scheme -eq "https" -and $PSVersionTable.PSVersion.Major -ge 6) { $irmParams.SkipCertificateCheck = $true }
    try {
        $result = Invoke-RestMethod @irmParams
        return [PSCustomObject]@{ Success = $true; Data = $result; Error = $null }
    } catch {
        # For HTTP error responses (4xx/5xx), $_.Exception.Message is usually just the generic
        # status text ("The remote server returned an error: (500) Internal Server Error") -
        # the actually useful bit is the response body hydownloader sent back (its error
        # message / traceback), which explains WHY it was rejected instead of leaving it a
        # black box. In Windows PowerShell 5.1, Invoke-RestMethod already reads that body for
        # you into $_.ErrorDetails.Message - manually re-reading $_.Exception.Response's stream
        # after that fails silently (the stream's already been consumed), which is why this
        # used to come back empty. Prefer ErrorDetails; only fall back to manual stream reading
        # (e.g. for exception types where ErrorDetails isn't populated) if that's not there.
        $errDetail = $_.Exception.Message
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            $errDetail = "$errDetail -- $($_.ErrorDetails.Message)"
        } else {
            try {
                if ($_.Exception.Response) {
                    $stream = $_.Exception.Response.GetResponseStream()
                    if ($stream -and $stream.CanRead) {
                        $reader = New-Object System.IO.StreamReader($stream)
                        $respBody = $reader.ReadToEnd()
                        if ($respBody) { $errDetail = "$errDetail -- $respBody" }
                    }
                }
            } catch { }
        }
        return [PSCustomObject]@{ Success = $false; Data = $null; Error = $errDetail }
    }
}

function Test-DaemonBusy {
    # Returns $true (busy), $false (idle), or $null (couldn't tell - API unreachable).
    $resp = Invoke-DaemonApi -Route "/get_status_info"
    if (-not $resp.Success) { return $null }
    $status = $resp.Data
    $busy = ($status.urls_queued -gt 0) -or ($status.subscriptions_due -gt 0) -or ($status.autoimport_jobs_due -gt 0)
    return [bool]$busy
}

function Stop-IdleComponents {
    Write-Host ""
    Write-Host "Checking what's currently active before shutting anything down..." -ForegroundColor Cyan

    $daemonProc = Get-RunningHydownloaderProc -Match "hydownloader-daemon"
    if ($daemonProc) {
        $busy = Test-DaemonBusy
        if ($busy -eq $true) {
            Write-Host "  hydownloader daemon is busy (downloads/subscriptions in progress) - leaving it running." -ForegroundColor Yellow
        } elseif ($busy -eq $false) {
            Write-Host "  hydownloader daemon is idle - sending a graceful shutdown..." -ForegroundColor Cyan
            $result = Invoke-DaemonApi -Route "/shutdown"
            if ($result.Success) {
                Write-Host "  shutdown requested (it'll finish anything genuinely in-flight, then exit)." -ForegroundColor Green
            } else {
                Write-Host "  couldn't reach the daemon's API to shut it down cleanly - leaving it running." -ForegroundColor Yellow
            }
        } else {
            Write-Host "  couldn't determine daemon status (API unreachable) - leaving it running, just in case." -ForegroundColor Yellow
        }
    } else {
        Write-Host "  hydownloader daemon isn't running - nothing to do there."
    }

    # The systray is just a monitor/control UI with no work of its own to finish - safe to close.
    $systrayProc = Get-Process -Name "hydownloader-systray" -ErrorAction SilentlyContinue
    if ($systrayProc) {
        Write-Host "  closing hydownloader systray (it's just a monitor UI - nothing to lose)..."
        $systrayProc | Stop-Process -ErrorAction SilentlyContinue
    }

    Write-Host "  Hydrus itself is never auto-closed - close it yourself whenever you're done with it." -ForegroundColor Cyan
}

# --- Text-based download / subscription management ---

function Add-DownloadUrl {
    Write-Host ""
    Write-Host "Paste the URL to download (or several, separated by commas). Blank cancels:" -ForegroundColor Cyan
    $raw = Read-Host "URL(s)"
    if (-not $raw) { Write-Host "Cancelled."; return }
    $urls = $raw -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    if ($urls.Count -eq 0) { Write-Host "Cancelled."; return }

    $body = @($urls | ForEach-Object { @{ url = $_ } })
    $resp = Invoke-DaemonApi -Route "/add_or_update_urls" -Body $body
    if ($resp.Success -and $resp.Data.status) {
        Write-Host "Queued $($urls.Count) URL(s) for download." -ForegroundColor Green
    } else {
        $err = if ($resp.Error) { $resp.Error } else { "daemon rejected the request" }
        Write-Host "Failed to queue: $err" -ForegroundColor Red
    }
    Write-Host ""
    Read-Host "Press Enter to go back"
}

function Add-SingleSubscription {
    # Shared by Add-Subscription (single URL or comma-separated batch) and
    # Import-SubscriptionsBatch (CSV/text file batch) - one URL in, one result out, so all
    # three entry points detect/add/verify exactly the same way instead of drifting apart.
    #
    # -PromptOnExisting is only set from the single-URL interactive path: it asks "add another
    # one anyway?" and treats "no" as a cancel. Every batch path (comma-separated or file-based)
    # instead just silently skips already-subscribed URLs - stopping to ask per-row would defeat
    # the point of a batch.
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][double]$Hours,
        [string]$AdditionalData = $null,
        [switch]$PromptOnExisting
    )

    $infoResp = Invoke-DaemonApi -Route "/url_info" -Body @{ urls = @($Url) }
    if (-not $infoResp.Success) {
        return [PSCustomObject]@{ Status = "Failed"; Detail = "couldn't reach the daemon: $($infoResp.Error)"; Id = $null }
    }
    $info = $infoResp.Data[0]

    if ($info.sub_downloader) {
        if ($info.existing_subscriptions -and $info.existing_subscriptions.Count -gt 0) {
            if ($PromptOnExisting) {
                Write-Host "You already have a matching subscription:" -ForegroundColor Yellow
                $info.existing_subscriptions | ForEach-Object { Write-Host "  id=$($_.id)  paused=$($_.paused)" }
                $go = Read-Host "Add another one anyway? (y/N)"
                if ($go -ne "y") {
                    return [PSCustomObject]@{ Status = "Skipped"; Detail = "cancelled"; Id = $null }
                }
            } else {
                return [PSCustomObject]@{ Status = "Skipped"; Detail = "already subscribed (id=$($info.existing_subscriptions[0].id))"; Id = $null }
            }
        }
        $downloader = $info.sub_downloader
        $keywords = $info.sub_keywords
    } else {
        $downloader = "raw"
        $keywords = $Url
    }

    # hydownloader's actual subscriptions table column is "check_interval" (seconds, despite
    # the bare name - see get_due_subscription_ids in db.py: max(check_interval, 60)). This
    # script was sending "check_interval_seconds" for every add, which doesn't exist as a
    # column at all - the daemon threw sqlite3.OperationalError on every single request and
    # Bottle turned that into a bare 500 with no detail in the HTTP response (the real
    # traceback only shows up in hydownloader-data\logs\daemon-launch-stderr.log). This was
    # almost certainly the actual cause of adds failing from the very start.
    $subEntry = @{ downloader = $downloader; keywords = $keywords; check_interval = [int]($Hours * 3600) }
    if ($AdditionalData) { $subEntry.additional_data = $AdditionalData }

    $addResp = Invoke-DaemonApi -Route "/add_or_update_subscriptions" -Body @($subEntry)
    if (-not ($addResp.Success -and $addResp.Data.status)) {
        $err = if ($addResp.Error) { $addResp.Error } else { "daemon rejected the request" }
        return [PSCustomObject]@{ Status = "Failed"; Detail = $err; Id = $null }
    }

    # The daemon reporting {"status":"ok"} only means it accepted and processed the request -
    # not that a new row actually landed in its subscriptions table (a bad downloader value,
    # a silently-skipped duplicate, etc. can still report ok). Re-fetch the live list and
    # confirm this exact downloader/keywords pair is actually in it before calling it added.
    Start-Sleep -Milliseconds 200
    $verifyResp = Invoke-DaemonApi -Route "/get_subscriptions" -Body @{}
    $match = $null
    if ($verifyResp.Success -and $verifyResp.Data) {
        $match = $verifyResp.Data | Where-Object { $_.downloader -eq $downloader -and $_.keywords -eq $keywords } | Select-Object -Last 1
    }
    if ($match) {
        return [PSCustomObject]@{ Status = "Added"; Detail = "$downloader / $keywords"; Id = $match.id }
    } else {
        return [PSCustomObject]@{ Status = "Failed"; Detail = "daemon said ok but it's not showing up in /get_subscriptions afterward"; Id = $null }
    }
}

function Add-Subscription {
    Write-Host ""
    Write-Host "Paste a URL to the artist/gallery/user/search you want to keep checking for new files." -ForegroundColor Cyan
    Write-Host "(paste several, separated by commas, to add them all as a batch.)" -ForegroundColor Cyan
    Write-Host "hydownloader will auto-detect the site; if it's not a site it recognizes for subscriptions," -ForegroundColor Cyan
    Write-Host "it'll still work by re-checking the exact URL on an interval. Blank cancels:" -ForegroundColor Cyan
    $raw = Read-Host "URL(s)"
    if (-not $raw) { Write-Host "Cancelled."; return }
    $urls = $raw -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    if ($urls.Count -eq 0) { Write-Host "Cancelled."; return }

    $hoursInput = Read-Host "Check how often, in hours? [24]"
    $hours = 24.0
    if ($hoursInput) {
        $parsed = 0.0
        if ([double]::TryParse($hoursInput, [ref]$parsed) -and $parsed -gt 0) { $hours = $parsed }
    }

    if ($urls.Count -eq 1) {
        $result = Add-SingleSubscription -Url $urls[0] -Hours $hours -PromptOnExisting
        switch ($result.Status) {
            "Added"   { Write-Host "Subscription added: id=$($result.Id)  $($result.Detail) (checks every $hours h)." -ForegroundColor Green }
            "Skipped" { Write-Host "Skipped: $($result.Detail)" -ForegroundColor Yellow }
            "Failed"  { Write-Host "Failed to add subscription: $($result.Detail)" -ForegroundColor Red }
        }
        Write-Host ""
        Read-Host "Press Enter to go back"
        return
    }

    # More than one URL - run them as a batch, same detection/verify per URL, one shared
    # check-interval for all of them (matches what was asked once, above).
    Write-Host ""
    Write-Host "Adding $($urls.Count) subscriptions (checking every $hours h)..." -ForegroundColor Cyan
    $added = 0; $skipped = 0; $failed = @()
    $i = 0
    foreach ($u in $urls) {
        $i++
        Write-Host ""
        Write-Host "[$i/$($urls.Count)] $u" -ForegroundColor Cyan
        $result = Add-SingleSubscription -Url $u -Hours $hours
        switch ($result.Status) {
            "Added"   { Write-Host "  added: id=$($result.Id)  $($result.Detail)" -ForegroundColor Green; $added++ }
            "Skipped" { Write-Host "  skipped: $($result.Detail)" -ForegroundColor Yellow; $skipped++ }
            "Failed"  { Write-Host "  failed: $($result.Detail)" -ForegroundColor Red; $failed += $u }
        }
    }

    Write-Host ""
    Write-Host "=====================================================" -ForegroundColor Green
    Write-Host " DONE: $added added, $skipped already subscribed, $($failed.Count) failed"
    Write-Host "====================================================="
    if ($failed.Count -gt 0) {
        Write-Host "Failed / couldn't add:" -ForegroundColor Red
        $failed | ForEach-Object { Write-Host "  $_" }
    }
    Write-Host ""
    Read-Host "Press Enter to go back"
}

function Import-SubscriptionsBatch {
    # File-based batch entry point - same Add-SingleSubscription helper as the comma-separated
    # path in Add-Subscription, just sourced from a CSV/text file instead of one prompt.
    Write-Host ""
    Write-Host "Batch-add subscriptions from a CSV or plain text file." -ForegroundColor Cyan
    Write-Host "CSV: needs a 'url' column; optional 'hours' and 'additional_data' columns override the defaults per row." -ForegroundColor Cyan
    Write-Host "Plain text: one URL per line. Blank lines and lines starting with # are skipped." -ForegroundColor Cyan
    $path = Read-Host "Path to file"
    if (-not $path) { Write-Host "Cancelled."; return }
    $path = $path.Trim('"', "'", ' ')
    if (-not (Test-Path $path)) {
        Write-Host "File not found: $path" -ForegroundColor Yellow
        Read-Host "Press Enter to go back"
        return
    }

    # Try it as a CSV with a 'url' column first; anything without that header is treated as
    # a plain list of URLs (one per non-blank, non-comment line) instead.
    $rows = @()
    $csvRows = $null
    try { $csvRows = @(Import-Csv -Path $path -ErrorAction Stop) } catch { $csvRows = $null }
    if ($csvRows -and $csvRows.Count -gt 0 -and ($csvRows[0].PSObject.Properties.Name -contains "url")) {
        foreach ($r in $csvRows) {
            if (-not $r.url) { continue }
            $rows += [PSCustomObject]@{ Url = $r.url.Trim(); Hours = $r.hours; AdditionalData = $r.additional_data }
        }
    } else {
        Get-Content -Path $path | ForEach-Object {
            $line = $_.Trim()
            if ($line -and -not $line.StartsWith('#')) {
                $rows += [PSCustomObject]@{ Url = $line; Hours = $null; AdditionalData = $null }
            }
        }
    }

    if ($rows.Count -eq 0) {
        Write-Host "No URLs found in that file." -ForegroundColor Yellow
        Read-Host "Press Enter to go back"
        return
    }

    Write-Host ""
    Write-Host "Found $($rows.Count) URL(s)." -ForegroundColor Cyan
    $defaultHoursInput = Read-Host "Default check interval in hours, for rows without their own 'hours' [24]"
    $defaultHours = 24.0
    if ($defaultHoursInput) {
        $parsed = 0.0
        if ([double]::TryParse($defaultHoursInput, [ref]$parsed) -and $parsed -gt 0) { $defaultHours = $parsed }
    }

    $confirm = Read-Host "Add $($rows.Count) subscription(s) now? (y/N)"
    if ($confirm -ne "y") { Write-Host "Cancelled."; return }

    $added = 0; $skipped = 0; $failed = @()
    $i = 0

    foreach ($row in $rows) {
        $i++
        Write-Host ""
        Write-Host "[$i/$($rows.Count)] $($row.Url)" -ForegroundColor Cyan

        $rowHours = $defaultHours
        if ($row.Hours) {
            $parsed = 0.0
            if ([double]::TryParse($row.Hours, [ref]$parsed) -and $parsed -gt 0) { $rowHours = $parsed }
        }

        $result = Add-SingleSubscription -Url $row.Url -Hours $rowHours -AdditionalData $row.AdditionalData
        switch ($result.Status) {
            "Added"   { Write-Host "  added: id=$($result.Id)  $($result.Detail)" -ForegroundColor Green; $added++ }
            "Skipped" { Write-Host "  skipped: $($result.Detail)" -ForegroundColor Yellow; $skipped++ }
            "Failed"  { Write-Host "  failed: $($result.Detail)" -ForegroundColor Red; $failed += $row.Url }
        }
    }

    Write-Host ""
    Write-Host "=====================================================" -ForegroundColor Green
    Write-Host " BATCH IMPORT DONE: $added added, $skipped already subscribed, $($failed.Count) failed"
    Write-Host "====================================================="
    if ($failed.Count -gt 0) {
        Write-Host "Failed / couldn't add:" -ForegroundColor Red
        $failed | ForEach-Object { Write-Host "  $_" }
    }
    Write-Host ""
    Read-Host "Press Enter to go back"
}

function Show-QueueStatus {
    $statusResp = Invoke-DaemonApi -Route "/get_status_info"
    if (-not $statusResp.Success) {
        Write-Host ""
        Write-Host "Couldn't reach the hydownloader daemon: $($statusResp.Error)" -ForegroundColor Yellow
        Read-Host "Press Enter to go back"
        return
    }
    $s = $statusResp.Data
    Write-Host ""
    Write-Host "=====================================================" -ForegroundColor Green
    Write-Host " DOWNLOAD QUEUE STATUS"
    Write-Host "====================================================="
    Write-Host "URL worker:          $($s.url_worker_status)$(if ($s.urls_paused) {'  [PAUSED]'})"
    Write-Host "Subscription worker: $($s.subscription_worker_status)$(if ($s.subscriptions_paused) {'  [PAUSED]'})"
    Write-Host "URLs queued:         $($s.urls_queued)"
    Write-Host "Subscriptions due:   $($s.subscriptions_due)"

    $urlsResp = Invoke-DaemonApi -Route "/get_queued_urls"
    if ($urlsResp.Success -and $urlsResp.Data) {
        $pending = $urlsResp.Data | Where-Object { $_.status -eq -1 }
        $errored = $urlsResp.Data | Where-Object { $_.status -gt 0 } | Sort-Object id -Descending | Select-Object -First 5
        Write-Host ""
        Write-Host "Pending single URLs: $($pending.Count)"
        if ($errored.Count -gt 0) {
            Write-Host "Recent errors:" -ForegroundColor Yellow
            $errored | ForEach-Object { Write-Host "  [$($_.id)] $($_.url) -> $($_.result_status)" }
        }
    }

    $subsResp = Invoke-DaemonApi -Route "/get_subscriptions"
    if ($subsResp.Success -and $subsResp.Data) {
        $due = $subsResp.Data | Where-Object { $_.due }
        Write-Host ""
        Write-Host "Subscriptions due for check: $($due.Count)"
        $due | Select-Object -First 5 | ForEach-Object { Write-Host "  [$($_.id)] $($_.downloader) / $($_.keywords)" }
    }
    Write-Host "====================================================="
    Write-Host ""
    $go = Read-Host "Watch live instead of a snapshot? (y/N)"
    if ($go -eq "y") {
        Watch-WorkerStatus
    } else {
        Read-Host "Press Enter to go back"
    }
}

function Watch-WorkerStatus {
    # /get_status_info is a snapshot - Show-QueueStatus only shows what the worker was doing
    # at the instant you asked, and a check on a small account can start and finish in under a
    # second, so the snapshot easily misses it entirely. This instead polls the same route once
    # a second and only prints a line when the status text actually changes, so you get a live
    # feed of "checking subscription: X..." -> "finished checking subscription: X..., new
    # files: N" instead of having to keep manually re-triggering the menu and hoping to catch it.
    Write-Host ""
    Write-Host "Watching URL / subscription worker status live - press any key to stop." -ForegroundColor Cyan
    Write-Host ""
    $lastSubStatus = $null
    $lastUrlStatus = $null
    $consecutiveFailures = 0
    while ($true) {
        if ([Console]::KeyAvailable) {
            [Console]::ReadKey($true) | Out-Null
            break
        }
        $resp = Invoke-DaemonApi -Route "/get_status_info" -TimeoutSec 5
        if ($resp.Success -and $resp.Data) {
            $consecutiveFailures = 0
            $s = $resp.Data
            $ts = Get-Date -Format 'HH:mm:ss'
            if ($s.subscription_worker_status -ne $lastSubStatus) {
                Write-Host "[$ts] subscriptions: $($s.subscription_worker_status)" -ForegroundColor Green
                $lastSubStatus = $s.subscription_worker_status
            }
            if ($s.url_worker_status -ne $lastUrlStatus) {
                Write-Host "[$ts] urls:          $($s.url_worker_status)" -ForegroundColor Cyan
                $lastUrlStatus = $s.url_worker_status
            }
        } else {
            $consecutiveFailures++
            Write-Host "[$(Get-Date -Format 'HH:mm:ss')] couldn't reach daemon: $($resp.Error)" -ForegroundColor Yellow
            # Don't spin forever hammering a daemon that's actually down - bail out after a
            # run of failures instead of silently polling a dead endpoint until manually killed.
            if ($consecutiveFailures -ge 5) {
                Write-Host "Daemon unreachable 5 times in a row - stopping." -ForegroundColor Red
                break
            }
        }
        Start-Sleep -Milliseconds 1000
    }
    Write-Host ""
    Write-Host "Stopped watching." -ForegroundColor Cyan
    Read-Host "Press Enter to go back"
}

function Show-Subscriptions {
    # hydownloader's /get_subscriptions handler (daemon.py route_get_subscriptions) does
    # `'from' in bottle.request.json` with no null-check - if no JSON body is sent at all,
    # bottle.request.json is None and that line throws (bare 500, no response body). Sending
    # an empty object keeps it a dict so the check just evaluates to false instead of crashing.
    $resp = Invoke-DaemonApi -Route "/get_subscriptions" -Body @{}
    if (-not $resp.Success) {
        Write-Host ""
        Write-Host "Couldn't reach the hydownloader daemon: $($resp.Error)" -ForegroundColor Yellow
        return $null
    }
    $subs = @($resp.Data | Sort-Object id)
    Write-Host ""
    if ($subs.Count -eq 0) {
        Write-Host "No subscriptions yet."
        return $subs
    }
    Write-Host ("{0,-5} {1,-14} {2,-32} {3,-7} {4,-5} {5}" -f "ID", "Downloader", "Keywords", "Paused", "Due", "Last result")
    foreach ($sub in $subs) {
        $kw = [string]$sub.keywords
        if ($kw.Length -gt 32) { $kw = $kw.Substring(0, 29) + "..." }
        Write-Host ("{0,-5} {1,-14} {2,-32} {3,-7} {4,-5} {5}" -f $sub.id, $sub.downloader, $kw, $sub.paused, $sub.due, $sub.last_result_status)
    }
    return $subs
}

function Get-SubscriptionById {
    # Shared verify helper for Manage-Subscriptions - same "don't trust {"status":"ok"},
    # re-fetch and check" philosophy as Add-SingleSubscription's verify-after-add. Returns
    # $null if unreachable or not found (both mean "can't confirm it").
    param([Parameter(Mandatory)][int]$Id)
    $resp = Invoke-DaemonApi -Route "/get_subscriptions" -Body @{}
    if (-not ($resp.Success -and $resp.Data)) { return $null }
    return $resp.Data | Where-Object { $_.id -eq $Id } | Select-Object -First 1
}

function Manage-Subscriptions {
    $subs = Show-Subscriptions
    if (-not $subs -or $subs.Count -eq 0) {
        Write-Host ""
        Read-Host "Press Enter to go back"
        return
    }
    Write-Host ""
    Write-Host "[P] Pause one   [R] Resume one   [D] Delete one   [Enter] Back"
    $action = Read-Host "Choice"
    if (-not $action) { return }

    $idInput = Read-Host "Subscription ID"
    $id = 0
    if (-not [int]::TryParse($idInput, [ref]$id)) {
        Write-Host "Not a valid ID." -ForegroundColor Yellow
        Read-Host "Press Enter to go back"
        return
    }

    switch ($action.ToUpper()) {
        "P" {
            $r = Invoke-DaemonApi -Route "/add_or_update_subscriptions" -Body @(@{ id = $id; paused = $true })
            if (-not ($r.Success -and $r.Data.status)) {
                Write-Host "Failed: $($r.Error)" -ForegroundColor Red
            } else {
                Start-Sleep -Milliseconds 200
                $sub = Get-SubscriptionById -Id $id
                if ($sub -and $sub.paused) {
                    Write-Host "Paused subscription $id." -ForegroundColor Green
                } else {
                    Write-Host "Daemon said ok, but subscription $id isn't showing as paused in /get_subscriptions - treating as failed." -ForegroundColor Red
                }
            }
        }
        "R" {
            $r = Invoke-DaemonApi -Route "/add_or_update_subscriptions" -Body @(@{ id = $id; paused = $false })
            if (-not ($r.Success -and $r.Data.status)) {
                Write-Host "Failed: $($r.Error)" -ForegroundColor Red
            } else {
                Start-Sleep -Milliseconds 200
                $sub = Get-SubscriptionById -Id $id
                if ($sub -and -not $sub.paused) {
                    Write-Host "Resumed subscription $id." -ForegroundColor Green
                } else {
                    Write-Host "Daemon said ok, but subscription $id isn't showing as resumed in /get_subscriptions - treating as failed." -ForegroundColor Red
                }
            }
        }
        "D" {
            $confirm = Read-Host "Really delete subscription ${id}: this only removes it from hydownloader, already-downloaded files are unaffected. (y/N)"
            if ($confirm -eq "y") {
                $r = Invoke-DaemonApi -Route "/delete_subscriptions" -Body @{ ids = @($id) }
                if (-not ($r.Success -and $r.Data.status)) {
                    Write-Host "Failed: $($r.Error)" -ForegroundColor Red
                } else {
                    Start-Sleep -Milliseconds 200
                    $sub = Get-SubscriptionById -Id $id
                    if (-not $sub) {
                        Write-Host "Deleted subscription $id." -ForegroundColor Green
                    } else {
                        Write-Host "Daemon said ok, but subscription $id is still showing up in /get_subscriptions - treating as failed." -ForegroundColor Red
                    }
                }
            } else {
                Write-Host "Cancelled."
            }
        }
        default { Write-Host "Not a valid choice." -ForegroundColor Yellow }
    }
    Write-Host ""
    Read-Host "Press Enter to go back"
}

function Test-GalleryDlOnPath {
    # Checks whether gallery-dl actually resolves on PATH. If not, tries to explain why -
    # specifically the "two different Python installs" trap: `pip install --user gallery-dl`
    # can land gallery-dl.exe in a Roaming\Python\PythonXXX\Scripts folder that never gets
    # added to PATH, while a totally different Python install's Scripts folder (e.g.
    # C:\PythonXXX\Scripts) is what's actually on PATH - so `python`/`pip` work fine but
    # `gallery-dl` doesn't, and it looks like a broken install when it's really just PATH.
    $cmd = Get-Command gallery-dl -ErrorAction SilentlyContinue
    if ($cmd) {
        return [PSCustomObject]@{ OnPath = $true; ResolvedPath = $cmd.Source; UserInstallPath = $null; Hint = $null }
    }

    $hint = $null
    $userInstallPath = $null
    try {
        $pipShow = & python -m pip show gallery-dl 2>$null
        $locationLine = $pipShow | Where-Object { $_ -match '^Location:\s*(.+)$' } | Select-Object -First 1
        if ($locationLine) {
            $sitePackages = ($locationLine -replace '^Location:\s*', '').Trim()
            # site-packages sits next to Scripts under the same PythonXXX root
            $pythonRoot = Split-Path $sitePackages -Parent
            $candidateScripts = Join-Path $pythonRoot "Scripts"
            $candidateExe = Join-Path $candidateScripts "gallery-dl.exe"
            if (Test-Path $candidateExe) {
                $userInstallPath = $candidateScripts
                $hint = "gallery-dl.exe exists at $candidateExe but that folder isn't on PATH."
            }
        }
    } catch {}

    return [PSCustomObject]@{ OnPath = $false; ResolvedPath = $null; UserInstallPath = $userInstallPath; Hint = $hint }
}

function Invoke-HealthCheck {
    Write-Host ""
    Write-Host "Checking service status..." -ForegroundColor Cyan
    $status = Get-ServiceStatus
    Write-Host "  Hydrus:               $(if ($status.HydrusRunning) {"running (PID $($status.HydrusPid))"} else {"NOT running"})"
    Write-Host "  hydownloader daemon:  $(if ($status.DaemonRunning) {"running (PID $($status.DaemonPid))"} else {"NOT running"})"
    Write-Host "  hydownloader systray: $(if ($status.SystrayRunning) {"running (PID $($status.SystrayPid))"} else {"NOT running"})"

    if (-not $status.HydrusRunning -or -not $status.DaemonRunning -or -not $status.SystrayRunning) {
        Write-Host ""
        $go = Read-Host "Restart whatever's down? (Y/n)"
        if ($go -ne "n") {
            Start-RequiredServices
        }
    } else {
        Write-Host ""
        Write-Host "Everything's up." -ForegroundColor Green
    }

    Write-Host ""
    Write-Host "Checking gallery-dl..." -ForegroundColor Cyan
    $gdl = Test-GalleryDlOnPath
    if ($gdl.OnPath) {
        Write-Host "  gallery-dl: found on PATH ($($gdl.ResolvedPath))" -ForegroundColor Green
    } elseif ($gdl.UserInstallPath) {
        Write-Host "  gallery-dl: installed but NOT on PATH" -ForegroundColor Yellow
        Write-Host "    $($gdl.Hint)" -ForegroundColor Yellow
        Write-Host "    Fix: [Environment]::SetEnvironmentVariable('Path', `$env:Path + ';$($gdl.UserInstallPath)', 'User')" -ForegroundColor Yellow
        Write-Host "    ...then close this window and open a brand new one (PATH only refreshes on new sessions)." -ForegroundColor Yellow
    } else {
        Write-Host "  gallery-dl: NOT found (not on PATH, and 'pip show' couldn't locate an install either)." -ForegroundColor Red
        Write-Host "    Run 'python -m pip install --user --upgrade gallery-dl' or re-run Setup-HydrusPipeline.ps1." -ForegroundColor Red
    }

    # A "PythonXXX" folder being on PATH doesn't guarantee it's the SAME Python install that
    # pip/python resolve to elsewhere - that mismatch is exactly what caused the gallery-dl
    # PATH gap above. Flag it so it doesn't quietly bite again with some other pip package.
    $pythonOnPath = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonOnPath) {
        $pathDirsWithPython = ($env:Path -split ';') | Where-Object { $_ -match 'Python\d' } | Select-Object -Unique
        if ($pathDirsWithPython.Count -gt 1) {
            Write-Host "  Note: multiple Python-related folders on PATH ($($pathDirsWithPython -join ', ')) - if a future 'pip install --user' package goes missing, check it landed in the same install 'python' resolves to ($($pythonOnPath.Source))." -ForegroundColor Yellow
        }
    }

    if (Test-Path $WatchdogStatusFile) {
        try {
            $wd = Get-Content $WatchdogStatusFile -Raw | ConvertFrom-Json
            Write-Host ""
            Write-Host "Background watchdog (checks every ${WatchdogIntervalSeconds}s, auto-restarts the daemon/systray only) last ran at $($wd.LastCheckLocal)."
            if ($wd.Actions -and $wd.Actions.Count -gt 0) {
                Write-Host "Its last check took action:"
                $wd.Actions | ForEach-Object { Write-Host "  - $_" }
            }
        } catch {}
    }
    Write-Host ""
    Read-Host "Press Enter to go back"
}

# ============================================================
# STARTUP
# ============================================================

Write-Host "Checking Hydrus pipeline services..." -ForegroundColor Cyan
Start-RequiredServices

# --- Background watchdog: restarts the daemon/systray (not Hydrus - closing that is usually
#     deliberate) if either one crashes. Runs on its own isolated runspace, so it can only see
#     what's handed to it via -MessageData, not this script's functions/variables. ---
$watchdogTimer = New-Object System.Timers.Timer
$watchdogTimer.Interval = $WatchdogIntervalSeconds * 1000
$watchdogTimer.AutoReset = $true

$null = Register-ObjectEvent -InputObject $watchdogTimer -EventName Elapsed -SourceIdentifier "HydrusPipelineWatchdog" -Action {
    $md = $Event.MessageData
    try {
        $actions = @()

        $daemonProc = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -match "hydownloader-daemon" }
        if (-not $daemonProc -and (Test-Path $md.HydownloaderConfigFile)) {
            $dLog = Join-Path $md.LogsDir "daemon-launch-stdout.log"
            $dErrLog = Join-Path $md.LogsDir "daemon-launch-stderr.log"
            Start-Process -FilePath "python" -ArgumentList "-m poetry run hydownloader-daemon start --path `"$($md.DataDir)`"" -WorkingDirectory $md.HydownloaderRepoDir -WindowStyle Minimized -RedirectStandardOutput $dLog -RedirectStandardError $dErrLog | Out-Null
            $actions += "restarted hydownloader daemon (was down)"
        }

        $systrayProc = Get-Process -Name "hydownloader-systray" -ErrorAction SilentlyContinue
        if (-not $systrayProc -and $md.SystrayExe -and (Test-Path $md.SystrayExe)) {
            Start-Process -FilePath $md.SystrayExe -WorkingDirectory (Split-Path $md.SystrayExe -Parent) | Out-Null
            $actions += "restarted hydownloader systray (was down)"
        }

        $statusObj = [PSCustomObject]@{
            LastCheckLocal = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
            HydrusRunning  = [bool](Get-Process -Name "hydrus_client" -ErrorAction SilentlyContinue)
            Actions        = $actions
        }
        $statusObj | ConvertTo-Json | Set-Content -Path $md.WatchdogStatusFile -Encoding UTF8

        if ($actions.Count -gt 0) {
            Write-Host ""
            foreach ($a in $actions) { Write-Host "[watchdog $(Get-Date -Format 'HH:mm:ss')] $a" -ForegroundColor Magenta }
        }
    } catch {}
} -MessageData @{
    HydownloaderConfigFile = $HydownloaderConfigFile
    HydownloaderRepoDir    = $HydownloaderRepoDir
    DataDir                = $DataDir
    LogsDir                = $LogsDir
    SystrayExe             = $SystrayExe
    WatchdogStatusFile     = $WatchdogStatusFile
}

$watchdogTimer.Start()

# Also catch the window simply being closed (X button), Ctrl+C, etc. - not just the [Q] menu
# choice. PowerShell.Exiting fires on every kind of exit, but its action runs in an isolated
# runspace with no access to this script's functions/variables, so it has to be fully
# self-contained; we hand it what it needs via -MessageData.
$null = Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action {
    $md = $Event.MessageData
    try {
        if (-not (Test-Path $md.ConfigFile)) { return }
        $hdCfg = Get-Content $md.ConfigFile -Raw | ConvertFrom-Json
        $port = $hdCfg.'daemon.port'; if (-not $port) { $port = 53211 }
        $hostName = $hdCfg.'daemon.host'; if (-not $hostName) { $hostName = "127.0.0.1" }
        $ssl = $hdCfg.'daemon.ssl'
        $accessKey = $hdCfg.'daemon.access-key'
        $serverPemExists = Test-Path (Join-Path $md.DataDir "server.pem")
        $scheme = if ($ssl -and $serverPemExists) { "https" } else { "http" }
        if ($accessKey) {
            $headers = @{ "HyDownloader-Access-Key" = $accessKey }
            $base = "${scheme}://${hostName}:${port}"
            $irmParams = @{ Uri = "$base/get_status_info"; Method = "Post"; Headers = $headers; TimeoutSec = 3; ErrorAction = "Stop" }
            if ($scheme -eq "https" -and $PSVersionTable.PSVersion.Major -ge 6) { $irmParams.SkipCertificateCheck = $true }
            try {
                $status = Invoke-RestMethod @irmParams
                $busy = ($status.urls_queued -gt 0) -or ($status.subscriptions_due -gt 0) -or ($status.autoimport_jobs_due -gt 0)
                if (-not $busy) {
                    $shutParams = @{ Uri = "$base/shutdown"; Method = "Post"; Headers = $headers; TimeoutSec = 3; ErrorAction = "Stop" }
                    if ($scheme -eq "https" -and $PSVersionTable.PSVersion.Major -ge 6) { $shutParams.SkipCertificateCheck = $true }
                    Invoke-RestMethod @shutParams | Out-Null
                }
            } catch {}
        }
    } catch {}
    try {
        $systrayProc = Get-Process -Name "hydownloader-systray" -ErrorAction SilentlyContinue
        if ($systrayProc) { $systrayProc | Stop-Process -ErrorAction SilentlyContinue }
    } catch {}
    try {
        Unregister-Event -SourceIdentifier "HydrusPipelineWatchdog" -ErrorAction SilentlyContinue
    } catch {}
} -MessageData @{ ConfigFile = $HydownloaderConfigFile; DataDir = $DataDir }

Write-Host ""
Write-Host "Ready." -ForegroundColor Green

# ============================================================
# MENU
# ============================================================

while ($true) {
    $status = Get-ServiceStatus
    Write-Host ""
    Write-Host "====================================================="
    Write-Host " HYDRUS PIPELINE"
    Write-Host "====================================================="
    Write-Host " Hydrus: $(if ($status.HydrusRunning) {'up'} else {'DOWN'})   daemon: $(if ($status.DaemonRunning) {'up'} else {'DOWN'})   systray: $(if ($status.SystrayRunning) {'up'} else {'DOWN'})"
    Write-Host "====================================================="
    Write-Host "  DOWNLOAD (text/API)"
    Write-Host "  [1] Add a URL to download (one-off)"
    Write-Host "  [2] Subscribe to a gallery / artist / user (recurring - comma-separate for a batch)"
    Write-Host "  [3] View download queue & recent activity"
    Write-Host "  [4] Manage subscriptions (list / pause / resume / delete)"
    Write-Host "  [9] Batch-import subscriptions from a CSV / text file"
    Write-Host "  [10] Watch live worker status (see checks happen in real time)"
    Write-Host ""
    Write-Host "  HYDRUS (GUI)"
    Write-Host "  [5] Organize / Browse - bring Hydrus to the front"
    Write-Host "  [6] Search / Discover  - open the systray"
    Write-Host ""
    Write-Host "  SYSTEM (text)"
    Write-Host "  [7] Health check - service status, restart anything crashed, verify gallery-dl"
    Write-Host "  [8] Configure API keys"
    Write-Host "  [Q] Quit - shuts down anything idle, leaves anything busy running"
    Write-Host "====================================================="
    $choice = Read-Host "Choice"

    switch ($choice) {
        "1" { Add-DownloadUrl }
        "2" { Add-Subscription }
        "3" { Show-QueueStatus }
        "4" { Manage-Subscriptions }
        "9" { Import-SubscriptionsBatch }
        "10" { Watch-WorkerStatus }
        "5" {
            # Organize/Tag and View/Browse used to be two separate menu entries, but both
            # just brought the same Hydrus window to the front - consolidated into one.
            if (-not (Show-ProcessWindow "hydrus_client")) {
                Write-Host "Hydrus window not found - is it running?" -ForegroundColor Yellow
            } else {
                Write-Host "Tip: use the tag panel on the right of any search page to add/remove tags on selected files."
                Write-Host "Tip: type tags into the search box (top left) or leave it blank and hit the search icon to browse everything."
            }
        }
        "6" {
            if (-not (Show-ProcessWindow "hydownloader-systray")) {
                Write-Host "Systray window not found - is it running? Check the log files under $LogsDir." -ForegroundColor Yellow
            }
        }
        "7" { Invoke-HealthCheck }
        "8" {
            & (Join-Path $ScriptDir "Configure-ApiKeys.ps1")
        }
        { $_ -in @("q","Q","quit","exit") } {
            Stop-IdleComponents
            Write-Host ""
            Write-Host "Done. You can close this window now."
            break
        }
        default {
            Write-Host "Not a valid choice." -ForegroundColor Yellow
        }
    }
    if ($choice -in @("q","Q","quit","exit")) { break }
}
