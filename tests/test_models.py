"""Model plumbing: softmax objective, negative sampling, result frames, win-model attributions."""
import numpy as np
import pandas as pd
import pytest

from draft_analyzer.features.dataset import NextActionData, results_frame, rows_of
from draft_analyzer.models.next_action import (NextActionModel, group_logloss, group_softmax, sample_training_rows)
from draft_analyzer.models.win import WinModel


def toy_data(n_actions=400, n_cand=12, seed=0, signal=2.5) -> NextActionData:
    """Each action has n_cand candidates; the true one is drawn from softmax(signal * x0)."""
    rng = np.random.default_rng(seed)
    N = n_actions * n_cand
    x0 = rng.normal(size=N).astype(np.float32)
    noise = rng.normal(size=N).astype(np.float32)
    label = np.zeros(N, dtype=bool)
    for a in range(n_actions):
        s = signal * x0[a * n_cand:(a + 1) * n_cand]
        p = np.exp(s - s.max())
        label[a * n_cand + rng.choice(n_cand, p=p / p.sum())] = True
    actions = pd.DataFrame({
        "game_id": [f"g{a // 20}" for a in range(n_actions)], "series_id": [f"s{a // 60}" for a in range(n_actions)],
        "action_index": [a % 20 for a in range(n_actions)],
        "action_type": ["pick" if (a % 20) in (6, 7, 8, 9, 10, 11, 16, 17, 18, 19) else "ban" for a in range(n_actions)],
        "phase": "x", "true_champ": 0, "start": np.arange(n_actions) * n_cand, "n_cand": n_cand,
        "datetime": pd.Timestamp("2026-01-01") + pd.to_timedelta(np.arange(n_actions), unit="h"),
    })
    return NextActionData(cand_names=["meta_x0", "meta_noise"], ctx_names=["ctx_const"],
                          X_cand=np.stack([x0, noise], axis=1), X_ctx=np.ones((n_actions, 1), np.float32),
                          row_action=np.repeat(np.arange(n_actions), n_cand).astype(np.int32),
                          row_champ=np.tile(np.arange(n_cand), n_actions).astype(np.int16), row_label=label, actions=actions)


def test_group_softmax_and_logloss():
    scores = np.array([0.0, 1.0, 2.0, 5.0, 5.0])
    starts, sizes = np.array([0, 3]), np.array([3, 2])
    p = group_softmax(scores, starts, sizes)
    np.testing.assert_allclose([p[:3].sum(), p[3:].sum()], [1, 1])
    np.testing.assert_allclose(p[3:], [0.5, 0.5])
    label = np.array([False, False, True, True, False])
    expected = np.mean([-np.log(np.exp(2) / (1 + np.e + np.exp(2))), -np.log(0.5)])
    assert group_logloss(scores, label, starts, sizes) == pytest.approx(expected)


def test_softmax_gradient_matches_finite_differences():
    rng = np.random.default_rng(1)
    sizes = np.array([4, 6, 3])
    starts = np.concatenate([[0], np.cumsum(sizes)[:-1]])
    s = rng.normal(size=sizes.sum())
    label = np.zeros(sizes.sum(), bool)
    label[[1, 4 + 5, 10 + 0]] = True
    offsets = rng.normal(scale=0.3, size=sizes.sum())
    offsets[label] = 0.0

    def loss(v):  # summed over groups
        return group_logloss(v + offsets, label, starts, sizes) * len(sizes)

    grad = group_softmax(s + offsets, starts, sizes) - label
    num = np.array([(loss(s + 1e-6 * np.eye(len(s))[i]) - loss(s - 1e-6 * np.eye(len(s))[i])) / 2e-6 for i in range(len(s))])
    np.testing.assert_allclose(grad, num, atol=1e-6)


def test_negative_sampling_keeps_label_and_is_unbiased():
    data = toy_data(n_actions=50, n_cand=30)
    idx = np.arange(50)
    rows, sizes, off = sample_training_rows(data, idx, neg_per_action=5, seed=0)
    assert (sizes == 6).all() and len(rows) == 300
    lab = data.row_label[rows]
    assert lab.reshape(50, 6).sum(axis=1).tolist() == [1] * 50  # exactly one true row per action
    np.testing.assert_allclose(off[~lab], np.log(29 / 5))
    assert (off[lab] == 0).all()
    assert (np.diff(rows) > 0).all() and (data.row_action[rows].reshape(50, 6) == np.arange(50)[:, None]).all()
    # all rows when sampling is off
    rows_all, sizes_all, off_all = sample_training_rows(data, idx, None)
    assert len(rows_all) == 1500 and (off_all == 0).all()
    # the weighted partition function is an unbiased estimate of the full one
    scores = np.random.default_rng(3).normal(size=len(data.row_label))
    full = np.add.reduceat(np.exp(scores), np.arange(0, 1500, 30))
    est = np.zeros(50)
    n_rep = 300
    for seed in range(n_rep):
        r, sz, o = sample_training_rows(data, idx, neg_per_action=5, seed=seed)
        est += np.add.reduceat(np.exp(scores[r] + o), np.arange(0, 300, 6))
    np.testing.assert_allclose(est.mean() / n_rep, full.mean(), rtol=0.03)


def test_results_frame_ranks_and_probabilities():
    data = toy_data(n_actions=3, n_cand=4)
    data.row_label[:] = False
    data.row_label[[0, 5, 11]] = True
    scores = np.array([3.0, 1, 0, 0,   0, 1, 2, 3,   1, 1, 1, 1.0])
    res = results_frame(data, scores, None)
    assert res.true_rank.tolist() == [1, 3, 4]  # ties are resolved pessimistically
    assert res.true_prob.iloc[2] == pytest.approx(0.25)
    assert res.true_prob.iloc[0] == pytest.approx(np.exp(3) / (np.exp(3) + np.e + 2))
    sub = results_frame(data, scores[4:8], np.array([1]))
    assert sub.true_rank.tolist() == [3] and len(rows_of(data, np.array([1]))) == 4


def test_model_learns_and_outputs_probabilities():
    data = toy_data(n_actions=600, n_cand=12, signal=2.5)
    fit, tune, val = np.arange(0, 400), np.arange(400, 500), np.arange(500, 600)
    m = NextActionModel(neg_per_action=6, params={"min_data_in_leaf": 20, "num_leaves": 7, "learning_rate": 0.1},
                        num_rounds=200, early_stopping=20).fit(data, fit, tune)
    res = m.evaluate(data, val)
    assert res.true_rank.eq(1).mean() > 0.45  # chance is 1/12
    assert -np.log(res.true_prob).mean() < np.log(12) - 0.5
    assert m.features == ["meta_x0", "meta_noise", "ctx_const"]
    imp = m.importance().set_index("feature").gain
    assert imp["meta_x0"] > 0.9
    # sampled-softmax training must stay calibrated on the FULL candidate set: mean predicted
    # probability of the top choice should match how often the top choice is right
    scores = m.predict_scores(data, val)
    p = group_softmax(scores, np.arange(0, len(scores), 12), np.full(100, 12)).reshape(100, 12)
    assert abs(p.max(axis=1).mean() - res.true_rank.eq(1).mean()) < 0.12
    np.testing.assert_allclose(p.sum(axis=1), 1.0)


def test_seed_average_serves_the_mean_logit_and_explains_it_exactly():
    from draft_analyzer.models.next_action import average_models

    data = toy_data(n_actions=300, n_cand=12)
    idx = np.arange(0, 250)
    kw = dict(neg_per_action=6, params={"min_data_in_leaf": 20, "num_leaves": 7, "learning_rate": 0.1})
    models = [NextActionModel(**{**kw, "params": {**kw["params"], "seed": s}}).fit(data, idx, None, num_rounds=30, seed=s)
              for s in (0, 1, 2)]
    for m, t in zip(models, (0.8, 0.9, 1.0)):
        m.temperature = {"pick": t, "ban": t}
    avg = average_models(models)
    X = data.matrix(rows_of(data, np.arange(250, 300)), avg.features)
    singles = np.stack([m.raw_matrix_scores(X) for m in models])
    assert singles.std(axis=0).max() > 1e-6  # the seeds really differ
    np.testing.assert_allclose(avg.raw_matrix_scores(X), singles.mean(axis=0), rtol=1e-9)
    np.testing.assert_allclose(avg.contributions(X).sum(axis=1), avg.raw_matrix_scores(X), rtol=1e-6, atol=1e-8)
    assert avg.temperature == {"pick": pytest.approx(0.9), "ban": pytest.approx(0.9)}
    assert models[0].members == [] and len(avg.members) == 2


def test_explanation_compares_each_candidate_with_the_likely_field():
    from draft_analyzer.explain import explain_next_action

    data = toy_data(n_actions=300, n_cand=12)
    m = NextActionModel(neg_per_action=6, params={"min_data_in_leaf": 20, "num_leaves": 7, "learning_rate": 0.1})
    m.fit(data, np.arange(0, 250), None, num_rounds=30)
    rows = rows_of(data, np.array([260]))
    X, ids = data.matrix(rows, m.features), data.row_champ[rows]
    names = [f"c{i}" for i in range(int(data.row_champ.max()) + 1)]
    scores = m.raw_matrix_scores(X) * m.temperature["pick"]
    p = np.exp(scores - scores.max()); p /= p.sum()
    by_prob = np.argsort(-p, kind="stable")

    whole = explain_next_action(m, X, ids, names, "pick", top_k=3, all_probs=True, field_size=None)
    likely = explain_next_action(m, X, ids, names, "pick", top_k=3, all_probs=True, field_size=5)
    # probabilities and ranking come from every candidate, whatever the field
    assert whole["probabilities"] == likely["probabilities"]
    assert [d["champion"] for d in likely["probabilities"]] == [names[int(ids[i])] for i in by_prob]
    assert [c["champion"] for c in likely["candidates"]] == [names[int(ids[i])] for i in by_prob[:3]]
    assert whole["field"]["size"] == len(p) and likely["field"] == {"size": 5, "probability": pytest.approx(p[by_prob[:5]].sum(), abs=1e-4)}

    for out, field_rows in ((whole, by_prob), (likely, by_prob[:5])):
        w = p[field_rows] / p[field_rows].sum()
        field_value = (w[:, None] * X[field_rows]).sum(axis=0)
        contrib = m.contributions(X[field_rows])[:, :-1] * m.temperature["pick"]
        rel = contrib - (w[:, None] * contrib).sum(axis=0)
        for k, cand in enumerate(out["candidates"]):
            for factor in cand["top_factors"]:
                for feat in factor["features"]:
                    j = m.features.index(feat["name"])
                    assert feat["value"] == pytest.approx(float(X[field_rows[k], j]), abs=1e-3)
                    assert feat["field_mean"] == pytest.approx(float(field_value[j]), abs=2e-3)
                    assert feat["shap_relative"] == pytest.approx(float(rel[k, j]), abs=2e-4)
    # the field always holds the candidates that are shown
    assert explain_next_action(m, X, ids, names, "pick", top_k=8, field_size=5)["field"]["size"] == 8


def test_win_model_contributions_are_additive():
    rng = np.random.default_rng(0)
    n = 2000
    df = pd.DataFrame({"elo_diff": rng.normal(size=n), "ctx_blue_first_pick": rng.choice([-1.0, 1.0], size=n),
                       "meta_x_sum_diff": rng.normal(size=n)})
    logit = 1.5 * df.elo_diff + 0.3 * df.meta_x_sum_diff + 0.1
    df["y"] = (rng.random(n) < 1 / (1 + np.exp(-logit))).astype(int)
    df["game_id"], df["series_id"], df["k"] = [f"g{i}" for i in range(n)], [f"s{i // 3}" for i in range(n)], 20
    m = WinModel(["elo_diff", "ctx_blue_first_pick", "meta_x_sum_diff"], C=1.0).fit(df)
    x = df.iloc[7][m.features].to_dict()
    contrib = m.contributions(x)
    p = m.predict(df.iloc[[7]])[0]
    assert m.base_logit + sum(contrib.values()) == pytest.approx(np.log(p / (1 - p)), abs=1e-9)
    coef = m.coefficients().set_index("feature").coef_per_sd
    assert coef["elo_diff"] == pytest.approx(1.5, abs=0.2) and abs(coef["ctx_blue_first_pick"]) < 0.15
