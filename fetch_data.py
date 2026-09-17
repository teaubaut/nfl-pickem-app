#!/usr/bin/env python3
"""
fetch_data.py - data builder for a weekly NFL straight-up (win/loss) pool.

Pipeline
  1. ESPN scoreboard    -> this week's games (IDs, teams, kickoff times)
  2. The Odds API       -> moneyline, spread, and total from US sportsbooks
  3. Vig-free math      -> fair win probabilities from the moneylines
  4. Kalshi (KXNFLGAME) -> prediction-market win probabilities (bid/ask midpoints)
  5. Blend              -> one probability per game, a pick, and upset flags
  6. Monday Night       -> projected final score snapped to key numbers
  7. Write data.json

Usage
  export ODDS_API_KEY=your_key_here
  python3 fetch_data.py                      # current week -> data.json
  python3 fetch_data.py --output picks.json
  python3 fetch_data.py --week 5 --season 2026 --season-type 2
  python3 fetch_data.py --kalshi-weight 0.4  # 60% sportsbooks / 40% Kalshi

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
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ODDS_URL = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/"
KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
KALSHI_SERIES = "KXNFLGAME"

DEFAULT_KALSHI_WEIGHT = 0.5      # share of the blend given to Kalshi
UPSET_BAND = (0.42, 0.49)        # underdog win prob that flags a live upset
MAX_KALSHI_SPREAD = 0.20         # ignore quotes with bid/ask wider than 20c
MATCH_WINDOW_HOURS = 36          # max kickoff mismatch when pairing sources
KEY_SCORES = [3, 6, 7, 10, 13, 14, 16, 17, 20, 21, 23, 24,
              27, 28, 30, 31, 34, 35, 38, 41, 42, 45]

HTTP_TIMEOUT = 20
HTTP_RETRIES = 3
USER_AGENT = "nfl-pool-fetcher/1.0"

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


def http_get_json(url: str, params: Optional[dict] = None) -> tuple[Any, Any]:
    """GET a JSON endpoint with retries on rate limits / server errors."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last_err: Optional[Exception] = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8")), resp.headers
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"GET {_redact(url)} -> HTTP {exc.code} {exc.reason}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = exc
        if attempt < HTTP_RETRIES:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GET {_redact(url)} failed after {HTTP_RETRIES} attempts: {last_err}")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
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
    data, _ = http_get_json(ESPN_URL, params or None)

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
        games.append({
            "game_id": str(ev.get("id")),
            "name": ev.get("name"),
            "short_name": ev.get("shortName"),
            "kickoff": kickoff,
            "status": status.get("description"),
            "completed": bool(status.get("completed")),
            "neutral_site": bool(comp.get("neutralSite")),
            "home": home, "away": away,
            "home_espn_abbr": home_t.get("abbreviation"),
            "away_espn_abbr": away_t.get("abbreviation"),
        })

    meta = {
        "season": (data.get("season") or {}).get("year"),
        "season_type": (data.get("season") or {}).get("type"),
        "week": (data.get("week") or {}).get("number"),
    }
    return games, meta


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
            and ask - bid <= MAX_KALSHI_SPREAD)


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
# 5. Blend + pick
# --------------------------------------------------------------------------- #
def blend(book: Optional[float], kalshi: Optional[float], kalshi_weight: float) -> Optional[float]:
    if book is None:
        return kalshi
    if kalshi is None:
        return book
    return (1 - kalshi_weight) * book + kalshi_weight * kalshi


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
        return {"team": None, "note": "No sportsbook or Kalshi data available"}
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
# 6. Monday Night Football projection
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
    games, meta = fetch_espn_games(args.week, args.season, args.season_type)
    log.info("ESPN: %d games (season %s, week %s)", len(games), meta["season"], meta["week"])
    sources: dict[str, Any] = {"espn": {"ok": True, "games": len(games)}}

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

    out_games, mnf = [], []
    for g in sorted(games, key=lambda x: x["kickoff"]):
        odds_ev = find_matching(
            g, odds_events,
            lambda e: {resolve_team(e.get("home_team")), resolve_team(e.get("away_team"))},
            lambda e: parse_iso(e.get("commence_time")),
        )
        odds = summarize_odds(odds_ev, g["home"], g["away"]) if odds_ev else None
        kalshi = kalshi_for_game(g, kalshi_events)

        book_p = odds["vig_free_home_prob"] if odds else None
        kalshi_p = kalshi["home_prob"] if kalshi else None
        combined = blend(book_p, kalshi_p, args.kalshi_weight)
        pick = make_pick(g, combined)

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
            "home": team_block(g["home"], g["home_espn_abbr"]),
            "away": team_block(g["away"], g["away_espn_abbr"]),
            "odds": odds,
            "kalshi": None if not kalshi else {
                "event_ticker": kalshi["event_ticker"], "markets": kalshi["markets"]},
            "probabilities": {
                "sportsbook_vig_free": pair(book_p),
                "kalshi": pair(kalshi_p),
                "combined": pair(combined),
                "weights": {
                    "sportsbook": 0 if book_p is None else (1 if kalshi_p is None else round(1 - args.kalshi_weight, 3)),
                    "kalshi": 0 if kalshi_p is None else (1 if book_p is None else round(args.kalshi_weight, 3)),
                },
            },
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
        }
        for r in out_games
    ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "season": meta["season"],
        "season_type": meta["season_type"],
        "week": meta["week"],
        "config": {
            "kalshi_weight": args.kalshi_weight,
            "upset_band": list(UPSET_BAND),
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
    p.add_argument("--kalshi-weight", type=float, default=DEFAULT_KALSHI_WEIGHT,
                   help="Kalshi share of the blended probability, 0-1 (default: 0.5)")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)
    if not 0 <= args.kalshi_weight <= 1:
        p.error("--kalshi-weight must be between 0 and 1")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    try:
        payload = build(args)
    except Exception as exc:
        log.error("Could not load the ESPN schedule: %s", exc)
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
