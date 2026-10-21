"""Move-quality features for one player of a chess game, computed with Stockfish.

Used by BOTH scripts/generate_dataset.py (offline) and the live app, so training data and
live requests are always turned into features by exactly the same code.
"""
import asyncio
import io
import logging
import math
import os
import shutil
import time
from contextlib import contextmanager

import chess
import chess.engine
import chess.pgn

# python-chess logs the offending text when a PGN is malformed. That text is attacker
# controlled, so keep it out of our logs (we report a generic error instead).
logging.getLogger("chess.pgn").setLevel(logging.CRITICAL)

FEATURE_NAMES = [
    "top1_match_rate",
    "top3_match_rate",
    "acpl",
    "cpl_std",
    "blunder_rate",
    "mistake_rate",
    "inaccuracy_rate",
    "max_best_move_streak",
    "n_moves_analysed",
]
FEATURE_LABELS = {
    "top1_match_rate": "Engine top-1 match rate",
    "top3_match_rate": "Engine top-3 match rate",
    "acpl": "Average centipawn loss",
    "cpl_std": "Centipawn loss std-dev",
    "blunder_rate": "Blunder rate (loss >= 200)",
    "mistake_rate": "Mistake rate (100-199)",
    "inaccuracy_rate": "Inaccuracy rate (50-99)",
    "max_best_move_streak": "Longest best-move streak / moves",
    "n_moves_analysed": "Moves analysed",
}

DEFAULT_DEPTH = 8            # must match the depth used to generate the training data
MOVE_TIME_LIMIT = 1.0        # seconds per position (hard cap on top of the depth limit)
TOTAL_TIMEOUT = 25.0         # seconds per game for the live app
ENGINE_HASH_MB = 16
MULTIPV = 3

OPENING_MOVES_SKIPPED = 8    # the analysed player's first 8 moves are opening theory
MIN_ANALYSED_MOVES = 10
MIN_PLIES = 20
MAX_PLIES = 300

SCORE_CAP = 1000             # centipawns; mate scores map to +/-1000
CPL_CAP = 1000
BLUNDER_CP, MISTAKE_CP, INACCURACY_CP = 200, 100, 50

_INFO = chess.engine.INFO_SCORE | chess.engine.INFO_PV
_ENGINE_ERRORS = (chess.engine.EngineError, chess.engine.EngineTerminatedError,
                  asyncio.TimeoutError, TimeoutError, OSError)


class GameRejected(ValueError):
    """The game cannot be judged (bad PGN, wrong side, too short). The message is safe to show."""


class EngineUnavailable(RuntimeError):
    """Stockfish is missing, crashed or exceeded the analysis time budget."""


def find_stockfish():
    return os.environ.get("STOCKFISH_PATH") or shutil.which("stockfish") or "/usr/games/stockfish"


def engine_available():
    path = find_stockfish()
    return os.path.isfile(path) and os.access(path, os.X_OK)


@contextmanager
def open_engine(path=None):
    """Start Stockfish; the context manager guarantees the process is shut down, even on errors."""
    with chess.engine.SimpleEngine.popen_uci(path or find_stockfish(), timeout=10) as engine:
        engine.configure({"Threads": 1, "Hash": ENGINE_HASH_MB})
        yield engine


def parse_side(side):
    sides = {"white": chess.WHITE, "black": chess.BLACK}
    if not isinstance(side, str) or side.strip().lower() not in sides:
        raise GameRejected("Side must be 'white' or 'black'")
    return sides[side.strip().lower()]


def read_single_game(pgn_text):
    """Parse untrusted PGN: exactly one legal, standard-chess game of MIN_PLIES..MAX_PLIES plies."""
    stream = io.StringIO(pgn_text.replace("\r\n", "\n").replace("\r", "\n"))
    try:
        game = chess.pgn.read_game(stream)
        if game is None:
            raise GameRejected("No chess game found in the PGN")
        if game.errors:
            raise GameRejected("PGN contains illegal, ambiguous or unparseable moves")
        if chess.pgn.read_game(stream) is not None:
            raise GameRejected("PGN must contain exactly one game")
        if type(game.board()) is not chess.Board:
            raise GameRejected("Only standard chess games are supported")
        moves = list(game.mainline_moves())
    except GameRejected:
        raise
    except Exception:  # parser edge cases must become a 400, never a 500
        raise GameRejected("PGN could not be parsed") from None

    if any(not move for move in moves):
        raise GameRejected("Null moves are not allowed")
    if not MIN_PLIES <= len(moves) <= MAX_PLIES:
        raise GameRejected(f"Game must have between {MIN_PLIES} and {MAX_PLIES} plies")
    return game


def score_to_cp(pov_score, color):
    """Engine score from `color`'s point of view in centipawns, mates mapped to +/-SCORE_CAP."""
    value = pov_score.pov(color).score(mate_score=100_000)
    return max(-SCORE_CAP, min(SCORE_CAP, value))


def analyze_game(pgn_text, side, depth=DEFAULT_DEPTH, engine=None,
                 move_time=MOVE_TIME_LIMIT, total_timeout=TOTAL_TIMEOUT):
    """Feature dict only (see analyze_game_detailed)."""
    return analyze_game_detailed(pgn_text, side, depth, engine, move_time, total_timeout)["features"]


def analyze_game_detailed(pgn_text, side, depth=DEFAULT_DEPTH, engine=None,
                          move_time=MOVE_TIME_LIMIT, total_timeout=TOTAL_TIMEOUT):
    """{"features": FEATURE_NAMES dict, "accuracy": Lichess-style accuracy %}.

    Return the FEATURE_NAMES dict for `side` ("white"/"black") of the game in `pgn_text`.

    Pass an open `engine` to reuse it (dataset generator); otherwise one is started and
    always closed again. Raises GameRejected (bad input) or EngineUnavailable (503).
    """
    color = parse_side(side)
    game = read_single_game(pgn_text)
    positions = _positions_to_judge(game, color)
    if len(positions) < MIN_ANALYSED_MOVES:
        # Checked before starting the engine so short games cost nothing.
        raise GameRejected(
            f"Game too short to judge: need at least {MIN_ANALYSED_MOVES} non-forced moves by the "
            f"chosen side after its first {OPENING_MOVES_SKIPPED} moves"
        )
    try:
        if engine is not None:
            return _run(engine, positions, depth, move_time, total_timeout)
        with open_engine() as own_engine:
            return _run(own_engine, positions, depth, move_time, total_timeout)
    except _ENGINE_ERRORS as exc:
        raise EngineUnavailable("Chess engine failed") from exc


def _positions_to_judge(game, color):
    """(board, played move) for each of the player's moves after the opening, skipping forced moves."""
    board = game.board()
    own_moves = 0
    positions = []
    for move in game.mainline_moves():
        if board.turn == color:
            own_moves += 1
            if own_moves > OPENING_MOVES_SKIPPED and board.legal_moves.count() > 1:
                positions.append((board.copy(), move))
        board.push(move)
    return positions


def _run(engine, positions, depth, move_time, total_timeout):
    deadline = time.monotonic() + total_timeout
    limit = chess.engine.Limit(depth=depth, time=move_time)
    token = object()  # new "game" for the engine -> ucinewgame -> fresh hash, reproducible results
    cpls, top1, top3, accuracies = [], [], [], []
    for board, move in positions:
        if time.monotonic() > deadline:
            raise EngineUnavailable("Analysis time budget exceeded")
        cpl, is_top1, is_top3, best_cp, played_cp = _judge_move(engine, board, move, limit, token)
        cpls.append(cpl)
        top1.append(is_top1)
        top3.append(is_top3)
        accuracies.append(move_accuracy(best_cp, played_cp))
    return {"features": _features(cpls, top1, top3),
            "accuracy": round(sum(accuracies) / len(accuracies), 1)}


def win_percent(cp):
    """Lichess win-probability curve (centipawns from the mover's view -> 0..100)."""
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def move_accuracy(best_cp, played_cp):
    """Lichess move-accuracy formula from the drop in win %."""
    drop = max(0.0, win_percent(best_cp) - win_percent(played_cp))
    return max(0.0, min(100.0, 103.1668 * math.exp(-0.04354 * drop) - 3.1669))


def _judge_move(engine, board, move, limit, token):
    mover = board.turn
    infos = engine.analyse(board, limit, multipv=MULTIPV, game=token, info=_INFO)
    lines = [(info["pv"][0], score_to_cp(info["score"], mover))
             for info in infos if info.get("pv") and "score" in info]
    if not lines:
        raise EngineUnavailable("Engine returned no analysis")
    ranked = [m for m, _ in lines]
    best_cp = lines[0][1]
    if move in ranked:
        played_cp = lines[ranked.index(move)][1]
    else:
        played_cp = _score_after(engine, board, move, limit, token)
    cpl = min(CPL_CAP, max(0, best_cp - played_cp))
    return cpl, move == ranked[0], move in ranked[:3], best_cp, played_cp


def _score_after(engine, board, move, limit, token):
    mover = board.turn
    board.push(move)
    try:
        if board.is_checkmate():
            return SCORE_CAP
        if board.is_game_over():
            return 0
        info = engine.analyse(board, limit, game=token, info=_INFO)
        if "score" not in info:
            raise EngineUnavailable("Engine returned no score")
        return score_to_cp(info["score"], mover)
    finally:
        board.pop()


def _features(cpls, top1, top3):
    n = len(cpls)
    mean = sum(cpls) / n
    std = math.sqrt(sum((c - mean) ** 2 for c in cpls) / n)
    streak = longest = 0
    for hit in top1:
        streak = streak + 1 if hit else 0
        longest = max(longest, streak)
    values = {
        "top1_match_rate": sum(top1) / n,
        "top3_match_rate": sum(top3) / n,
        "acpl": mean,
        "cpl_std": std,
        "blunder_rate": sum(c >= BLUNDER_CP for c in cpls) / n,
        "mistake_rate": sum(MISTAKE_CP <= c < BLUNDER_CP for c in cpls) / n,
        "inaccuracy_rate": sum(INACCURACY_CP <= c < MISTAKE_CP for c in cpls) / n,
        "max_best_move_streak": longest / n,
        "n_moves_analysed": n,
    }
    return {name: round(float(values[name]), 6) for name in FEATURE_NAMES}
