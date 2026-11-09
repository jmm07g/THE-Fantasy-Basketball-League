"""
ESPN Fantasy Basketball — Private League Data Sync
League ID: 249846 | 12 teams | H2H Categories

Pulls rosters, free agents, matchups, and transactions from ESPN's private
fantasy API (via the espn_api library), then recomputes the Power Rankings /
All-Play / Badges / Playoff Odds metrics used by the HTML dashboard, and
writes a refreshed JSON data file the dashboard can be rebuilt from.

WHY espn_api INSTEAD OF RAW requests CALLS
-------------------------------------------
Earlier versions of this script hit ESPN's API directly with `requests` and
hand-parsed the JSON. That worked for rosters/free agents/transactions, but
H2H Categories leagues store weekly box-score stats in a shape that isn't
exposed cleanly through the raw `mMatchupScore` view (no per-category totals,
only win/loss/tie flags). `espn_api` (pip install espn_api) is an actively
maintained open-source library purpose-built for ESPN's fantasy API and
already solves this — it parses category box scores correctly, handles SWID
formatting, and talks to the right host internally.

SECURITY — READ BEFORE USING
-----------------------------
This script authenticates using your ESPN session cookies (espn_s2, SWID).
NEVER hardcode them here and NEVER commit them to git.

MANUAL / LOCAL RUN
----------------------------------------------------------
Two ways to supply credentials for a local run:

Option A — one-time per terminal session:
    export ESPN_S2="..."
    export ESPN_SWID="{...}"
    python espn_league_sync.py

Option B — .env file (recommended, no retyping each time):
    1. Create a file named ".env" in the same folder as this script:
         ESPN_S2=...
         ESPN_SWID={...}
    2. Add ".env" to your .gitignore so it never gets committed.
    3. Just run:  python espn_league_sync.py
       The script auto-loads .env if present (no extra packages needed).

Getting fresh cookie values (do this every time they stop working):
    1. Log into fantasy.espn.com in your browser, open your league.
    2. Open DevTools (F12) -> Application/Storage tab -> Cookies ->
       https://fantasy.espn.com
    3. Copy the full "espn_s2" value and the full "SWID" value
       (keep the curly braces on SWID, e.g. {89FE16C2-...-BEBA8A}).
    4. Paste into your .env file (or export commands) — never into chat,
       email, or any file you might commit/share.

If you ever pasted real cookie values into a chat, Slack, email, etc.,
treat them as burned: log out of ESPN everywhere (or change your
password) to invalidate them, then generate fresh ones.

DEPENDENCIES
------------
    pip install espn_api
"""

import os
import sys
import json
import statistics
from datetime import datetime

try:
    from espn_api.basketball import League
except ImportError:
    sys.exit(
        "Missing dependency 'espn_api'. Install it first:\n"
        "    pip install espn_api"
    )


def _load_dotenv(path=".env"):
    """Minimal .env loader — no external dependency required.
    Only sets a variable if it isn't already set in the real environment."""
    if not os.path.exists(path):
        return
    # encoding="utf-8-sig" strips a leading BOM if Notepad/Windows added one
    # (common cause of "Missing credentials" even when the file looks correct).
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


_load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LEAGUE_ID = 249846
NUM_TEAMS = 12
SEASON_WEEKS_TOTAL = 17          # hardcoded per earlier project decision

# ESPN's "season" param = the year the season ENDS (e.g. a 2026-27 season is
# requested as season=2027). Override via env var if ESPN disagrees.
SEASON_YEAR = int(os.environ.get("ESPN_SEASON_YEAR", "2027"))

ESPN_S2 = os.environ.get("ESPN_S2")
ESPN_SWID = os.environ.get("ESPN_SWID")

if not ESPN_S2 or not ESPN_SWID:
    sys.exit(
        "Missing credentials. Set ESPN_S2 and ESPN_SWID environment "
        "variables (never hardcode them in this file)."
    )

# ESPN requires SWID wrapped in curly braces, e.g. "{89FE16C2-...-BEBA8A}".
# Browser dev tools sometimes show it without braces — add them back if missing.
if ESPN_SWID and not ESPN_SWID.startswith("{"):
    ESPN_SWID = "{" + ESPN_SWID.strip("{}") + "}"

CATEGORIES = ["FG%", "FT%", "3PM", "REB", "AST", "STL", "BLK", "PTS"]

# espn_api's own stat-name keys for category box scores occasionally differ
# slightly in capitalization/abbreviation from our dashboard's category names.
# Map dashboard category -> espn_api stat key.
ESPN_API_STAT_KEY = {
    "FG%": "FG%",
    "FT%": "FT%",
    "3PM": "3PM",
    "REB": "REB",
    "AST": "AST",
    "STL": "STL",
    "BLK": "BLK",
    "PTS": "PTS",
}


def connect():
    try:
        league = League(league_id=LEAGUE_ID, year=SEASON_YEAR, espn_s2=ESPN_S2, swid=ESPN_SWID)
    except Exception as e:
        sys.exit(
            f"Failed to connect to ESPN league {LEAGUE_ID} (season {SEASON_YEAR}): {e}\n"
            f"Check that your cookies are fresh and LEAGUE_ID/SEASON_YEAR are correct."
        )
    return league


# ---------------------------------------------------------------------------
# Pull: teams / rosters
# ---------------------------------------------------------------------------

def fetch_teams_and_rosters(league):
    teams = []
    for t in league.teams:
        roster = []
        for p in t.roster:
            roster.append({
                "player_id": getattr(p, "playerId", None),
                "name": getattr(p, "name", None),
                "pro_team": getattr(p, "proTeam", None),
                "lineup_slot": getattr(p, "lineupSlot", None),
                "injury_status": getattr(p, "injuryStatus", None),
            })
        teams.append({
            "team_id": t.team_id,
            "team_name": t.team_name,
            "abbrev": getattr(t, "team_abbrev", None),
            "roster": roster,
            # Actual league standing (H2H matchup record, not category record).
            "wins": getattr(t, "wins", 0),
            "losses": getattr(t, "losses", 0),
            "ties": getattr(t, "ties", 0),
        })
    return teams


# ---------------------------------------------------------------------------
# Pull: free agents (top N available, ranked by ESPN's own ownership %)
# ---------------------------------------------------------------------------

def fetch_free_agents(league, limit=150):
    free_agents = []
    try:
        players = league.free_agents(size=limit)
    except Exception as e:
        print(f"Warning: free agent pull failed ({e}); continuing without it.")
        return free_agents

    for p in players:
        stats_block = getattr(p, "stats", {}) or {}
        season_stats = {}
        # espn_api stats dict is keyed by scoring period / "total"/"0" style keys
        # depending on version; prefer a season-total entry if present.
        for key in ("0", "00", "total"):
            if key in stats_block:
                season_stats = stats_block[key].get("avg", {}) or stats_block[key].get("total", {})
                break
        cat_line = {cat: season_stats.get(espn_key) for cat, espn_key in ESPN_API_STAT_KEY.items()}
        free_agents.append({
            "player_id": getattr(p, "playerId", None),
            "name": getattr(p, "name", None),
            "percent_owned": getattr(p, "percent_owned", None),
            "stats": cat_line,
        })
    return free_agents


# ---------------------------------------------------------------------------
# Pull: matchups / weekly category results via espn_api box scores
# ---------------------------------------------------------------------------

def fetch_matchups(league, current_week):
    """Returns (weekly_stats, weekly_pairs).
    weekly_stats: {week: {team_id: {cat: value}}} — per-team category totals that week.
    weekly_pairs: {week: [(team_id_a, team_id_b), ...]} — actual real-schedule matchups,
    used to compute each team's TRUE category W-L-T against their real opponent
    (distinct from the dashboard's simulated All-Play record against everyone)."""
    weeks = {}
    pairs = {}

    for wk in range(1, current_week + 1):
        try:
            box_scores = league.box_scores(matchup_period=wk)
        except Exception as e:
            print(f"Warning: could not fetch box scores for week {wk} ({e}); skipping.")
            continue

        for bs in box_scores:
            home_team = getattr(bs, "home_team", None)
            away_team = getattr(bs, "away_team", None)
            home_stats = getattr(bs, "home_stats", None) or {}
            away_stats = getattr(bs, "away_stats", None) or {}

            if not home_stats and not away_stats:
                continue  # week hasn't posted stats yet (e.g. in-progress/future week)

            # Guard against ESPN returning an all-zero/all-TIE placeholder
            # shell for a week that hasn't actually started yet (observed for
            # the in-progress current week: every stat value is 0.0 and every
            # result is 'TIE'). Treat that as "no real data" rather than a
            # legitimate 0-0 week.
            def _looks_like_placeholder(stats_dict):
                if not stats_dict:
                    return True
                entries = list(stats_dict.values())
                return all(
                    isinstance(e, dict) and e.get("value") in (0, 0.0, None)
                    and e.get("result") in (None, "TIE", "UNDECIDED")
                    for e in entries
                )

            if _looks_like_placeholder(home_stats) and _looks_like_placeholder(away_stats):
                continue

            if os.environ.get("ESPN_DEBUG_DUMP") and (home_stats or away_stats):
                sample = home_stats or away_stats
                print(f"DEBUG week {wk} raw stat keys: {list(sample.keys())}")
                first_key = next(iter(sample), None)
                if first_key is not None:
                    print(f"DEBUG sample entry for '{first_key}': {sample[first_key]!r}")

            for team, stats in ((home_team, home_stats), (away_team, away_stats)):
                if team is None or not stats:
                    continue
                team_id = getattr(team, "team_id", None)
                if team_id is None:
                    continue
                cat_vals = {}
                for cat, espn_key in ESPN_API_STAT_KEY.items():
                    entry = stats.get(espn_key)
                    # espn_api box score stat entries are
                    # {'value': <value>, 'result': 'WIN'/'LOSS'/'TIE'}
                    # (confirmed via ESPN_DEBUG_DUMP output for this league).
                    val = entry.get("value") if isinstance(entry, dict) else entry
                    cat_vals[cat] = val
                weeks.setdefault(wk, {})[team_id] = cat_vals

            if (home_team is not None and away_team is not None
                    and getattr(home_team, "team_id", None) is not None
                    and getattr(away_team, "team_id", None) is not None):
                pairs.setdefault(wk, []).append((home_team.team_id, away_team.team_id))

    return weeks, pairs


def compute_actual_cat_records(weekly_stats, weekly_pairs):
    """Real head-to-head category W-L-T per team, based on actual scheduled opponents
    each week (not the All-Play round-robin simulation, which the dashboard computes
    client-side from raw weekly values)."""
    records = {}
    for wk, pair_list in weekly_pairs.items():
        week_data = weekly_stats.get(wk, {})
        for a, b in pair_list:
            ta, tb = week_data.get(a, {}), week_data.get(b, {})
            records.setdefault(a, {"w": 0, "l": 0, "t": 0})
            records.setdefault(b, {"w": 0, "l": 0, "t": 0})
            for cat in CATEGORIES:
                va, vb = ta.get(cat), tb.get(cat)
                if va is None or vb is None:
                    continue
                if va > vb:
                    records[a]["w"] += 1
                    records[b]["l"] += 1
                elif va < vb:
                    records[b]["w"] += 1
                    records[a]["l"] += 1
                else:
                    records[a]["t"] += 1
                    records[b]["t"] += 1
    return records


# ---------------------------------------------------------------------------
# Pull: transactions (adds/drops/trades) for waiver-impact tracking
# ---------------------------------------------------------------------------

def fetch_transactions(league, size=50):
    txns = []
    try:
        activity = league.recent_activity(size=size)
    except Exception as e:
        print(f"Warning: transactions pull failed ({e}); continuing without it.")
        return txns

    for act in activity:
        for action in getattr(act, "actions", []):
            # Each action tuple is typically (team, action_type, player, bid_amount).
            team, action_type, player = action[0], action[1], action[2]
            txns.append({
                "date": getattr(act, "date", None),
                "team_id": getattr(team, "team_id", None),
                "team_name": getattr(team, "team_name", None),
                "action": action_type,
                "player": getattr(player, "name", str(player)),
            })
    return txns


# ---------------------------------------------------------------------------
# Metric recomputation (mirrors the dashboard's existing logic)
# ---------------------------------------------------------------------------

def zscores_for_week(week_data):
    """week_data: {team_id: {cat: value}} -> {team_id: {cat: z}}"""
    cats = CATEGORIES
    means = {}
    stdevs = {}
    for c in cats:
        vals = [t[c] for t in week_data.values() if t.get(c) is not None]
        if not vals:
            # No team has data for this category this week (e.g. in-progress
            # or not-yet-started week) -- skip it rather than crash.
            means[c] = None
            stdevs[c] = None
            continue
        means[c] = statistics.mean(vals)
        stdevs[c] = statistics.pstdev(vals) or 1e-9
    out = {}
    for tid, cat_vals in week_data.items():
        out[tid] = {}
        for c in cats:
            v = cat_vals.get(c)
            if v is None or means[c] is None:
                out[tid][c] = 0.0
                continue
            z = (v - means[c]) / stdevs[c]
            out[tid][c] = z
    return out


def power_score(z_row):
    return sum(z_row.values())


def _pct_rank_0_to_1(value, all_values):
    """Percentile rank (0=worst,1=best) of value within all_values, used for Expected Cat Wins."""
    if value is None or not all_values:
        return 0.0
    n = len(all_values)
    if n <= 1:
        return 0.5
    rank = sum(1 for v in all_values if v < value) + 0.5 * sum(1 for v in all_values if v == value)
    return rank / (n - 1) if n > 1 else 0.5


def build_dataset(league, current_week):
    teams = fetch_teams_and_rosters(league)
    free_agents = fetch_free_agents(league)
    weekly_matchups, weekly_pairs = fetch_matchups(league, current_week)
    if not weekly_matchups:
        sys.exit(
            f"No matchup data came back for any week <= {current_week}. "
            f"Check that LEAGUE_ID/SEASON_YEAR are correct and that ESPN has "
            f"posted box scores yet for this season."
        )
    # weekly_matchups can be non-empty at the week/team level but still carry
    # all-None category values if ESPN_API_STAT_KEY doesn't match the stat
    # names espn_api actually returns for this league/version. Catch that here
    # instead of silently shipping an empty 'weekly' array downstream.
    any_real_value = any(
        v is not None
        for wk_data in weekly_matchups.values()
        for cat_vals in wk_data.values()
        for v in cat_vals.values()
    )
    if not any_real_value:
        sys.exit(
            "Matchup weeks/teams were found, but every category value came back "
            "None -- ESPN_API_STAT_KEY almost certainly doesn't match the stat "
            "names espn_api is returning for this league. Re-run with "
            "ESPN_DEBUG_DUMP=1 set and check the printed 'DEBUG week ... raw stat "
            "keys' line to see the real key names, then update ESPN_API_STAT_KEY."
        )
    transactions = fetch_transactions(league)
    actual_cat_records = compute_actual_cat_records(weekly_matchups, weekly_pairs)

    team_names = {t["team_id"]: t["team_name"] for t in teams}
    standings = {t["team_id"]: t for t in teams}

    weekly_z = {wk: zscores_for_week(data) for wk, data in weekly_matchups.items()}
    weekly_power = {
        wk: {tid: power_score(z) for tid, z in zrows.items()}
        for wk, zrows in weekly_z.items()
    }

    season_power = {}
    for tid in team_names:
        series = [weekly_power[wk][tid] for wk in sorted(weekly_power) if tid in weekly_power[wk]]
        season_power[tid] = {
            "weekly": series,
            "avg": statistics.mean(series) if series else 0,
            "consistency_std": statistics.pstdev(series) if len(series) > 1 else 0,
            "trend_slope": (series[-1] - series[0]) / max(len(series) - 1, 1) if len(series) > 1 else 0,
        }

    # Season z-profile (avg z per category across all weeks) -> matches dashboard's zProfile shape.
    z_profile = []
    for tid, name in team_names.items():
        cat_avgs = {}
        for cat in CATEGORIES:
            vals = [weekly_z[wk][tid][cat] for wk in weekly_z if tid in weekly_z[wk]]
            cat_avgs[cat] = round(statistics.mean(vals), 2) if vals else 0.0
        z_profile.append({"team": name, **cat_avgs})

    # Expected Cat Wins: percentile rank per category per week, summed (matches dashboard methodology).
    expected_cat_w = {tid: 0.0 for tid in team_names}
    for wk, data in weekly_matchups.items():
        for cat in CATEGORIES:
            cat_vals = {t: v[cat] for t, v in data.items() if v.get(cat) is not None}
            all_vals = list(cat_vals.values())
            for tid, v in cat_vals.items():
                expected_cat_w[tid] += _pct_rank_0_to_1(v, all_vals)

    # Power rankings in the exact shape the HTML dashboard expects.
    power_rows = []
    for tid, name in team_names.items():
        rec = actual_cat_records.get(tid, {"w": 0, "l": 0, "t": 0})
        avg_power = season_power[tid]["avg"]
        zp = next((z for z in z_profile if z["team"] == name), {})
        cat_scores = {c: zp.get(c, 0.0) for c in CATEGORIES}
        sorted_cats = sorted(cat_scores.items(), key=lambda kv: kv[1], reverse=True)
        top_cats = ", ".join(c for c, _ in sorted_cats[:2])
        weak_cats = ", ".join(c for c, _ in sorted_cats[-2:])
        power_rows.append({
            "team": name,
            "team_id": tid,
            "powerScore": avg_power,
            "consistency": season_power[tid]["consistency_std"],
            "trend": season_power[tid]["trend_slope"],
            "catW": rec["w"], "catL": rec["l"], "catT": rec["t"],
            "expCatW": expected_cat_w[tid],
            "luckIndex": rec["w"] - expected_cat_w[tid],
            "topCats": top_cats,
            "weakCats": weak_cats,
            # actual standing rank derived from real W-L record (ties as half-wins), used for rankDelta
            "leagueW": standings.get(tid, {}).get("wins", 0),
            "leagueL": standings.get(tid, {}).get("losses", 0),
            "leagueT": standings.get(tid, {}).get("ties", 0),
        })

    # Rank by power score (power rank) and by actual league W-L (actual rank), then compute deltas.
    power_rows.sort(key=lambda r: r["powerScore"], reverse=True)
    for i, r in enumerate(power_rows):
        r["powerRank"] = i + 1
    power_rows.sort(key=lambda r: (r["leagueW"] + 0.5 * r["leagueT"]), reverse=True)
    for i, r in enumerate(power_rows):
        r["actualRank"] = i + 1
        r["rankDelta"] = r["actualRank"] - r["powerRank"]
    power_rows.sort(key=lambda r: r["powerRank"])
    for r in power_rows:
        r.pop("team_id", None)
        r.pop("leagueW", None)
        r.pop("leagueL", None)
        r.pop("leagueT", None)

    # Tidy weekly records -> matches dashboard's DATA.weekly shape (week, team, cat, val).
    weekly_tidy = []
    for wk, data in weekly_matchups.items():
        for tid, cat_vals in data.items():
            name = team_names.get(tid)
            if not name:
                continue
            for cat in CATEGORIES:
                v = cat_vals.get(cat)
                if v is not None:
                    weekly_tidy.append({"week": wk, "team": name, "cat": cat, "val": v})

    weeks_remaining = max(SEASON_WEEKS_TOTAL - current_week, 0)

    # "current_week" from ESPN is the LIVE/in-progress week (used correctly for
    # weeks_remaining in the Playoff Odds sim). But the dashboard header should
    # display the most recent week that actually has posted stats -- i.e. the
    # highest week key that survived fetch_matchups' placeholder filter -- not
    # the live week, which usually has no real data yet.
    last_completed_week = max(weekly_matchups.keys()) if weekly_matchups else current_week

    dataset = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "league_id": LEAGUE_ID,
        "season_year": SEASON_YEAR,
        "current_week": current_week,
        "last_completed_week": last_completed_week,
        "weeks_remaining": weeks_remaining,
        "num_teams": NUM_TEAMS,
        "categories": CATEGORIES,
        "teams": teams,
        "team_names": team_names,
        "free_agents": free_agents,
        "transactions": transactions,
        # Dashboard-ready shapes (consumed directly by build_dashboard.py):
        "powerRankings": power_rows,
        "zProfile": z_profile,
        "weekly": weekly_tidy,
    }
    return dataset


def main():
    league = connect()

    # espn_api exposes the live fantasy week directly -- no more guessing
    # between ESPN's raw "scoringPeriodId" (NBA day count) and the real
    # fantasy matchup week.
    current_week = getattr(league, "currentMatchupPeriod", None) or getattr(league, "current_week", None)
    if not current_week:
        sys.exit("Could not determine current week from espn_api League object.")

    dataset = build_dataset(league, current_week)

    out_path = os.environ.get("OUTPUT_JSON_PATH", "league_data.json")
    with open(out_path, "w") as f:
        json.dump(dataset, f, indent=2, default=str)

    print(f"Wrote {out_path} — week {current_week}, {len(dataset['teams'])} teams, "
          f"{len(dataset['free_agents'])} free agents, {len(dataset['transactions'])} transactions.")


if __name__ == "__main__":
    main()
