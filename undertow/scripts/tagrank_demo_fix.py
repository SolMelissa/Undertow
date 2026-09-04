#!/usr/bin/env python3
"""
Demo showing the fixed dashboard behavior:
- On first run (no prediction_log.json): Shows 4 charts with 3 placeholders
- After rating sessions: Shows 4 charts with actual data

Manual demo script, not part of the pytest suite - run directly with
`python scripts/tagrank_demo_fix.py`.
"""

import sys
from pathlib import Path

# Point to TagRank project for imports
tagrank_path = Path(__file__).resolve().parent.parent.parent.parent / "tagrank"
if tagrank_path.exists():
    sys.path.insert(0, str(tagrank_path))

import matplotlib
matplotlib.use('Agg')

from tagrank.graphs import build_session_summary_figures
from trueskill import Rating

print("=" * 70)
print("DASHBOARD FIX DEMO - NOW SHOWS 4 CHARTS ALWAYS")
print("=" * 70)

# Test 1: Empty prediction log (first run scenario)
print("\n[SCENARIO 1] First run - no prediction data yet")
print("-" * 70)
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

# Test 2: With prediction data
print("\n[SCENARIO 2] After rating session - with actual prediction data")
print("-" * 70)
sample_entries = [
    {
        "date": "2024-08-30",
        "time": "10:30:00",
        "user_selection": "A",
        "tag_prediction": "A",
        "photo_prediction": "A",
        "confidence": 0.85,
        "tag_gap": 2.5,
        "photo_gap": 1.5,
    },
    {
        "date": "2024-08-30",
        "time": "10:31:00",
        "user_selection": "B",
        "tag_prediction": "A",
        "photo_prediction": "B",
        "confidence": 0.45,
        "tag_gap": 0.5,
        "photo_gap": 1.2,
    },
    {
        "date": "2024-08-31",
        "time": "14:15:00",
        "user_selection": "A",
        "tag_prediction": "A",
        "photo_prediction": "A",
        "confidence": 0.92,
        "tag_gap": 3.2,
        "photo_gap": 2.1,
    },
]
figures = build_session_summary_figures(sample_entries, sample_tags, figure_height=700)
print(f"✓ Generated {len(figures)} figures with ACTUAL prediction data:")
for i, fig in enumerate(figures, 1):
    title = fig.axes[0].get_title()
    print(f"  {i}. {title}")

print("\n" + "=" * 70)
print("RESULT: Dashboard now always shows 4 charts!")
print("  - When empty: 3 placeholders + 1 tag ranking chart")
print("  - When filled: 3 data charts + 1 tag ranking chart")
print("=" * 70)
