"""Lets the console TUI be launched on its own via `python -m hydrus_pipeline.tui`, without
re-running the service-start sequence in menu.py - used as the fallback/"Console UI" option
from the web dashboard, where services are already running and only the interface is being
opened. Running this doesn't install the exit handlers menu.main() does (no idle-shutdown-on-
close behavior) - that's intentional: with the web dashboard as the primary interface now,
whichever one exits first shouldn't tear down the shared services the other one is using.
"""

from .app import run

if __name__ == "__main__":
    run()
