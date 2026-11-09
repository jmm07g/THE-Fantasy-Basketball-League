"""
Regenerate the Fantasy Power Rankings dashboard HTML from league_data.json.

Reads the dataset produced by espn_league_sync.py (powerRankings, zProfile,
weekly, plus current_week) and writes a fresh, fully self-contained HTML file
with that data baked in as the DATA constant — same template/styling/features
as the hand-built dashboard, just re-populated with live numbers.

Usage:
    python build_dashboard.py
    (reads league_data.json, writes index.html in the same folder)

Env overrides:
    INPUT_JSON_PATH   default "league_data.json"
    OUTPUT_HTML_PATH  default "index.html"
    TEMPLATE_HTML_PATH default "dashboard_template.html"
"""

import os
import re
import json
import sys

INPUT_JSON_PATH = os.environ.get("INPUT_JSON_PATH", "league_data.json")
OUTPUT_HTML_PATH = os.environ.get("OUTPUT_HTML_PATH", "index.html")
TEMPLATE_HTML_PATH = os.environ.get("TEMPLATE_HTML_PATH", "dashboard_template.html")

CATEGORIES = ["FG%", "FT%", "3PM", "REB", "AST", "STL", "BLK", "PTS"]

METHODOLOGY = [
    ["Power Score", "Sum of each team's z-scores (standardized vs league mean/stdev that week) across all 8 categories, averaged across weeks. Measures true statistical strength, independent of schedule/luck."],
    ["Consistency (StdDev)", "Standard deviation of a team's weekly z-score sum. Lower = more reliably good/bad; higher = boom/bust."],
    ["Trend (slope)", "Linear slope of weekly z-score sum over the season so far. Positive = team is trending up; negative = trending down."],
    ["Expected Cat W", "Each week, teams are percentile-ranked (0=worst,1=best) in each of the 8 categories; that percentile is the team's implied category win rate vs a random opponent. Summed over all category-decisions so far."],
    ["Luck Index", "Actual category wins minus Expected Cat W. Positive = team has outperformed what its raw stats deserve (lucky schedule/close wins); negative = team is being shortchanged by categories lost narrowly."],
    ["Rank Delta", "Actual League Rank minus Power Rank. Positive = team's underlying stats are stronger than its record shows (buy-low candidate); negative = record is propped up relative to true strength (sell-high candidate)."],
    ["Top/Weak Categories", "The 2 categories where the team's season z-score is highest / lowest — i.e. what they should trade for or consider punting."],
    ["Build (Punt Strategy Detector)", "Classifies each team's roster construction from its season z-profile spread: 'Punt X/Y' = at least one category abandoned (z <= -1.0) while another is elite (z >= 1.0); 'Stars & Scrubs' = high variance across all 8 cats without a clear single punt; 'Balanced' = low variance, no major strength or weakness; 'Mixed Build' = in between."],
    ["All-Play Record", "Treats every week as if each team played all 11 other teams in every category (roto-style) instead of just one head-to-head opponent. Removes schedule luck — often the truest read on who is actually best. 'Sched Luck' = (actual win% - all-play win%) x 100; positive means the real schedule has been kinder than raw stats deserve."],
    ["Power Rankings Movement", "Recomputes each team's power rank independently for every single week (not cumulative) and plots it over time, like an F1 standings chart. Shows who is rising, falling, or steady."],
    ["Playoff Odds Simulator", "Monte Carlo simulation: each remaining week, every team's 8 category totals are drawn from a normal distribution using that team's own season mean/standard deviation (observed from weekly data so far). Teams are randomly paired each simulated week, category results tallied, and the process repeats for the chosen number of simulations. Reports the % of simulations in which each team finishes within the specified number of playoff spots. Assumes each team's current talent level/variance holds — it does not account for trades, injuries, or schedule strength."],
    ["Manager of the Week", "Compares each team's weekly z-score total between the two most recent weeks. The biggest positive jump wins; the biggest drop gets the tongue-in-cheek 'Free Fall Award.' A hot-streak indicator, not a season-quality measure."],
    ["Weekly Badge Cabinet", "Category-leader badges (e.g. Sharpshooter, Glass Cleaner) are awarded to whoever posted the single best raw total in that category during the most recent week. 'Wooden Spoon' goes to the lowest combined weekly z-score. Recalculates every week as new data comes in."],
    ["Season Storylines (badges)", "Season-long badges derived from existing metrics: Horseshoe Award/Cursed = highest/lowest Luck Index, Roller Coaster/Mr. Reliable = highest/lowest Consistency, Rocket Ship/Cooling Off = highest/lowest Trend, Buy-Low/Sell-High = highest/lowest Rank Delta."],
    ["To update", "This file is auto-generated weekly by the GitHub Actions pipeline (espn_league_sync.py -> build_dashboard.py). No manual edits needed — they'll be overwritten on the next run."],
]


def load_dataset(path):
    if not os.path.exists(path):
        sys.exit(f"Input file not found: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_data_blob(dataset):
    """Assemble the exact DATA object shape the dashboard's JS expects."""
    required = ["powerRankings", "zProfile", "weekly"]
    missing = [k for k in required if k not in dataset]
    if missing:
        sys.exit(
            f"league_data.json is missing required key(s): {missing}.\n"
            f"This usually means espn_league_sync.py in the repo is an older version "
            f"that doesn't compute these fields. Re-download the latest "
            f"espn_league_sync.py (the one that builds powerRankings/zProfile/weekly), "
            f"overwrite it in the repo, and re-run the sync step before rebuilding."
        )
    # Guard against a "technically valid but empty" dataset — e.g. the sync script
    # requested a matchup period that doesn't exist (wrong week number), so ESPN
    # returned no matchups and every list above is empty. Shipping that would
    # silently regenerate a placeholder-looking dashboard instead of failing loudly.
    empty = [k for k in required if len(dataset[k]) == 0]
    if empty:
        sys.exit(
            f"league_data.json has required key(s) but they're EMPTY: {empty}.\n"
            f"This usually means espn_league_sync.py queried the wrong matchup week "
            f"(e.g. confused ESPN's day-count 'scoringPeriodId' with the real fantasy "
            f"'currentMatchupPeriod'), so no matchup data came back. Check current_week "
            f"in league_data.json against the real fantasy week on espn.com before "
            f"re-running."
        )
    return {
        "powerRankings": dataset["powerRankings"],
        "zProfile": dataset["zProfile"],
        "weekly": dataset["weekly"],
        "methodology": METHODOLOGY,
    }


def inject_into_template(template_html, data_blob, current_week):
    # Replace the `const DATA = {...};` line (single statement, ends with `};` right
    # before `const CATS`) with the freshly generated JSON blob.
    new_data_js = "const DATA = " + json.dumps(data_blob) + ";"
    pattern = re.compile(r"const DATA = \{.*?\};(?=\s*const CATS)", re.DOTALL)
    if not pattern.search(template_html):
        sys.exit("Could not find 'const DATA = {...};' block in template — template structure changed.")
    # Use a function replacement, not a raw string: json.dumps() output can contain
    # backslash sequences (e.g. \u escapes), and re.sub() treats backslashes in a
    # string replacement as regex escape codes, which throws "bad escape \u".
    # A function replacement is inserted literally, with no backslash processing.
    html = pattern.sub(lambda m: new_data_js, template_html, count=1)

    # Update header "Through Week N" text.
    html = re.sub(
        r"(<header>.*?<p>)Through Week \d+(</p>)",
        rf"\1Through Week {current_week}\2",
        html, count=1, flags=re.DOTALL,
    )

    # Update footer week reference.
    html = re.sub(
        r"Data through Week \d+",
        f"Data through Week {current_week}",
        html, count=1,
    )

    return html


def main():
    dataset = load_dataset(INPUT_JSON_PATH)
    # Display the most recent week with real posted stats, not ESPN's "live"
    # current_week (which is often in-progress / no data yet). Falls back to
    # current_week for older league_data.json files that predate this field.
    current_week = dataset.get("last_completed_week", dataset.get("current_week", "?"))

    if not os.path.exists(TEMPLATE_HTML_PATH):
        sys.exit(
            f"Template not found: {TEMPLATE_HTML_PATH}\n"
            f"Save the dashboard HTML artifact into the repo once, named "
            f"'{TEMPLATE_HTML_PATH}', so this script has a template to re-populate "
            f"with fresh data each run."
        )
    with open(TEMPLATE_HTML_PATH, encoding="utf-8") as f:
        template_html = f.read()

    data_blob = build_data_blob(dataset)
    new_html = inject_into_template(template_html, data_blob, current_week)

    with open(OUTPUT_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(new_html)

    print(f"Wrote {OUTPUT_HTML_PATH} — Week {current_week}, "
          f"{len(data_blob['powerRankings'])} teams, {len(data_blob['weekly'])} weekly records.")


if __name__ == "__main__":
    main()
