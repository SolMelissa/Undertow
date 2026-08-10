<#
  Configure-ApiKeys.ps1
  ----------------------
  On-demand credential configuration for the Hydrus pipeline: Reddit OAuth and the Hydrus
  Client API key. Shows what's already configured (masked) and only prompts for the ones
  you choose to (re)set - it never nags you automatically. Run this any time you want to
  add/replace a key (e.g. once Reddit's app creation is back up), or use option 2 to check
  whether gallery-dl's built-in shared Reddit client already covers what you need without
  registering a custom app at all.
#>

$InstallRoot   = "$env:USERPROFILE\HydrusPipeline"
$DataDir       = Join-Path $InstallRoot "hydownloader-data"
$HydrusApiUrl  = "http://localhost:45869"
$GalleryDlConfigFile = "$env:USERPROFILE\gallery-dl\config.json"
$ImportJobsFile = Join-Path $DataDir "hydownloader-import-jobs.py"

function Set-Utf8NoBom {
    # Windows PowerShell 5.1's `Set-Content -Encoding UTF8` silently prepends a UTF-8 BOM,
    # which Python's json module (and anything else strict about encoding) chokes on with
    # "Expecting value: line 1 column 1 (char 0)". Every file this script writes gets read
    # back by Python (gallery-dl's config, hydownloader-import-jobs.py) - never by something
    # that needs a BOM - so always write plain UTF-8 without one.
    param([string]$Path, [string]$Content)
    [System.IO.File]::WriteAllText($Path, $Content, (New-Object System.Text.UTF8Encoding($false)))
}

function Mask-Secret {
    param([string]$Value)
    if (-not $Value) { return "(not set)" }
    if ($Value.Length -le 8) { return "****" }
    return $Value.Substring(0,4) + "..." + $Value.Substring($Value.Length-4,4)
}

function Get-RedditStatus {
    try {
        $cfg = Get-Content $GalleryDlConfigFile -Raw | ConvertFrom-Json
        $cid = $cfg.extractor.reddit.'client-id'
        $tok = $cfg.extractor.reddit.'refresh-token'
        $configured = [bool]($cid -and $cid -ne "PLACEHOLDER_SET_BELOW")
        return [PSCustomObject]@{ Configured = $configured; ClientId = $cid; RefreshToken = $tok }
    } catch {
        return [PSCustomObject]@{ Configured = $false; ClientId = $null; RefreshToken = $null }
    }
}

function Get-HydrusKeyStatus {
    try {
        $content = Get-Content $ImportJobsFile -Raw -ErrorAction Stop
        if ($content -match 'defAPIKey\s*=\s*["'']([0-9a-fA-F]{64})["'']') {
            return [PSCustomObject]@{ Configured = $true; Key = $Matches[1] }
        }
    } catch {}
    return [PSCustomObject]@{ Configured = $false; Key = $null }
}

function Show-Status {
    $reddit = Get-RedditStatus
    $hydrus = Get-HydrusKeyStatus
    Write-Host ""
    Write-Host "=====================================================" -ForegroundColor Green
    Write-Host " CURRENT CREDENTIAL STATUS"
    Write-Host "====================================================="
    Write-Host "1) Reddit OAuth:   $(if ($reddit.Configured) {'configured - client-id ' + (Mask-Secret $reddit.ClientId)} else {'NOT configured (gallery-dl still works via its built-in shared client - see option 2)'})"
    Write-Host "2) Hydrus API key: $(if ($hydrus.Configured) {'configured - ' + (Mask-Secret $hydrus.Key)} else {'NOT configured'})"
    Write-Host "====================================================="
}

while ($true) {
    Show-Status
    Write-Host ""
    Write-Host "What do you want to do?"
    Write-Host "  [1] (Re)configure Reddit OAuth (custom app - better rate limits + private/quarantined subs)"
    Write-Host "  [2] Test Reddit downloads WITHOUT a custom app (uses gallery-dl's built-in shared client)"
    Write-Host "  [3] (Re)configure Hydrus API key"
    Write-Host "  [Q] Done / close"
    $choice = Read-Host "Choice"

    switch ($choice) {
        "1" {
            Write-Host ""
            Write-Host "Note: this step is optional. gallery-dl already ships with its own built-in, shared" -ForegroundColor Cyan
            Write-Host "OAuth client and uses it automatically if you skip this - that's normally enough for" -ForegroundColor Cyan
            Write-Host "downloading public subreddits/galleries. Registering your own app below only buys you" -ForegroundColor Cyan
            Write-Host "a private (non-shared) rate limit and access to quarantined/private subreddits. Try" -ForegroundColor Cyan
            Write-Host "option 2 first if you just want to confirm Reddit downloading works at all." -ForegroundColor Cyan
            Write-Host ""
            Write-Host "You need a Reddit 'installed app' to get OAuth working:"
            Write-Host "  1. Log into Reddit, go to https://www.reddit.com/prefs/apps"
            Write-Host "  2. Click 'create another app', choose type 'installed app' (NOT web app/script)"
            Write-Host "  3. Set redirect uri to: http://localhost:6414/"
            Write-Host "  4. Click 'create app', then copy the client ID (the string under the app name)"
            Write-Host ""
            Write-Host "If the CAPTCHA on that page won't load: this has been a known issue since Reddit" -ForegroundColor Yellow
            Write-Host "rolled out a 'Responsible Builder Policy' requiring manual approval for new apps" -ForegroundColor Yellow
            Write-Host "(mid-2026). File a ticket at https://support.reddithelp.com/hc/en-us/requests/new" -ForegroundColor Yellow
            Write-Host "(category: Developer Platform & Data API Usage) - describe the use case as personal," -ForegroundColor Yellow
            Write-Host "non-commercial archival, not scraping/resale. No published turnaround time." -ForegroundColor Yellow
            Start-Process "https://www.reddit.com/prefs/apps"
            $clientId = Read-Host "Paste your Reddit client ID here (or press Enter to cancel)"
            if ($clientId) {
                $redditUsername = Read-Host "Your Reddit username (for the User-Agent header)"
                $userAgent = "gallery-dl:hydownloader-pipeline:v1.0 (by /u/$redditUsername)"

                $cfg = Get-Content $GalleryDlConfigFile -Raw | ConvertFrom-Json
                $cfg.extractor.reddit.'client-id' = $clientId
                $cfg.extractor.reddit.'user-agent' = $userAgent
                Set-Utf8NoBom -Path $GalleryDlConfigFile -Content ($cfg | ConvertTo-Json -Depth 10)

                Write-Host ""
                Write-Host ">>> Running the OAuth authorization flow (will open a browser tab)..." -ForegroundColor Cyan
                gallery-dl oauth:reddit
                Write-Host ""
                Write-Host "Copy the 'refresh-token' gallery-dl just printed:" -ForegroundColor Yellow
                $refreshToken = Read-Host "Paste it here to save it automatically (or press Enter to edit the file by hand later)"
                if ($refreshToken) {
                    $cfg = Get-Content $GalleryDlConfigFile -Raw | ConvertFrom-Json
                    $cfg.extractor.reddit.'refresh-token' = $refreshToken
                    Set-Utf8NoBom -Path $GalleryDlConfigFile -Content ($cfg | ConvertTo-Json -Depth 10)
                    Write-Host "Saved. Reddit OAuth is now configured." -ForegroundColor Green
                } else {
                    Write-Host "OK - edit $GalleryDlConfigFile by hand later (extractor.reddit.refresh-token)."
                }
            } else {
                Write-Host "Cancelled."
            }
        }
        "2" {
            Write-Host ""
            Write-Host "This tests whether gallery-dl can already reach Reddit using its own built-in shared" -ForegroundColor Cyan
            Write-Host "OAuth client - no app registration needed. Uses --simulate, so nothing gets saved." -ForegroundColor Cyan
            $testSub = Read-Host "Subreddit to test against (Enter for 'pics')"
            if (-not $testSub) { $testSub = "pics" }
            Write-Host ""
            Write-Host ">>> Running: gallery-dl --simulate https://www.reddit.com/r/$testSub/ --range 1-3" -ForegroundColor Cyan
            gallery-dl --simulate "https://www.reddit.com/r/$testSub/" --range 1-3
            Write-Host ""
            Write-Host "If file URLs printed above with no errors: Reddit downloading already works right" -ForegroundColor Green
            Write-Host "now via the shared default client. You likely don't need option 1 unless you want" -ForegroundColor Green
            Write-Host "better rate limits or access to quarantined/private subreddits." -ForegroundColor Green
            Write-Host "If it errored (rate-limit / blocked / auth error) instead: option 1, or a support" -ForegroundColor Yellow
            Write-Host "ticket if the CAPTCHA is still broken, is the next step." -ForegroundColor Yellow
        }
        "3" {
            Write-Host ""
            Write-Host "Get this from Hydrus: services -> manage services -> enable Client API (port 45869),"
            Write-Host "then services -> review services -> Client API -> generate a new access key."
            $apiKey = Read-Host "Paste your Hydrus Client API access key now (or press Enter to cancel)"
            if ($apiKey) {
                if (Test-Path $ImportJobsFile) {
                    $jobsContent = Get-Content $ImportJobsFile -Raw
                    $jobsContent = $jobsContent -replace "(apiURL\s*=\s*)['`"][^'`"]*['`"]", "`$1`"$HydrusApiUrl`""
                    $jobsContent = $jobsContent -replace "(apiKey\s*=\s*)['`"][^'`"]*['`"]", "`$1`"$apiKey`""
                    Set-Utf8NoBom -Path $ImportJobsFile -Content $jobsContent
                    Write-Host "Saved. Hydrus API key is now configured." -ForegroundColor Green
                    Write-Host "(This does NOT touch hydownloader-config.json's own separate 'daemon.access-key' secret.)"
                } else {
                    Write-Host "$ImportJobsFile not found - has hydownloader been set up yet?" -ForegroundColor Yellow
                }
            } else {
                Write-Host "Cancelled."
            }
        }
        { $_ -in @("q","Q","quit","exit") } {
            Write-Host "Done."
            break
        }
        default {
            Write-Host "Not a valid choice." -ForegroundColor Yellow
        }
    }
    if ($choice -in @("q","Q","quit","exit")) { break }
}

Write-Host ""
Read-Host "Press Enter to continue"
