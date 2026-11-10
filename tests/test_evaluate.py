import numpy as np
import pandas as pd
import pytest

from draft_analyzer.evaluate import (
    bootstrap_ci, compare_table, format_markdown_table, next_action_metrics,
    next_action_report, paired_bootstrap, phase_of, reliability_diagram, win_metrics,
)


def na_frame(ranks, probs, series=None, types=None):
    n = len(ranks)
    return pd.DataFrame({
        "game_id": np.arange(n) // 4,
        "series_id": series if series is not None else np.arange(n) // 8,
        "action_index": np.arange(n) % 4 + 0,
        "action_type": types or ["ban"] * n,
        "true_rank": ranks,
        "true_prob": probs,
    })


def test_phase_mapping():
    assert phase_of(0) == "ban1" and phase_of(5) == "ban1"
    assert phase_of(6) == "pick1" and phase_of(11) == "pick1"
    assert phase_of(12) == "ban2" and phase_of(15) == "ban2"
    assert phase_of(16) == "pick2" and phase_of(19) == "pick2"
    out = phase_of(np.array([0, 6, 12, 16]))
    assert list(out) == ["ban1", "pick1", "ban2", "pick2"]
    assert list(phase_of(pd.Series([5, 6]))) == ["ban1", "pick1"]


def test_next_action_metrics_hand():
    df = na_frame([1, 3, 7, 20], [0.5, 0.25, 0.1, 0.0])
    m = next_action_metrics(df)
    assert m["top1"] == 0.25 and m["top5"] == 0.5 and m["top10"] == 0.75
    expected = (-np.log(0.5) - np.log(0.25) - np.log(0.1) - np.log(1e-12)) / 4
    assert m["log_loss"] == pytest.approx(expected)
    assert m["n_actions"] == 4 and m["n_games"] == 1 and m["n_series"] == 1


def test_next_action_report_slices():
    df = pd.DataFrame({
        "game_id": [1] * 4, "series_id": [1] * 4,
        "action_index": [0, 6, 12, 16],
        "action_type": ["ban", "pick", "ban", "pick"],
        "true_rank": [1, 2, 3, 1], "true_prob": [0.5, 0.4, 0.3, 0.2],
    })
    rep = next_action_report(df)
    assert list(rep["slice"]) == ["all", "ban", "pick", "ban1", "pick1", "ban2", "pick2"]
    row = rep[rep["slice"] == "pick"].iloc[0]
    assert row["top1"] == 0.5 and row["n_actions"] == 2
    assert rep[rep["slice"] == "pick1"].iloc[0]["top1"] == 0.0


def test_win_metrics_hand():
    df = pd.DataFrame({"game_id": [1, 2], "series_id": [1, 1], "y": [1, 0], "p": [0.8, 0.4]})
    m = win_metrics(df)
    assert m["brier"] == pytest.approx((0.04 + 0.16) / 2)
    assert m["log_loss"] == pytest.approx((-np.log(0.8) - np.log(0.6)) / 2)
    assert m["accuracy"] == 1.0 and m["n_games"] == 2 and m["n_series"] == 1


def win_synth(n_series=300, games=3, noise=1.0, seed=0):
    rng = np.random.default_rng(seed)
    n = n_series * games
    y = rng.integers(0, 2, n)
    logit = (2 * y - 1) * 0.5 + rng.normal(0, noise, n)
    return pd.DataFrame({"game_id": np.arange(n), "series_id": np.arange(n) // games,
                         "y": y, "p": 1 / (1 + np.exp(-logit))})


def test_paired_bootstrap_basic():
    a = win_synth(noise=0.5, seed=1)
    b = win_synth(noise=3.0, seed=1)
    r = paired_bootstrap(a, b, "log_loss", n_boot=500)
    assert r["diff"] == pytest.approx(win_metrics(a)["log_loss"] - win_metrics(b)["log_loss"])
    assert r["ci_low"] <= r["diff"] <= r["ci_high"]
    assert r["n_series"] == 300 and r["n_rows"] == 900 and r["n_boot"] == 500


def test_identical_models_not_significant():
    a = win_synth()
    r = paired_bootstrap(a, a.copy(), "brier", n_boot=200)
    assert r["diff"] == 0 and r["ci_low"] == 0 and r["ci_high"] == 0
    assert not r["significant"]


def test_clearly_better_is_significant():
    rng = np.random.default_rng(3)
    n = 300 * 3
    y = rng.integers(0, 2, n)
    good = np.where(y == 1, 0.75, 0.25)
    base = pd.DataFrame({"game_id": np.arange(n), "series_id": np.arange(n) // 3, "y": y, "p": 0.5})
    model = base.assign(p=good)
    r = paired_bootstrap(model, base, "log_loss", n_boot=500)
    assert r["diff"] < 0 and r["significant"] and r["ci_high"] < 0
    tab = compare_table({"base": base, "model": model}, "base",
                        ["log_loss", "accuracy"], kind="win", n_boot=300)
    v = tab.set_index(["model", "metric"])["verdict"]
    assert v[("model", "log_loss")] == "better"
    assert v[("model", "accuracy")] == "better"
    assert v[("base", "log_loss")] == "baseline"
    tab2 = compare_table({"base": model, "worse": base}, "base", ["log_loss"], kind="win", n_boot=300)
    assert tab2.set_index("model").loc["worse", "verdict"] == "worse"


def test_resampling_unit_is_series():
    # Perfectly correlated rows within series: series-level CI must be much wider
    # than a naive row-level CI on the same data.
    rng = np.random.default_rng(0)
    n_series, per = 50, 20
    outcome = rng.integers(0, 2, n_series).repeat(per)
    df = pd.DataFrame({"game_id": np.arange(n_series * per),
                       "series_id": np.arange(n_series * per) // per,
                       "y": outcome, "p": 0.6})
    df_rowlevel = df.assign(series_id=np.arange(len(df)))
    wide = bootstrap_ci(df, "accuracy", n_boot=1000)
    naive = bootstrap_ci(df_rowlevel, "accuracy", n_boot=1000)
    assert (wide["ci_high"] - wide["ci_low"]) > 2 * (naive["ci_high"] - naive["ci_low"])
    # single series -> degenerate CI
    one = df[df["series_id"] == 0]
    r = bootstrap_ci(one, "accuracy", n_boot=100)
    assert r["ci_low"] == r["ci_high"] == r["value"]


def test_next_action_paired_and_mismatch():
    a = na_frame([1, 2, 3, 4, 5, 6, 7, 8], [0.5] * 8, series=[0, 0, 0, 0, 1, 1, 1, 1])
    b = na_frame([2, 2, 3, 4, 5, 6, 7, 8], [0.4] * 8, series=[0, 0, 0, 0, 1, 1, 1, 1])
    r = paired_bootstrap(a, b, "top1", n_boot=100)
    assert r["diff"] == pytest.approx(1 / 8)
    with pytest.raises(ValueError, match="row sets differ"):
        paired_bootstrap(a, b.iloc[:-1], "top1")
    c = b.copy()
    c.loc[0, "game_id"] = 99
    with pytest.raises(ValueError, match="row sets differ"):
        paired_bootstrap(a, c, "top1")


def test_reliability_diagram_and_markdown(tmp_path):
    df = win_synth(100)
    path = tmp_path / "rel.png"
    tab = reliability_diagram({"m": df}, path, n_bins=5, title="t")
    assert path.exists() and path.stat().st_size > 0
    assert tab["n"].sum() == len(df)
    md = format_markdown_table(tab)
    assert md.splitlines()[0].startswith("| model") and "---" in md.splitlines()[1]
