"""M2 baselines.

Next action
  A: rank champions by as-of patch presence (pick + ban rate), unavailable champions excluded.
  B: A plus the relevant team's recent pick frequency (acting team for a pick, opponent for a ban).
Win
  1: as-of team Elo only.
  2: Elo + side + first-pick indicator.

The baseline statistics are a stat block of the as-of engine (so they obey the same leakage
rules as every other feature) but use the prefix ``base_`` and are never fed to the M3 models.
To report log loss a ranking needs probabilities: score = tau * log(stat + eps), softmax over
the available candidates, with (tau, eps[, w]) fitted on the training period per action type.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from ..features.asof import ChampionIndex, Decayed, DecayedMap, DraftContext, GameRecord, PreGame, StatBlock, shrink
from ..features.dataset import NextActionData, results_frame, rows_of


class BaselineBlock(StatBlock):
    name = "baseline"
    prefix = "base_"

    def __init__(self, champs: ChampionIndex, prior_half_life: float = 60.0, k_patch: float = 20.0,
                 team_half_life: float = 60.0, k_team: float = 3.0):
        self.C = champs.C
        self.k_patch, self.k_team = k_patch, k_team
        self.patch_games: dict[str, float] = {}
        self.patch_counts: dict[str, np.ndarray] = {}
        self.long_counts = Decayed(self.C, prior_half_life)
        self.long_games = Decayed(1, prior_half_life)
        self.team_picks = DecayedMap(self.C, team_half_life)
        self.team_games = DecayedMap(1, team_half_life)
        self._presence = None
        self._team = {}

    def begin_game(self, pre: PreGame) -> None:
        lg = self.long_games.read(pre.t)[0]
        prior = self.long_counts.read(pre.t) / lg if lg > 0 else np.zeros(self.C)
        n = self.patch_counts.get(pre.patch)
        g = self.patch_games.get(pre.patch, 0.0)
        self._presence = shrink(n if n is not None else np.zeros(self.C), g, prior, self.k_patch)
        self._team = {
            side: self.team_picks.read(pre.team(side), pre.t) / (self.team_games.read(pre.team(side), pre.t)[0] + self.k_team)
            for side in ("Blue", "Red")
        }

    def candidate_features(self, ctx: DraftContext) -> dict[str, np.ndarray]:
        relevant = ctx.acting_side if ctx.is_pick else ctx.opp_side
        return {"base_presence": self._presence, "base_team_freq": self._team[relevant]}

    def update(self, g: GameRecord, exp_blue: float) -> None:
        t, patch = g.pre.t, g.pre.patch
        taken = [c for c in g.actions if c >= 0]
        if patch not in self.patch_counts:
            self.patch_counts[patch] = np.zeros(self.C)
        self.patch_counts[patch][taken] += 1.0
        self.patch_games[patch] = self.patch_games.get(patch, 0.0) + 1.0
        self.long_counts.add(t, taken, 1.0)
        self.long_games.add(t, 0, 1.0)
        for side in ("Blue", "Red"):
            picks = [c for c in g.picks(side) if c >= 0]
            self.team_picks.get(g.pre.team(side)).add(t, picks, 1.0)
            self.team_games.get(g.pre.team(side)).add(t, 0, 1.0)


# --------------------------------------------------------------------------------------
# Next-action baselines
# --------------------------------------------------------------------------------------
def _group_logloss(scores: np.ndarray, label: np.ndarray, starts: np.ndarray, sizes: np.ndarray) -> float:
    mx = np.repeat(np.maximum.reduceat(scores, starts), sizes)
    lse = np.log(np.add.reduceat(np.exp(scores - mx), starts)) + mx[starts]
    return float(np.mean(lse - scores[label]))


@dataclass
class PresenceBaseline:
    """Baseline A (use_team=False) or B (use_team=True)."""

    use_team: bool = False
    params: dict = field(default_factory=dict)  # action_type -> (tau, eps, w)

    TAUS = (0.6, 0.8, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
    EPSS = (3e-4, 1e-3, 3e-3, 1e-2, 3e-2)
    WS = (0.1, 0.25, 0.5, 1.0, 2.0, 4.0)

    def _columns(self, data: NextActionData, rows) -> tuple[np.ndarray, np.ndarray]:
        i_p, i_t = data.cand_names.index("base_presence"), data.cand_names.index("base_team_freq")
        X = np.asarray(data.X_cand[rows][:, [i_p, i_t]], dtype=np.float64)
        return X[:, 0], X[:, 1]

    @staticmethod
    def _score(pres, team, tau, eps, w):
        return tau * np.log(pres + w * team + eps)

    def fit(self, data: NextActionData, action_idx: np.ndarray) -> "PresenceBaseline":
        acts = data.actions.iloc[action_idx]
        for typ in ("ban", "pick"):
            idx = action_idx[(acts["action_type"] == typ).to_numpy()]
            rows = rows_of(data, idx)
            pres, team = self._columns(data, rows)
            label = data.row_label[rows]
            sizes = data.actions["n_cand"].to_numpy()[idx]
            starts = np.concatenate([[0], np.cumsum(sizes)[:-1]])
            best = (np.inf, None)
            for eps, w in product(self.EPSS, self.WS if self.use_team else (0.0,)):
                base = np.log(pres + w * team + eps)
                for tau in self.TAUS:
                    ll = _group_logloss(tau * base, label, starts, sizes)
                    if ll < best[0]:
                        best = (ll, (tau, eps, w))
            self.params[typ] = best[1]
        return self

    def predict_scores(self, data: NextActionData, action_idx: np.ndarray) -> np.ndarray:
        rows = rows_of(data, action_idx)
        pres, team = self._columns(data, rows)
        is_pick = (data.actions["action_type"].to_numpy() == "pick")[data.row_action[rows]]
        out = np.empty(len(pres))
        for typ, m in (("pick", is_pick), ("ban", ~is_pick)):
            tau, eps, w = self.params[typ]
            out[m] = self._score(pres[m], team[m], tau, eps, w)
        return out

    def evaluate(self, data: NextActionData, action_idx: np.ndarray) -> pd.DataFrame:
        return results_frame(data, self.predict_scores(data, action_idx), action_idx)


# --------------------------------------------------------------------------------------
# Win baselines
# --------------------------------------------------------------------------------------
WIN_BASELINE_FEATURES = {
    "elo": (["elo_diff"], False),  # symmetric: no intercept, so no side information
    "elo_side_fp": (["elo_diff", "ctx_blue_first_pick"], True),  # intercept = blue side effect
}


@dataclass
class WinBaseline:
    kind: str = "elo"
    model: LogisticRegression | None = None

    def fit(self, win: pd.DataFrame) -> "WinBaseline":
        feats, intercept = WIN_BASELINE_FEATURES[self.kind]
        self.model = LogisticRegression(C=1e6, fit_intercept=intercept, max_iter=1000)
        self.model.fit(win[feats].to_numpy(), win["y"].to_numpy())
        return self

    def predict(self, win: pd.DataFrame) -> np.ndarray:
        feats, _ = WIN_BASELINE_FEATURES[self.kind]
        return self.model.predict_proba(win[feats].to_numpy())[:, 1]

    def evaluate(self, win: pd.DataFrame) -> pd.DataFrame:
        out = win[["game_id", "series_id", "y"]].reset_index(drop=True).copy()
        out["p"] = self.predict(win)
        return out

    def describe(self) -> dict:
        feats, intercept = WIN_BASELINE_FEATURES[self.kind]
        d = dict(zip(feats, self.model.coef_[0].round(4).tolist()))
        if intercept:
            d["intercept(blue side)"] = round(float(self.model.intercept_[0]), 4)
        return d


def tune_elo(games: pd.DataFrame, champs: ChampionIndex, eval_from: str, eval_to: str) -> pd.DataFrame:
    """Grid-search Elo hyperparameters by the as-of log loss of the raw Elo expectation.

    Only games in [eval_from, eval_to) are scored, and eval_to must not exceed the validation
    start, so validation/test data never influence the choice.
    """
    from ..features.asof import Engine, records_from_games, to_days
    from ..features.elo import EloBlock

    recs = records_from_games(games, champs)
    lo, hi = to_days(eval_from), to_days(eval_to)
    rows = []
    for k, reg, reg_season in product((12, 16, 20, 24, 28, 32, 40), (0.0, 0.1, 0.2, 0.3), (0.0, 0.2, 0.35, 0.5)):
        eng = Engine(champs, [EloBlock(k=k, regress=reg, regress_season=reg_season)])
        ll = []

        def visit(g, e):
            if lo <= g.pre.t < hi:
                p = min(max(e.expected_blue(g.pre), 1e-9), 1 - 1e-9)
                ll.append(-np.log(p if g.blue_win else 1 - p))

        eng.process(recs, visit)
        rows.append({"k": k, "regress": reg, "regress_season": reg_season, "log_loss": float(np.mean(ll)), "n": len(ll)})
    return pd.DataFrame(rows).sort_values("log_loss").reset_index(drop=True)
