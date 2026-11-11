# LoL Pro Draft Analyzer

Given a professional League of Legends draft in progress, this predicts:

1. a ranked probability distribution over the next pick or ban,
2. a win probability for each side,
3. the top factors behind both, as structured JSON (grouped SHAP values).

Phases 1 to 3 of `lol-tool.md` are built (pro data only, no Riot API, no LLM). Results and open questions are in
[`docs/REPORT.md`](docs/REPORT.md).

| Validation (651 games, 240 series) | Top-1 | Top-5 | Top-10 | Log loss |
|---|---|---|---|---|
| Baseline: patch presence + team frequency | 12.0% | 39.6% | 58.1% | 3.528 |
| Next-action model | 28.2% | 67.0% | 83.3% | 2.528 |

The win model is a tie with the Elo + side + first-pick baseline (log loss 0.6369 vs 0.6415); the draft effect it
reports is not statistically supported, and the CLI and web UI label it that way.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Python 3.11+. Secrets, if ever needed, go in `.env` (gitignored): `FANDOM_USER`, `FANDOM_BOT_PASSWORD`.

## Pipeline

Every network response is cached on disk and never re-downloaded.

```bash
# 1. Data. Google Drive usually blocks anonymous downloads; use your browser session,
#    or drop the yearly CSVs into data/raw/oe/ by hand.
.venv/bin/python -m draft_analyzer.ingest.oracles_elixir --download --cookies-from-browser chrome
.venv/bin/python -m draft_analyzer.ingest.ddragon          # champion metadata
.venv/bin/python -m draft_analyzer.ingest.join             # Leaguepedia cross-check (optional)
.venv/bin/python -m draft_analyzer.report m1               # reports/M1_ingestion.md

# 2. Features and baselines
.venv/bin/python -m draft_analyzer.experiments build       # ~1 min, writes ~5 GB to data/processed/features
.venv/bin/python -m draft_analyzer.experiments m2          # reports/M2_baselines.md

# 3. Models
.venv/bin/python -m draft_analyzer.experiments m3          # ~1 h 45: regimes, ablations, final + live models, win model
.venv/bin/python -m draft_analyzer.report m3               # reports/M3_models.md

.venv/bin/python -m draft_analyzer.experiments history     # optional, ~15 min: reports/history_leagues.md
.venv/bin/python -m draft_analyzer.experiments expbuild    # optional: accuracy experiments, see reports/tuning.md
.venv/bin/python -m draft_analyzer.experiments player fit base,target,pinch,both
.venv/bin/python -m draft_analyzer.experiments player report
.venv/bin/python -m draft_analyzer.experiments tune         # hyperparameter search on the tune period

.venv/bin/python -m pytest                                  # 114 tests
```

Leagues and seasons are set in `draft_analyzer/config.py`, or with `DRAFT_LEAGUES="LCK,LPL"` and
`DRAFT_YEARS="2025,2026"`. History-only leagues (see below) are set with `DRAFT_HISTORY_LEAGUES` (`*` = every
other league in the files) and `DRAFT_HISTORY_BLOCKS`.

## Web UI

```bash
.venv/bin/python -m draft_analyzer.web        # then open http://127.0.0.1:8000
```

Pick the two teams (rosters fill in from their latest game), who has first pick, and click each ban and pick as
the real draft happens. After every click the page splits the champions into the likely ones (1% or more, shown
large with their probability), the other available ones and the unavailable ones; lists the top 12 candidates
with their three main factors (hover a row for the full breakdown down to single features); and moves a
chess-style evaluation bar for win probability, with a graph over the draft. League, patch and Fearless mode
are behind the summary at the top left. Picks fill the team columns as splash art, and each candidate's main
reasons are given in words that name the team and the fact behind them ("T1 picked it lately", "pairs with
Gen.G's picks"), with the numbers in the hover card. When a draft is complete the grid is replaced by a recap of how the model rated each action; record the
winner and the next game of the series starts with the Fearless burns applied. Below 860 px wide the page
becomes one scrolling column with the two teams side by side. The server runs on localhost only and keeps the models in memory (about 0.25 s per prediction).
Champion icons and splash art are fetched from Data Dragon once and cached in `data/raw/ddragon/img/`; the page
itself loads nothing from other hosts, so it uses the condensed fonts already on the machine.

## Predicting from the command line

Replay a historical draft up to an action index (features are as-of that game):

```bash
.venv/bin/python -m draft_analyzer.predict --game-id LOLTMNT02_488304 --upto 6
```

Follow a live draft from a state file, editing `actions` as the draft happens:

```bash
.venv/bin/python -m draft_analyzer.predict --state examples/state_series_in_data.json
```

Both print the top 10 candidates with probabilities, the win probability (full model, baseline, and the
difference), and the top factor groups. Add `--json` for the full structured output.

State file fields: `patch`, `league`, `blue_team`, `red_team`, `first_pick_team`, `blue_players` and
`red_players` (top, jungle, mid, bot, support), `fearless` (`hard`, `soft` or `none`), and `actions` (champion
names in draft order, `null` for a missed ban). Earlier games of the series come from one of:

- `series_id` of a series already in the dataset (`examples/state_series_in_data.json`), or
- `previous_games`, each with its teams, all 20 actions and the winner (`examples/state_new_series.json`).

`burned`, `blue_burned` and `red_burned` can list extra unavailable champions.

## How it works

- **Draft order is keyed on draft slot, never on side.** From 2026 the First Selection rule lets the red side
  draft first (63% of 2026 games). The 20-action sequence lives in one place, `draft_analyzer/draft.py`.
- **No leakage, by construction.** `features/asof.py` walks games in start-time order and applies a finished game
  to the statistics only once its end time is at or before the start of the game being featurised. Feature code
  receives pre-game facts and the actions so far, nothing else. Game 2 of a series sees game 1.
- **History-only games.** Lower-tier and predecessor leagues (LCK CL, LDL, NACL, EMEA Masters, PCS, VCS, LJL,
  CBLOL, LLA) and out-of-scope events (the "LCS 2027" amateur promotion tournament) go through the same engine
  under the same end-time rule, but only update player statistics. They are never predicted, trained on or
  scored. On validation this is a tie with using no history, and no variant tried beats it
  (`reports/history_leagues.md`).
- **Stat blocks** (`features/`): champion meta (patch presence shrunk toward a long-run prior, draft-position
  rates, win rate relative to Elo expectation), role distributions and open-role fit, team tendencies, player
  mastery and how much of each player's pool is still available, pairwise synergy and counters, Elo, and series
  context (Fearless burns, earlier bans, score).
- **Next-action model** (`models/next_action.py`): LightGBM scoring each available champion, trained with a
  listwise softmax objective so outputs are probabilities. Trained on all games with a 365-day time decay, on
  every candidate, and served as the mean logit of three fits with different seeds (`reports/tuning.md`). The
  final fits are memory-heavy (three at once swapped on a 24 GB machine) and run one at a time.
- **Win model** (`models/win.py`): logistic regression on Elo, side, first pick and draft features, trained on
  partial drafts so it works at any point.
- **Evaluation** (`evaluate.py`): time-based splits by series; every comparison is a paired bootstrap over series
  with a 95% interval, and a model wins only if the interval excludes zero.

## Layout

```
draft_analyzer/
  config.py  draft.py  evaluate.py  explain.py  predict.py  experiments.py  report.py
  web/       server.py  static/
  ingest/    oracles_elixir.py  leaguepedia.py  ddragon.py  join.py
  features/  asof.py  dataset.py  champion.py  team.py  player.py  pairwise.py  roles.py  context.py  elo.py
  models/    baselines.py  next_action.py  win.py
tests/       docs/       reports/       examples/
data/raw/    data/processed/            # gitignored
```

## Data

Oracle's Elixir match data is for non-commercial use. Champion metadata is from Riot's Data Dragon. Leaguepedia
is used only to cross-check draft order on a sample.
