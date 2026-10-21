"""Fetch head-to-head games from the public Lichess API and score both players' fair play."""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from app.chess_features import GameRejected, analyze_game_detailed, open_engine

LICHESS_URL = "https://lichess.org/api/games/user/{user}"   # fixed host: no SSRF
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,29}$")
MAX_GAMES_LIMIT = 20
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
FETCH_TIMEOUT = 15
BATCH_TIME_BUDGET = 50.0       # seconds of engine time per request (stays under gunicorn's 60 s)
STANDARD_PERFS = "ultraBullet,bullet,blitz,rapid,classical,correspondence"

# Engine-likeness rule for one game: very high best-move match with almost no centipawn loss.
ENGINE_LIKE_TOP1 = 0.80
ENGINE_LIKE_ACPL = 15.0
SUSPICIOUS_SHARE = 0.30        # player is "suspicious" if >= 30 % of their games look engine-like


class LichessError(Exception):
    """Safe-to-show error talking to Lichess (status is the HTTP code to return)."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.message = message
        self.status = status


def validate_username(name):
    if not isinstance(name, str) or not USERNAME_RE.match(name.strip()):
        raise ValueError("Lichess usernames are 2-30 letters, digits, '_' or '-'")
    return name.strip()


def fetch_head_to_head(player1, player2, max_games):
    """All recent standard games between the two players (newest first), as Lichess JSON dicts."""
    query = urllib.parse.urlencode({"vs": player2, "max": max_games, "pgnInJson": "true",
                                    "perfType": STANDARD_PERFS, "moves": "true"})
    url = LICHESS_URL.format(user=urllib.parse.quote(player1)) + "?" + query
    headers = {"Accept": "application/x-ndjson", "User-Agent": "secure-ai-gate-fairplay/1.0"}
    token = os.environ.get("LICHESS_TOKEN", "").strip()
    if token:   # optional personal token: higher Lichess rate limits
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:  # nosec B310 - fixed https host
            body = resp.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise LichessError("LICHESS_TOKEN is invalid or expired", 502) from None
        if exc.code == 404:
            raise LichessError("Lichess player not found", 404) from None
        if exc.code == 429:
            raise LichessError("Lichess rate limit reached - wait a minute and retry", 503) from None
        raise LichessError("Lichess returned an error") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise LichessError("Could not reach Lichess") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise LichessError("Lichess response too large")

    if body.lstrip()[:1] == b"<":
        raise LichessError("Lichess returned an unexpected response")
    games = []
    for line in body.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            try:
                game = json.loads(line)
            except ValueError:
                continue
            if isinstance(game, dict) and game.get("id") and game.get("pgn"):
                games.append(game)
    return games


def _name(game, color):
    return ((game.get("players", {}).get(color, {}).get("user") or {}).get("name") or "?")


def head_to_head_report(player1, player2, max_games=10):
    """Fetch, analyse both sides of every game, and aggregate a fair-play report per player."""
    player1, player2 = validate_username(player1), validate_username(player2)
    if player1.lower() == player2.lower():
        raise ValueError("Choose two different players")
    max_games = max(1, min(int(max_games), MAX_GAMES_LIMIT))
    games = fetch_head_to_head(player1, player2, max_games)

    per_game, skipped, timed_out = [], 0, False
    stats = {p.lower(): {"name": p, "games": [], "flagged": 0} for p in (player1, player2)}
    deadline = time.monotonic() + BATCH_TIME_BUDGET
    with open_engine() as engine:
        for g in games:
            if time.monotonic() > deadline:
                timed_out = True
                break
            pgn = g.get("pgn", "")
            row = {"id": g.get("id", "?"), "url": f"https://lichess.org/{g.get('id', '')}",
                   "white": _name(g, "white"), "black": _name(g, "black"),
                   "winner": g.get("winner", "draw"), "speed": g.get("speed", "?"), "sides": {}}
            for color in ("white", "black"):
                who = row[color].lower()
                if who not in stats:
                    continue
                try:
                    res = analyze_game_detailed(pgn, color, engine=engine)
                except GameRejected:
                    continue
                f = res["features"]
                engine_like = f["top1_match_rate"] >= ENGINE_LIKE_TOP1 and f["acpl"] <= ENGINE_LIKE_ACPL
                row["sides"][color] = {"accuracy": res["accuracy"], "acpl": f["acpl"],
                                       "top1": f["top1_match_rate"], "engine_like": engine_like}
                stats[who]["games"].append(res)
                stats[who]["flagged"] += engine_like
            if row["sides"]:
                per_game.append(row)
            else:
                skipped += 1

    players = []
    for s in stats.values():
        n = len(s["games"])
        if n:
            acc = sum(r["accuracy"] for r in s["games"]) / n
            acpl = sum(r["features"]["acpl"] for r in s["games"]) / n
            top1 = sum(r["features"]["top1_match_rate"] for r in s["games"]) / n
            share = s["flagged"] / n
            verdict = "Suspicious — review recommended" if share >= SUSPICIOUS_SHARE else "Fair play"
            fair_score = round(100 * (1 - share))
        else:
            acc = acpl = top1 = None
            verdict, fair_score = "Not enough data", None
        players.append({"name": s["name"], "games_analysed": n, "avg_accuracy": acc and round(acc, 1),
                        "avg_acpl": acpl if acpl is None else round(acpl, 1),
                        "top1_match": top1 if top1 is None else round(100 * top1, 1),
                        "engine_like_games": s["flagged"], "fair_play_score": fair_score,
                        "verdict": verdict})
    return {"player1": player1, "player2": player2, "games_found": len(games),
            "games_analysed": len(per_game), "skipped": skipped, "timed_out": timed_out,
            "players": players, "games": per_game}
