"""
Textual-based full-screen console UI - replaces the old numbered PS1-style menu (input()/
print() loop) and the Rich Live TUI dashboard, both of which were built around what a plain
scrolling console could do. Textual gives real widget layout, a compositor that only redraws
what actually changed (no more full-screen rewrites), DataTable row actions instead of typing
subscription IDs by hand, modal dialogs, toast notifications, and a command palette.

This package is presentation only - `app.py` is the only thing in here with opinions about
what things look like. Every actual action (subscribe, pause, delete, health check, ...) goes
through the existing backend modules one level up (api_client, subscriptions, services,
logtail, api_keys, webui), unchanged.
"""
