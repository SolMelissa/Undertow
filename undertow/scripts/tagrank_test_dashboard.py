#!/usr/bin/env python3
"""End-to-end test for the TagRank session summary dashboard."""

import sys
from pathlib import Path
from datetime import datetime, timedelta

tagrank_path = Path(__file__).resolve().parent.parent.parent.parent / "tagrank"
if tagrank_path.exists():
    sys.path.insert(0, str(tagrank_path))

try:
    from trueskill import Rating
    from tagrank.graphs import build_session_summary_figures, calculate_tag_count_for_height
    import matplotlib.pyplot as plt
except ImportError as e:
    print(f"Error: Could not import required modules: {e}")
    print("Make sure TagRank and its dependencies are installed.")
    sys.exit(1)


def generate_sample_prediction_log():
    base_date = datetime(2024, 1, 15)
    entries = []
    for i in range(30):
        date_str = (base_date + timedelta(days=i // 10)).strftime("%Y-%m-%d")
        accuracy_factor = min(0.9, 0.5 + (i / 60))
        entry = {
            "date": date_str,
            "time": f"{10 + (i % 8):02d}:{30 + (i % 30):02d}:00",
            "user_selection": "A" if i % 3 != 0 else "B",
            "tag_prediction": "A",
            "photo_prediction": "B" if i % 4 == 0 else "A",
            "confidence": 0.4 + (accuracy_factor * 0.55),
            "tag_gap": 2.5 + (i * 0.1),
            "photo_gap": 1.5 + (i * 0.05),
        }
        entries.append(entry)
    return entries


def main():
    print("=" * 60)
    print("TAGRANK SESSION SUMMARY DASHBOARD - E2E TEST")
    print("=" * 60)

    sample_entries = generate_sample_prediction_log()
    print(f"\n✓ Generated {len(sample_entries)} sample prediction log entries")

    sample_tags = [
        ("bright", Rating(mu=28.0, sigma=2.0)),
        ("dark", Rating(mu=22.0, sigma=3.0)),
        ("colorful", Rating(mu=25.0, sigma=2.5)),
    ]
    print(f"✓ Created {len(sample_tags)} sample tags with TrueSkill ratings")

    print("\n--- Building Dashboard ---")
    figures = build_session_summary_figures(sample_entries, sample_tags, figure_height=700)
    print(f"✓ Built {len(figures)} figure(s)")

    print("\n--- Figure Details ---")
    for i, fig in enumerate(figures, 1):
        axes_count = len(fig.axes)
        title = fig.axes[0].get_title() if fig.axes else "No axes"
        print(f"  Figure {i}: {title} ({axes_count} axes)")

    print("\n" + "=" * 60)
    print("✓ DASHBOARD BUILD COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
