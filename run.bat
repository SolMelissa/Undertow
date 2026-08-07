@echo off
REM Launches the Hydrus Pipeline (Python version). This is what the Desktop shortcut points
REM to instead of powershell.exe -File Launch-HydrusPipeline.ps1.
REM
REM Interpreter lookup order:
REM   1. ".venv" next to this file - the original layout, kept first so any existing
REM      in-folder venv keeps working exactly as before.
REM   2. "%LOCALAPPDATA%\HydrusPipeline\venv" - used when this folder lives somewhere
REM      cloud-synced (iCloud Drive). A venv is thousands of small files plus native
REM      binaries; keeping it out of the synced folder avoids constant sync churn and
REM      stops the sync client from evicting or half-writing python.exe / *.pyd.
REM   3. System python on PATH.
cd /d "%~dp0"

set "PIPELINE_PY="
if exist ".venv\Scripts\python.exe" set "PIPELINE_PY=.venv\Scripts\python.exe"
if not defined PIPELINE_PY if exist "%LOCALAPPDATA%\HydrusPipeline\venv\Scripts\python.exe" set "PIPELINE_PY=%LOCALAPPDATA%\HydrusPipeline\venv\Scripts\python.exe"

if defined PIPELINE_PY (
    "%PIPELINE_PY%" -m hydrus_pipeline
) else (
    python -m hydrus_pipeline
)

if errorlevel 1 (
    echo.
    echo Hydrus Pipeline exited with an error - see above.
    pause
)
