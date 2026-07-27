"""
Creates the "Hydrus Pipeline" Desktop shortcut, pointing at run.bat instead of
powershell.exe. Equivalent of Create-DesktopShortcut.ps1. Safe to re-run - just overwrites
the shortcut. Run with: python -m hydrus_pipeline.shortcut
"""

from __future__ import annotations

import os
from pathlib import Path

from . import config

PACKAGE_DIR = Path(__file__).resolve().parent
RUN_BAT = PACKAGE_DIR.parent / "run.bat"


def create_desktop_shortcut() -> None:
    try:
        import win32com.client
    except ImportError:
        print("pywin32 isn't installed - run: pip install -r requirements.txt")
        return

    if not RUN_BAT.exists():
        print(f"Could not find {RUN_BAT} - aborting.")
        return

    desktop_dir = Path(os.path.join(os.environ["USERPROFILE"], "Desktop"))
    shortcut_path = desktop_dir / "Hydrus Pipeline.lnk"

    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(str(shortcut_path))
    shortcut.TargetPath = str(RUN_BAT)
    shortcut.WorkingDirectory = str(RUN_BAT.parent)
    if config.HYDRUS_EXE.exists():
        shortcut.IconLocation = str(config.HYDRUS_EXE)
    shortcut.Description = "Start the Hydrus pipeline (Hydrus, hydownloader daemon, systray) and open the menu."
    shortcut.Save()

    print(f"Created Desktop shortcut: {shortcut_path}")


if __name__ == "__main__":
    create_desktop_shortcut()
    input("Press Enter to close")
