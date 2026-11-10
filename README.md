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
| Next-action model | 27.1% | 66.3% | 83.1% | 2.566 |

The win model is a tie with the Elo + side + first-pick baseline (log loss 0.6370 vs 0.6415); the draft effect it
reports is not statistically supported.

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
.venv/bin/python -m draft_analyzer.experiments build       # ~45 s, writes ~5 GB to data/processed/features
.venv/bin/python -m draft_analyzer.experiments m2          # reports/M2_baselines.md

# 3. Models
.venv/bin/python -m draft_analyzer.experiments m3          # ~25 min: regimes, ablations, win model, live refit
.venv/bin/python -m draft_analyzer.report m3               # reports/M3_models.md

.venv/bin/python -m pytest                                  # 101 tests
```

Leagues and seasons are set in `draft_analyzer/config.py`, or with `DRAFT_LEAGUES="LCK,LPL"` and
`DRAFT_YEARS="2025,2026"`.

## Predicting

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
- **Stat blocks** (`features/`): champion meta (patch presence shrunk toward a long-run prior, draft-position
  rates, win rate relative to Elo expectation), role distributions and open-role fit, team tendencies, player
  mastery, pairwise synergy and counters, Elo, and series context (Fearless burns, earlier bans, score).
- **Next-action model** (`models/next_action.py`): LightGBM scoring each available champion, trained with a
  listwise softmax objective so outputs are probabilities.
- **Win model** (`models/win.py`): logistic regression on Elo, side, first pick and draft features, trained on
  partial drafts so it works at any point.
- **Evaluation** (`evaluate.py`): time-based splits by series; every comparison is a paired bootstrap over series
  with a 95% interval, and a model wins only if the interval excludes zero.

## Layout

```
draft_analyzer/
  config.py  draft.py  evaluate.py  explain.py  predict.py  experiments.py  report.py
  ingest/    oracles_elixir.py  leaguepedia.py  ddragon.py  join.py
  features/  asof.py  dataset.py  champion.py  team.py  player.py  pairwise.py  roles.py  context.py  elo.py
  models/    baselines.py  next_action.py  win.py
tests/       docs/       reports/       examples/
data/raw/    data/processed/            # gitignored
```

## Data

Oracle's Elixir match data is for non-commercial use. Champion metadata is from Riot's Data Dragon. Leaguepedia
is used only to cross-check draft order on a sample.
