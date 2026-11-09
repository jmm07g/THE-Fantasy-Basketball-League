"""
ESPN Fantasy Basketball — Private League Data Sync
League ID: 249846 | 12 teams | H2H Categories

Pulls rosters, free agents, matchups, and transactions from ESPN's private
fantasy API, then recomputes the Power Rankings / All-Play / Badges /
Playoff Odds metrics used by the HTML dashboard, and writes a refreshed
JSON data file the dashboard can be rebuilt from.

SECURITY — READ BEFORE USING
-----------------------------
This script authenticates using your ESPN session cookies (espn_s2, SWID).
NEVER hardcode them here and NEVER commit them to git.

MANUAL / LOCAL RUN (current setup — no cloud automation)
----------------------------------------------------------
ESPN's bot protection blocks requests from GitHub Actions' datacenter IPs,
so this runs manually from your own machine/network instead. Two ways to
supply credentials for a local run:

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
"""

import os
import sys
import json
import statistics
from datetime import datetime

import requests


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

BASE_URL = (
    f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{SEASON_YEAR}"
    f"/segments/0/leagues/{LEAGUE_ID}"
)
# NOTE: fantasy.espn.com (the web-app domain) sits behind aggressive Akamai
# bot protection that returns HTTP 202 + an HTML challenge page for plain
# requests-library calls, regardless of cookie validity or source IP.
# lm-api-reads.fantasy.espn.com is ESPN's dedicated read-only API host used
# by the fantasy app's own JS client and is not behind that same wall.

# ESPN requires SWID wrapped in curly braces, e.g. "{89FE16C2-...-BEBA8A}".
# Browser dev tools sometimes show it without braces — add them back if missing.
if ESPN_SWID and not ESPN_SWID.startswith("{"):
    ESPN_SWID = "{" + ESPN_SWID.strip("{}") + "}"

COOKIES = {"espn_s2": ESPN_S2, "SWID": ESPN_SWID}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"https://fantasy.espn.com/basketball/league?leagueId={LEAGUE_ID}",
    "Origin": "https://fantasy.espn.com",
    "X-Fantasy-Source": "kona",
    "X-Fantasy-Platform": "kona-PROD-1dc90067f0ba96801d4c38f290b39b7d7d6c4e80",
}

CATEGORIES = ["FG%", "FT%", "3PM", "REB", "AST", "STL", "BLK", "PTS"]

# ESPN stat IDs for the 8 standard categories (season-to-date / weekly stat
# blocks come back keyed by these). Confirm against a live pull — ESPN
# occasionally shifts ids between sports/seasons.
STAT_ID_MAP = {
    "FG%": 19,
    "FT%": 20,
    "3PM": 17,
    "REB": 6,
    "AST": 3,
    "STL": 2,
    "BLK": 1,
    "PTS": 0,
}


def _get(view_params, extra=None):
    """GET a league view from ESPN, raising on failure."""
    params = {"view": view_params} if isinstance(view_params, str) else {"view": view_params}
    if extra:
        params.update(extra)
    resp = requests.get(BASE_URL, params=params, cookies=COOKIES, headers=HEADERS, timeout=30)
    if resp.status_code == 401:
        sys.exit("ESPN returned 401 Unauthorized — cookies are invalid/expired. Re-auth and update secrets.")
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "")
    if "json" not in content_type.lower():
        snippet = resp.text[:300].replace("\n", " ")
        sys.exit(
            f"ESPN did not return JSON (Content-Type: {content_type!r}, status {resp.status_code}). "
            f"This almost always means auth failed silently (bad/expired espn_s2 or malformed SWID) "
            f"and ESPN served a login/error page instead. First 300 chars of response:\n{snippet}"
        )
    try:
        return resp.json()
    except ValueError:
        snippet = resp.text[:300].replace("\n", " ")
        sys.exit(f"Failed to parse JSON from ESPN response. First 300 chars:\n{snippet}")


# ---------------------------------------------------------------------------
# Pull: teams / rosters
# ---------------------------------------------------------------------------

def fetch_teams_and_rosters():
    data = _get(["mTeam", "mRoster"])
    teams = []
    for t in data.get("teams", []):
        team_name = f"{t.get('location', '').strip()} {t.get('nickname', '').strip()}".strip()
        roster = []
        for entry in t.get("roster", {}).get("entries", []):
            pool = entry.get("playerPoolEntry", {})
            player = pool.get("player", {})
            roster.append({
                "player_id": player.get("id"),
                "name": player.get("fullName"),
                "pro_team_id": player.get("proTeamId"),
                "lineup_slot_id": entry.get("lineupSlotId"),
                "injury_status": player.get("injuryStatus"),
            })
        record = t.get("record", {}).get("overall", {})
        teams.append({
            "team_id": t.get("id"),
            "team_name": team_name or f"Team {t.get('id')}",
            "abbrev": t.get("abbrev"),
            "roster": roster,
            # Actual league standing (H2H matchup record, not category record).
            "wins": record.get("wins", 0),
            "losses": record.get("losses", 0),
            "ties": record.get("ties", 0),
        })
    return teams


# ---------------------------------------------------------------------------
# Pull: free agents (top N available, ranked by ESPN's own rank)
# ---------------------------------------------------------------------------

def fetch_free_agents(limit=150):
    filt = {
        "players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
            "limit": limit,
            "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
        }
    }
    headers = dict(HEADERS)
    headers["X-Fantasy-Filter"] = json.dumps(filt)
    resp = requests.get(
        BASE_URL, params={"view": "kona_player_info"},
        cookies=COOKIES, headers=headers, timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    free_agents = []
    for p in data.get("players", []):
        player = p.get("player", {})
        stats_blocks = player.get("stats", [])
        season_stats = next(
            (s.get("stats", {}) for s in stats_blocks if s.get("statSplitTypeId") == 0), {}
        )
        cat_line = {cat: season_stats.get(str(sid)) for cat, sid in STAT_ID_MAP.items()}
        free_agents.append({
            "player_id": player.get("id"),
            "name": player.get("fullName"),
            "percent_owned": p.get("ratings", {}).get("0", {}).get("positionalRanking"),
            "stats": cat_line,
        })
    return free_agents


# ---------------------------------------------------------------------------
# Pull: matchups / weekly category results
# ---------------------------------------------------------------------------

def fetch_matchups(current_week):
    """Returns (weekly_stats, weekly_pairs).
    weekly_stats: {week: {team_id: {cat: value}}} — per-team category totals that week.
    weekly_pairs: {week: [(team_id_a, team_id_b), ...]} — actual real-schedule matchups,
    used to compute each team's TRUE category W-L-T against their real opponent
    (distinct from the dashboard's simulated All-Play record against everyone)."""
    data = _get("mMatchupScore", extra={"matchupPeriodId": current_week})
    weeks = {}
    pairs = {}
    for m in data.get("schedule", []):
        period = m.get("matchupPeriodId")
        home = m.get("home")
        away = m.get("away")
        for side in (home, away):
            if not side:
                continue
            team_id = side.get("teamId")
            cats = side.get("cumulativeScore", {}).get("scoreByStat", {})
            weeks.setdefault(period, {})[team_id] = {
                stat_name: cats.get(str(sid), {}).get("score")
                for stat_name, sid in STAT_ID_MAP.items()
            }
        if home and away and home.get("teamId") is not None and away.get("teamId") is not None:
            pairs.setdefault(period, []).append((home["teamId"], away["teamId"]))
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

def fetch_transactions():
    data = _get("mTransactions2")
    txns = []
    for t in data.get("transactions", []):
        txns.append({
            "type": t.get("type"),
            "status": t.get("status"),
            "team_id": t.get("teamId"),
            "scoring_period": t.get("scoringPeriodId"),
            "items": [
                {
                    "player_id": i.get("playerId"),
                    "from": i.get("fromTeamId"),
                    "to": i.get("toTeamId"),
                    "type": i.get("type"),
                }
                for i in t.get("items", [])
            ],
        })
    return txns


# ---------------------------------------------------------------------------
# Metric recomputation (mirrors the dashboard's existing logic)
# ---------------------------------------------------------------------------

PCT_CATS = {"FG%", "FT%"}


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


def build_dataset(current_week):
    teams = fetch_teams_and_rosters()
    free_agents = fetch_free_agents()
    weekly_matchups, weekly_pairs = fetch_matchups(current_week)
    transactions = fetch_transactions()
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

    dataset = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "league_id": LEAGUE_ID,
        "season_year": SEASON_YEAR,
        "current_week": current_week,
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
    status = _get("mStatus")
    # IMPORTANT: ESPN's status response has two different "current period" fields.
    # - currentMatchupPeriod = the actual fantasy WEEK number (what we want).
    # - scoringPeriodId = the NBA season's DAY count (e.g. ~21 in early Nov),
    #   which is NOT the fantasy week and will silently break matchup lookups
    #   (queries a nonexistent matchup period -> empty data -> blank/placeholder
    #   dashboard, with the header showing the wrong "Week 21" etc.).
    # Always prefer currentMatchupPeriod; only fall back to scoringPeriodId if
    # ESPN's response shape is missing it entirely.
    status_block = status.get("status", {})
    current_week = status_block.get("currentMatchupPeriod") or status.get("scoringPeriodId")
    if not current_week:
        sys.exit("Could not determine current week from ESPN response.")

    dataset = build_dataset(current_week)

    out_path = os.environ.get("OUTPUT_JSON_PATH", "league_data.json")
    with open(out_path, "w") as f:
        json.dump(dataset, f, indent=2, default=str)

    print(f"Wrote {out_path} — week {current_week}, {len(dataset['teams'])} teams, "
          f"{len(dataset['free_agents'])} free agents, {len(dataset['transactions'])} transactions.")


if __name__ == "__main__":
    main()
