#!/usr/bin/env python3
"""Generate the simulated fair-play dataset with Stockfish (run once, offline).

Each game has a "subject" player following one policy:
  human          softmax over Stockfish MultiPV evals + occasional random moves (4 strength bands)
  engine         always Stockfish's best move
  smart_cheater  engine move with probability p in [0.3, 0.7], otherwise the human policy
The opponent always plays the human policy. The subject's features are computed with
app.chess_features.analyze_game, the exact code the live app uses.

Output: data/fairplay.csv and demo PGNs in app/samples/. Finished games are cached in
data/fairplay.partial.csv, so an interrupted run resumes where it stopped.

Usage: python scripts/generate_dataset.py [--games 1200] [--depth 8] [--workers N] [--seed 42]
"""
import argparse
import csv
import io
import math
import os
import random
import sys
import time
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import chess  # noqa: E402
import chess.engine  # noqa: E402
import chess.pgn  # noqa: E402

from app.chess_features import (  # noqa: E402
    DEFAULT_DEPTH, FEATURE_NAMES, GameRejected, analyze_game, engine_available, open_engine,
    score_to_cp,
)

DATA_DIR = ROOT / "data"
OUT_PATH = DATA_DIR / "fairplay.csv"
PARTIAL_PATH = DATA_DIR / "fairplay.partial.csv"
SAMPLES_DIR = ROOT / "app" / "samples"

FINAL_FIELDS = ["game_id", "policy", "band", "subject", "opening", "cheat_ratio",
                *FEATURE_NAMES, "label"]
PARTIAL_FIELDS = FINAL_FIELDS + ["run", "pgn"]

MAX_GAME_PLIES = 160
HUMAN_MULTIPV = 5
MAX_ATTEMPTS = 5
GENERATOR_ANALYSIS_TIMEOUT = 600
POLICIES = ["human", "engine", "smart_cheater"]
POLICY_WEIGHTS = [0.5, 0.25, 0.25]
SMART_RATIO_RANGE = (0.3, 0.7)
BANDS = {
    "strong":   {"temperature": 30,  "blunder_prob": 0.03},
    "club":     {"temperature": 50,  "blunder_prob": 0.06},
    "casual":   {"temperature": 80,  "blunder_prob": 0.10},
    "beginner": {"temperature": 120, "blunder_prob": 0.15},
}
OPENINGS = {
    "Ruy Lopez": "e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6",
    "Italian Game": "e4 e5 Nf3 Nc6 Bc4 Bc5 c3 Nf6",
    "Scotch Game": "e4 e5 Nf3 Nc6 d4 exd4 Nxd4 Nf6",
    "Petrov Defence": "e4 e5 Nf3 Nf6 Nxe5 d6 Nf3 Nxe4",
    "King's Gambit": "e4 e5 f4 exf4 Nf3 g5",
    "Sicilian Najdorf": "e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6",
    "Sicilian Dragon": "e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 g6",
    "French Defence": "e4 e6 d4 d5 Nc3 Nf6",
    "Caro-Kann": "e4 c6 d4 d5 Nc3 dxe4 Nxe4 Bf5",
    "Scandinavian": "e4 d5 exd5 Qxd5 Nc3 Qa5",
    "Pirc Defence": "e4 d6 d4 Nf6 Nc3 g6",
    "Queen's Gambit Declined": "d4 d5 c4 e6 Nc3 Nf6 Bg5 Be7",
    "Queen's Gambit Accepted": "d4 d5 c4 dxc4 Nf3 Nf6 e3 e6",
    "Slav Defence": "d4 d5 c4 c6 Nf3 Nf6 Nc3 dxc4",
    "King's Indian": "d4 Nf6 c4 g6 Nc3 Bg7 e4 d6",
    "Nimzo-Indian": "d4 Nf6 c4 e6 Nc3 Bb4",
    "Grunfeld": "d4 Nf6 c4 g6 Nc3 d5",
    "Catalan": "d4 Nf6 c4 e6 g3 d5 Bg2 Be7",
    "London System": "d4 d5 Bf4 Nf6 e3 e6 Nf3 c5",
    "Dutch Defence": "d4 f5 g3 Nf6 Bg2 e6",
    "English Opening": "c4 e5 Nc3 Nf6 g3 d5",
    "Reti Opening": "Nf3 d5 g3 Nf6 Bg2 e6",
}
ENGINE_INFO = chess.engine.INFO_SCORE | chess.engine.INFO_PV


def human_move(engine, board, rng, band, limit, token):
    legal = list(board.legal_moves)
    if len(legal) == 1:
        return legal[0]
    infos = engine.analyse(board, limit, multipv=HUMAN_MULTIPV, game=token, info=ENGINE_INFO)
    lines = [(i["pv"][0], score_to_cp(i["score"], board.turn))
             for i in infos if i.get("pv") and "score" in i]
    if not lines:
        return rng.choice(legal)
    if rng.random() < band["blunder_prob"]:
        top = {m for m, _ in lines}
        others = [m for m in legal if m not in top]
        if others:
            return rng.choice(others)
    best = lines[0][1]
    weights = [math.exp((cp - best) / band["temperature"]) for _, cp in lines]
    return rng.choices([m for m, _ in lines], weights=weights)[0]


def engine_move(engine, board, limit, token):
    info = engine.analyse(board, limit, game=token, info=ENGINE_INFO)
    pv = info.get("pv")
    return pv[0] if pv else next(iter(board.legal_moves))


def simulate_game(engine, rng, depth, policy, cheat_ratio, subject_color,
                  subject_band, opponent_band, opening_name):
    limit = chess.engine.Limit(depth=depth)
    token = object()
    board = chess.Board()
    for san in OPENINGS[opening_name].split():
        board.push_san(san)
    while board.ply() < MAX_GAME_PLIES and not board.is_game_over(claim_draw=True):
        if board.turn == subject_color:
            cheat = policy == "engine" or (policy == "smart_cheater" and rng.random() < cheat_ratio)
            if cheat:
                move = engine_move(engine, board, limit, token)
            else:
                move = human_move(engine, board, rng, BANDS[subject_band], limit, token)
        else:
            move = human_move(engine, board, rng, BANDS[opponent_band], limit, token)
        board.push(move)

    side = "white" if subject_color == chess.WHITE else "black"
    game = chess.pgn.Game.from_board(board)
    game.headers["Event"] = "Fair-Play Gate simulated game"
    game.headers["Site"] = "Secure AI Gate (simulation)"
    game.headers["White"] = "Subject" if subject_color == chess.WHITE else "Opponent"
    game.headers["Black"] = "Opponent" if subject_color == chess.WHITE else "Subject"
    game.headers["Result"] = board.result(claim_draw=True)
    game.headers["Opening"] = opening_name
    game.headers["FairPlaySubject"] = side
    return str(game), side


def play_one(task):
    """Worker: simulate + analyse one game. Each game has its own seed -> reproducible."""
    game_id, seed, depth = task
    rng = random.Random(seed)
    policy = rng.choices(POLICIES, weights=POLICY_WEIGHTS)[0]
    if policy == "human":
        cheat_ratio = 0.0
    elif policy == "engine":
        cheat_ratio = 1.0
    else:
        cheat_ratio = round(rng.uniform(*SMART_RATIO_RANGE), 4)
    subject_band = rng.choice(sorted(BANDS))
    try:
        with open_engine() as engine:
            for _ in range(MAX_ATTEMPTS):
                subject_color = rng.choice([chess.WHITE, chess.BLACK])
                opponent_band = rng.choice(sorted(BANDS))
                opening_name = rng.choice(sorted(OPENINGS))
                pgn_text, side = simulate_game(engine, rng, depth, policy, cheat_ratio, subject_color,
                                               subject_band, opponent_band, opening_name)
                try:
                    features = analyze_game(pgn_text, side, depth=depth, engine=engine,
                                            total_timeout=GENERATOR_ANALYSIS_TIMEOUT)
                except GameRejected:
                    continue  # e.g. game ended too early: play another one
                return {"game_id": game_id, "policy": policy, "band": subject_band,
                        "subject": side, "opening": opening_name, "cheat_ratio": cheat_ratio,
                        **features, "label": 0 if policy == "human" else 1, "pgn": pgn_text}
    except Exception as exc:  # one bad game must not kill a long run
        return {"game_id": game_id, "error": f"{type(exc).__name__}: {exc}"}
    return {"game_id": game_id, "error": "no analysable game after retries"}


def load_partial(run):
    """Finished games from an earlier (possibly interrupted) run with the same seed/depth."""
    rows = {}
    if not PARTIAL_PATH.exists():
        return rows
    try:
        with PARTIAL_PATH.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("run") != run or any(row.get(k) in (None, "") for k in PARTIAL_FIELDS):
                    continue
                try:
                    rows[int(row["game_id"])] = row
                except ValueError:
                    continue
    except csv.Error:
        pass  # torn last line after a hard kill: keep what was read
    return rows


def rewrite_partial(rows):
    with PARTIAL_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PARTIAL_FIELDS)
        writer.writeheader()
        for game_id in sorted(rows):
            writer.writerow(rows[game_id])


def write_dataset(rows):
    with OUT_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FINAL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_samples(rows):
    """Pick the clearest human game and the clearest engine game as UI demo samples."""
    def num(row, key):
        return float(row[key])

    humans = [r for r in rows if r["policy"] == "human" and num(r, "n_moves_analysed") >= 15]
    engines = [r for r in rows if r["policy"] == "engine" and num(r, "n_moves_analysed") >= 12]
    if not humans or not engines:
        print("WARNING: not enough games to pick demo samples; rerun with more --games")
        return
    fair = min(humans, key=lambda r: (num(r, "top1_match_rate"), -num(r, "acpl")))
    suspicious = max(engines, key=lambda r: (num(r, "top1_match_rate"), -num(r, "acpl")))

    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    for name, row in (("fair", fair), ("suspicious", suspicious)):
        game = chess.pgn.read_game(io.StringIO(row["pgn"]))
        game.headers["Event"] = f"Fair-play demo ({name})"
        game.headers["White"] = "Academy Student A"
        game.headers["Black"] = "Academy Student B"
        path = SAMPLES_DIR / f"{name}_game.pgn"
        path.write_text(str(game) + "\n", encoding="utf-8")
        print(f"Sample {path.relative_to(ROOT)}: game {row['game_id']} ({row['policy']}), "
              f"subject={row['subject']}, top1={num(row, 'top1_match_rate'):.2f}, "
              f"acpl={num(row, 'acpl'):.1f}")


def fmt(seconds):
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


def parse_args():
    parser = argparse.ArgumentParser(description="Generate the simulated fair-play dataset.")
    parser.add_argument("--games", type=int, default=1200)
    parser.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if args.games < 1 or args.depth < 1 or args.workers < 1:
        parser.error("--games, --depth and --workers must be positive")
    if args.depth != DEFAULT_DEPTH:
        print(f"WARNING: the app analyses at depth {DEFAULT_DEPTH}; training on depth "
              f"{args.depth} features will make live features inconsistent.")
    return args


def main():
    args = parse_args()
    if not engine_available():
        sys.exit("Stockfish not found. Install it (brew install stockfish) or set STOCKFISH_PATH.")

    DATA_DIR.mkdir(exist_ok=True)
    run = f"seed={args.seed};depth={args.depth}"
    done = load_partial(run)
    rewrite_partial(done)
    todo = [(gid, args.seed * 1_000_003 + gid, args.depth)
            for gid in range(args.games) if gid not in done]
    print(f"{len(done)} games cached, {len(todo)} to simulate "
          f"(depth {args.depth}, {args.workers} workers, seed {args.seed})")

    failures = 0
    if todo:
        started = time.monotonic()
        with PARTIAL_PATH.open("a", newline="", encoding="utf-8") as f, Pool(args.workers) as pool:
            writer = csv.DictWriter(f, fieldnames=PARTIAL_FIELDS, extrasaction="ignore")
            for k, row in enumerate(pool.imap_unordered(play_one, todo), 1):
                if "error" in row:
                    failures += 1
                    print(f"\n  game {row['game_id']} skipped: {row['error']}")
                else:
                    row["run"] = run
                    writer.writerow(row)
                    f.flush()
                    done[row["game_id"]] = row
                elapsed = time.monotonic() - started
                eta = elapsed / k * (len(todo) - k)
                print(f"\r  [{k}/{len(todo)}] {100 * k / len(todo):5.1f}%  "
                      f"elapsed {fmt(elapsed)}  eta {fmt(eta)}", end="", flush=True)
        print()

    rows = [done[gid] for gid in sorted(done) if gid < args.games]
    write_dataset(rows)
    write_samples(rows)

    counts = {p: sum(r["policy"] == p for r in rows) for p in POLICIES}
    print(f"Wrote {len(rows)} games to {OUT_PATH.relative_to(ROOT)}  {counts}"
          + (f"  ({failures} skipped, rerun to retry them)" if failures else ""))


if __name__ == "__main__":
    main()
