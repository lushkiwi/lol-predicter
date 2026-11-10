# Feature block contract (read before writing a stat block)

All model features come from "stat blocks" plugged into the as-of engine in
`draft_analyzer/features/asof.py`. Read that file fully, then `features/roles.py`,
`features/elo.py` and `features/context.py` as reference implementations.

## Non-negotiable rules (leakage)
- A block changes state ONLY inside `update(g: GameRecord, exp_blue: float)`. That is the only
  method that sees outcomes (the draft actions, final roles, winner). The engine calls it once
  the game has finished and never before a later game needs it.
- Feature methods (`begin_game`, `candidate_features`, `context_features`, `win_features`) are
  pure reads of state as of `pre.t` / `ctx.pre.t`. They receive a `PreGame` / `DraftContext`
  only: facts known before the draft plus the actions made so far in THIS game. They must not
  mutate statistics. `begin_game(pre)` may cache per-game arrays (called once per game, after
  the engine has applied all finished games and before any feature call for that game).
- Never use wall-clock time. Time is `pre.t` (days, float).
- `exp_blue` is the Elo expectation of a blue win as of the game's start. Use it for
  "win relative to expectation" residuals: for a team on side s, residual = won(s) - exp(s),
  with exp(Blue) = exp_blue and exp(Red) = 1 - exp_blue. Never use raw win rate (it is
  confounded by team strength).

## Interface
```python
class MyBlock(StatBlock):
    name = "champion"      # unique; used by engine.block(name)
    prefix = "meta_"       # every feature name starts with this
    def __init__(self, champs: ChampionIndex, **hyperparams): ...
    def begin_game(self, pre): ...
    def candidate_features(self, ctx) -> dict[str, np.ndarray]:   # each array shape (C,), one value per champion id
    def context_features(self, ctx) -> dict[str, float]:           # same value for every candidate
    def win_features(self, ctx) -> dict[str, float]:               # oriented BLUE minus RED
    def update(self, g, exp_blue): ...
```
- The constructor must work as `Block(champs)` with defaults for every hyperparameter
  (`features/__init__.py::default_blocks` instantiates it that way and already registers the
  class names `ChampionMetaBlock`, `TeamTendencyBlock`, `PlayerMasteryBlock`, `PairwiseBlock`
  from modules `champion`, `team`, `player`, `pairwise`). Do not edit `features/__init__.py`.
- Other blocks are reachable via `self.engine.block("roles")` etc. The `roles` block offers
  `role_probs()` -> (C,5) P(role|champion), `open_roles(picks)` -> (5,) P(role still open) and
  `candidate_role_weights(picks)` -> (C,5) P(candidate would play role r | open roles). Its
  `begin_game` runs before yours (blocks are ordered), so these are valid inside your
  `begin_game` and feature methods.
- Champions are integer ids `0..C-1`; `-1` means a missed ban (skip it). Roles are indexed
  top=0, jng=1, mid=2, bot=3, sup=4. `pre.blue_players` / `pre.red_players` are player ids in
  that role order (starting rosters are known before the draft and are valid features).
- Key accessors: `ctx.is_pick`, `ctx.k` (action index 0-19; 20 = draft complete), `ctx.phase`,
  `ctx.slot` ('first'/'second'), `ctx.acting_side`, `ctx.opp_side`, `ctx.picks(side)`,
  `ctx.bans(side)` (lists of champion ids so far), `ctx.pre.team(side)`,
  `ctx.pre.players(side)`, `ctx.pre.burned(side)`; `g.picks(side)`, `g.bans(side)`,
  `g.roles(side)` (champion id per role), `g.won(side)`, `g.actions` (20 ids in draft order),
  `draft.ACTION_TYPE / ACTION_SLOT / ACTION_PHASE` (from `draft_analyzer/draft.py`).
- Draft order is by draft SLOT, never by side: from 2026 the red side can have first pick.
  Use `ctx.acting_side` / `pre.side_of_slot(slot)`; never assume blue drafts first.
- `candidate_features` must be valid for both pick and ban actions and must NOT branch the
  set of returned keys on action type (the feature matrix has fixed columns). Features are
  "acting team" / "opponent" oriented: `*_own` = the team making the next action, `*_opp` =
  the other team. For a ban, the champion would otherwise go to the opponent, so the
  opponent-oriented features are the informative ones; the model learns that via the
  `ctx_is_pick` context feature.
- `candidate_features` at `ctx.k == 20` is never called. `win_features` IS called at any k in
  {0..20} (partial drafts), using only the picks locked so far; at k == 0 with no picks, pick-
  based features must be exactly 0.0.

## Numerics / performance
- Use the helpers in `asof.py`: `Decayed` (time-decayed array; `add(t, idx, val)`, `read(t)`),
  `DecayedMap` (per-entity `Decayed`, e.g. per team/player), `shrink(num, den, prior, k)`.
- Everything is vectorised over champions with numpy; no Python loops over all champions in
  feature methods. `candidate_features` is called ~140k times (20 actions x ~7k games): keep
  it to a handful of vector ops and cache whatever only depends on the game in `begin_game`.
  Budget: the whole block should add well under ~60 seconds to a full pass over 7,172 games.
- Small samples: shrink every rate toward a sensible prior; return 0.0 (not NaN) when there is
  no evidence. No NaN/inf in outputs, ever.
- Keep hyperparameters (half-lives in days, shrinkage strengths) as constructor arguments with
  defaults.

## Tests (each block ships `tests/test_features_<module>.py`)
Use tiny synthetic games built directly as `PreGame` / `GameRecord` objects with a small
`ChampionIndex` (e.g. 30 fake champions) and an `Engine` containing `EloBlock`, `RoleBlock`
and your block. Required:
1. Hand-computed value checks for the main features on 3-6 games.
2. Leakage: features computed for game G are identical whether or not G's own draft/outcome
   and all later games are changed (run two engines over two versions of the data).
3. Series visibility: game 2 of a series sees game 1 (features change when game 1 changes),
   including when both are on the same day an hour apart.
4. A red-side-first-pick game produces the right acting/opponent orientation.
5. No NaN/inf, correct shapes, and feature names all start with the block's prefix.
Also run a smoke pass over the real data (script below) and report runtime and a sanity table
(e.g. top champions by your main features for a recent game) in your reply:
```python
import pandas as pd, time
from draft_analyzer.features import build_engine
from draft_analyzer.features.asof import records_from_games, DraftContext
games = pd.read_parquet("data/processed/games.parquet")
eng = build_engine(); recs = records_from_games(games, eng.champs)
def visit(g, e):
    for k in range(20):
        names, X = e.candidate_matrix(DraftContext(g.pre, g.actions[:k]))
    e.win_vector(DraftContext(g.pre, g.actions))
t = time.time(); eng.process(recs, visit); print(time.time() - t)
```
Use `.venv/bin/python` and `.venv/bin/python -m pytest tests/test_features_<module>.py`. Do not
touch git. Edit only your own module and test file.
