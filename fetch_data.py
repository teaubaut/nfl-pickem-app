#!/usr/bin/env python3
"""
fetch_data.py - data builder for a weekly NFL straight-up (win/loss) pool.

Pipeline
  1. ESPN scoreboard    -> this week's games (IDs, teams, kickoff times)
  2. The Odds API       -> moneyline, spread, and total from US sportsbooks
  3. Vig-free math      -> fair win probabilities from the moneylines
  4. Kalshi (KXNFLGAME) -> prediction-market win probabilities (bid/ask midpoints)
  5. Polymarket (Gamma) -> second prediction market (bid/ask midpoints, volume, liquidity)
  6. Blend              -> weighted average of the three, a pick, and upset flags
     Public splits      -> Action Network moneyline ticket % as a proxy for pool picks
  7. Monday Night       -> projected final score snapped to key numbers
  8. Write data.json

Usage
  export ODDS_API_KEY=your_key_here
  python3 fetch_data.py                      # current week -> data.json
  python3 fetch_data.py --output picks.json
  python3 fetch_data.py --week 5 --season 2026 --season-type 2
  python3 fetch_data.py --book-weight 2      # sportsbooks count double

Requires Python 3.9+. Standard library only (no pip installs).
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import math
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo

    EASTERN = ZoneInfo("America/New_York")
except Exception:  # zoneinfo/tzdata unavailable (e.g. bare Windows install)
    EASTERN = timezone(timedelta(hours=-5), "ET")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
# ESPN has been returning 403s to scripts on site.api.espn.com; the
# site.web.api.espn.com host serves the same JSON and is tried first.
ESPN_URLS = [
    "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
]
ESPN_HEADERS = {"Referer": "https://www.espn.com/", "Origin": "https://www.espn.com"}
ESPN_RETRY_CODES = (403, 429, 500, 502, 503, 504)
ODDS_URL = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/"
KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
KALSHI_SERIES = "KXNFLGAME"

POLYMARKET_URL = "https://gamma-api.polymarket.com"
# Action Network's scoreboard is date-based, so it is queried once per game date.
# v2 nests splits under markets[book].event.moneyline[].bet_info; v1 uses flat fields.
ACTION_ENDPOINTS = [
    ("v2", "https://api.actionnetwork.com/web/v2/scoreboard/nfl"),
    ("v1", "https://api.actionnetwork.com/web/v1/scoreboard/nfl"),
]
ACTION_BOOK_IDS = "15,30,68,69,71,75,79"
ACTION_CONSENSUS_BOOK_ID = 15    # preferred odds row for public splits when present
PUBLIC_DEBUG_FILE = "public_debug.json"
LEVERAGE_FLAG = 0.15             # win prob minus public % that counts as "high leverage"
LEVERAGE_MAX_WIN_PROB = 0.65     # skip flags on big favorites (see public_splits notes)

# Relative blend weights (renormalized over whichever sources have a price).
# Equal weights = (Vegas + Kalshi + Polymarket) / 3.
DEFAULT_WEIGHTS = {"sportsbook": 1.0, "kalshi": 1.0, "polymarket": 1.0}
UPSET_BAND = (0.42, 0.49)        # underdog win prob that flags a live upset
MAX_MARKET_SPREAD = 0.20         # ignore quotes with bid/ask wider than 20c
MATCH_WINDOW_HOURS = 36          # max kickoff mismatch when pairing sources
KEY_SCORES = [3, 6, 7, 10, 13, 14, 16, 17, 20, 21, 23, 24,
              27, 28, 30, 31, 34, 35, 38, 41, 42, 45]

HTTP_TIMEOUT = 20
HTTP_RETRIES = 3
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.5 Safari/605.1.15")
DEFAULT_RETRY_CODES = (429, 500, 502, 503, 504)

log = logging.getLogger("fetch_data")

# --------------------------------------------------------------------------- #
# Team mapping (canonical key = ESPN abbreviation)
# --------------------------------------------------------------------------- #
TEAMS: dict[str, dict[str, Any]] = {
    "ARI": {"name": "Arizona Cardinals", "codes": ["ARI", "ARZ"], "aliases": ["arizona", "cardinals"]},
    "ATL": {"name": "Atlanta Falcons", "codes": ["ATL"], "aliases": ["atlanta", "falcons"]},
    "BAL": {"name": "Baltimore Ravens", "codes": ["BAL"], "aliases": ["baltimore", "ravens"]},
    "BUF": {"name": "Buffalo Bills", "codes": ["BUF"], "aliases": ["buffalo", "bills"]},
    "CAR": {"name": "Carolina Panthers", "codes": ["CAR"], "aliases": ["carolina", "panthers"]},
    "CHI": {"name": "Chicago Bears", "codes": ["CHI"], "aliases": ["chicago", "bears"]},
    "CIN": {"name": "Cincinnati Bengals", "codes": ["CIN"], "aliases": ["cincinnati", "bengals"]},
    "CLE": {"name": "Cleveland Browns", "codes": ["CLE"], "aliases": ["cleveland", "browns"]},
    "DAL": {"name": "Dallas Cowboys", "codes": ["DAL"], "aliases": ["dallas", "cowboys"]},
    "DEN": {"name": "Denver Broncos", "codes": ["DEN"], "aliases": ["denver", "broncos"]},
    "DET": {"name": "Detroit Lions", "codes": ["DET"], "aliases": ["detroit", "lions"]},
    "GB":  {"name": "Green Bay Packers", "codes": ["GB", "GNB"], "aliases": ["green bay", "packers"]},
    "HOU": {"name": "Houston Texans", "codes": ["HOU"], "aliases": ["houston", "texans"]},
    "IND": {"name": "Indianapolis Colts", "codes": ["IND"], "aliases": ["indianapolis", "colts"]},
    "JAX": {"name": "Jacksonville Jaguars", "codes": ["JAX", "JAC"], "aliases": ["jacksonville", "jaguars"]},
    "KC":  {"name": "Kansas City Chiefs", "codes": ["KC", "KAN"], "aliases": ["kansas city", "chiefs"]},
    "LV":  {"name": "Las Vegas Raiders", "codes": ["LV", "LVR", "OAK"], "aliases": ["las vegas", "raiders"]},
    "LAC": {"name": "Los Angeles Chargers", "codes": ["LAC"], "aliases": ["chargers", "la chargers", "los angeles c"]},
    "LAR": {"name": "Los Angeles Rams", "codes": ["LAR", "LA"], "aliases": ["rams", "la rams", "los angeles r"]},
    "MIA": {"name": "Miami Dolphins", "codes": ["MIA"], "aliases": ["miami", "dolphins"]},
    "MIN": {"name": "Minnesota Vikings", "codes": ["MIN"], "aliases": ["minnesota", "vikings"]},
    "NE":  {"name": "New England Patriots", "codes": ["NE", "NWE"], "aliases": ["new england", "patriots"]},
    "NO":  {"name": "New Orleans Saints", "codes": ["NO", "NOR"], "aliases": ["new orleans", "saints"]},
    "NYG": {"name": "New York Giants", "codes": ["NYG"], "aliases": ["giants", "ny giants", "new york g"]},
    "NYJ": {"name": "New York Jets", "codes": ["NYJ"], "aliases": ["jets", "ny jets", "new york j"]},
    "PHI": {"name": "Philadelphia Eagles", "codes": ["PHI"], "aliases": ["philadelphia", "eagles"]},
    "PIT": {"name": "Pittsburgh Steelers", "codes": ["PIT"], "aliases": ["pittsburgh", "steelers"]},
    "SF":  {"name": "San Francisco 49ers", "codes": ["SF", "SFO"], "aliases": ["san francisco", "49ers", "niners"]},
    "SEA": {"name": "Seattle Seahawks", "codes": ["SEA"], "aliases": ["seattle", "seahawks"]},
    "TB":  {"name": "Tampa Bay Buccaneers", "codes": ["TB", "TAM"], "aliases": ["tampa bay", "buccaneers", "bucs"]},
    "TEN": {"name": "Tennessee Titans", "codes": ["TEN"], "aliases": ["tennessee", "titans"]},
    "WSH": {"name": "Washington Commanders", "codes": ["WSH", "WAS"], "aliases": ["washington", "commanders"]},
}


def _norm(text: str) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


_CODE_INDEX = {code: key for key, t in TEAMS.items() for code in t["codes"]}
_NAME_INDEX = {_norm(s): key for key, t in TEAMS.items() for s in [t["name"], *t["aliases"]]}


def resolve_team(text: Optional[str]) -> Optional[str]:
    """Map any team label (abbr, city, nickname, full name) to a canonical key."""
    if not text:
        return None
    raw = text.strip()
    if raw.upper() in _CODE_INDEX:
        return _CODE_INDEX[raw.upper()]
    key = _norm(raw)
    if key in _NAME_INDEX:
        return _NAME_INDEX[key]
    # A known alias appearing as whole words inside a longer label.
    hits = {abbr for alias, abbr in _NAME_INDEX.items()
            if len(alias) >= 4 and re.search(rf"\b{re.escape(alias)}\b", key)}
    if len(hits) == 1:
        return hits.pop()
    # Last resort: fuzzy match (typos, odd spacing).
    close = difflib.get_close_matches(key, list(_NAME_INDEX), n=1, cutoff=0.82)
    return _NAME_INDEX[close[0]] if close else None


def team_block(key: str, source_abbr: Optional[str] = None) -> dict:
    block = {"abbr": key, "name": TEAMS[key]["name"]}
    if source_abbr and source_abbr != key:
        block["espn_abbr"] = source_abbr
    return block


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _redact(url: str) -> str:
    return re.sub(r"(apiKey=)[^&]+", r"\1***", url)


def http_get_json(url: str, params: Optional[dict] = None, *,
                  retry_codes: tuple = DEFAULT_RETRY_CODES,
                  extra_headers: Optional[dict] = None,
                  backoff: float = 2.0) -> tuple[Any, Any]:
    """GET a JSON endpoint with retries on rate limits / server errors."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        **(extra_headers or {}),
    }
    req = urllib.request.Request(url, headers=headers)
    last_err: Optional[Exception] = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8")), resp.headers
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code not in retry_codes:
                raise RuntimeError(f"GET {_redact(url)} -> HTTP {exc.code} {exc.reason}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = exc
        if attempt < HTTP_RETRIES:
            time.sleep(backoff * 2 ** (attempt - 1))
    raise RuntimeError(f"GET {_redact(url)} failed after {HTTP_RETRIES} attempts: {last_err}")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1)
    if re.search(r"[+-]\d{2}$", text):
        text += ":00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def r4(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 4)


def pair(home: Optional[float]) -> Optional[dict]:
    return None if home is None else {"home": r4(home), "away": r4(1 - home)}


def american_to_prob(odds: float) -> Optional[float]:
    odds = float(odds)
    if odds >= 100:
        return 100 / (odds + 100)
    if odds <= -100:
        return -odds / (-odds + 100)
    return None


def prob_to_american(p: Optional[float]) -> Optional[int]:
    if p is None or not 0 < p < 1:
        return None
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


# --------------------------------------------------------------------------- #
# 1. ESPN
# --------------------------------------------------------------------------- #
def fetch_espn_games(week: Optional[int], season: Optional[int],
                     season_type: Optional[int]) -> tuple[list[dict], dict]:
    params: dict[str, Any] = {}
    if week:
        params["week"] = week
    if season:
        params["dates"] = season
    if season_type:
        params["seasontype"] = season_type
    data, errors = None, []
    for url in ESPN_URLS:
        try:
            data, _ = http_get_json(url, params or None, retry_codes=ESPN_RETRY_CODES,
                                    extra_headers=ESPN_HEADERS, backoff=10)
            break
        except Exception as exc:
            log.warning("ESPN host failed: %s", exc)
            errors.append(str(exc))
    if data is None:
        raise RuntimeError("; ".join(errors))

    games = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        sides = {c.get("homeAway"): c for c in comp.get("competitors", [])}
        if "home" not in sides or "away" not in sides:
            continue
        home_t, away_t = sides["home"].get("team", {}), sides["away"].get("team", {})
        home = resolve_team(home_t.get("abbreviation")) or resolve_team(home_t.get("displayName"))
        away = resolve_team(away_t.get("abbreviation")) or resolve_team(away_t.get("displayName"))
        kickoff = parse_iso(ev.get("date"))
        if not (home and away and kickoff):
            log.warning("Skipping ESPN event %s (unrecognized teams or date)", ev.get("id"))
            continue
        status = (comp.get("status") or ev.get("status") or {}).get("type", {})
        winner = next((side for side in ("home", "away") if sides[side].get("winner")), None)
        games.append({
            "game_id": str(ev.get("id")),
            "name": ev.get("name"),
            "short_name": ev.get("shortName"),
            "kickoff": kickoff,
            "status": status.get("description"),
            "completed": bool(status.get("completed")),
            "neutral_site": bool(comp.get("neutralSite")),
            "home": home, "away": away,
            "home_score": _num(sides["home"].get("score")),
            "away_score": _num(sides["away"].get("score")),
            "winner": {"home": home, "away": away}.get(winner),
            "home_espn_abbr": home_t.get("abbreviation"),
            "away_espn_abbr": away_t.get("abbreviation"),
        })

    meta = {
        "season": (data.get("season") or {}).get("year"),
        "season_type": (data.get("season") or {}).get("type"),
        "week": (data.get("week") or {}).get("number"),
        "schedule_source": "espn",
    }
    return games, meta


# --------------------------------------------------------------------------- #
# 1b. Fallback schedule from The Odds API (used only if ESPN is unreachable)
# --------------------------------------------------------------------------- #
def estimate_week(now: datetime) -> dict:
    """Estimate season/week from the calendar (weeks start the Tuesday after Labor Day)."""
    today = now.astimezone(EASTERN).date()

    def week1_start(year: int) -> date:
        sept1 = date(year, 9, 1)
        labor_day = sept1 + timedelta(days=(0 - sept1.weekday()) % 7)
        return labor_day + timedelta(days=1)

    season = today.year if today >= week1_start(today.year) else today.year - 1
    week = (today - week1_start(season)).days // 7 + 1
    if week <= 18:
        return {"season": season, "season_type": 2, "week": week}
    if week <= 23:
        return {"season": season, "season_type": 3, "week": week - 18}
    return {"season": season, "season_type": None, "week": None}


def games_from_odds(events: list[dict], now: datetime) -> list[dict]:
    """Build this week's games (through Monday night) from Odds API events."""
    days_to_tuesday = (1 - now.weekday()) % 7
    window_end = datetime.combine(now.date() + timedelta(days=days_to_tuesday),
                                  datetime.min.time(), timezone.utc) + timedelta(hours=12)
    if window_end <= now:
        window_end += timedelta(days=7)
    window_start = now - timedelta(hours=6)

    games = []
    for ev in events:
        kickoff = parse_iso(ev.get("commence_time"))
        home = resolve_team(ev.get("home_team"))
        away = resolve_team(ev.get("away_team"))
        if not (kickoff and home and away) or not window_start <= kickoff < window_end:
            continue
        games.append({
            "game_id": f"odds-{ev.get('id')}",
            "name": f"{TEAMS[away]['name']} at {TEAMS[home]['name']}",
            "short_name": f"{away} @ {home}",
            "kickoff": kickoff,
            "status": "Scheduled",
            "completed": False,
            "neutral_site": False,
            "home": home, "away": away,
            "home_espn_abbr": home, "away_espn_abbr": away,
        })
    return games


# --------------------------------------------------------------------------- #
# 2-3. The Odds API + vig-free probabilities
# --------------------------------------------------------------------------- #
def fetch_odds_events(api_key: str) -> tuple[list[dict], dict]:
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    data, headers = http_get_json(ODDS_URL, params)
    quota = {
        "requests_remaining": headers.get("x-requests-remaining"),
        "requests_used": headers.get("x-requests-used"),
    }
    return (data if isinstance(data, list) else []), quota


def find_matching(game: dict, events: list[dict], teams_of, time_of) -> Optional[dict]:
    """Pick the event with the same two teams and the closest kickoff."""
    target = {game["home"], game["away"]}
    best: Optional[tuple[float, dict]] = None
    for ev in events:
        if teams_of(ev) != target:
            continue
        t = time_of(ev)
        diff = abs((t - game["kickoff"]).total_seconds()) if t else 0.0
        if diff <= MATCH_WINDOW_HOURS * 3600 and (best is None or diff < best[0]):
            best = (diff, ev)
    return best[1] if best else None


def summarize_odds(ev: dict, home: str, away: str) -> dict:
    """Consensus lines across books, oriented to ESPN's home/away."""
    imp_home, imp_away, novig_home, holds, books = [], [], [], [], []
    spreads_home, totals = [], []

    for bk in ev.get("bookmakers", []):
        markets = {m.get("key"): m for m in bk.get("markets", [])}

        h2h = markets.get("h2h")
        if h2h:
            prices = {resolve_team(o.get("name")): o.get("price") for o in h2h.get("outcomes", [])}
            ph, pa = prices.get(home), prices.get(away)
            ih = american_to_prob(ph) if ph is not None else None
            ia = american_to_prob(pa) if pa is not None else None
            if ih and ia:
                imp_home.append(ih)
                imp_away.append(ia)
                novig_home.append(ih / (ih + ia))
                holds.append(ih + ia - 1)
                books.append(bk.get("key"))

        for o in (markets.get("spreads") or {}).get("outcomes", []):
            if o.get("point") is None:
                continue
            team = resolve_team(o.get("name"))
            if team == home:
                spreads_home.append(float(o["point"]))
            elif team == away:
                spreads_home.append(-float(o["point"]))

        for o in (markets.get("totals") or {}).get("outcomes", []):
            if o.get("name") == "Over" and o.get("point") is not None:
                totals.append(float(o["point"]))

    spread = round(statistics.median(spreads_home) * 2) / 2 if spreads_home else None
    total = round(statistics.median(totals) * 2) / 2 if totals else None
    fair_home = statistics.fmean(novig_home) if novig_home else None

    return {
        "odds_api_event_id": ev.get("id"),
        "bookmakers_used": books,
        "moneyline": None if not books else {
            "home": prob_to_american(statistics.median(imp_home)),
            "away": prob_to_american(statistics.median(imp_away)),
            "avg_hold_pct": round(statistics.fmean(holds) * 100, 2),
        },
        "fair_moneyline": None if fair_home is None else {
            "home": prob_to_american(fair_home),
            "away": prob_to_american(1 - fair_home),
        },
        "spread": None if spread is None else {"home": spread, "away": -spread},
        "total": total,
        "vig_free_home_prob": fair_home,
    }


# --------------------------------------------------------------------------- #
# 4. Kalshi
# --------------------------------------------------------------------------- #
_KALSHI_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")


def fetch_kalshi_markets() -> list[dict]:
    markets: list[dict] = []
    cursor = None
    for _ in range(20):  # pagination safety cap
        params: dict[str, Any] = {"limit": 100, "series_ticker": KALSHI_SERIES, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        data, _ = http_get_json(KALSHI_URL, params)
        page = data.get("markets") or []
        markets.extend(page)
        cursor = data.get("cursor")
        if not cursor or not page:
            break
    return markets


def _kalshi_price(m: dict, field: str) -> Optional[float]:
    """Kalshi quotes in dollars (`*_dollars`) or legacy integer cents."""
    val = m.get(f"{field}_dollars")
    if val not in (None, ""):
        try:
            return float(val)
        except (TypeError, ValueError):
            pass
    val = m.get(field)
    return val / 100 if isinstance(val, (int, float)) else None


def _usable_quote(bid: Optional[float], ask: Optional[float]) -> bool:
    return (bid is not None and ask is not None and 0 < bid <= ask < 1
            and ask - bid <= MAX_MARKET_SPREAD)


def kalshi_yes_prob(m: dict) -> tuple[Optional[float], Optional[str]]:
    yb, ya = _kalshi_price(m, "yes_bid"), _kalshi_price(m, "yes_ask")
    nb, na = _kalshi_price(m, "no_bid"), _kalshi_price(m, "no_ask")
    estimates = []
    if _usable_quote(yb, ya):
        estimates.append((yb + ya) / 2)
    if _usable_quote(nb, na):
        estimates.append(1 - (nb + na) / 2)
    if estimates:
        return statistics.fmean(estimates), "bid_ask_midpoint"
    last = _kalshi_price(m, "last_price")
    if last is not None and 0 < last < 1:
        return last, "last_price"
    return None, None


def kalshi_market_team(m: dict) -> Optional[str]:
    ticker = m.get("ticker") or ""
    suffix = ticker.rsplit("-", 1)[-1] if ticker.count("-") >= 2 else ""
    return resolve_team(suffix) or resolve_team(m.get("yes_sub_title"))


def kalshi_event_date(event_ticker: str) -> Optional[date]:
    match = _KALSHI_DATE.search(event_ticker or "")
    if not match:
        return None
    try:
        return datetime.strptime("".join(match.groups()), "%y%b%d").date()
    except ValueError:
        return None


def group_kalshi_events(markets: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for m in markets:
        et = m.get("event_ticker") or ""
        team = kalshi_market_team(m)
        if not et or not team:
            continue
        g = groups.setdefault(et, {"event_ticker": et, "date": kalshi_event_date(et), "markets": {}})
        g["markets"][team] = m
    return list(groups.values())


def kalshi_for_game(game: dict, events: list[dict]) -> Optional[dict]:
    kickoff_date = game["kickoff"].astimezone(EASTERN).date()
    candidates = [e for e in events
                  if e["date"] is None or abs((e["date"] - kickoff_date).days) <= 1]
    ev = find_matching(game, candidates, lambda e: set(e["markets"]), lambda e: None)
    if not ev:
        return None

    detail, probs = [], {}
    for side in ("home", "away"):
        m = ev["markets"].get(game[side])
        if not m:
            continue
        p, method = kalshi_yes_prob(m)
        probs[side] = p
        detail.append({
            "ticker": m.get("ticker"),
            "team": game[side],
            "yes_bid": _kalshi_price(m, "yes_bid"),
            "yes_ask": _kalshi_price(m, "yes_ask"),
            "no_bid": _kalshi_price(m, "no_bid"),
            "no_ask": _kalshi_price(m, "no_ask"),
            "yes_prob": r4(p),
            "method": method,
            "volume": m.get("volume"),
        })

    ph, pa = probs.get("home"), probs.get("away")
    if ph is not None and pa is not None and ph + pa > 0:
        home_prob = ph / (ph + pa)  # normalize the two contracts
    elif ph is not None:
        home_prob = ph
    elif pa is not None:
        home_prob = 1 - pa
    else:
        home_prob = None
    return {"event_ticker": ev["event_ticker"], "home_prob": home_prob, "markets": detail}


# --------------------------------------------------------------------------- #
# 5. Polymarket (Gamma API, no key needed)
# --------------------------------------------------------------------------- #
# Game moneylines have two outcomes named after the teams; spread markets use
# team names too, so anything that looks like a spread/total/prop is skipped.
_PM_NOT_MONEYLINE = re.compile(
    r"spread|o/u|\bover\b|\bunder\b|total|half|quarter|\b[12]h\b|\bq[1-4]\b|points|yards|touchdown|\(",
    re.I,
)


def _json_list(value: Any) -> list:
    """Gamma returns outcomes/outcomePrices as JSON-encoded strings."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _num(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def polymarket_filters() -> list[dict]:
    """NFL series id from /sports if available, then a tag-slug fallback."""
    filters: list[dict] = []
    try:
        sports, _ = http_get_json(f"{POLYMARKET_URL}/sports")
        for s in sports if isinstance(sports, list) else []:
            if str(s.get("sport", "")).lower() == "nfl" and s.get("series"):
                filters.append({"series_id": s["series"]})
                break
    except Exception as exc:
        log.warning("Polymarket /sports lookup failed: %s", exc)
    filters.append({"tag_slug": "nfl"})
    return filters


def fetch_polymarket_events() -> tuple[list[dict], dict]:
    last_filter: dict = {}
    for flt in polymarket_filters():
        last_filter = flt
        events: list[dict] = []
        for page in range(10):  # pagination safety cap
            params = {**flt, "active": "true", "closed": "false",
                      "limit": 100, "offset": page * 100}
            data, _ = http_get_json(f"{POLYMARKET_URL}/events", params)
            batch = data if isinstance(data, list) else (data or {}).get("data") or []
            events.extend(batch)
            if len(batch) < 100:
                break
        if events:
            return events, flt
    return [], last_filter


def polymarket_candidates(events: list[dict]) -> list[dict]:
    """One moneyline market per game event."""
    out = []
    for ev in events:
        title = _norm(ev.get("title") or "")
        best = None
        for m in ev.get("markets") or []:
            if m.get("closed"):
                continue
            outcomes = _json_list(m.get("outcomes"))
            if len(outcomes) != 2:
                continue
            teams = [resolve_team(str(o)) for o in outcomes]
            if None in teams or teams[0] == teams[1]:
                continue
            market_type = str(m.get("sportsMarketType") or "").lower()
            question = str(m.get("question") or "")
            if market_type:
                if market_type != "moneyline":
                    continue
                rank = 0
            elif _norm(question) == title:
                rank = 1
            elif not _PM_NOT_MONEYLINE.search(question):
                rank = 2
            else:
                continue
            if best is None or rank < best[0]:
                best = (rank, m, teams)
        if not best:
            continue
        _, market, teams = best
        start = (parse_iso(market.get("gameStartTime")) or parse_iso(ev.get("startTime"))
                 or parse_iso(ev.get("gameStartTime")))
        out.append({"event": ev, "market": market, "teams": teams, "time": start})
    return out


def polymarket_first_outcome_prob(m: dict) -> tuple[Optional[float], Optional[str]]:
    """bestBid/bestAsk and outcomePrices[0] all refer to the first outcome."""
    bid, ask = _num(m.get("bestBid")), _num(m.get("bestAsk"))
    if _usable_quote(bid, ask):
        return (bid + ask) / 2, "bid_ask_midpoint"
    prices = [_num(p) for p in _json_list(m.get("outcomePrices"))]
    if len(prices) == 2 and None not in prices and 0 < prices[0] < 1 and sum(prices) > 0:
        return prices[0] / sum(prices), "outcome_price"
    last = _num(m.get("lastTradePrice"))
    if last is not None and 0 < last < 1:
        return last, "last_trade"
    return None, None


def polymarket_for_game(game: dict, candidates: list[dict]) -> Optional[dict]:
    cand = find_matching(game, candidates, lambda c: set(c["teams"]), lambda c: c["time"])
    if not cand:
        return None
    m, ev, teams = cand["market"], cand["event"], cand["teams"]
    p0, method = polymarket_first_outcome_prob(m)
    home_prob = None
    if p0 is not None:
        home_prob = p0 if teams[0] == game["home"] else 1 - p0
    slug = ev.get("slug")
    return {
        "home_prob": home_prob,
        "detail": {
            "event_slug": slug,
            "url": f"https://polymarket.com/event/{slug}" if slug else None,
            "question": m.get("question"),
            "quoted_team": teams[0],
            "best_bid": _num(m.get("bestBid")),
            "best_ask": _num(m.get("bestAsk")),
            "last_trade": _num(m.get("lastTradePrice")),
            "method": method,
            "volume": _num(m.get("volumeNum") if m.get("volumeNum") is not None else m.get("volume")),
            "volume_24h": _num(m.get("volume24hr")),
            "liquidity": _num(m.get("liquidityNum") if m.get("liquidityNum") is not None else m.get("liquidity")),
        },
    }


# --------------------------------------------------------------------------- #
# 6. Blend + pick
# --------------------------------------------------------------------------- #
def blend(probs: dict[str, Optional[float]],
          weights: dict[str, float]) -> tuple[Optional[float], dict[str, float]]:
    """Weighted average over sources that have a price; returns effective weights."""
    live = {k: weights.get(k, 0.0) for k, v in probs.items()
            if v is not None and weights.get(k, 0.0) > 0}
    total = sum(live.values())
    effective = {k: round(live.get(k, 0.0) / total, 3) if total else 0.0 for k in probs}
    if not total:
        return None, effective
    return sum(probs[k] * w for k, w in live.items()) / total, effective


def confidence_tier(p: float) -> str:
    if p >= 0.75:
        return "strong"
    if p >= 0.60:
        return "solid"
    if p >= 0.55:
        return "lean"
    return "toss-up"


def make_pick(game: dict, home_prob: Optional[float]) -> dict:
    if home_prob is None:
        return {"team": None, "note": "No sportsbook or prediction-market data available"}
    pick = game["home"] if home_prob >= 0.5 else game["away"]
    dog = game["away"] if pick == game["home"] else game["home"]
    p_pick = max(home_prob, 1 - home_prob)
    p_dog = 1 - p_pick
    live_upset = UPSET_BAND[0] <= p_dog <= UPSET_BAND[1]
    return {
        "team": pick,
        "name": TEAMS[pick]["name"],
        "win_probability": r4(p_pick),
        "confidence": confidence_tier(p_pick),
        "coin_flip": abs(home_prob - 0.5) < 1e-9,
        "underdog": dog,
        "underdog_win_probability": r4(p_dog),
        "live_upset_candidate": live_upset,
        "upset_note": (f"{TEAMS[dog]['name']} at {p_dog:.1%} is inside the "
                       f"{UPSET_BAND[0]:.0%}-{UPSET_BAND[1]:.0%} upset band") if live_upset else None,
    }


# --------------------------------------------------------------------------- #
# 6b. Public betting splits (Action Network, unofficial, no key)
# --------------------------------------------------------------------------- #
# Moneyline *ticket* % is a proxy for what casual pool players pick. It is
# weakest on big favorites: bettors dodge -400 moneylines and bet the spread
# instead, so ticket % understates how many pool players take the favorite.
# That is why leverage flags are limited to games at or under
# LEVERAGE_MAX_WIN_PROB.
_PUBLIC_KEYS = [
    ("ml_home_public", "ml_away_public"),
    ("moneyline_home_public", "moneyline_away_public"),
    ("ml_home_tickets", "ml_away_tickets"),
]


def _public_pair(odds_row: dict) -> Optional[tuple[float, float]]:
    for home_key, away_key in _PUBLIC_KEYS:
        home, away = _num(odds_row.get(home_key)), _num(odds_row.get(away_key))
        if home is None or away is None or home + away <= 0:
            continue
        if home > 1 or away > 1:  # whole-number percentages (65 = 65%)
            home, away = home / 100, away / 100
        total = home + away
        return home / total, away / total  # normalize to sum to 1
    return None


def _pct(value: Any) -> Optional[float]:
    v = _num(value)
    if v is None or v < 0:
        return None
    return v / 100 if v > 1 else v


def _v2_public_pair(game: dict) -> Optional[tuple[float, float]]:
    """v2: markets[book_id].event.moneyline[] -> {side, bet_info: {tickets: {percent}}}."""
    markets = game.get("markets")
    if not isinstance(markets, dict):
        return None
    books = sorted(markets, key=lambda k: 0 if str(k) == str(ACTION_CONSENSUS_BOOK_ID) else 1)
    for book in books:
        node = markets.get(book)
        if not isinstance(node, dict):
            continue
        event = node.get("event") if isinstance(node.get("event"), dict) else node
        lines = event.get("moneyline")
        if not isinstance(lines, list):
            continue
        found = {}
        for line in lines:
            if not isinstance(line, dict):
                continue
            info = line.get("bet_info") or {}
            tickets = info.get("tickets") if isinstance(info, dict) else None
            value = _pct((tickets or {}).get("percent")) if isinstance(tickets, dict) else None
            if line.get("side") in ("home", "away") and value is not None:
                found[line["side"]] = value
        if len(found) == 2 and sum(found.values()) > 0:
            total = sum(found.values())
            return found["home"] / total, found["away"] / total
    return None


def _split_paths(node: Any, prefix: str = "", out: Optional[list] = None, depth: int = 0) -> list:
    """Debug helper: every field path that looks like a betting split."""
    out = [] if out is None else out
    if len(out) >= 40 or depth > 7:
        return out
    if isinstance(node, dict):
        for k, v in node.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if re.search(r"public|ticket|bet_info|percent|money", str(k), re.I) and not isinstance(v, (dict, list)):
                out.append(f"{path} = {v}")
            _split_paths(v, path, out, depth + 1)
    elif isinstance(node, list):
        for i, v in enumerate(node[:3]):
            _split_paths(v, f"{prefix}[{i}]", out, depth + 1)
    return out


def parse_action_game(game: dict) -> Optional[dict]:
    teams = {t.get("id"): t for t in game.get("teams") or [] if isinstance(t, dict)}
    home_t = teams.get(game.get("home_team_id")) or {}
    away_t = teams.get(game.get("away_team_id")) or {}
    home = resolve_team(home_t.get("abbr")) or resolve_team(home_t.get("full_name"))
    away = resolve_team(away_t.get("abbr")) or resolve_team(away_t.get("full_name"))
    if not (home and away):
        return None
    split = _v2_public_pair(game)
    if not split:
        rows = sorted((r for r in game.get("odds") or [] if isinstance(r, dict)),
                      key=lambda r: 0 if r.get("book_id") == ACTION_CONSENSUS_BOOK_ID else 1)
        for row in rows:
            split = _public_pair(row)
            if split:
                break
    return {"home": home, "away": away, "time": parse_iso(game.get("start_time")),
            "home_pct": split[0] if split else None, "away_pct": split[1] if split else None}


def fetch_public_splits(game_dates: list[str], debug: Optional[list] = None,
                        needed: Optional[set] = None) -> list[dict]:
    """Try each endpoint across this week's game dates; first one with splits wins.

    The v2 response usually covers the whole week, so requests stop as soon as
    every matchup in `needed` (a set of frozenset team pairs) has splits.
    """
    headers = {"Referer": "https://www.actionnetwork.com/", "Origin": "https://www.actionnetwork.com"}
    for name, url in ACTION_ENDPOINTS:
        found: dict[tuple, dict] = {}
        for day in game_dates or [None]:
            params = {"bookIds": ACTION_BOOK_IDS}
            if day:
                params["date"] = day
            entry: dict[str, Any] = {"endpoint": name, "date": day}
            try:
                data, _ = http_get_json(url, params, extra_headers=headers)
            except Exception as exc:
                entry["error"] = str(exc)
                log.warning("Action Network %s %s failed: %s", name, day, exc)
                if debug is not None:
                    debug.append(entry)
                continue
            games = data.get("games", []) if isinstance(data, dict) else []
            parsed = [p for p in (parse_action_game(g) for g in games if isinstance(g, dict)) if p]
            with_split = [p for p in parsed if p["home_pct"] is not None]
            for p in with_split:
                found[(frozenset((p["home"], p["away"])), p["time"])] = p
            entry.update({
                "games": len(games),
                "matchups": [f"{p['away']}@{p['home']}" for p in parsed],
                "with_splits": len(with_split),
            })
            if debug is not None:
                entry["top_level_keys"] = sorted(data.keys()) if isinstance(data, dict) else type(data).__name__
                if games:
                    entry["game_keys"] = sorted(games[0].keys())
                    entry["split_like_fields"] = _split_paths(games[0])
                debug.append(entry)
            log.info("Action Network %s %s: %d games, %d with public splits",
                     name, day or "(no date)", len(games), len(with_split))
            if needed and needed <= {pair for pair, _ in found}:
                break
        if found:
            return list(found.values())
    return []


def add_public_leverage(game: dict, pick: dict, home_prob: Optional[float],
                        splits: list[dict]) -> Optional[dict]:
    """Adds public % and leverage (our win prob minus public %) to the pick."""
    match = find_matching(game, splits, lambda s: {s["home"], s["away"]}, lambda s: s["time"])
    if not match:
        return None
    pct = {match["home"]: match["home_pct"], match["away"]: match["away_pct"]}
    public = {"home": r4(pct.get(game["home"])), "away": r4(pct.get(game["away"]))}
    if not pick.get("team") or home_prob is None:
        return public

    p_pick = pick["win_probability"]
    p_dog = pick["underdog_win_probability"]
    lev_pick = p_pick - pct[pick["team"]]
    lev_dog = p_dog - pct[pick["underdog"]]
    eligible = p_pick <= LEVERAGE_MAX_WIN_PROB
    pick.update({
        "public_pick_pct": r4(pct[pick["team"]]),
        "leverage": r4(lev_pick),
        "high_leverage_play": eligible and lev_pick > LEVERAGE_FLAG,
        "underdog_public_pct": r4(pct[pick["underdog"]]),
        "underdog_leverage": r4(lev_dog),
        "high_leverage_underdog": eligible and lev_dog > LEVERAGE_FLAG,
    })
    return public


# --------------------------------------------------------------------------- #
# 7. Monday Night Football projection
# --------------------------------------------------------------------------- #
def snap_to_key(raw: float) -> int:
    rounded = round_half_up(raw)
    return min(KEY_SCORES, key=lambda k: (abs(k - rounded), abs(k - raw)))


def project_mnf(game: dict, odds: Optional[dict], pick: dict) -> dict:
    base = {
        "game_id": game["game_id"],
        "matchup": game["name"],
        "kickoff_et": game["kickoff"].astimezone(EASTERN).isoformat(),
    }
    spread = (odds or {}).get("spread")
    total = (odds or {}).get("total")
    if spread is None or total is None:
        return {**base, "projection_available": False,
                "note": "Spread and total required for a score projection"}

    home_spread = spread["home"]
    magnitude = abs(home_spread)
    if home_spread < 0:
        fav, dog = game["home"], game["away"]
    elif home_spread > 0:
        fav, dog = game["away"], game["home"]
    else:  # pick'em: lean on the blended probability
        fav = pick.get("team") or game["home"]
        dog = game["away"] if fav == game["home"] else game["home"]

    fav_raw = (total + magnitude) / 2
    dog_raw = (total - magnitude) / 2
    fav_score, dog_score = snap_to_key(fav_raw), snap_to_key(dog_raw)
    if magnitude > 0 and fav_score <= dog_score:
        fav_score = next((k for k in KEY_SCORES if k > dog_score), dog_score + 3)

    scores = {fav: fav_score, dog: dog_score}
    return {
        **base,
        "projection_available": True,
        "favorite": fav,
        "underdog": dog,
        "spread": magnitude,
        "total": total,
        "raw": {"favorite": round(fav_raw, 2), "underdog": round(dog_raw, 2)},
        "projected": {"favorite": fav_score, "underdog": dog_score},
        "projected_final": {
            "home": {"team": game["home"], "score": scores[game["home"]]},
            "away": {"team": game["away"], "score": scores[game["away"]]},
        },
        "projected_total": fav_score + dog_score,
        "projected_margin": fav_score - dog_score,
    }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def build(args: argparse.Namespace) -> dict:
    sources: dict[str, Any] = {}

    odds_events: list[dict] = []
    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    if not api_key:
        log.warning("ODDS_API_KEY not set; skipping sportsbook odds")
        sources["odds_api"] = {"ok": False, "error": "ODDS_API_KEY not set"}
    else:
        try:
            odds_events, quota = fetch_odds_events(api_key)
            sources["odds_api"] = {"ok": True, "events": len(odds_events), **quota}
            log.info("Odds API: %d events (%s requests left)", len(odds_events), quota["requests_remaining"])
        except Exception as exc:  # keep going with whatever else we have
            log.error("Odds API failed: %s", exc)
            sources["odds_api"] = {"ok": False, "error": str(exc)}

    kalshi_events: list[dict] = []
    try:
        markets = fetch_kalshi_markets()
        kalshi_events = group_kalshi_events(markets)
        sources["kalshi"] = {"ok": True, "markets": len(markets), "events": len(kalshi_events)}
        log.info("Kalshi: %d markets across %d events", len(markets), len(kalshi_events))
    except Exception as exc:
        log.error("Kalshi failed: %s", exc)
        sources["kalshi"] = {"ok": False, "error": str(exc)}

    poly_candidates: list[dict] = []
    try:
        poly_events, poly_filter = fetch_polymarket_events()
        poly_candidates = polymarket_candidates(poly_events)
        sources["polymarket"] = {"ok": True, "events": len(poly_events),
                                 "game_markets": len(poly_candidates), "filter": poly_filter}
        log.info("Polymarket: %d events, %d game moneylines", len(poly_events), len(poly_candidates))
    except Exception as exc:
        log.error("Polymarket failed: %s", exc)
        sources["polymarket"] = {"ok": False, "error": str(exc)}

    weights = {"sportsbook": args.book_weight, "kalshi": args.kalshi_weight,
               "polymarket": args.poly_weight}

    try:
        games, meta = fetch_espn_games(args.week, args.season, args.season_type)
        sources["espn"] = {"ok": True, "games": len(games)}
        log.info("ESPN: %d games (season %s, week %s)", len(games), meta["season"], meta["week"])
    except Exception as exc:
        now = datetime.now(timezone.utc)
        games = games_from_odds(odds_events, now) if not args.week else []
        if not games:
            raise RuntimeError(f"ESPN unavailable and no fallback schedule: {exc}") from exc
        meta = {**estimate_week(now), "schedule_source": "odds_api_fallback"}
        sources["espn"] = {"ok": False, "error": str(exc),
                           "fallback": f"{len(games)} games built from The Odds API; week number estimated"}
        log.warning("ESPN failed (%s); using %d games from The Odds API, estimated week %s",
                    exc, len(games), meta["week"])

    game_dates = sorted({g["kickoff"].astimezone(EASTERN).strftime("%Y%m%d") for g in games})
    public_debug: Optional[list] = [] if args.debug_public else None
    public_splits: list[dict] = []
    try:
        public_splits = fetch_public_splits(
            game_dates, public_debug, needed={frozenset((g["home"], g["away"])) for g in games})
        sources["action_network"] = {"ok": bool(public_splits), "games_with_splits": len(public_splits)}
        if not public_splits:
            sources["action_network"]["error"] = "No public moneyline splits returned"
    except Exception as exc:
        log.error("Action Network failed: %s", exc)
        sources["action_network"] = {"ok": False, "error": str(exc)}
    if public_debug is not None:
        write_json({"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "game_dates": game_dates, "requests": public_debug}, PUBLIC_DEBUG_FILE)
        log.info("Wrote Action Network diagnostics to %s", PUBLIC_DEBUG_FILE)

    out_games, mnf = [], []
    for g in sorted(games, key=lambda x: x["kickoff"]):
        odds_ev = find_matching(
            g, odds_events,
            lambda e: {resolve_team(e.get("home_team")), resolve_team(e.get("away_team"))},
            lambda e: parse_iso(e.get("commence_time")),
        )
        odds = summarize_odds(odds_ev, g["home"], g["away"]) if odds_ev else None
        kalshi = kalshi_for_game(g, kalshi_events)
        poly = polymarket_for_game(g, poly_candidates)

        book_p = odds["vig_free_home_prob"] if odds else None
        kalshi_p = kalshi["home_prob"] if kalshi else None
        poly_p = poly["home_prob"] if poly else None
        combined, effective = blend(
            {"sportsbook": book_p, "kalshi": kalshi_p, "polymarket": poly_p}, weights)
        pick = make_pick(g, combined)
        public = add_public_leverage(g, pick, combined, public_splits)

        kickoff_et = g["kickoff"].astimezone(EASTERN)
        is_mnf = kickoff_et.weekday() == 0

        if odds:
            odds = {k: v for k, v in odds.items() if k != "vig_free_home_prob"}

        record = {
            "game_id": g["game_id"],
            "name": g["name"],
            "short_name": g["short_name"],
            "status": g["status"],
            "completed": g["completed"],
            "neutral_site": g["neutral_site"],
            "kickoff_utc": g["kickoff"].astimezone(timezone.utc).isoformat(),
            "kickoff_et": kickoff_et.isoformat(),
            "weekday_et": kickoff_et.strftime("%A"),
            "is_monday_night": is_mnf,
            "result": None if g.get("home_score") is None and g.get("away_score") is None else {
                "home_score": g.get("home_score"), "away_score": g.get("away_score"),
                "winner": g.get("winner"), "final": g["completed"],
            },
            "home": team_block(g["home"], g["home_espn_abbr"]),
            "away": team_block(g["away"], g["away_espn_abbr"]),
            "odds": odds,
            "kalshi": None if not kalshi else {
                "event_ticker": kalshi["event_ticker"], "markets": kalshi["markets"]},
            "polymarket": poly["detail"] if poly else None,
            "probabilities": {
                "sportsbook_vig_free": pair(book_p),
                "kalshi": pair(kalshi_p),
                "polymarket": pair(poly_p),
                "combined": pair(combined),
                "weights": effective,
            },
            "public_pct": public,
            "pick": pick,
        }
        out_games.append(record)
        if is_mnf:
            mnf.append(project_mnf(g, odds, pick))

    picks = [
        {
            "game_id": r["game_id"],
            "matchup": r["short_name"],
            "pick": r["pick"].get("team"),
            "win_probability": r["pick"].get("win_probability"),
            "confidence": r["pick"].get("confidence"),
            "live_upset_candidate": r["pick"].get("live_upset_candidate", False),
            "public_pick_pct": r["pick"].get("public_pick_pct"),
            "high_leverage_play": r["pick"].get("high_leverage_play", False),
            "high_leverage_underdog": r["pick"].get("high_leverage_underdog", False),
        }
        for r in out_games
    ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "season": meta["season"],
        "season_type": meta["season_type"],
        "week": meta["week"],
        "schedule_source": meta.get("schedule_source"),
        "config": {
            "weights": weights,
            "upset_band": list(UPSET_BAND),
            "leverage_flag": LEVERAGE_FLAG,
            "leverage_max_win_prob": LEVERAGE_MAX_WIN_PROB,
            "key_scores": KEY_SCORES,
        },
        "sources": sources,
        "summary": {
            "games": len(out_games),
            "picks": picks,
            "live_upset_candidates": [p for p in picks if p["live_upset_candidate"]],
        },
        "monday_night_football": mnf,
        "games": out_games,
    }


def write_json(payload: dict, path: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)  # atomic: never leaves a half-written data.json


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build data.json for a weekly NFL straight-up pool.")
    p.add_argument("--output", "-o", default="data.json", help="output path (default: data.json)")
    p.add_argument("--week", type=int, help="NFL week (default: ESPN's current week)")
    p.add_argument("--season", type=int, help="season year, used with --week")
    p.add_argument("--season-type", type=int, choices=[1, 2, 3], help="1=pre, 2=regular, 3=post")
    p.add_argument("--book-weight", type=float, default=DEFAULT_WEIGHTS["sportsbook"],
                   help="relative weight of vig-free sportsbook odds (default: 1)")
    p.add_argument("--kalshi-weight", type=float, default=DEFAULT_WEIGHTS["kalshi"],
                   help="relative weight of Kalshi (default: 1)")
    p.add_argument("--poly-weight", type=float, default=DEFAULT_WEIGHTS["polymarket"],
                   help="relative weight of Polymarket (default: 1; 0 turns it off)")
    p.add_argument("--debug-public", action="store_true",
                   help=f"write raw Action Network diagnostics to {PUBLIC_DEBUG_FILE}")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)
    ws = (args.book_weight, args.kalshi_weight, args.poly_weight)
    if min(ws) < 0 or sum(ws) == 0:
        p.error("weights must be >= 0 and at least one must be positive")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    try:
        payload = build(args)
    except Exception as exc:
        log.error("Could not build picks: %s", exc)
        return 1

    write_json(payload, args.output)
    log.info("Wrote %d games to %s", payload["summary"]["games"], args.output)
    for p in payload["summary"]["picks"]:
        flag = "  <- upset watch" if p["live_upset_candidate"] else ""
        prob = f"{p['win_probability']:.1%}" if p["win_probability"] is not None else "n/a"
        log.info("  %-12s pick %-4s %6s%s", p["matchup"], p["pick"] or "-", prob, flag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
