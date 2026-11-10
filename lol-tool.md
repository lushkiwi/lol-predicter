# LoL Pro Draft Analyzer

## Goal

Build a Python system that, given a professional League of Legends draft in progress, outputs:

1. A ranked probability distribution over the next pick or ban
2. A calibrated win probability for each side
3. The top factors behind both, as structured data (later turned into prose by an LLM)

This session covers **Phases 1 to 3 only** (pro data, no Riot API, no LLM). Phases 4 and 5 are listed at the end for context. Do not start them.

## Working rules

- Stop after each milestone and report results: what was built, metrics, and anything surprising. Do not start the next milestone until I confirm. If specifically asked to continue without check-ins, report this information in documentation files for future review.
- Inspect real data before writing parsers. Do not assume column names or table schemas. Print them, then write code against what you see.
- Prefer simple, inspectable models first. Only add complexity if it beats the previous baseline on the validation split **by more than noise** (see "Evaluation rules").
- Keep secrets out of code. Read any keys from environment variables loaded from `.env`, and make sure `.env` is in `.gitignore`.
- Cache every network response to disk. Never re-download data already on disk.
- Write pytest tests for the parts where silent bugs are likely (listed below).

## Stack

- Python 3.11+
- pandas (or polars), numpy, scikit-learn, lightgbm, shap
- `mwclient` or `leaguepedia_parser` for Leaguepedia (only if needed, see M1)
- `gdown` for Oracle's Elixir (fallback: I download the CSVs manually into `data/raw/oe/`)
- pytest
- Manage deps with `uv` or a plain `requirements.txt`

## Data sources

### Oracle's Elixir (primary pro dataset)
- Free yearly CSVs (one per year), linked from oracleselixir.com, hosted on Google Drive. Non-commercial use only.
- Each game is expected to have per-player rows plus per-team rows. Verify this.
- Use for: results, patch, league, split, date/time, side, team, player, champion, position, and bans.
- **Check first:** the team rows may already contain ordered pick columns, ordered ban columns, and a first-pick flag. If they do, and they are populated for the configured leagues and years, the full 20-action sequence can be reconstructed from Oracle's Elixir alone. Report coverage per league/year before deciding whether Leaguepedia is needed.
- Do NOT use in-game stats (kills, gold, damage, etc.) as model features. They are post-game and leak the outcome.

### Leaguepedia Cargo API (pick/ban order, fallback and cross-check)
- Endpoint: `https://lol.fandom.com/api.php` (Cargo queries). Verify current access rules (anonymous reads may be restricted or heavily throttled).
- Key tables: `PicksAndBansS7`, `ScoreboardGames`, `ScoreboardPlayers`. Inspect fields via Special:CargoTables first.
- Role depends on the Oracle's Elixir check above:
  - If OE has full draft order: pull Leaguepedia for a sample of about 200 games only and use it to validate the reconstructed order.
  - If OE does not: Leaguepedia is the source of truth for draft order and must be pulled for all games.
- Throttle (around 1 request per second or slower), back off on errors, and cache all results as JSON or parquet in `data/raw/leaguepedia/`.
- If throttling or login is a blocker, tell me and I will provide Fandom bot credentials via `.env` (`FANDOM_USER`, `FANDOM_BOT_PASSWORD`).

### Data Dragon (champion metadata)
- `https://ddragon.leagueoflegends.com`. No key. Use for the champion list, IDs, tags (Mage, Marksman, etc.) and base stats.

### Scope
- Major leagues (LCK, LPL, LEC, LCS/LTA, LCP, international events), 2024 through 2026. Make leagues and years configurable.
- 2024 is mostly pre-Fearless and is a different draft distribution. See "Regime handling" below.

## Domain notes

### Draft order and First Selection
- The 20-action sequence in terms of **draft slot** (F = first-pick team, S = second-pick team):
  - Ban phase 1: F, S, F, S, F, S
  - Pick phase 1: F, S, S, F, F, S
  - Ban phase 2: S, F, S, F
  - Pick phase 2: S, F, F, S
- Through 2025, F was always blue side. **From the 2026 season, the First Selection rule decouples side from draft order:** one team chooses either side or draft priority, the other team gets the remaining choice. Red side can draft first.
- Therefore: never derive draft order from side. Store `first_pick_team` per game from the data, key every sequence computation on draft slot, and keep side as a separate feature.
- Verify the sequence against real data (both pre-2026 and 2026 games, including at least a few 2026 games where red drafted first) and encode it once, in one place.

### Roles
- Picks do not reveal roles during the draft. Roles are only known after the game. Model role as a probability distribution per champion, learned from history (flex picks matter).
- Starting rosters are known before the draft and are valid features.

### Fearless draft
- Hard Fearless (a champion picked by either team earlier in the series cannot be picked again in that series) has been standard across tier-1 leagues and international events since 2025. Some 2024 events used variants (for example soft Fearless) or no Fearless.
- Still detect it empirically per league/split rather than hardcoding (if champions never repeat within a series, treat it as Fearless; distinguish hard from soft by whether the opponent's earlier picks also never repeat). Store the result as a flag.
- Series state is a first-class input, not just an availability mask:
  - champions already used in the series (unavailable)
  - game number in series and series score
  - each team's remaining comfort pool given what has been burned

### Regime handling
- Pre-Fearless and Fearless drafts differ, and 2026 First Selection changes the meaning of side. Do not assume older data helps.
- Compare at least: (a) train on Fearless-era games only, (b) train on all games with time-decay weights and a regime flag. Keep whichever validates better.

### Patches
- Patches matter, but many champions stay relevant across patches. Champion strength should combine a long-run prior with patch-specific evidence.

## Critical correctness requirement: no leakage

Every feature for a game must be computed **only from games that finished before that game started**.

- Order games by start timestamp, or by (date, series, game number in series) if timestamps are missing. Do **not** use a date-only cutoff: earlier games in the same series and on the same day are valid history and are the most important context under Fearless.
- Applies to champion pick/ban/win rates, player champion pools, team ratings, synergy and counter stats, and everything else.
- Implement features as point-in-time ("as-of") computations.
- Add a test that fails if any feature for game G uses data from G or any later game, and a second test confirming that game 2 of a series does see game 1.
- Validation and test splits are **time-based** (for example train on everything before the most recent split, validate on the most recent split). Never use random splits. Never split a series across train and validation.

## Evaluation rules

The dataset is small (a few thousand games per year), so small metric differences are noise.

- Every model-vs-baseline comparison must report a paired bootstrap 95% confidence interval on the metric difference, resampling by series (not by game or by action).
- "Beats the baseline" means the interval excludes zero. Otherwise report it as a tie and keep the simpler model.
- State the validation set size (games and series) next to every metric.

## Milestones

### M1: Ingestion and unified draft table
- Download Oracle's Elixir CSVs for the configured years. Print the schema. Determine whether ordered picks, ordered bans, and first-pick are present and populated. **Report this before building anything on Leaguepedia.**
- Build the draft sequence from OE if possible, with Leaguepedia as a sampled cross-check. Otherwise pull Leaguepedia pick/ban order for all games and join.
- If joining: try a direct game ID match first. If IDs do not line up, fall back to matching on date + teams + game number in series. Report the match rate and inspect unmatched games.
- Output `data/processed/draft_actions.parquet` with one row per draft action:
  `game_id, series_id, game_in_series, datetime, patch, league, split, blue_team, red_team, first_pick_team, action_index (0-19), action_type (ban/pick), draft_slot (first/second), side, acting_team, champion, winner_side`
- Also output `data/processed/games.parquet` with one row per game, including starting rosters, final roles per champion, `first_pick_team`, Fearless flag, and champions unavailable at the start of the game.
- Handle and report irregular drafts (missed bans, remakes, fewer than 20 actions) rather than silently dropping them.
- Tests: draft order parsing, draft slot and side assignment (including a 2026 red-side-first-pick game), series reconstruction, join correctness on a few hand-checked games.
- **Report:** OE draft-order coverage, row counts per league/year, join or cross-check agreement rate, missing data, Fearless detection results, share of 2026 games where first pick was on red side.

### M2: Baselines
- Next action baseline A: rank champions by as-of patch presence (pick + ban rate), excluding unavailable champions (already picked/banned this game, or Fearless-burned).
- Next action baseline B: A plus the relevant team's recent frequency (acting team's picks for pick actions; opponent's picks for ban actions).
- Win baseline 1: as-of team Elo only. Regress ratings toward the league mean at split boundaries to account for roster changes. Note that cross-region ratings rest on few international games.
- Win baseline 2: Elo + side + first-pick indicator.
- Metrics:
  - Next action: top-1, top-5, top-10 hit rate and log loss, reported separately for picks and bans and per draft phase.
  - Win: log loss, Brier score, and a calibration plot (reliability diagram) saved to `reports/`.
- **Report:** baseline numbers with validation set sizes. These are the numbers to beat.

### M3: Real models
- **Next action model:** LightGBM scoring (draft state, candidate champion) pairs, trained with a **softmax (listwise cross-entropy) objective over the available candidates** so outputs are probabilities. Do not use lambdarank (its scores are not probabilities). If a custom objective is impractical, train a binary classifier per candidate and renormalize with a fitted temperature.
  - Train one joint model and separate pick/ban models. Keep the separate models only if they win under the evaluation rules.
  - Candidate features (all as-of):
    - Champion patch presence, pick rate, ban rate, smoothed with a cross-patch prior (empirical Bayes shrinkage toward the champion's long-run value, with time-decay weights)
    - Champion win rate **relative to Elo expectation** (wins minus expected wins given team ratings), not raw win rate, which is confounded by team strength
    - Acting team's frequency on the champion; opponent's frequency (ban signal)
    - Player mastery: games on the champion for each player on the relevant team, weighted by the probability the champion fits that player's role. For bans, use the opponent's players.
    - Open roles for the acting team, and the champion's role distribution
    - Synergy with the team's locked picks, counter value against enemy picks. Pro-only pairwise samples are tiny, so shrink heavily toward zero and treat these as low-confidence until Phase 4 solo queue data exists. Report whether they help at all.
    - Champion attributes from Data Dragon tags
    - Action index, phase, draft slot, side, game number in series, Fearless-burned counts, remaining comfort pool size
- **Win model:** logistic regression first (team Elo diff, side, first-pick, plus draft features such as aggregate adjusted champion strength, synergy, counter, player mastery). Try LightGBM only if it beats logistic regression under the evaluation rules. Calibrate if needed (Platt or isotonic).
  - Report win probability two ways: full model, and "draft effect" (prediction with draft features minus prediction from win baseline 2).
  - Expect the draft effect to be small. If the full model does not beat the baseline, say so plainly rather than tuning until it does.
  - Win probability should be producible at any point in the draft, not just after it ends. Train on partial drafts too.
- **Explanations:** compute SHAP values for both models. Before export, **sum SHAP values within groups of correlated features** (for example "champion meta strength", "player comfort", "team tendency", "synergy/counter", "team strength", "series context") and expose the top groups per prediction as structured JSON, with the raw feature values underneath.
- **CLI demo, two modes:**
  - Replay: `python -m draft_analyzer.predict --game-id <id> --upto <action_index>`
  - Live/manual: `python -m draft_analyzer.predict --state state.json`, where the file gives patch, league, both teams, starting rosters, sides, first-pick team, champions burned earlier in the series, and the actions so far. This is the path for following a real draft as it happens.
  - Both print the top 10 next-action candidates with probabilities, current win probability (full and draft effect), and the top factors.
- **Report:** metrics vs. baselines on the validation split with confidence intervals, feature importance, an ablation showing which feature groups actually earn their place, and failure cases (where the model is confidently wrong).

## Suggested layout

```
draft_analyzer/
  config.py           # leagues, years, paths
  ingest/
    oracles_elixir.py
    leaguepedia.py
    ddragon.py
    join.py
  draft.py            # draft slot sequence, availability, Fearless and series logic
  features/
    asof.py           # point-in-time stat helpers
    champion.py
    team.py
    player.py
    pairwise.py       # synergy and counters
  models/
    baselines.py
    next_action.py
    win.py
  evaluate.py         # metrics, series-level paired bootstrap
  explain.py          # grouped SHAP to JSON
  predict.py          # CLI (replay and manual state)
tests/
data/raw/  data/processed/   # gitignored
reports/
```

## Out of scope for this session (context only)

- **Phase 4: Solo queue data.** Riot match-v5 API (high elo) to estimate current-patch champion strength and, most importantly, synergy and counter stats that pro data is too small to support. Pretrain, then fine-tune on pro. Needs `RIOT_API_KEY` in `.env`. Dev keys expire every 24 hours and are heavily rate limited, so the fetcher must be resumable.
- **Phase 5: LLM explanation layer.** Feed the grouped SHAP JSON to an LLM to write natural-language reasoning. The LLM never produces probabilities, only explains them. Needs `ANTHROPIC_API_KEY`.
- A UI or automated live draft integration (the manual state file in M3 is the stand-in).
