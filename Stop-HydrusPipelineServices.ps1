<#
  Stop-HydrusPipelineServices.ps1
  --------------------------------
  Stops the hydownloader daemon and hydownloader-systray ONLY (leaves Hydrus itself running).
  Used when the config has changed and the running daemon/systray need to pick up fresh values -
  simply editing hydownloader-config.json does not affect an already-running process.
#>

Write-Host ">>> Stopping hydownloader daemon (if running)..." -ForegroundColor Cyan
Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match "hydownloader-daemon" } |
    ForEach-Object {
        Write-Host "  stopping PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

Write-Host ">>> Stopping hydownloader-systray (if running)..." -ForegroundColor Cyan
Get-Process -Name "hydownloader-systray" -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "  stopping PID $($_.Id))"
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 2
Write-Host ""
Write-Host "Done. Hydrus itself was left running. Re-run Setup-HydrusPipeline.ps1 to start fresh copies of the daemon and systray with the corrected config." -ForegroundColor Green
Read-Host "Press Enter to close this window"
