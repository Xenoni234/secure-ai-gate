# Chess Fair-Play Gate

An AI anti-cheat screen for chess (Lichess head-to-head checks), delivered through a
DevSecOps pipeline. It is a college mini-project on *Security Testing for AI Systems*.

A coach or arbiter pastes a game (PGN) and picks the side to check. Stockfish scores that player's
moves, and a Random Forest classifier returns **Fair play** or **Suspicious — likely engine-assisted**
with a confidence. The output is *decision support for arbiters, not an automatic ban.*

## Architecture

```
                        offline, once (Mac)                            CI / runtime
 ┌──────────────────────────────────────────────┐
 │ scripts/generate_dataset.py                  │
 │  Stockfish self-play: human / engine /       │   data/fairplay.csv ──► app/train.py
 │  smart_cheater subjects vs human opponents   │   app/samples/*.pgn     (RandomForest + gates,
 │  features via app/chess_features.py ─────────┼──►                       SHA-256 fingerprint)
 └──────────────────────────────────────────────┘                              │ model.joblib
                                                                               ▼ model_meta.json
   browser ──► /  /analyze-ui ─┐        ┌───────────────── Flask (gunicorn, non-root) ─────────────┐
                               ├──────► │ rate limit ─► validate PGN ─► analysis slot (max 2)      │
   client ──► /api/v1/analyze ─┤        │   ─► app/chess_features.py ─► Stockfish (depth 8, caps)  │
     (X-API-Key)               │        │   ─► features ─► model (hash-verified) ─► verdict        │
   client ──► /api/v1/predict ─┘        │ security headers · generic JSON errors · Jinja autoescape│
                                        └──────────────────────────────────────────────────────────┘

 Jenkins (Freestyle job, configured in the UI – no Groovy):
          Checkout → venv → Bandit → pip-audit → Train + Quality Gate → pytest (unit/API/adversarial,
          engine tests included) → Docker build → Trivy → Deploy staging (compose) → Selenium UI
          (plain WebDriver, headless Chromium) → OWASP ZAP baseline → Promote :stable
```

### Features (per analysed player)
Features come from `app/chess_features.py`, the same code that builds the dataset and serves live requests.

- The player's first 8 moves are skipped as opening theory.
- Forced moves (only one legal move) are skipped.
- At least 10 analysed moves are required.

| Feature | Meaning |
|---|---|
| `top1_match_rate` / `top3_match_rate` | share of moves equal to Stockfish's best / top-3 (MultiPV 3) |
| `acpl`, `cpl_std` | average centipawn loss and its spread (loss capped at 1000; mate = ±1000) |
| `blunder_rate`, `mistake_rate`, `inaccuracy_rate` | loss ≥ 200, 100–199, 50–99 |
| `max_best_move_streak` | longest run of best moves ÷ moves analysed |
| `n_moves_analysed` | number of judged moves |

### API
| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /health` | – | status, model version, engine present |
| `GET /` (`?sample=fair` / `?sample=suspicious`) | – | UI |
| `POST /analyze-ui` | per-IP rate limit | form handler |
| `POST /api/v1/analyze` `{"pgn": str, "side": "white"\|"black"}` | `X-API-Key` + rate limit | full analysis |
| `POST /api/v1/predict` `{"features": {name: number}}` | `X-API-Key` + rate limit | model only (fast tests) |

## Threat model

| Threat | Example | Controls | Tested by |
|---|---|---|---|
| **Evasion (smart cheater)** | uses the engine on only 30–70 % of moves | trained on smart cheaters; gate: ≥ 90 % of full-engine cheaters caught; smart-cheater detection reported per cheat ratio (known weakness) | `test_model.py`, `test_adversarial.py` |
| **False accusation** | strong honest player flagged | threshold picked for ≤ 8 % false-accusation rate (cross-validation); gate ≤ 0.10; disclaimer in UI | `test_false_accusation_gate` |
| **DoS via expensive analysis** | flood of 300-ply games | per-key/IP rate limits (10/min for analysis); max 2 concurrent engines → 503; depth 8 + 1 s/position + 25 s/game caps; 20 KB PGN; 32 KB body | `test_analyze_rate_limiting`, `test_analysis_slots_exhausted_returns_503` |
| **Malicious / malformed PGN** | illegal moves, two games, variants, null moves, huge comments | python-chess strict parse, exactly one standard game, 20–300 plies, fixed error messages (input never echoed), parser errors → 400, PGN parser logs silenced | PGN tests in `test_api_security.py` |
| **XSS via PGN headers** | `[White "<script>alert(1)</script>"]` | Jinja autoescape, CSP without inline scripts | API + Selenium XSS tests |
| **Model tampering** | swapped `model.joblib` | SHA-256 checked before `joblib.load` | `test_tampered_model_is_rejected` |
| **Model probing / extraction** | many `/predict` calls | API key, rate limit, confidence rounded to 2 decimals, out-of-distribution inputs → 422 | `/predict` tests |
| **Info leakage** | stack traces, engine path | generic JSON error handlers, 503 with fixed text | `test_errors_do_not_leak_internals`, `test_engine_failure_returns_generic_503` |
| **Supply chain / container** | vulnerable deps or image | pinned deps, Bandit, pip-audit, Trivy, non-root read-only container, ZAP | Jenkins stages |

## Generate the dataset (once, on a machine with Stockfish)

```bash
brew install stockfish
.venv/bin/python scripts/generate_dataset.py            # --games 1200 --depth 8 --workers N-1 --seed 42
```

This writes `data/fairplay.csv` and `app/samples/{fair,suspicious}_game.pgn`. Commit both.

Each game's subject is drawn from three policies:

- **human** (50 %): softmax over Stockfish MultiPV-5 evaluations, in four strength bands, plus random blunders.
- **engine** (25 %): always plays Stockfish's best move.
- **smart_cheater** (25 %): plays the engine move with probability p ∈ [0.3, 0.7], otherwise moves like a human.

The opponent always plays the human policy. Games start from 22 common openings, and the subject's colour is random.

Finished games are cached in `data/fairplay.partial.csv`, which is git-ignored. Re-running reuses them, so an interrupted run resumes where it stopped. Delete that file to start over.

## Run locally

```bash
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m app.train
.venv/bin/python -m pytest tests --ignore=tests/selenium -v
docker network inspect ci-net >/dev/null 2>&1 || docker network create ci-net
docker build -t secure-ai-gate:latest .
API_KEY=dev-key docker compose up -d app
open http://localhost:5050
```

Tests marked `engine` need Stockfish. They are skipped only when no binary is found. The lookup order is
`STOCKFISH_PATH`, then `stockfish` on PATH, then `/usr/games/stockfish`.

### Selenium UI tests (plain WebDriver, no Grid)
The tests start a browser directly with `webdriver.Chrome()` on the machine that runs them:
in Jenkins a headless Chromium installed in the Jenkins image (`CHROME_BIN`, `CHROMEDRIVER`),
on a laptop your installed Google Chrome. To watch the browser test the deployed site:

```bash
HEADLESS=false DEMO_PAUSE=2 .venv/bin/python -m pytest tests/selenium -v
```

## Jenkins setup (UI only – Freestyle job, no Groovy)

```bash
docker compose -f jenkins/docker-compose.yml up -d --build    # Jenkins on http://localhost:8080
```

1. **Manage Jenkins → Credentials → (global) → Add Credentials**: kind *Secret text*, ID `secure-ai-api-key`.
2. **New Item → Freestyle project** named `secure-ai-gate`.
3. **General**: *Discard old builds* (keep 15); *This project is parameterized* → Boolean `FAIL_ON_CVE` (default off).
4. **Source Code Management → Git**: `https://github.com/Xenoni234/secure-ai-gate.git`, branch `*/main`.
5. **Build Triggers → Poll SCM**: `* * * * *` (checks GitHub every minute).
6. **Build Environment**: *Use secret text(s) or file(s)* → variable `API_KEY`, credential `secure-ai-api-key`;
   *Add timestamps to the Console Output*.
7. **Build Steps**: eleven *Execute shell* steps, one per gate (setup, Bandit, pip-audit, train + quality gate,
   pytest, Docker build, Trivy, deploy to staging, Selenium, OWASP ZAP, promote). The exact commands are in
   [`jenkins/freestyle-job.xml`](jenkins/freestyle-job.xml); pip-audit, Trivy and ZAP use *Advanced → Exit code to set
   build unstable* = `99`.
8. **Post-build Actions**: *Publish JUnit test result report* (`reports/junit-*.xml`) and *Archive the artifacts* (`reports/**`).

## Limitations (honest)

- **Simulated cheaters, not real flagged games.** "Human" is a softmax over engine evaluations, which
  approximates human error but is not human. Real players err differently (time trouble, tactics
  blindness, opening preparation).
- The engine policy and the analysis use the same engine and depth. Real cheaters use other engines or
  depths, which lowers top-1 match rates.
- `n_moves_analysed` can pick up artefacts: engine games tend to end sooner against weak opponents.
- Stockfish versions differ between macOS (Homebrew), Jenkins and the app image (Debian), so live
  features drift slightly from the training features.
- The output is a statistical indicator for one game. Fair-play decisions need many games, context and
  a human arbiter.
- Rate limits are in-memory and per process. That fits one gunicorn worker; scaling out needs a shared store (e.g. Redis).

**Future work:** train on real academy tournament data, with arbiter-confirmed labels. Add clock-time
features (move-time consistency is a strong cheating signal). Aggregate across a player's games.
Add rating-aware thresholds.
