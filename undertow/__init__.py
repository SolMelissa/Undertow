"""
undertow
========

Python port of the PowerShell Undertow launcher (Launch-HydrusPipeline.ps1,
Configure-ApiKeys.ps1, Create-DesktopShortcut.ps1, Stop-HydrusPipelineServices.ps1).

Ported to fix a recurring class of bug in the PowerShell version: ConvertTo-Json silently
collapsing single-item arrays into bare objects, and Invoke-RestMethod burying HTTP error
response bodies behind PowerShell-version-dependent stream-reading quirks. `requests` with
`json=` doesn't have either problem, so every daemon API call in this package gets a real
error body for free instead of needing hand-rolled recovery.

Entry point: `python -m undertow` (see __main__.py), or run.bat which activates the
venv first if one exists next to this package.
"""

__version__ = "1.14.17"
