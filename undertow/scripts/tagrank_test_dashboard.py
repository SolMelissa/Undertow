#!/usr/bin/env python3
"""
End-to-end test for the TagRank session summary dashboard.
Generates sample prediction log data and displays the analytics charts.
Run with: python scripts/tagrank_test_dashboard.py

Manual e2e script, not part of the pytest suite (it opens matplotlib figures
for visual inspection) - run directly with `python scripts/tagrank_test_dashboard.py`.
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timedelta

# Add TagRank directory to path so we can import tagrank modules
tagrank_path = Path(__file__).resolve().parent.parent.parent.parent / "tagrank"
if tagrank_path.exists():
    sys.path.insert(0, str(tagrank_path))

from trueskill import Rating
from tagrank.graphs import build_session_summary_figures, calculate_tag_count_for_height
import matplotlib.pyplot as plt


def generate_sample_prediction_log() -> list[dict]:
    """Generate realistic sample prediction log entries for testing."""
    base_date = datetime(2024, 1, 15)
    entries = []

    for i in range(30):
        date_str = (base_date + timedelta(days=i // 10)).strftime("%Y-%m-%d")

        # Simulate improving accuracy over time
        accuracy_factor = min(0.9, 0.5 + (i / 60))
        tag_correct = i % 3 != 0  # ~67% accuracy
        photo_correct = i % 4 == 0  # ~25% accuracy

        entry = {
            "date": date_str,
            "time": f"{10 + (i % 8):02d}:{30 + (i % 30):02d}:00",
            "user_selection": "A" if tag_correct else "B",
            "tag_prediction": "A",
            "photo_prediction": "B" if photo_correct else "A",
            "confidence": 0.4 + (accuracy_factor * 0.55),
            "tag_gap": 2.5 + (i * 0.1),
            "photo_gap": 1.5 + (i * 0.05),
        }
        entries.append(entry)

    return entries


def main():
    """Run the dashboard test."""
    print("=" * 60)
    print("TAGRANK SESSION SUMMARY DASHBOARD - END-TO-END TEST")
    print("=" * 60)

    # Generate sample data
    sample_entries = generate_sample_prediction_log()
    print(f"\n✓ Generated {len(sample_entries)} sample prediction log entries")

    # Create sample tags
    sample_tags = [
        ("bright", Rating(mu=28.0, sigma=2.0)),
        ("dark", Rating(mu=22.0, sigma=3.0)),
        ("colorful", Rating(mu=25.0, sigma=2.5)),
        ("minimalist", Rating(mu=20.0, sigma=4.0)),
        ("detailed", Rating(mu=24.0, sigma=2.8)),
        ("abstract", Rating(mu=19.0, sigma=3.5)),
        ("sharp", Rating(mu=23.0, sigma=2.2)),
        ("blurry", Rating(mu=15.0, sigma=5.0)),
        ("vibrant", Rating(mu=26.0, sigma=2.3)),
        ("muted", Rating(mu=18.0, sigma=4.2)),
    ]
    print(f"✓ Created {len(sample_tags)} sample tags with TrueSkill ratings")

    # Test adaptive tag count at different heights
    print("\n--- Adaptive Tag Count ---")
    for height in [400, 700, 1200]:
        count = calculate_tag_count_for_height(height)
        print(f"  Height {height}px → {count} tags to display")

    # Build the dashboard
    print("\n--- Building Dashboard ---")
    figures = build_session_summary_figures(
        sample_entries,
        sample_tags,
        figure_height=700
    )
    print(f"✓ Built {len(figures)} separate figure(s)")

    # Verify figures
    print("\n--- Figure Details ---")
    for i, fig in enumerate(figures, 1):
        axes_count = len(fig.axes)
        title = fig.axes[0].get_title() if fig.axes else "No axes"
        print(f"  Figure {i}: {title} ({axes_count} axes)")

    # Display figures (blocking - close each to see the next)
    print("\n" + "=" * 60)
    print("READY TO DISPLAY FIGURES")
    print("=" * 60)
    print("\nDisplaying figures one at a time...")
    print("Close each figure window to proceed to the next one.")
    print("\nFigures to display:")
    for i, fig in enumerate(figures, 1):
        title = fig.axes[0].get_title() if fig.axes else f"Figure {i}"
        print(f"  {i}. {title}")

    print("\nStarting display...")
    for i, figure in enumerate(figures, 1):
        print(f"\n[{i}/{len(figures)}] Displaying: {figure.axes[0].get_title()}")
        plt.show()

    print("\n" + "=" * 60)
    print("✓ DASHBOARD DISPLAY COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
