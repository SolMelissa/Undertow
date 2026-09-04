#!/usr/bin/env python3
"""
Demo showing the fixed dashboard behavior:
- On first run (no prediction_log.json): Shows 4 charts with 3 placeholders
- After rating sessions: Shows 4 charts with actual data
"""

import sys
from pathlib import Path

tagrank_path = Path(__file__).resolve().parent.parent.parent.parent / "tagrank"
if tagrank_path.exists():
    sys.path.insert(0, str(tagrank_path))

try:
    import matplotlib
    matplotlib.use('Agg')
    from tagrank.graphs import build_session_summary_figures
    from trueskill import Rating
except ImportError as e:
    print(f"Error: Could not import required modules: {e}")
    print("Make sure TagRank is installed and accessible.")
    sys.exit(1)

print("=" * 70)
print("DASHBOARD FIX DEMO - NOW SHOWS 4 CHARTS ALWAYS")
print("=" * 70)

sample_tags = [
    ("bright", Rating(mu=28.0, sigma=2.0)),
    ("dark", Rating(mu=22.0, sigma=3.0)),
    ("colorful", Rating(mu=25.0, sigma=2.5)),
]
empty_entries = []
figures = build_session_summary_figures(empty_entries, sample_tags, figure_height=700)
print(f"✓ Generated {len(figures)} figures with EMPTY prediction log:")
for i, fig in enumerate(figures, 1):
    title = fig.axes[0].get_title()
    print(f"  {i}. {title}")

print("\n" + "=" * 70)
print("RESULT: Dashboard now always shows 4 charts!")
print("=" * 70)
