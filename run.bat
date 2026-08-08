@echo off
REM Launches Undertow (Python version). This is what the Desktop shortcut points
REM to instead of powershell.exe -File Launch-HydrusPipeline.ps1.
REM
REM Interpreter lookup order:
REM   1. ".venv" next to this file - the original layout, kept first so any existing
REM      in-folder venv keeps working exactly as before.
REM   2. "%LOCALAPPDATA%\Undertow\venv" - used when this folder lives somewhere
REM      cloud-synced (iCloud Drive). A venv is thousands of small files plus native
REM      binaries; keeping it out of the synced folder avoids constant sync churn and
REM      stops the sync client from evicting or half-writing python.exe / *.pyd.
REM   3. System python on PATH.
cd /d "%~dp0"

REM Background Claude sessions push their commits straight to origin/master, but
REM this checkout only sees them once something actually runs "git pull" - so
REM without this, the shortcut can launch stale code with no visible sign anything
REM is wrong. Best-effort: only fast-forwards (never touches local edits/merges),
REM and any failure (offline, no git, diverged history) is silently ignored so a
REM sync hiccup never blocks launching the app.
where git >nul 2>nul && git rev-parse --is-inside-work-tree >nul 2>nul && (
    git pull --ff-only >nul 2>nul
)

set "PIPELINE_PY="
if exist ".venv\Scripts\python.exe" set "PIPELINE_PY=.venv\Scripts\python.exe"
if not defined PIPELINE_PY if exist "%LOCALAPPDATA%\Undertow\venv\Scripts\python.exe" set "PIPELINE_PY=%LOCALAPPDATA%\Undertow\venv\Scripts\python.exe"

if defined PIPELINE_PY (
    "%PIPELINE_PY%" -m undertow
) else (
    python -m undertow
)

if errorlevel 1 (
    echo.
    echo Undertow exited with an error - see above.
    pause
)
