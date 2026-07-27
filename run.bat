@echo off
REM Launches the Hydrus Pipeline (Python version). Prefers a venv next to this file if one
REM exists, falls back to system python otherwise. This is what the Desktop shortcut points
REM to now instead of powershell.exe -File Launch-HydrusPipeline.ps1.
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m hydrus_pipeline
) else (
    python -m hydrus_pipeline
)

if errorlevel 1 (
    echo.
    echo Hydrus Pipeline exited with an error - see above.
    pause
)
