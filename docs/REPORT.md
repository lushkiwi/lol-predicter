# Build report: Phases 1 to 3 (M1, M2, M3)

Written 2026-11-10, revised the same day after review (see "Changes after review" below). The spec (`lol-tool.md`) asks for a stop and a report after each milestone unless told to
continue without check-ins; this build ran straight through, so the three milestone reports are collected here
for review. Numbers come from the generated tables in `reports/`, which are the source of truth:

| Milestone | Generated tables | Extra detail |
|---|---|---|
| M1 ingestion | `reports/M1_ingestion.md` | `docs/M1_crosscheck.md` (Leaguepedia cross-check), `docs/research_rules.md`, `docs/research_leaguepedia.md` |
| M2 baselines | `reports/M2_baselines.md` | |
| M3 models | `reports/M3_models.md` | |
| History-only leagues | `reports/history_leagues.md` | |

Phases 4 (solo queue data) and 5 (LLM explanation layer) were not started.

## Headline

- **Next pick/ban model beats both baselines by a wide margin.** Validation (651 games, 240 series, 13,015
  actions): top-1 28.2%, top-5 67.0%, top-10 83.3%, log loss 2.528, against 12.0% / 39.6% / 58.1% / 3.528 for the
  best baseline. Every difference has a series-level bootstrap interval far from zero. The test split (149
  games from 2026-09-17: 106 Worlds, 33 LCS, 10 LEC) gives 27.9% / 65.6% / 83.0% / 2.551.
- **The win model does not beat the baseline.** With the full draft known, logistic regression scores log loss
  0.6369 against 0.6415 for Elo + side + first pick: a tie (difference -0.0046, CI -0.0133 to +0.0043). On the
  test split it is slightly worse than the baseline, also a tie. The draft effect is not detectable in this data.
- **Oracle's Elixir alone is enough for draft order.** Leaguepedia was only needed as a cross-check, and it
  agrees on first pick and side in every game compared.

## Changes after review

Made after the first version of this report, at the user's request. All numbers below are from the final run.

- **"LCS 2027" is out of scope.** The 23 games OE labels LCS 2027 are an October 2026 promotion tournament
  between five amateur teams, with no LCS team in it. They are no longer predicted or scored; the test split is
  now 149 games (59 series), all Worlds 2026. `config.OUT_OF_SCOPE_EVENTS` holds the rule.
- **History-only leagues were added.** 7,431 games from LCK CL, LDL, NACL, EMEA Masters, PCS, VCS, LJL, CBLOL and
  LLA (plus the 23 promotion games) now pass through the same as-of engine and update player statistics. They are
  never predicted, trained on or scored. 392 of the 629 players in scope also appear in them. LDL has no draft
  order in OE, so it only counts for statistics that do not need it.
- **It makes no measurable difference on validation.** Five ways of using the history were compared against none
  (`reports/history_leagues.md`); none beats it. Feeding player statistics only is a clean tie (log loss -0.0010,
  CI -0.0051 to +0.0032) and is the default, because it was asked for and gives players arriving from those
  leagues non-empty statistics. Restricted to the 281 validation games that include a newcomer, it is still a tie.
- **History does not feed Elo.** Each league is its own rating pool, so a team that dominates a lower-tier league
  would arrive at Worlds rated like a top team. With Elo fed from history, the win baseline on the 149 test games
  went from 0.6624 to 0.6743 (a tie, CI -0.011 to +0.034, but in the wrong direction), so it was left out.
- **The training-regime decision flipped.** The model now trains on all games with time decay instead of the
  Fearless era only. See "Decisions made by the evaluation rules" under M3.
- **The test split has now been scored twice** (once per full run). No decision used it. On the same 149 games
  the earlier model scored 26.4% / 64.3% / 82.6% / 2.598, a tie with the final model on every metric.
- **CLI:** the draft effect on win probability is now labelled as not validated.
- **Accuracy round (after the web UI).** Three changes to the next-action model were adopted on validation
  evidence and one was rejected; see "Accuracy round" under M3. Validation log loss went from 2.562 to 2.528 and
  top-1 from 27.4% to 28.2%. The test split was scored a third time for the new model; no decision used it.
- **Web UI** added (`python -m draft_analyzer.web`), see the end of the M3 section.

## M1: ingestion and the unified draft table

**Built:** `ingest/oracles_elixir.py` (download, load, build), `draft.py` (the 20-action sequence encoded once by
draft slot, series reconstruction, Fearless detection), `ingest/ddragon.py`, `ingest/leaguepedia.py` and
`ingest/join.py` (cross-check). Outputs: `data/processed/games.parquet`, `draft_actions.parquet`,
`champions.parquet`, `fearless_summary.csv`, `leaguepedia_crosscheck.parquet`.

**Oracle's Elixir schema (inspected before any parser was written):** 12 rows per game (10 player rows, 2 team
rows). Team rows carry `firstPick`, `ban1..5` and `pick1..5` in each team's own order. For the configured
leagues these are populated in 100% of games for picks and first pick; 33 games have a missed ban.

**Data:** 7,149 games, 2,925 series, 142,980 draft-action rows, across LCK, LPL, LEC, LCS, LTA (N/S), LCP, MSI,
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

**First Selection (2026):** red had first pick in 62.6% of the 2,556 games played from 2026-01-14, and in none
before. In those games blue side won 55.1% and the first-pick team won 49.8%. Teams with the choice mostly take
blue side and concede first pick. The share varies by league (LCK 74%, LPL 71%, LEC 44%).

**Fearless detection (empirical, per league / season / split / stage):** hard Fearless in every 2025 and 2026
group (70 groups, 5,124 games) with a repeat rate of exactly zero; no Fearless in 2024 (1,589 games) except LPL
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
  "LCS 2027" games from October 2026 that are a promotion tournament with amateur teams (now out of scope).
- Google Drive blocks anonymous downloads of the CSVs with a quota error; they were downloaded with
  `gdown --cookies-from-browser chrome`, as approved.

## M2: baselines

**Built:** the as-of feature engine (`features/asof.py`), the dataset builder (`features/dataset.py`),
`models/baselines.py`, `evaluate.py` (metrics, series-level paired bootstrap, reliability diagram).

**Splits, by series start:** train to 2026-07-19 (6,349 games), validation 2026-07-22 to 2026-09-13 (651 games,
240 series: the last regional split of 2026 with playoffs), test from 2026-09-17 (149 games, 59 series). The
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
`elo.py` (99 features: 65 per candidate, 34 per draft state); `models/next_action.py`, `models/win.py`;
`explain.py`; `predict.py`; `experiments.py` and `report.py`.

### Next pick/ban

LightGBM over (draft state, candidate champion) pairs with a custom listwise softmax objective over the available
candidates, so outputs are probabilities. Evaluation always uses all candidates.

Two training settings are in use. The final model (the table below, the CLI and the web UI) trains on every
candidate and is the mean logit of three fits with different seeds. The experiments that need many fits (regime,
joint vs separate, ablations) train on 60 sampled negatives per action with an importance-weight correction to
the partition function; a single fit of that kind scores about 2.56.

| Validation | Top-1 | Top-5 | Top-10 | Log loss |
|---|---|---|---|---|
| Baseline B | 12.0% | 39.6% | 58.1% | 3.528 |
| Model | 28.2% | 67.0% | 83.3% | 2.528 |
| Difference (95% CI) | +16.3 (15.4 to 17.1) | +27.4 (26.5 to 28.3) | +25.2 (24.3 to 26.1) | -0.999 (-1.024 to -0.976) |

The model beats B in every phase. The largest gains are in the second pick phase (top-5 64% vs 22%), where role
fit and what is already locked narrow the field.

**Accuracy round** (`reports/player_targeting.md`, `reports/tuning.md`; all choices on validation, none on test):

| Step | Top-1 | Top-5 | Log loss | Against the step above (log loss, 95% CI) |
|---|---|---|---|---|
| First M3 model: one fit, 60 sampled negatives | 27.4% | 66.4% | 2.562 | |
| Mean logit of 3 seeds | 27.9% | 66.7% | 2.543 | -0.019 (-0.022 to -0.016) |
| Train on every candidate | 27.6% | 66.8% | 2.533 | -0.009 (-0.013 to -0.006) |
| Pool-pinch player features | 28.2% | 67.0% | 2.527 | -0.0065 (-0.0107 to -0.0024) |

- **Seed averaging** is the largest and safest gain. It also changes what "noise" means here: single fits of one
  recipe differ by up to 0.015 log loss, more than the 0.005 first measured.
- **Every candidate instead of 60 sampled negatives.** A search over tree settings, time decay and the number of
  negatives, scored on the tune period only, found that the number of negatives is the one setting that matters
  (tune-period log loss with 30: 2.67; 60: 2.575; 120: 2.550; all, tried with a 730-day decay: 2.547). On
  validation its top-1 is a tie, leaning lower (-0.3 points, CI -0.7 to +0.1); the gain is in the probabilities.
- **Pool-pinch features** (how much of a player's champion pool is left after Fearless burns and this game's picks
  and bans) passed the rule set before the fits: adopt only if the log-loss interval excludes zero. With sampled
  negatives the same comparison was a tie (-0.003), so this is a second look at the same games and a small gain.
  Its top-1 gain is +0.6 points (CI +0.2 to +1.1).
- Over the three changes together, top-1 improves most in the first pick phase (29.9% to 31.5%); the other
  phases are ties on top-1 and better on log loss, except the first ban phase, a tie on both.
- **Targeted-ban features** (which champions get banned against a player, following the player across teams)
  were rejected: log loss +0.006 (CI +0.003 to +0.010), worse. The code stays behind
  `PlayerMasteryBlock(targeted_bans=True)`, off by default, so the experiment can be rerun.
- The retrained final model scores 2.528 on validation, against 2.527 in the experiment: the same recipe, fitted
  again with a different thread count.
- On the test split the first M3 model scored 27.4% / 64.3% / 82.4% / 2.596 and the final model 27.9% / 65.6% /
  83.0% / 2.551. The first model's per-action test scores were not kept, so there is no paired interval for
  that difference; with 149 games the top-1 change is well inside noise.

**Decisions made by the evaluation rules:**
- **Regime:** training on all games with a 365-day time decay and a regime flag beats training on the Fearless
  era only (log loss -0.010, CI -0.014 to -0.006). Kept: all games with decay. Each regime is now judged on the
  mean logit of three seeds, because single fits are too noisy for this decision: the single fits score 2.570 /
  2.566 / 2.574 (Fearless only) and 2.564 / 2.567 / 2.552 (all games), and on seed 0 alone the comparison was a
  tie that would have fallen back to Fearless only. The first run of this report chose Fearless only by 0.013,
  before player history from lower tiers was added.
- **Joint vs separate:** not settled. On three-seed averages in the sampled setting, separate pick and ban models
  have better log loss (-0.008, CI -0.014 to -0.003) but not better top-1 (-0.4 points, a tie), and they are
  worse in the first ban phase on both. The final model is one joint model; separate models have not been
  compared when trained on every candidate (open question 7).

**Ablation (log loss without the group minus full model):**

| Group removed | Log loss change | Verdict |
|---|---|---|
| Champion meta strength | +0.244 | earns its place |
| Synergy / counter (pairwise) | +0.143 | earns its place |
| Role fit | +0.093 | earns its place |
| Team tendency | +0.025 | earns its place |
| Player comfort | +0.024 | earns its place |
| Series context | +0.016 | earns its place |
| Champion attributes (Data Dragon tags) | +0.009 | earns its place |
| Team strength (Elo) | +0.004 | tie |

Ablations are single fits in the sampled setting (reference 2.564), so the rows below about +0.015 are of the
same size as seed-to-seed noise, which the intervals do not include. That covers champion attributes, which was
a tie in the previous run.

The pairwise group needs a caveat. Split in two, the win-based synergy and counter estimates add nothing
(+0.004, CI -0.001 to +0.010: a tie), while the "how often are these champions drafted together or into each
other" features carry +0.138. That answers the spec's question: pro-only synergy and counter win statistics do
not help; drafting-habit co-occurrence does.

**Calibration:** the top-1 probability of the final model is close to its accuracy up to 50% (mean confidence
34% gives 34%; 44% gives 42%) and a few points underconfident above it (54% gives 59%; 68% gives 72%; 87% gives
92%). The first M3 model was overconfident above 30%.

**Failure cases:** the most confident misses are a handful of contested champions predicted at 76 to 84% and not
taken (Orianna 7 times in the top 15, Milio 3 times); the actual choice was ranked 2 to 10 in all of them. Only
0.4% of actions chose a champion ranked below 50, mostly in pick phase 2.

### Win probability

| Validation, full draft known | Log loss | vs baseline 2 (95% CI) |
|---|---|---|
| Baseline 2: Elo + side + first pick | 0.6415 | |
| Logistic regression, pre-game features only | 0.6420 | +0.0004 (-0.0019 to +0.0028), tie |
| Logistic regression, pre-game + draft features | 0.6369 | -0.0046 (-0.0133 to +0.0043), tie |
| LightGBM, same features | 0.6307 | -0.0108 (-0.0198 to -0.0021), better |

- **The draft does not measurably improve the prediction.** The same model after the draft vs before it:
  -0.0038 log loss for logistic regression, -0.0026 for LightGBM, both ties. No draft feature group earns its
  place in the ablation.
- **LightGBM's edge is not a draft effect and did not hold up.** It already beats the baseline before the draft
  (k = 0), so the gain is in how it uses Elo, not in the picks. It does not beat logistic regression on the tune
  period, and on the test split both full models are slightly worse than baseline 2 (0.6749 and 0.6756 against
  0.6624, ties). Elo was overconfident
  in the validation window (fitted slope 1.73 there vs 2.46 in training), which favours any more conservative
  model. LightGBM vs logistic regression with the full draft is a tie (-0.0062, CI -0.0152 to +0.0023), so
  logistic regression is kept.
- The reported draft effect is still shown by the CLI (mean absolute size 4.1 percentage points after a full
  draft), and the CLI and web UI both label it as not validated.

### Explanations and CLI

- `explain.py` sums SHAP values within feature groups and exports the top groups per prediction as JSON with the
  raw feature values underneath. The next-action model uses LightGBM's exact TreeSHAP; the win model uses the
  closed-form linear SHAP. For candidates, contributions are reported relative to the probability-weighted
  field, so context shared by all candidates cancels.
- `predict.py` has both modes from the spec. Replay uses the validated model with features as-of the game; live
  mode uses a refit on all data and accepts earlier games of the series either from the dataset or inline in the
  state file. Both were run end to end.
- `web/server.py` and `web/static/` are a local web UI on the live path (`python -m draft_analyzer.web`). The
  user clicks each ban and pick as a real draft happens; the page shows the probability of every available
  champion (likely ones large, the long tail small), the top 12 candidates with their main factors and a hover
  card with the full breakdown, a vertical evaluation bar for win probability with the baseline marked, a graph
  of win probability over the draft, and a recap of the model's rating of each action once a draft is complete. Recording a game's winner starts the next game
  of the series with Fearless burns applied. A prediction takes about 0.25 s (three boosters of 1,050 to 1,400 trees
  each, with exact SHAP). Checked in headless Chrome at 1440, 1280 and
  1100 px wide and at phone width (390 px): a full draft with a missed ban and undos, switching first pick, a
  second Fearless game, reload, no console errors. It has not been tried in other browsers or on a real phone. There is no automatic feed of live drafts; entry is by hand.

## Leakage

- Feature code only receives pre-game facts plus the actions made so far; outcomes are only visible to `update`,
  which the engine calls once a game's end time is at or before the start of the game being featurised.
- History-only games go through the same engine under the same end-time rule; `tests/test_history.py` checks
  that an unfinished history game is invisible, that history reaches only the blocks configured for it, and that
  history games are never featurised.
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

1. **The 2 LEC 2026 games with lost draft order** could be patched from Leaguepedia; a further 37 role-ordered
   games are unchecked.
2. **Training-run noise is not in the intervals.** Every interval is a bootstrap over validation series for the
   fitted models at hand. Single fits of one recipe differ by up to 0.015 validation log loss, which is larger
   than the smaller effects reported from single fits (history leagues, the weakest ablation groups). The final
   model, the regime choice, the joint-vs-separate comparison and the accuracy round use three-seed averages,
   which shrinks this noise but does not put it in the intervals.
3. **Cross-region Elo** rests on few international games (documented in the spec as expected). Lower-tier Elo
   was tried as history and left out for the same reason.
4. **The 71 LCP and LTA promotion games from October 2025** are still in scope (training period). They involve
   the leagues' own teams, unlike the LCS 2027 event, so they were left in.
5. **CBLOL 2024 and 2026** are history only. CBLOL teams were in scope in 2025 as LTA South; whether CBLOL
   should be a target league is a scope decision.
6. **Fandom bot credentials** are only needed if you want the Cargo route; nothing in Phases 1 to 3 is blocked on
   them.
7. **Separate pick and ban models** may be worth a further 0.008 log loss (see "Joint vs separate"). Testing it
   properly means training them on every candidate with three seeds (about an hour) and one more look at the
   validation split, which has now been used for several choices in a row.
8. **Validation has been reused.** The accuracy round made four adopt-or-reject choices on the same 651 games.
   The Worlds 2026 final is not in the data and would be a clean test of the final model.
9. **No live draft feed.** The web UI and CLI take the draft by hand.
