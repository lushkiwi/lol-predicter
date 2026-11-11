"""Next pick/ban model: LightGBM scoring (draft state, candidate champion) pairs.

Trained with a listwise softmax cross-entropy over the available candidates of each action
(custom objective), so the outputs are probabilities by construction. Lambdarank is not used:
its scores are not probabilities.

Training uses a random subset of negatives per action for speed. The softmax partition
function is kept unbiased by giving each sampled negative the weight n_negatives / n_sampled
(added to its score as a log-offset). Evaluation always uses the full candidate set.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import lightgbm as lgb
import numpy as np
import pandas as pd

from ..features.dataset import NextActionData, results_frame, rows_of

DEFAULT_PARAMS = dict(
    learning_rate=0.06, num_leaves=47, min_data_in_leaf=150, feature_fraction=0.7,
    lambda_l2=20.0, max_bin=127, verbosity=-1, num_threads=0, seed=0, deterministic=False,
)


def exclude_baseline(names: list[str]) -> list[str]:
    return [n for n in names if not n.startswith("base_")]


def group_softmax(scores: np.ndarray, starts: np.ndarray, sizes: np.ndarray) -> np.ndarray:
    mx = np.repeat(np.maximum.reduceat(scores, starts), sizes)
    e = np.exp(scores - mx)
    return e / np.repeat(np.add.reduceat(e, starts), sizes)


def group_logloss(scores: np.ndarray, label: np.ndarray, starts: np.ndarray, sizes: np.ndarray,
                  weights: np.ndarray | None = None) -> float:
    mx = np.maximum.reduceat(scores, starts)
    lse = np.log(np.add.reduceat(np.exp(scores - np.repeat(mx, sizes)), starts)) + mx
    ll = lse - scores[label]
    return float(np.average(ll, weights=weights))


def sample_training_rows(data: NextActionData, action_idx: np.ndarray, neg_per_action: int | None,
                         seed: int = 0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Pick candidate rows for training.

    Returns (row indices, group sizes, log-offsets). With ``neg_per_action=None`` all rows are
    kept and offsets are zero. Otherwise each action keeps its true row plus up to
    ``neg_per_action`` uniformly sampled negatives, each with offset log(n_neg / n_kept).
    """
    rows = rows_of(data, action_idx)
    if isinstance(rows, slice):
        rows = np.arange(len(data.row_action))
    sizes = data.actions["n_cand"].to_numpy()[action_idx]
    if neg_per_action is None:
        return rows, sizes, np.zeros(len(rows), dtype=np.float64)
    rng = np.random.default_rng(seed)
    label = data.row_label[rows]
    grp = np.repeat(np.arange(len(sizes)), sizes)
    u = rng.random(len(rows))
    u[label] = -1.0  # the true row always sorts first within its group
    order = np.lexsort((u, grp))
    starts = np.concatenate([[0], np.cumsum(sizes)[:-1]])
    rank = np.arange(len(rows)) - np.repeat(starts, sizes)  # rank within group after the sort
    keep_sorted = rank <= neg_per_action  # rank 0 is the true row
    kept = np.sort(order[keep_sorted])
    n_neg = sizes - 1
    n_kept = np.minimum(n_neg, neg_per_action)
    new_sizes = n_kept + 1
    off_per_group = np.log(np.maximum(n_neg, 1) / np.maximum(n_kept, 1))
    offsets = np.repeat(off_per_group, new_sizes)
    offsets[label[kept]] = 0.0
    return rows[kept], new_sizes, offsets


def time_decay_weights(data: NextActionData, action_idx: np.ndarray, half_life_days: float | None) -> np.ndarray:
    """Per-action weights 0.5 ** (age / half_life), age measured from the newest training action."""
    if half_life_days is None:
        return np.ones(len(action_idx))
    t = data.actions["datetime"].to_numpy()[action_idx]
    age = (t.max() - t) / np.timedelta64(1, "D")
    w = 0.5 ** (age / half_life_days)
    return w / w.mean()


@dataclass
class NextActionModel:
    features: list[str] | None = None  # None = every non-baseline feature
    params: dict = field(default_factory=dict)
    neg_per_action: int | None = 60
    half_life_days: float | None = None
    num_rounds: int = 2000
    early_stopping: int = 60
    booster: lgb.Booster | None = None
    best_iteration: int | None = None
    temperature: dict = field(default_factory=lambda: {"pick": 1.0, "ban": 1.0})
    history: list = field(default_factory=list)
    members: list = field(default_factory=list)  # seed average: further (booster, best_iteration) pairs

    # ---- training ----
    def _dataset(self, data, action_idx, seed):
        rows, sizes, offsets = sample_training_rows(data, action_idx, self.neg_per_action, seed)
        X = data.matrix(rows, self.features)
        y = data.row_label[rows].astype(np.float64)
        gw = np.repeat(time_decay_weights(data, action_idx, self.half_life_days), sizes)
        starts = np.concatenate([[0], np.cumsum(sizes)[:-1]])
        return X, y, starts, sizes, offsets, gw

    def fit(self, data: NextActionData, train_idx: np.ndarray, tune_idx: np.ndarray | None = None,
            num_rounds: int | None = None, seed: int = 0, verbose: int = 0) -> "NextActionModel":
        """Fit on ``train_idx`` actions. With ``tune_idx`` use early stopping on its full-candidate
        log loss and fit the temperatures; otherwise train exactly ``num_rounds`` rounds."""
        self.features = data.select_features(self.features if self.features is not None else exclude_baseline(data.feature_names))
        params = {**DEFAULT_PARAMS, **self.params}
        X, y, starts, sizes, offsets, gw = self._dataset(data, train_idx, seed)

        def fobj(preds, dataset):
            p = group_softmax(preds + offsets, starts, sizes)
            return (p - y) * gw, np.maximum(p * (1.0 - p), 1e-6) * gw

        dtrain = lgb.Dataset(X, label=y, feature_name=self.features, params=params, free_raw_data=True)
        dtrain.construct()
        del X  # only the binned copy is needed from here; with every candidate the raw matrix is several GB
        params["objective"] = fobj
        callbacks, valid_sets, feval = [], [], None
        if tune_idx is not None:
            t_rows = rows_of(data, tune_idx)
            Xt = data.matrix(t_rows, self.features)
            t_label = data.row_label[t_rows]
            t_sizes = data.actions["n_cand"].to_numpy()[tune_idx]
            t_starts = np.concatenate([[0], np.cumsum(t_sizes)[:-1]])
            dtune = lgb.Dataset(Xt, label=t_label.astype(np.float64), reference=dtrain, free_raw_data=False)

            def feval(preds, dataset):
                return "softmax_logloss", group_logloss(preds, t_label, t_starts, t_sizes), False

            valid_sets = [dtune]
            callbacks = [lgb.early_stopping(self.early_stopping, verbose=bool(verbose)), lgb.record_evaluation(hist := {})]
            if verbose:
                callbacks.append(lgb.log_evaluation(verbose))
        self.booster = lgb.train(params, dtrain, num_boost_round=num_rounds or self.num_rounds,
                                 valid_sets=valid_sets, valid_names=["tune"] if valid_sets else None,
                                 feval=feval, callbacks=callbacks)
        if tune_idx is not None:
            self.best_iteration = self.booster.best_iteration
            self.history = hist["tune"]["softmax_logloss"]
            self._fit_temperature(data, tune_idx)
        else:
            self.best_iteration = self.booster.current_iteration()
        return self

    def _fit_temperature(self, data: NextActionData, idx: np.ndarray) -> None:
        raw = self.raw_scores(data, idx)
        acts = data.actions.iloc[idx]
        sizes_all = acts["n_cand"].to_numpy()
        is_pick_group = (acts["action_type"] == "pick").to_numpy()
        label = data.row_label[rows_of(data, idx)]
        row_group_pick = np.repeat(is_pick_group, sizes_all)
        for typ, gm, rm in (("pick", is_pick_group, row_group_pick), ("ban", ~is_pick_group, ~row_group_pick)):
            if not gm.any():
                continue
            sizes = sizes_all[gm]
            starts = np.concatenate([[0], np.cumsum(sizes)[:-1]])
            s, l = raw[rm], label[rm]
            grid = np.linspace(0.6, 1.6, 41)
            self.temperature[typ] = float(grid[int(np.argmin([group_logloss(s * a, l, starts, sizes) for a in grid]))])

    # ---- prediction ----
    def _boosters(self) -> list:
        return [(self.booster, self.best_iteration)] + list(getattr(self, "members", ()))

    def raw_matrix_scores(self, X: np.ndarray) -> np.ndarray:
        """Logits; for a seed average, the mean logit of its boosters."""
        return np.mean([b.predict(X, num_iteration=it, raw_score=True) for b, it in self._boosters()], axis=0)

    def contributions(self, X: np.ndarray) -> np.ndarray:
        """(n, F + 1) SHAP contributions to the logit (last column: the base value). SHAP is
        linear in the model, so the mean over the boosters explains the mean logit exactly."""
        return np.mean([b.predict(X, num_iteration=it, pred_contrib=True) for b, it in self._boosters()], axis=0)

    def raw_scores(self, data: NextActionData, action_idx: np.ndarray, chunk: int = 4000) -> np.ndarray:
        out = []
        for i in range(0, len(action_idx), chunk):
            rows = rows_of(data, action_idx[i:i + chunk])
            out.append(self.raw_matrix_scores(data.matrix(rows, self.features)))
        return np.concatenate(out) if out else np.zeros(0)

    def predict_scores(self, data: NextActionData, action_idx: np.ndarray) -> np.ndarray:
        """Temperature-scaled logits for every candidate row of the given actions."""
        raw = self.raw_scores(data, action_idx)
        acts = data.actions.iloc[action_idx]
        scale = np.where(acts["action_type"].to_numpy() == "pick", self.temperature["pick"], self.temperature["ban"])
        return raw * np.repeat(scale, acts["n_cand"].to_numpy())

    def evaluate(self, data: NextActionData, action_idx: np.ndarray) -> pd.DataFrame:
        return results_frame(data, self.predict_scores(data, action_idx), action_idx)

    def importance(self) -> pd.DataFrame:
        gain = self.booster.feature_importance("gain", iteration=self.best_iteration)
        return (pd.DataFrame({"feature": self.features, "gain": gain / gain.sum()})
                .sort_values("gain", ascending=False).reset_index(drop=True))


def average_models(models: list[NextActionModel]) -> NextActionModel:
    """One model that serves the mean logit of several fits of the same recipe (different seeds).
    Temperatures are averaged; the first model's importance() stands for the group."""
    first = models[0]
    assert all(m.features == first.features for m in models), "seed averages need identical features"
    out = NextActionModel(features=list(first.features), params=dict(first.params), neg_per_action=first.neg_per_action,
                          half_life_days=first.half_life_days, booster=first.booster, best_iteration=first.best_iteration,
                          history=list(first.history), members=[(m.booster, m.best_iteration) for m in models[1:]])
    out.temperature = {k: float(np.mean([m.temperature[k] for m in models])) for k in first.temperature}
    for attr in ("stage1_best_iteration", "stage1_tune_logloss"):
        if hasattr(first, attr):
            setattr(out, attr, getattr(first, attr))
    return out


def fit_with_refit(data: NextActionData, fit_idx: np.ndarray, tune_idx: np.ndarray, verbose: int = 0,
                   refit_rounds_scale: float = 1.1, seed: int = 0, **kwargs) -> NextActionModel:
    """Standard recipe: early-stop on the tune period, then refit on fit+tune with the chosen
    number of rounds (scaled up slightly for the larger training set). Temperatures come from
    the first stage, where the tune period was held out. ``seed`` drives both the negative
    sampling and LightGBM."""
    kwargs = {**kwargs, "params": {**kwargs.get("params", {}), "seed": seed}}
    stage1 = NextActionModel(**kwargs).fit(data, fit_idx, tune_idx, seed=seed, verbose=verbose)
    rounds = int(round(stage1.best_iteration * refit_rounds_scale))
    final = NextActionModel(**kwargs)
    final.fit(data, np.sort(np.concatenate([fit_idx, tune_idx])), None, num_rounds=rounds, seed=seed)
    final.temperature = dict(stage1.temperature)
    final.history = stage1.history
    final.stage1_best_iteration = stage1.best_iteration
    final.stage1_tune_logloss = min(stage1.history) if stage1.history else None
    return final
