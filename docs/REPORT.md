# Build report: Phases 1 to 3 (M1, M2, M3)

Written 2026-11-10. The spec (`lol-tool.md`) asks for a stop and a report after each milestone unless told to
continue without check-ins; this build ran straight through, so the three milestone reports are collected here
for review. Numbers come from the generated tables in `reports/`, which are the source of truth:

| Milestone | Generated tables | Extra detail |
|---|---|---|
| M1 ingestion | `reports/M1_ingestion.md` | `docs/M1_crosscheck.md` (Leaguepedia cross-check), `docs/research_rules.md`, `docs/research_leaguepedia.md` |
| M2 baselines | `reports/M2_baselines.md` | |
| M3 models | `reports/M3_models.md` | |

Phases 4 (solo queue data) and 5 (LLM explanation layer) were not started.

## Headline

- **Next pick/ban model beats both baselines by a wide margin.** Validation (651 games, 240 series, 13,015
  actions): top-1 27.1%, top-5 66.3%, top-10 83.1%, log loss 2.566, against 12.0% / 39.6% / 58.1% / 3.528 for the
  best baseline. Every difference has a series-level bootstrap interval far from zero. The held-out test split
  (Worlds 2026 and later, 172 games, scored once) gives 26.5% / 64.5% / 82.7% / 2.585.
- **The win model does not beat the baseline.** With the full draft known, logistic regression scores log loss
  0.6370 against 0.6415 for Elo + side + first pick: a tie (difference -0.0045, CI -0.0133 to +0.0045). On the
  test split it is slightly worse than the baseline, also a tie. The draft effect is not detectable in this data.
- **Oracle's Elixir alone is enough for draft order.** Leaguepedia was only needed as a cross-check, and it
  agrees on first pick and side in every game compared.

## M1: ingestion and the unified draft table

**Built:** `ingest/oracles_elixir.py` (download, load, build), `draft.py` (the 20-action sequence encoded once by
draft slot, series reconstruction, Fearless detection), `ingest/ddragon.py`, `ingest/leaguepedia.py` and
`ingest/join.py` (cross-check). Outputs: `data/processed/games.parquet`, `draft_actions.parquet`,
`champions.parquet`, `fearless_summary.csv`, `leaguepedia_crosscheck.parquet`.

**Oracle's Elixir schema (inspected before any parser was written):** 12 rows per game (10 player rows, 2 team
rows). Team rows carry `firstPick`, `ban1..5` and `pick1..5` in each team's own order. For the configured
leagues these are populated in 100% of games for picks and first pick; 33 games have a missed ban.

**Data:** 7,172 games, 2,932 series, 143,440 draft-action rows, across LCK, LPL, LEC, LCS, LTA (N/S), LCP, MSI,
Worlds, First Stand and EWC, from 2024-01-13 to 2026-11-09.

**Cross-check against Leaguepedia** (stratified sample of 245 games, 74 of them 2026 red-first-pick):

| Compared | Agreement |
|---|---|
| Games matched (201 by Riot game id, 44 LPL by date + teams + game number) | 245 / 245 |
| First-pick team | 245 / 245 |
| Map side | 245 / 245 |
| Each team's ban order | 245 / 245 |
| Each team's pick order, and the full 20-action sequence | 244 / 245 |

All 1,444 games on the fetched wiki pages were also compared at no extra request cost: first pick and side agree
in all of them (418 red-first), the full sequence in 1,437.

**First Selection (2026):** red had first pick in 62.8% of the 2,579 games played from 2026-01-14, and in none
before. In those games blue side won 55.3% and the first-pick team won 49.6%. Teams with the choice mostly take
blue side and concede first pick. The share varies by league (LCK 74%, LPL 71%, LEC 44%).

**Fearless detection (empirical, per league / season / split / stage):** hard Fearless in every 2025 and 2026
group (71 groups, 5,147 games) with a repeat rate of exactly zero; no Fearless in 2024 (1,589 games) except LPL
2024 Summer Placements, detected as soft Fearless (130 games); 306 games are in best-of-1 stages where the
question does not arise.

**Irregular data, kept and flagged rather than dropped:**
- 33 games with a missed ban (null `champion` for that action; every game still has 20 rows).
- 2 games where OE shows an impossible duplicate ban; Leaguepedia records a missed ban, so the later duplicate is
  nulled.
- 3 games in 2 series whose game 1 is absent from OE (`series_incomplete`).
- 49 games list both teams' picks exactly in role order. 12 were checked: 10 genuine, 2 (LEC 2026) are OE
  entries that lost the draft order. Flagged (`picks_in_role_order`), not patched.

**Surprises:**
- A news report said KT had blue side and DNS first pick in the 2026 LCK Cup opener, which contradicts OE. The
  wiki's schedule, scoreboard and pick/ban template all side with OE; the report describes game 3, not game 1.
- Anonymous Leaguepedia Cargo queries are rate-limited to the point of being unusable, but anonymous page-source
  reads work and were enough (23 live requests, all cached). The bot-password Cargo path is written but untested.
- OE's `year` is a season label: the 2026 file holds LCP and LTA promotion games from October 2025, and 23
  "LCS 2027" games from October 2026 that are a promotion tournament with amateur teams.
- Google Drive blocks anonymous downloads of the CSVs with a quota error; they were downloaded with
  `gdown --cookies-from-browser chrome`, as approved.

## M2: baselines

**Built:** the as-of feature engine (`features/asof.py`), the dataset builder (`features/dataset.py`),
`models/baselines.py`, `evaluate.py` (metrics, series-level paired bootstrap, reliability diagram).

**Splits, by series start:** train to 2026-07-19 (6,349 games), validation 2026-07-22 to 2026-09-13 (651 games,
240 series: the last regional split of 2026 with playoffs), test from 2026-09-17 (172 games, 66 series). The
last part of training (from 2026-04-01, 1,087 games) is the tune period for early stopping and hyperparameters.
No series straddles a boundary.

**Next action, validation (13,015 actions; a uniform guess over the ~154 available champions scores log loss 5.03):**

| Baseline | Top-1 | Top-5 | Top-10 | Log loss |
|---|---|---|---|---|
| A: as-of patch presence | 10.9% | 37.3% | 56.0% | 3.582 |
| B: A + relevant team's recent pick frequency | 12.0% | 39.6% | 58.1% | 3.528 |

B beats A on every metric overall (log loss -0.055, CI -0.065 to -0.044). The gain is in picks; for bans B's
top-1 and top-5 are ties with A. Both are much better at ban phase 1 (top-5 58%) than at pick phase 2 (22%).

**Win, validation (651 games):**

| Baseline | Log loss | Brier | Accuracy |
|---|---|---|---|
| Constant (training blue win rate) | 0.6846 | 0.2457 | 58.1% |
| 1: Elo only | 0.6456 | 0.2267 | 60.5% |
| 2: Elo + side + first pick | 0.6415 | 0.2246 | 63.3% |

Baseline 2 vs 1 is a tie on log loss (-0.0041, CI -0.0094 to +0.0010). The fitted first-pick coefficient is
small (+0.04 log-odds for the first-pick team) next to the blue-side intercept (+0.15). Calibration plot:
`reports/m2_win_calibration.png`.

Elo settings (K = 28, no regression inside a season, 35% toward the league mean at season boundaries) were
grid-searched on the training period only. Regressing at every split boundary, as the spec suggests, was
slightly worse than regressing only between seasons; the optimum is flat.

## M3: models

**Built:** stat blocks `features/champion.py`, `team.py`, `player.py`, `pairwise.py`, `roles.py`, `context.py`,
`elo.py` (93 features: 59 per candidate, 34 per draft state); `models/next_action.py`, `models/win.py`;
`explain.py`; `predict.py`; `experiments.py` and `report.py`.

### Next pick/ban

LightGBM over (draft state, candidate champion) pairs with a custom listwise softmax objective over the available
candidates, so outputs are probabilities. Training samples 60 negatives per action with an importance-weight
correction to the partition function; evaluation uses all candidates.

| Validation | Top-1 | Top-5 | Top-10 | Log loss |
|---|---|---|---|---|
| Baseline B | 12.0% | 39.6% | 58.1% | 3.528 |
| Model | 27.1% | 66.3% | 83.1% | 2.566 |
| Difference (95% CI) | +15.1 (14.4 to 15.9) | +26.6 (25.7 to 27.6) | +25.0 (24.1 to 25.9) | -0.961 (-0.985 to -0.938) |

The model beats B in every phase. The largest gains are in the second pick phase (top-5 62% vs 22%), where role
fit and what is already locked narrow the field.

**Decisions made by the evaluation rules:**
- **Regime:** training on the Fearless era only (2025 on) beats training on all games with time decay and a
  regime flag (log loss +0.013 for all-games, CI +0.008 to +0.018). Pre-Fearless data hurts slightly. Kept:
  Fearless era only.
- **Joint vs separate:** separate pick and ban models tie with the joint model (log loss +0.004, CI -0.003 to
  +0.011). Kept: one joint model.

**Ablation (log loss without the group minus full model; all intervals exclude zero):**

| Group removed | Log loss change |
|---|---|
| Champion meta strength | +0.247 |
| Synergy / counter (pairwise) | +0.145 |
| Role fit | +0.112 |
| Series context | +0.028 |
| Team tendency | +0.027 |
| Player comfort | +0.024 |
| Team strength (Elo) | +0.008 |
| Champion attributes (Data Dragon tags) | +0.007 |

The pairwise group needs a caveat. Split in two, the win-based synergy and counter estimates add nothing
(+0.002, CI -0.003 to +0.008: a tie), while the "how often are these champions drafted together or into each
other" features carry the whole +0.144. That answers the spec's question: pro-only synergy and counter win
statistics do not help; drafting-habit co-occurrence does.

Team tendency, player comfort and series context each help log loss and top-5, but their top-1 effects are ties.

**Calibration:** the top-1 probability is honest across the range (for example, mean confidence 54% gives 53%
accuracy; 68% gives 67%; 86% gives 82%).

**Failure cases:** the most confident misses are a handful of contested champions predicted at 82 to 90% and not
taken (Orianna 8 times in the top 15, Milio 4 times); the actual choice was ranked 2 to 7 in all of them. Only
0.4% of actions chose a champion ranked below 50, mostly in pick phase 2.

### Win probability

| Validation, full draft known | Log loss | vs baseline 2 (95% CI) |
|---|---|---|
| Baseline 2: Elo + side + first pick | 0.6415 | |
| Logistic regression, pre-game features only | 0.6420 | +0.0004 (-0.0019 to +0.0028), tie |
| Logistic regression, pre-game + draft features | 0.6370 | -0.0045 (-0.0133 to +0.0045), tie |
| LightGBM, same features | 0.6294 | -0.0121 (-0.0212 to -0.0030), better |

- **The draft does not measurably improve the prediction.** The same model after the draft vs before it:
  -0.0032 log loss for logistic regression, -0.0034 for LightGBM, both ties. No draft feature group earns its
  place in the ablation.
- **LightGBM's edge is not a draft effect and did not hold up.** It already beats the baseline before the draft
  (k = 0), so the gain is in how it uses Elo, not in the picks. It does not beat logistic regression on the tune
  period, and on the test split both full models are slightly worse than baseline 2 (ties). Elo was overconfident
  in the validation window (fitted slope 1.73 there vs 2.46 in training), which favours any more conservative
  model. LightGBM vs logistic regression with the full draft is a tie (-0.0076, CI -0.0163 to +0.0008), so
  logistic regression is kept.
- The reported draft effect is still shown by the CLI (mean absolute size 4.3 percentage points after a full
  draft), but it should be read as unvalidated.

### Explanations and CLI

- `explain.py` sums SHAP values within feature groups and exports the top groups per prediction as JSON with the
  raw feature values underneath. The next-action model uses LightGBM's exact TreeSHAP; the win model uses the
  closed-form linear SHAP. For candidates, contributions are reported relative to the probability-weighted
  field, so context shared by all candidates cancels.
- `predict.py` has both modes from the spec. Replay uses the validated model with features as-of the game; live
  mode uses a refit on all data and accepts earlier games of the series either from the dataset or inline in the
  state file. Both were run end to end.

## Leakage

- Feature code only receives pre-game facts plus the actions made so far; outcomes are only visible to `update`,
  which the engine calls once a game's end time is at or before the start of the game being featurised.
- Tests (`tests/test_asof.py`, run over every block): features of a game do not change when its own result,
  final roles and all later games change; pre-draft features do not see the game's own draft; mid-draft features
  do not see later actions; a concurrent game that has not finished is invisible; game 2 of a series does see
  game 1 an hour earlier.
- An independent review of the feature code found no leak. It did find 4 actions (bans of a Fearless-burned
  champion, all LPL) where the true answer had to be added to the candidate set by hand; these are now excluded
  from training and evaluation. It also found that the live CLI treated soft Fearless as hard; fixed.
- One caveat: the Fearless mode per split is detected from that whole split's games. It behaves as rule
  metadata (every group is at a repeat rate of exactly 0 or above 0.5), but it is not strictly as-of.

## Not done, and open questions

1. **Win model shipped by the CLI.** It shows logistic regression with draft features next to baseline 2. Since
   that model is a tie with the baseline, an alternative is to report only the baseline until Phase 4 data
   exists.
2. **The 23 "LCS 2027" promotion games** (amateur teams, labelled LCS by OE) are in the test split. They could be
   excluded from scope.
3. **Player history is limited to the configured leagues.** Adding tier-2 leagues (LCK CL, LDL, NACL, EMEA
   Masters) as history only would give rookies a track record; player comfort is currently a small effect.
4. **The 2 LEC 2026 games with lost draft order** could be patched from Leaguepedia; a further 37 role-ordered
   games are unchecked.
5. **Cross-region Elo** rests on few international games (documented in the spec as expected).
6. **Fandom bot credentials** are only needed if you want the Cargo route; nothing in Phases 1 to 3 is blocked on
   them.
7. Hyperparameters of the next-action model were set once and not searched. Negative sampling (60 per action)
   was not compared against training on all candidates.
