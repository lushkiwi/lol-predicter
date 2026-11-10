"""Win probability model.

Logistic regression first (Elo difference, side, first pick, plus draft features), trained on
partial drafts too so a probability can be produced at any point of the draft. Rows are
(game, checkpoint k) with k in {0, 6, 12, 16, 20}; draft features are sums over the picks
locked so far, so they are exactly zero before the first pick and the model then reduces to
(roughly) win baseline 2.

P(blue wins) is the target. The "draft effect" is the full model's probability minus win
baseline 2's (Elo + side + first pick).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

META_COLS = {"game_id", "series_id", "k", "y", "datetime", "league", "split"}
BASE_FEATURES = ["elo_diff", "ctx_blue_first_pick"]
UNPENALISED_SCALE = 30.0


def feature_columns(win: pd.DataFrame) -> list[str]:
    return [c for c in win.columns if c not in META_COLS]


def draft_feature_columns(win: pd.DataFrame) -> list[str]:
    """Features that depend on the picks made (zero at k=0)."""
    k0 = win[win["k"] == 0]
    return [c for c in feature_columns(win) if (k0[c] == 0).all() and (win[c] != 0).any()]


@dataclass
class WinModel:
    features: list[str]
    C: float = 0.05
    scaler: StandardScaler | None = None
    model: LogisticRegression | None = None

    def _z(self, X: np.ndarray) -> np.ndarray:
        """Standardise; base features (Elo, first pick) are scaled up so the L2 penalty, which is
        there to shrink the weak draft features, leaves them effectively unpenalised."""
        return self.scaler.transform(X) * self._scale

    def fit(self, win: pd.DataFrame) -> "WinModel":
        X = win[self.features].to_numpy(dtype=float)
        self.scaler = StandardScaler().fit(X)
        self._scale = np.array([UNPENALISED_SCALE if f in BASE_FEATURES else 1.0 for f in self.features])
        self.model = LogisticRegression(C=self.C, max_iter=5000).fit(self._z(X), win["y"].to_numpy())
        return self

    def predict(self, win: pd.DataFrame) -> np.ndarray:
        return self.model.predict_proba(self._z(win[self.features].to_numpy(dtype=float)))[:, 1]

    def evaluate(self, win: pd.DataFrame) -> pd.DataFrame:
        out = win[["game_id", "series_id", "k", "y"]].reset_index(drop=True).copy()
        out["p"] = self.predict(win)
        return out

    def coefficients(self) -> pd.DataFrame:
        """Standardised coefficients (logit change per 1 SD of the feature)."""
        return (pd.DataFrame({"feature": self.features, "coef_per_sd": self.model.coef_[0] * self._scale})
                .assign(abs=lambda d: d.coef_per_sd.abs()).sort_values("abs", ascending=False)
                .drop(columns="abs").reset_index(drop=True))

    def contributions(self, x: dict) -> dict[str, float]:
        """Exact additive logit contributions of each feature for one row.

        For a linear model the SHAP value of feature j (interventional, independent features)
        is coef_j * (x_j - mean_j); the base value is the logit at the training mean.
        """
        v = np.array([[float(x[f]) for f in self.features]])
        z = self._z(v)[0]
        return dict(zip(self.features, (self.model.coef_[0] * z).tolist()))

    @property
    def base_logit(self) -> float:
        return float(self.model.intercept_[0])


def choose_C(win_fit: pd.DataFrame, win_tune: pd.DataFrame, features: list[str],
             grid=(1e-5, 3e-5, 1e-4, 3e-4, 0.001, 0.003, 0.01, 0.03, 0.1, 1.0)) -> tuple[float, pd.DataFrame]:
    """Pick the L2 strength by log loss on the tune period (all checkpoints)."""
    rows = []
    y = win_tune["y"].to_numpy()
    for C in grid:
        p = np.clip(WinModel(features, C=C).fit(win_fit).predict(win_tune), 1e-9, 1 - 1e-9)
        rows.append({"C": C, "log_loss": float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))})
    table = pd.DataFrame(rows)
    return float(table.loc[table.log_loss.idxmin(), "C"]), table


@dataclass
class WinGBM:
    """LightGBM alternative, kept only if it beats logistic regression under the bootstrap rule."""

    features: list[str]
    params: dict = field(default_factory=dict)
    booster: object = None

    def fit(self, win_fit: pd.DataFrame, win_tune: pd.DataFrame | None = None, num_rounds: int | None = None) -> "WinGBM":
        import lightgbm as lgb

        params = dict(objective="binary", learning_rate=0.02, num_leaves=4, min_data_in_leaf=200, feature_fraction=0.7,
                      lambda_l2=50.0, verbosity=-1, seed=0)
        params.update(self.params)
        dtrain = lgb.Dataset(win_fit[self.features].to_numpy(dtype=float), label=win_fit["y"].to_numpy())
        if win_tune is not None:
            dtune = lgb.Dataset(win_tune[self.features].to_numpy(dtype=float), label=win_tune["y"].to_numpy(), reference=dtrain)
            self.booster = lgb.train(params, dtrain, 3000, valid_sets=[dtune], callbacks=[lgb.early_stopping(100, verbose=False)])
        else:
            self.booster = lgb.train(params, dtrain, num_rounds)
        return self

    def predict(self, win: pd.DataFrame) -> np.ndarray:
        return self.booster.predict(win[self.features].to_numpy(dtype=float))

    def evaluate(self, win: pd.DataFrame) -> pd.DataFrame:
        out = win[["game_id", "series_id", "k", "y"]].reset_index(drop=True).copy()
        out["p"] = self.predict(win)
        return out
