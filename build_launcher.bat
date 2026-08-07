@echo off
REM Publishes the launcher/ .NET project (Program.cs, WebView2 app frame) into a single
REM self-contained Undertow.exe at the project root, via the dotnet SDK. Re-run this
REM any time launcher/Program.cs or launcher/launcher.csproj changes. The resulting
REM Undertow.exe is a real .exe, so unlike run.bat it can be pinned to the Start menu
REM AND the taskbar, and shows the dashboard in its own window (see launcher/Program.cs)
REM instead of a browser tab.
setlocal
cd /d "%~dp0"

where dotnet >nul 2>nul
if errorlevel 1 (
    echo Could not find "dotnet" on PATH - install the .NET SDK from https://dotnet.microsoft.com/download
    exit /b 1
)

dotnet publish launcher\launcher.csproj -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -o "%~dp0publish_tmp"
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

if exist "%~dp0HydrusPipeline.exe" del /f /q "%~dp0HydrusPipeline.exe"
move /y "%~dp0publish_tmp\Undertow.exe" "%~dp0Undertow.exe" >nul
rmdir /s /q "%~dp0publish_tmp"

echo Built Undertow.exe
