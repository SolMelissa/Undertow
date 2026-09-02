@echo off
REM Publishes the launcher/ .NET project (Program.cs, WebView2 app frame) into a single
REM Undertow.exe at the project root, via the dotnet SDK. Re-run this any time
REM launcher/Program.cs or launcher/launcher.csproj changes. The resulting Undertow.exe
REM is a real .exe, so unlike run.bat it can be pinned to the Start menu AND the taskbar,
REM and shows the dashboard in its own window (see launcher/Program.cs) instead of a
REM browser tab.
REM
REM Framework-dependent, not self-contained (see launcher.csproj) - this machine always has
REM the matching .NET runtime installed, so there's no reason to bundle a private copy of it
REM into a ~160MB exe. If you ever build this for a machine without .NET 8 installed, add
REM --self-contained true back (and IncludeNativeLibrariesForSelfExtract=true) on this line.
setlocal
cd /d "%~dp0"

where dotnet >nul 2>nul
if errorlevel 1 (
    echo Could not find "dotnet" on PATH - install the .NET SDK from https://dotnet.microsoft.com/download
    exit /b 1
)

dotnet publish launcher\launcher.csproj -c Release -r win-x64 --self-contained false -p:PublishSingleFile=true -o "%~dp0publish_tmp"
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

if exist "%~dp0HydrusPipeline.exe" del /f /q "%~dp0HydrusPipeline.exe"
move /y "%~dp0publish_tmp\Undertow.exe" "%~dp0Undertow.exe" >nul
REM WebView2Loader.dll is a loose native dependency that framework-dependent
REM PublishSingleFile does NOT bundle into the exe - it must sit next to
REM Undertow.exe at runtime or the app fails with 0x8007007E on launch.
move /y "%~dp0publish_tmp\WebView2Loader.dll" "%~dp0WebView2Loader.dll" >nul
rmdir /s /q "%~dp0publish_tmp"

echo Built Undertow.exe
