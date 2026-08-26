"""
Creates the "Undertow" Desktop shortcut, pointing at Undertow.exe (built from
UndertowLauncher.cs via build_launcher.bat) instead of powershell.exe. Using a real
.exe rather than run.bat means the resulting shortcut can be pinned to the Start menu AND
the taskbar - Windows won't let you pin a .lnk that targets a .bat to the taskbar. Falls back
to run.bat if the exe hasn't been built yet. Equivalent of Create-DesktopShortcut.ps1. Safe
to re-run - just overwrites the shortcut. Run with: python -m undertow.shortcut
"""

from __future__ import annotations

import os
from pathlib import Path

from . import config

PACKAGE_DIR = Path(__file__).resolve().parent
LAUNCHER_EXE = PACKAGE_DIR.parent / "Undertow.exe"
RUN_BAT = PACKAGE_DIR.parent / "run.bat"


def create_desktop_shortcut() -> None:
    try:
        import win32com.client
    except ImportError:
        print("pywin32 isn't installed - run: pip install -r requirements.txt")
        return

    target = LAUNCHER_EXE if LAUNCHER_EXE.exists() else RUN_BAT
    if not target.exists():
        print(f"Could not find {LAUNCHER_EXE} or {RUN_BAT} - aborting.")
        return

    desktop_dir = Path(os.path.join(os.environ["USERPROFILE"], "Desktop"))
    shortcut_path = desktop_dir / "Undertow.lnk"

    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(str(shortcut_path))
    shortcut.TargetPath = str(target)
    shortcut.WorkingDirectory = str(target.parent)
    hydrus_icon = config.HYDRUS_DIR / "static" / "hydrus.ico"
    if hydrus_icon.exists():
        shortcut.IconLocation = str(hydrus_icon)
    shortcut.Description = "Start the Hydrus pipeline (Hydrus, hydownloader daemon, systray) and open the menu."
    shortcut.Save()

    print(f"Created Desktop shortcut: {shortcut_path}")


if __name__ == "__main__":
    create_desktop_shortcut()
    input("Press Enter to close")
