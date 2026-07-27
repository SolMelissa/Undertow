<#
  Create-DesktopShortcut.ps1
  ----------------------------
  One-time helper: creates a "Hydrus Pipeline" shortcut on the Desktop that runs
  Launch-HydrusPipeline.ps1. Safe to re-run - just overwrites the shortcut.
#>

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$LauncherPs1 = Join-Path $ScriptDir "Launch-HydrusPipeline.ps1"
$HydrusExe  = "$env:USERPROFILE\HydrusPipeline\hydrus\hydrus_client.exe"
$DesktopDir = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopDir "Hydrus Pipeline.lnk"

if (-not (Test-Path $LauncherPs1)) {
    Write-Host "Could not find Launch-HydrusPipeline.ps1 next to this script - aborting." -ForegroundColor Yellow
    Read-Host "Press Enter to close"
    exit 1
}

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "powershell.exe"
$Shortcut.Arguments = "-NoExit -ExecutionPolicy Bypass -File `"$LauncherPs1`""
$Shortcut.WorkingDirectory = $ScriptDir
if (Test-Path $HydrusExe) {
    $Shortcut.IconLocation = $HydrusExe
}
$Shortcut.Description = "Start the Hydrus pipeline (Hydrus, hydownloader daemon, systray) and open the menu."
$Shortcut.Save()

Write-Host "Created Desktop shortcut: $ShortcutPath" -ForegroundColor Green
Read-Host "Press Enter to close"
