"""Evaluation utilities: metrics, series-level paired bootstrap, reliability plots.

Inputs are pandas DataFrames in one of two formats:

* next-action results: ``game_id, series_id, action_index, action_type,
  true_rank, true_prob`` (one row per draft action)
* win results: ``game_id, series_id, y, p`` (one row per game / checkpoint)

All bootstrapped metrics are means of per-row scores, and resampling is done by
series (best-of-N match), since games within a series are not independent.
Callers wanting a slice (e.g. only picks) should pre-filter both frames.
"""
from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

EPS = 1e-12
PHASES = ["ban1", "pick1", "ban2", "pick2"]
NEXT_ACTION_METRICS = ("top1", "top5", "top10", "log_loss")
WIN_METRICS = ("log_loss", "brier", "accuracy")
HIGHER_IS_BETTER = {"top1", "top5", "top10", "accuracy"}
DEFAULT_KEYS = {"next_action": ["game_id", "action_index"], "win": ["game_id"]}


# --------------------------------------------------------------------- phases
def phase_of(action_index):
    """Map action_index (0-19) to 'ban1'/'pick1'/'ban2'/'pick2'.

    Accepts a scalar (returns str), or an array/Series (returns same-shaped
    object array / Series).
    """
    if isinstance(action_index, pd.Series):
        return pd.Series(_phase_array(action_index.to_numpy()), index=action_index.index)
    out = _phase_array(np.asarray(action_index))
    return str(out) if out.ndim == 0 else out


def _phase_array(a: np.ndarray) -> np.ndarray:
    return np.select(
        [a < 6, a < 12, a < 16, a < 20], PHASES, default="unknown"
    ).astype(object)


def _with_phase(res: pd.DataFrame) -> pd.DataFrame:
    if "phase" in res.columns:
        return res
    return res.assign(phase=phase_of(res["action_index"]))


# ---------------------------------------------------------- per-row scores
def row_scores(res: pd.DataFrame, metric: str, kind: str | None = None) -> np.ndarray:
    """Per-row score whose mean is the metric. ``kind`` is inferred if omitted."""
    if kind is None:
        kind = "next_action" if "true_rank" in res.columns else "win"
    if kind == "next_action":
        if metric in ("top1", "top5", "top10"):
            k = int(metric[3:])
            return (res["true_rank"].to_numpy() <= k).astype(float)
        if metric == "log_loss":
            return -np.log(np.clip(res["true_prob"].to_numpy(float), EPS, None))
    elif kind == "win":
        y = res["y"].to_numpy(float)
        p = np.clip(res["p"].to_numpy(float), EPS, 1 - EPS)
        if metric == "log_loss":
            return -(y * np.log(p) + (1 - y) * np.log(1 - p))
        if metric == "brier":
            return (p - y) ** 2
        if metric == "accuracy":
            return ((p >= 0.5).astype(float) == y).astype(float)
    else:
        raise ValueError(f"unknown kind {kind!r}")
    raise ValueError(f"unknown metric {metric!r} for kind {kind!r}")


# ------------------------------------------------------------ point metrics
def next_action_metrics(res: pd.DataFrame) -> dict:
    """Top-1/5/10 accuracy, mean log loss and sample sizes for next-action results."""
    return {
        "top1": float(row_scores(res, "top1").mean()),
        "top5": float(row_scores(res, "top5").mean()),
        "top10": float(row_scores(res, "top10").mean()),
        "log_loss": float(row_scores(res, "log_loss").mean()),
        "n_actions": int(len(res)),
        "n_games": int(res["game_id"].nunique()),
        "n_series": int(res["series_id"].nunique()),
    }


def next_action_report(res: pd.DataFrame) -> pd.DataFrame:
    """Metrics for overall, per action_type and per phase slices (one row each)."""
    res = _with_phase(res)
    slices = [("overall", "all", res)]
    for t in ("ban", "pick"):
        slices.append(("action_type", t, res[res["action_type"] == t]))
    for ph in PHASES:
        slices.append(("phase", ph, res[res["phase"] == ph]))
    rows = []
    for group, name, sub in slices:
        if len(sub) == 0:
            continue
        rows.append({"group": group, "slice": name, **next_action_metrics(sub)})
    return pd.DataFrame(rows)


def win_metrics(res: pd.DataFrame) -> dict:
    """Log loss, Brier score, accuracy and sample sizes for win predictions."""
    return {
        "log_loss": float(row_scores(res, "log_loss", "win").mean()),
        "brier": float(row_scores(res, "brier", "win").mean()),
        "accuracy": float(row_scores(res, "accuracy", "win").mean()),
        "n_games": int(res["game_id"].nunique()),
        "n_series": int(res["series_id"].nunique()),
    }


# ---------------------------------------------------------------- bootstrap
def _infer_kind(res: pd.DataFrame) -> str:
    return "next_action" if "true_rank" in res.columns else "win"


def _resample(
    sums: np.ndarray, counts: np.ndarray, n_boot: int, seed: int, level: float
) -> tuple[float, float]:
    """Series-level bootstrap of sum(sums)/sum(counts); returns the CI."""
    rng = np.random.default_rng(seed)
    n = len(sums)
    w = rng.multinomial(n, np.full(n, 1.0 / n), size=n_boot).astype(float)
    stats = (w @ sums) / (w @ counts)
    alpha = (1 - level) / 2
    lo, hi = np.quantile(stats, [alpha, 1 - alpha])
    return float(lo), float(hi)


def paired_bootstrap(
    res_a: pd.DataFrame,
    res_b: pd.DataFrame,
    metric: str,
    keys: list[str] | None = None,
    n_boot: int = 2000,
    seed: int = 0,
    level: float = 0.95,
) -> dict:
    """Paired series-level bootstrap CI for metric(a) - metric(b).

    Both frames must contain exactly the same rows on ``keys`` (defaults:
    ``game_id, action_index`` for next-action, ``game_id`` for win). To evaluate
    a slice, pre-filter both frames identically before calling.
    """
    kind = _infer_kind(res_a)
    keys = list(keys) if keys is not None else DEFAULT_KEYS[kind]
    a = res_a[keys + ["series_id"]].assign(sa=row_scores(res_a, metric, kind))
    b = res_b[keys].assign(sb=row_scores(res_b, metric, kind))
    for name, df in (("a", a), ("b", b)):
        if df.duplicated(keys).any():
            raise ValueError(f"duplicate keys {keys} in results {name}")
    if len(a) != len(b):
        raise ValueError(
            f"row sets differ: a has {len(a)} rows, b has {len(b)} rows on keys {keys}"
        )
    m = a.merge(b, on=keys, how="inner")
    if len(m) != len(a):
        raise ValueError(f"row sets differ: only {len(m)} of {len(a)} rows match on keys {keys}")
    m["d"] = m["sa"] - m["sb"]
    g = m.groupby("series_id")["d"].agg(["sum", "count"])
    sums, counts = g["sum"].to_numpy(float), g["count"].to_numpy(float)
    lo, hi = _resample(sums, counts, n_boot, seed, level)
    return {
        "metric": metric,
        "a": float(m["sa"].mean()),
        "b": float(m["sb"].mean()),
        "diff": float(m["d"].mean()),
        "ci_low": lo,
        "ci_high": hi,
        "significant": bool(lo > 0 or hi < 0),
        "n_rows": int(len(m)),
        "n_series": int(len(g)),
        "n_boot": int(n_boot),
    }


def bootstrap_ci(
    res: pd.DataFrame, metric: str, n_boot: int = 2000, seed: int = 0, level: float = 0.95
) -> dict:
    """Series-level bootstrap CI for a single model's metric."""
    s = pd.Series(row_scores(res, metric, _infer_kind(res)))
    g = s.groupby(res["series_id"].to_numpy()).agg(["sum", "count"])
    sums, counts = g["sum"].to_numpy(float), g["count"].to_numpy(float)
    lo, hi = _resample(sums, counts, n_boot, seed, level)
    return {
        "metric": metric,
        "value": float(s.mean()),
        "ci_low": lo,
        "ci_high": hi,
        "n_rows": int(len(s)),
        "n_series": int(len(g)),
        "n_boot": int(n_boot),
    }


def compare_table(
    results: dict[str, pd.DataFrame],
    baseline: str,
    metrics: list[str],
    kind: str = "next_action",
    n_boot: int = 2000,
    seed: int = 0,
) -> pd.DataFrame:
    """Per model x metric: value, diff vs baseline, CI, verdict.

    Verdict is 'better'/'worse' when the CI excludes zero (taking metric
    direction into account), else 'tie'; the baseline row's verdict is 'baseline'.
    """
    if kind not in DEFAULT_KEYS:
        raise ValueError(f"unknown kind {kind!r}")
    base = results[baseline]
    rows = []
    for name, res in results.items():
        for metric in metrics:
            if name == baseline:
                v = float(row_scores(res, metric, kind).mean())
                rows.append(dict(model=name, metric=metric, value=v, diff=0.0,
                                 ci_low=0.0, ci_high=0.0, verdict="baseline"))
                continue
            r = paired_bootstrap(res, base, metric, n_boot=n_boot, seed=seed)
            if not r["significant"]:
                verdict = "tie"
            else:
                good = (r["diff"] > 0) == (metric in HIGHER_IS_BETTER)
                verdict = "better" if good else "worse"
            rows.append(dict(model=name, metric=metric, value=r["a"], diff=r["diff"],
                             ci_low=r["ci_low"], ci_high=r["ci_high"], verdict=verdict))
    return pd.DataFrame(rows)


# -------------------------------------------------------------- calibration
def reliability_diagram(
    res_by_model: dict[str, pd.DataFrame],
    path,
    n_bins: int = 10,
    title: str | None = None,
) -> pd.DataFrame:
    """Save a reliability (calibration) plot to ``path`` and return the binned table.

    Table columns: model, bin_low, bin_high, n, mean_pred, obs_freq (empty bins omitted).
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    edges = np.linspace(0.0, 1.0, n_bins + 1)
    rows = []
    for name, res in res_by_model.items():
        p = np.clip(res["p"].to_numpy(float), 0.0, 1.0)
        y = res["y"].to_numpy(float)
        idx = np.clip(np.digitize(p, edges[1:-1]), 0, n_bins - 1)
        for b in range(n_bins):
            sel = idx == b
            if sel.any():
                rows.append(dict(model=name, bin_low=edges[b], bin_high=edges[b + 1],
                                 n=int(sel.sum()), mean_pred=float(p[sel].mean()),
                                 obs_freq=float(y[sel].mean())))
    table = pd.DataFrame(rows, columns=["model", "bin_low", "bin_high", "n", "mean_pred", "obs_freq"])

    fig, (ax, axh) = plt.subplots(
        2, 1, figsize=(5.5, 6.5), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    ax.plot([0, 1], [0, 1], "k--", lw=1, label="perfect")
    models = list(res_by_model)
    width = 1.0 / n_bins / max(len(models), 1)
    for i, name in enumerate(models):
        t = table[table["model"] == name]
        ax.plot(t["mean_pred"], t["obs_freq"], "o-", label=name)
        centers = t["bin_low"] + width * (i + 0.5)
        axh.bar(centers, t["n"], width=width, align="center", label=name)
    ax.set_ylabel("Observed frequency (blue win)")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.legend(loc="upper left", fontsize=8)
    if title:
        ax.set_title(title)
    axh.set_xlabel("Predicted P(blue wins)")
    axh.set_ylabel("Count")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return table


# ------------------------------------------------------------------ markdown
def format_markdown_table(df: pd.DataFrame, floatfmt: str = ".4f") -> str:
    """Render a DataFrame as a GitHub-flavoured markdown table (no tabulate needed)."""
    def fmt(v) -> str:
        if isinstance(v, (bool, np.bool_)):
            return str(v)
        if isinstance(v, (float, np.floating)):
            return "" if np.isnan(v) else format(v, floatfmt)
        return str(v)

    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for row in df.itertuples(index=False):
        lines.append("| " + " | ".join(fmt(v) for v in row) + " |")
    return "\n".join(lines)
