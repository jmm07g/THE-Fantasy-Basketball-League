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
import math
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
    """Returns (txns, raw_activity). raw_activity is kept (not just the flattened
    txns list) so the Waiver Wire Report Card can regroup actions by activity
    (one activity = one roster move event, usually containing a DROP+ADD pair)."""
    txns = []
    raw_activity = []
    try:
        activity = league.recent_activity(size=size)
        raw_activity = activity
    except Exception as e:
        print(f"Warning: transactions pull failed ({e}); continuing without it.")
        return txns, raw_activity

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
                "player_id": getattr(player, "playerId", None),
            })
    return txns, raw_activity


# ---------------------------------------------------------------------------
# Pull: draft results (for Draft Grade Retrospective)
# ---------------------------------------------------------------------------
# This league is an AUCTION draft, not a snake draft -- there's no meaningful
# "pick order" (round_num/round_pick are largely meaningless/constant in
# auction mode). The real "cost" signal is the $ bid amount spent on each
# player, so grading is based on auction value, not draft position.

def fetch_draft(league):
    """Returns a list of picks with the auction bid amount (dollars spent).
    espn_api's Pick objects expose this as `bid_amount` for auction-draft
    leagues. If it's ever missing/zero across the board (e.g. league type
    detection issue), callers should treat draft grading as unavailable
    rather than silently falling back to a meaningless pick-order metric."""
    picks = []
    try:
        draft = league.draft
    except Exception as e:
        print(f"Warning: draft pull failed ({e}); continuing without it.")
        return picks

    for p in draft:
        team = getattr(p, "team", None)
        picks.append({
            "player_id": getattr(p, "playerId", None),
            "player_name": getattr(p, "playerName", None),
            "team_id": getattr(team, "team_id", None),
            "bid_amount": getattr(p, "bid_amount", None),
        })
    return picks


# ---------------------------------------------------------------------------
# Player pool (rostered + free agents) with season-average stats — shared
# building block for Z-Score Contribution and Draft Grade Retrospective.
# ---------------------------------------------------------------------------

def _season_avg_from_player(p):
    stats_block = getattr(p, "stats", {}) or {}
    season_stats = {}
    for key in ("0", "00", "total"):
        if key in stats_block:
            season_stats = stats_block[key].get("avg", {}) or stats_block[key].get("total", {})
            break
    return {cat: season_stats.get(espn_key) for cat, espn_key in ESPN_API_STAT_KEY.items()}


def build_player_pool(league, teams, free_agents_raw):
    """Combines rostered players (with season-avg stats pulled fresh per player)
    and already-fetched free agents into one pool, then z-scores every player
    against the pool for each category. Used for player contribution + draft grade.
    Returns {player_id: {name, team_id/None, cats:{...}, z:{...}, zTotal}}."""
    pool = {}

    for t in teams:
        for rp in t["roster"]:
            pid = rp.get("player_id")
            if pid is None:
                continue
            pool[pid] = {
                "player_id": pid, "name": rp.get("name"),
                "team_id": t["team_id"], "team_name": t["team_name"],
                "cats": None,  # filled below once we have live player stats
            }

    # Pull live per-player stat lines for rostered players via league.player_info,
    # since roster objects from fetch_teams_and_rosters don't carry stat blocks.
    try:
        all_rostered_ids = list(pool.keys())
        # espn_api supports fetching full player objects (with stats) in bulk
        # via league.player_info(playerId=...) one at a time; batch where possible.
        for pid in all_rostered_ids:
            try:
                p = league.player_info(playerId=pid)
                if p is not None:
                    pool[pid]["cats"] = _season_avg_from_player(p)
            except Exception:
                continue
    except Exception as e:
        print(f"Warning: rostered player stat pull failed ({e}); contribution data may be incomplete.")

    # Add free agents (already have stats from fetch_free_agents).
    for fa in free_agents_raw:
        pid = fa.get("player_id")
        if pid is None or pid in pool:
            continue
        pool[pid] = {
            "player_id": pid, "name": fa.get("name"),
            "team_id": None, "team_name": None,
            "cats": fa.get("stats"),
        }

    # Z-score every player in the pool per category (players missing a stat are excluded
    # from that category's mean/stdev, and get z=0 for it rather than skewing results).
    means, stdevs = {}, {}
    for cat in CATEGORIES:
        vals = [v["cats"][cat] for v in pool.values() if v.get("cats") and v["cats"].get(cat) is not None]
        if vals:
            means[cat] = statistics.mean(vals)
            stdevs[cat] = statistics.pstdev(vals) or 1e-9
        else:
            means[cat] = None
            stdevs[cat] = None

    for entry in pool.values():
        z = {}
        cats = entry.get("cats") or {}
        for cat in CATEGORIES:
            v = cats.get(cat)
            if v is None or means[cat] is None:
                z[cat] = 0.0
            else:
                z[cat] = (v - means[cat]) / stdevs[cat]
        entry["z"] = z
        entry["zTotal"] = sum(z.values())

    return pool


# ---------------------------------------------------------------------------
# NEW FEATURE 1: Player Z-Score Contribution
# ---------------------------------------------------------------------------

def build_player_contribution(teams, player_pool):
    """For each team, breaks down which rostered players are driving (or hurting)
    each category, using the season-average z-scores computed in build_player_pool.
    Returns a list of {team, players:[{name, zTotal, cats:{...}}], topContributor,
    biggestDrag} sorted by team name."""
    out = []
    for t in teams:
        players = []
        for rp in t["roster"]:
            pid = rp.get("player_id")
            entry = player_pool.get(pid) if pid is not None else None
            if entry is None or entry.get("cats") is None:
                continue
            players.append({
                "name": entry["name"],
                "zTotal": round(entry["zTotal"], 2),
                "cats": {c: round(entry["z"][c], 2) for c in CATEGORIES},
                "injury_status": rp.get("injury_status"),
            })
        players.sort(key=lambda p: p["zTotal"], reverse=True)
        top = players[0] if players else None
        drag = players[-1] if players else None
        out.append({
            "team": t["team_name"],
            "players": players,
            "topContributor": top["name"] if top else None,
            "biggestDrag": drag["name"] if drag else None,
        })
    out.sort(key=lambda r: r["team"])
    return out


# ---------------------------------------------------------------------------
# NEW FEATURE 2: Waiver Wire Report Card
# ---------------------------------------------------------------------------

def build_waiver_report(raw_activity, player_pool, team_names):
    """Groups each transaction activity (usually a DROP+ADD pair happening at the
    same timestamp) and scores it using CURRENT season z-score totals of the
    players involved. NOTE: this is a simplification -- it grades adds/drops
    against each player's full-season value, not their value at the moment of
    the transaction (ESPN doesn't expose a clean historical snapshot through
    this API for that). Treat this as "how did this move age", not a
    point-in-time trade grade."""
    report = []
    for act in raw_activity:
        actions = getattr(act, "actions", [])
        if not actions:
            continue
        date = getattr(act, "date", None)
        # Group actions in this activity by team, since one "activity" can be
        # one team's DROP+ADD pair.
        by_team = {}
        for action in actions:
            team, action_type, player = action[0], action[1], action[2]
            tid = getattr(team, "team_id", None)
            by_team.setdefault(tid, []).append((action_type, player))

        for tid, acts in by_team.items():
            adds = [p for (a, p) in acts if a and "ADD" in str(a).upper()]
            drops = [p for (a, p) in acts if a and "DROP" in str(a).upper()]
            if not adds and not drops:
                continue
            for add_player in (adds or [None]):
                add_pid = getattr(add_player, "playerId", None) if add_player else None
                add_entry = player_pool.get(add_pid) if add_pid is not None else None
                add_z = round(add_entry["zTotal"], 2) if add_entry else None
                drop_player = drops[0] if drops else None
                drop_pid = getattr(drop_player, "playerId", None) if drop_player else None
                drop_entry = player_pool.get(drop_pid) if drop_pid is not None else None
                drop_z = round(drop_entry["zTotal"], 2) if drop_entry else None
                net = (add_z - drop_z) if (add_z is not None and drop_z is not None) else None
                report.append({
                    "date": date,
                    "team": team_names.get(tid, "Unknown"),
                    "added": getattr(add_player, "name", None) if add_player else None,
                    "added_z": add_z,
                    "dropped": getattr(drop_player, "name", None) if drop_player else None,
                    "dropped_z": drop_z,
                    "net_value": round(net, 2) if net is not None else None,
                })
    # Most impactful (by |net_value|) first; moves we couldn't score sort last.
    report.sort(key=lambda r: (r["net_value"] is None, -(r["net_value"] or 0) if r["net_value"] is not None else 0))
    return report


# ---------------------------------------------------------------------------
# NEW FEATURE 3: Matchup Win Probability (for the CURRENT/upcoming week)
# ---------------------------------------------------------------------------

def _normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def build_matchup_win_probability(league, current_week, season_power, team_names, weekly_matchups):
    """Estimates each team's win probability per category (and overall) for the
    CURRENT week's real matchup pairing, using each team's own season mean/stdev
    per category observed so far (same distributional approach as the Playoff
    Odds simulator, applied to the real upcoming pairing instead of a random one).
    This is team-level, not lineup/player-level (ESPN doesn't cleanly expose
    "games remaining this week per roster slot" through this API)."""
    try:
        box_scores = league.box_scores(matchup_period=current_week)
    except Exception as e:
        print(f"Warning: could not fetch current-week matchups for win probability ({e}).")
        return []

    # Per-team, per-category mean/stdev from completed weeks so far.
    cat_stats = {}
    for tid in team_names:
        cat_stats[tid] = {}
        for cat in CATEGORIES:
            vals = [weekly_matchups[wk][tid][cat] for wk in weekly_matchups
                    if tid in weekly_matchups[wk] and weekly_matchups[wk][tid].get(cat) is not None]
            if vals:
                mean = statistics.mean(vals)
                sd = statistics.pstdev(vals) if len(vals) > 1 else mean * 0.08
                cat_stats[tid][cat] = (mean, max(sd, mean * 0.04 if mean else 1.0))
            else:
                cat_stats[tid][cat] = (0.0, 1.0)

    previews = []
    for bs in box_scores:
        home_team = getattr(bs, "home_team", None)
        away_team = getattr(bs, "away_team", None)
        if home_team is None or away_team is None:
            continue
        home_id = getattr(home_team, "team_id", None)
        away_id = getattr(away_team, "team_id", None)
        if home_id not in team_names or away_id not in team_names:
            continue

        cat_probs = {}
        home_favored_count = 0
        for cat in CATEGORIES:
            hm, hs = cat_stats[home_id][cat]
            am, a_sd = cat_stats[away_id][cat]
            # P(home > away) for Home - Away ~ Normal(hm-am, sqrt(hs^2+a_sd^2))
            diff_mean = hm - am
            diff_sd = math.sqrt(hs ** 2 + a_sd ** 2) or 1e-9
            p_home = 1 - _normal_cdf((0 - diff_mean) / diff_sd)
            cat_probs[cat] = round(p_home, 3)
            if p_home > 0.5:
                home_favored_count += 1

        previews.append({
            "week": current_week,
            "home": team_names[home_id],
            "away": team_names[away_id],
            "catProbs": cat_probs,
            "homeFavoredCats": home_favored_count,
            "awayFavoredCats": len(CATEGORIES) - home_favored_count,
            "homeOverallFavorite": home_favored_count > len(CATEGORIES) / 2,
        })
    return previews


# ---------------------------------------------------------------------------
# NEW FEATURE 4: Draft Grade Retrospective
# ---------------------------------------------------------------------------

def build_draft_grades(draft_picks, player_pool, team_names):
    """AUCTION draft grading: compares each player's $ cost at auction to their
    current-season performance rank (by zTotal across the whole player pool),
    converted into an implied '$ rank' so cost and performance are on the same
    scale. A $60 player who now ranks #85 in zTotal is a bust; a $2 player who
    now ranks #10 is a steal of the year. Aggregates to a per-team average
    '$ value delta' (positive = team got more performance than it paid for)."""
    if not draft_picks:
        return {"picks": [], "team_grades": []}

    # Only picks with a real $ amount are usable for auction-value grading.
    priced_picks = [p for p in draft_picks if p.get("bid_amount") is not None]
    if not priced_picks:
        print("Warning: no bid_amount found on draft picks (league may not be "
              "detected as auction-type by espn_api); draft grades unavailable.")
        return {"picks": [], "team_grades": []}

    # Rank every player in the pool by zTotal (1 = best performer in the league).
    ranked_pool = sorted(player_pool.values(), key=lambda p: p["zTotal"], reverse=True)
    perf_rank = {p["player_id"]: i + 1 for i, p in enumerate(ranked_pool)}
    pool_size = len(ranked_pool)

    # Rank players by $ spent too (1 = most expensive), so we can express
    # "expected performance rank given the price paid" on the same 1..N scale
    # as perf_rank, making picks of different price tiers comparable.
    priced_picks_sorted = sorted(priced_picks, key=lambda p: p["bid_amount"], reverse=True)
    cost_rank = {p["player_id"]: i + 1 for i, p in enumerate(priced_picks_sorted)}

    picks_out = []
    team_deltas = {}
    for pick in priced_picks:
        pid = pick.get("player_id")
        entry = player_pool.get(pid)
        pr = perf_rank.get(pid)
        cr = cost_rank.get(pid)
        if entry is None or pr is None or cr is None:
            continue  # player no longer in pool (dropped/retired/etc.)
        # Scale cost_rank (1..len(priced_picks)) onto the same range as perf_rank
        # (1..pool_size) so a $ rank of #1 and a performance rank of #1 are
        # directly comparable even though the pools are different sizes.
        scaled_cost_rank = cr * (pool_size / len(priced_picks)) if priced_picks else cr
        value_delta = scaled_cost_rank - pr  # positive = outperforming price tag
        picks_out.append({
            "player": pick.get("player_name") or entry.get("name"),
            "team": team_names.get(pick.get("team_id"), "Unassigned"),
            "auctionValue": pick["bid_amount"],
            "performanceRank": pr,
            "valueDelta": round(value_delta, 1),
            "zTotal": round(entry["zTotal"], 2),
        })
        tid = pick.get("team_id")
        if tid is not None:
            team_deltas.setdefault(tid, []).append(value_delta)

    picks_out.sort(key=lambda r: r["valueDelta"], reverse=True)

    team_grades = []
    for tid, deltas in team_deltas.items():
        team_grades.append({
            "team": team_names.get(tid, "Unknown"),
            "avgValueDelta": round(statistics.mean(deltas), 1),
            "picksTracked": len(deltas),
        })
    team_grades.sort(key=lambda r: r["avgValueDelta"], reverse=True)

    return {"picks": picks_out, "team_grades": team_grades}


# ---------------------------------------------------------------------------
# NEW FEATURE 5: Rivalry Score (head-to-head history between every team pair)
# ---------------------------------------------------------------------------

def build_rivalry_scores(weekly_matchups, weekly_pairs, team_names):
    """Tracks every real matchup played between each pair of teams this season:
    category W-L-T each time they've faced off, plus the closest-margin category
    (biggest 'nail-biter') as a storyline hook."""
    pair_records = {}  # frozenset({a,b}) -> {a_catw, b_catw, ties, meetings, closest}

    for wk, pairs in weekly_pairs.items():
        week_data = weekly_matchups.get(wk, {})
        for a, b in pairs:
            key = tuple(sorted((a, b)))
            rec = pair_records.setdefault(key, {
                "a": key[0], "b": key[1], "a_catw": 0, "b_catw": 0, "ties": 0,
                "meetings": 0, "closest_cat": None, "closest_margin": None, "closest_week": None,
            })
            rec["meetings"] += 1
            ta, tb = week_data.get(a, {}), week_data.get(b, {})
            for cat in CATEGORIES:
                va, vb = ta.get(cat), tb.get(cat)
                if va is None or vb is None:
                    continue
                if va > vb:
                    rec["a_catw" if a == key[0] else "b_catw"] += 1
                elif va < vb:
                    rec["b_catw" if a == key[0] else "a_catw"] += 1
                else:
                    rec["ties"] += 1
                # Track closest % margin seen between this pair (percentage cats
                # compared as relative distance, counting cats compared as raw totals
                # using relative margin so FG%/FT% and raw counting stats are comparable).
                denom = (abs(va) + abs(vb)) or 1e-9
                margin_pct = abs(va - vb) / denom
                if rec["closest_margin"] is None or margin_pct < rec["closest_margin"]:
                    rec["closest_margin"] = round(margin_pct, 4)
                    rec["closest_cat"] = cat
                    rec["closest_week"] = wk

    out = []
    for rec in pair_records.values():
        out.append({
            "teamA": team_names.get(rec["a"], "Unknown"),
            "teamB": team_names.get(rec["b"], "Unknown"),
            "meetings": rec["meetings"],
            "teamA_catW": rec["a_catw"],
            "teamB_catW": rec["b_catw"],
            "ties": rec["ties"],
            "closestCat": rec["closest_cat"],
            "closestMarginPct": round(rec["closest_margin"] * 100, 1) if rec["closest_margin"] is not None else None,
            "closestWeek": rec["closest_week"],
        })
    out.sort(key=lambda r: r["meetings"], reverse=True)
    return out


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
    transactions, raw_activity = fetch_transactions(league)
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

    # ------------------------------------------------------------------
    # New features (each wrapped so one failure doesn't sink the whole run —
    # same defensive pattern as free_agents/transactions above).
    # ------------------------------------------------------------------
    player_pool = {}
    try:
        player_pool = build_player_pool(league, teams, free_agents)
    except Exception as e:
        print(f"Warning: player pool build failed ({e}); player-level features will be empty.")

    player_contribution = []
    try:
        player_contribution = build_player_contribution(teams, player_pool)
    except Exception as e:
        print(f"Warning: player contribution build failed ({e}).")

    waiver_report = []
    try:
        waiver_report = build_waiver_report(raw_activity, player_pool, team_names)
    except Exception as e:
        print(f"Warning: waiver report build failed ({e}).")

    matchup_win_prob = []
    try:
        matchup_win_prob = build_matchup_win_probability(
            league, current_week, season_power, team_names, weekly_matchups
        )
    except Exception as e:
        print(f"Warning: matchup win probability build failed ({e}).")

    draft_grades = {"picks": [], "team_grades": []}
    try:
        draft_picks = fetch_draft(league)
        draft_grades = build_draft_grades(draft_picks, player_pool, team_names)
    except Exception as e:
        print(f"Warning: draft grade build failed ({e}).")

    rivalry_scores = []
    try:
        rivalry_scores = build_rivalry_scores(weekly_matchups, weekly_pairs, team_names)
    except Exception as e:
        print(f"Warning: rivalry score build failed ({e}).")

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
        "playerContribution": player_contribution,
        "waiverReport": waiver_report,
        "matchupWinProb": matchup_win_prob,
        "draftGrades": draft_grades,
        "rivalryScores": rivalry_scores,
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
