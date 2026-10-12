"""
Scripts-tab entry point for the Hydrus-specific STAG+ wrapper (scripts/image_tagging/Run-HydrusTagger.ps1).
STAG+ itself, its venv and models live in <image_and_video_management>\image_tagging\local\stag; the
wrapper adds the Hydrus namespace mapping and converts STAG's XMP output to plaintext .txt sidecars.
scripts_runner.list_scripts() only globs scripts/*.py, hence this thin launcher around the .ps1.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from undertow import config  # noqa: E402

WRAPPER = Path(__file__).resolve().parent / "image_tagging" / "Run-HydrusTagger.ps1"


def main() -> int:
    if not (config.STAG_DIR / "stag-plus" / "stag.py").exists():
        print(f"STAG+ not found at {config.STAG_DIR}. Expected under {config.IMAGE_TAGGING_DIR}.")
        return 1
    pwsh = shutil.which("pwsh")
    if not pwsh:
        print("PowerShell 7 (pwsh) is required for Run-HydrusTagger.ps1.")
        return 1
    folder = input("Image folder to tag (blank = pick in a dialog): ").strip().strip('"')
    cmd = [pwsh, "-NoProfile", "-File", str(WRAPPER)]
    if folder:
        cmd += ["-ImageRoot", folder]
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
