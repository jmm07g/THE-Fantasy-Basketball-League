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
        teams.append({
            "team_id": t.get("id"),
            "team_name": team_name or f"Team {t.get('id')}",
            "abbrev": t.get("abbrev"),
            "roster": roster,
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
    data = _get("mMatchupScore", extra={"matchupPeriodId": current_week})
    weeks = {}
    for m in data.get("schedule", []):
        period = m.get("matchupPeriodId")
        for side_key in ("home", "away"):
            side = m.get(side_key)
            if not side:
                continue
            team_id = side.get("teamId")
            cats = side.get("cumulativeScore", {}).get("scoreByStat", {})
            weeks.setdefault(period, {})[team_id] = {
                stat_name: cats.get(str(sid), {}).get("score")
                for stat_name, sid in STAT_ID_MAP.items()
            }
    return weeks


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
    means = {c: statistics.mean(t[c] for t in week_data.values() if t.get(c) is not None) for c in cats}
    stdevs = {
        c: (statistics.pstdev(t[c] for t in week_data.values() if t.get(c) is not None) or 1e-9)
        for c in cats
    }
    out = {}
    for tid, cat_vals in week_data.items():
        out[tid] = {}
        for c in cats:
            v = cat_vals.get(c)
            if v is None:
                out[tid][c] = 0.0
                continue
            z = (v - means[c]) / stdevs[c]
            out[tid][c] = z
    return out


def power_score(z_row):
    return sum(z_row.values())


def build_dataset(current_week):
    teams = fetch_teams_and_rosters()
    free_agents = fetch_free_agents()
    weekly_matchups = fetch_matchups(current_week)
    transactions = fetch_transactions()

    team_names = {t["team_id"]: t["team_name"] for t in teams}

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
        "weekly_matchup_stats": weekly_matchups,
        "weekly_zscores": weekly_z,
        "weekly_power_scores": weekly_power,
        "season_power_summary": season_power,
        "transactions": transactions,
    }
    return dataset


def main():
    status = _get("mStatus")
    current_week = status.get("scoringPeriodId") or status.get("status", {}).get("currentMatchupPeriod")
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
