"""Grouped SHAP explanations as structured JSON (input for the Phase 5 LLM layer).

SHAP values are summed within groups of correlated features (see ``features.FEATURE_GROUPS``)
before export; the raw feature values sit underneath each group.

* Next-action model: exact TreeSHAP of the candidate's score (LightGBM ``pred_contrib``, the
  same algorithm as ``shap.TreeExplainer``). Because the output is a softmax across
  candidates, the useful quantity is how a candidate differs from its rivals, so each group's
  contribution is also reported "relative": the candidate's SHAP minus the probability-
  weighted mean SHAP of the field. The field is the ``field_size`` likeliest candidates
  (12 by default, the choices actually in play), weighted by their probabilities: the
  question answered is "why this champion rather than the other likely ones". Context
  features (the same for every candidate) mostly cancel in the relative view.
  ``field_size=None`` compares with every available champion instead. That is about ten
  times the work, because SHAP is the expensive step and is then needed for all ~150
  candidates (0.9 s instead of 0.09 s per draft state on a small host), and it lets the
  long tail of champions nobody would take set the baseline. The probabilities themselves
  never depend on this choice.
* Win model: logistic regression, for which SHAP is exact and closed-form:
  coef * (x - training mean), in logit units.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from .features import group_of


def _group_indices(names: list[str]) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = defaultdict(list)
    for i, n in enumerate(names):
        groups[group_of(n)].append(i)
    return dict(groups)


FIELD_SIZE = 12  # the likeliest candidates a champion's factors are compared with


def explain_next_action(model, X: np.ndarray, cand_ids: np.ndarray, champion_names: list[str], action_type: str,
                        top_k: int = 10, top_groups: int = 4, features_per_group: int = 4,
                        all_probs: bool = False, field_size: int | None = FIELD_SIZE) -> dict:
    """Explain the top-k candidates of one action.

    ``X`` is the (n_available, F) feature matrix in ``model.features`` order and ``cand_ids``
    the champion ids of its rows. Probabilities come from the scores of every candidate; SHAP
    is computed only for the field (see the module docstring), which always includes the top-k.
    """
    temp = model.temperature.get(action_type, 1.0)
    scores = model.raw_matrix_scores(X) * temp
    p = np.exp(scores - scores.max())
    p /= p.sum()
    by_prob = np.argsort(-p, kind="stable")      # every candidate, likeliest first
    cand_ids = np.asarray(cand_ids)

    n_field = len(p) if field_size is None else min(len(p), max(top_k, field_size))
    rows = by_prob[:n_field]                     # the field; row k below is the k-th likeliest candidate
    w = p[rows] / p[rows].sum()                  # its weights
    Xf = np.asarray(X, dtype=float)[rows]
    phi = (model.contributions(X[rows]) * temp)[:, :-1]
    groups = _group_indices(model.features)
    gnames = list(groups)
    G = np.stack([phi[:, idx].sum(axis=1) for idx in groups.values()], axis=1)  # (n_field, n_groups)
    G_rel = G - (w[:, None] * G).sum(axis=0, keepdims=True)
    phi_rel = phi - (w[:, None] * phi).sum(axis=0, keepdims=True)
    field = (w[:, None] * Xf).sum(axis=0)        # the field's weighted value of each feature

    out = []
    for k in range(min(top_k, n_field)):
        top = np.argsort(-np.abs(G_rel[k]))[:top_groups]
        factors = []
        for gi in top:
            idx = groups[gnames[gi]]
            feat_order = sorted(idx, key=lambda j: -abs(phi_rel[k, j]))[:features_per_group]
            factors.append({
                "group": gnames[gi],
                "shap_relative": round(float(G_rel[k, gi]), 4),
                "shap_raw": round(float(G[k, gi]), 4),
                "direction": "for" if G_rel[k, gi] > 0 else "against",
                "features": [{"name": model.features[j], "value": round(float(Xf[k, j]), 4),
                              "field_mean": round(float(field[j]), 4),
                              "shap_relative": round(float(phi_rel[k, j]), 4)} for j in feat_order],
            })
        out.append({"rank": k + 1, "champion": champion_names[int(cand_ids[rows[k]])],
                    "probability": round(float(p[rows[k]]), 4), "top_factors": factors})
    res = {"action_type": action_type, "n_candidates": int(len(cand_ids)), "units": "log-odds (softmax logit)",
           "field": {"size": int(n_field), "probability": round(float(p[rows].sum()), 4)},
           "candidates": out}
    if all_probs:  # every available champion, most likely first
        res["probabilities"] = [{"champion": champion_names[int(cand_ids[i])], "probability": round(float(p[i]), 5)}
                                for i in by_prob]
    return res


def explain_win(win_model, baseline_model, x: dict, top_groups: int = 5, features_per_group: int = 4) -> dict:
    """Explain P(blue wins) for one draft state. ``x`` is the engine's win-feature dict."""
    import pandas as pd

    row = pd.DataFrame([x])
    p_full = float(win_model.predict(row)[0])
    p_base = float(baseline_model.predict(row)[0])
    contrib = win_model.contributions(x)
    groups: dict[str, dict] = defaultdict(lambda: {"shap": 0.0, "features": []})
    for name, c in contrib.items():
        g = groups[group_of(name)]
        g["shap"] += c
        g["features"].append({"name": name, "value": round(float(x[name]), 4), "shap": round(float(c), 4)})
    ranked = sorted(groups.items(), key=lambda kv: -abs(kv[1]["shap"]))[:top_groups]
    return {
        "p_blue_win": round(p_full, 4),
        "p_red_win": round(1 - p_full, 4),
        "p_blue_win_baseline": round(p_base, 4),  # Elo + side + first pick only
        "draft_effect_blue": round(p_full - p_base, 4),
        "units": "log-odds of a blue win, relative to the average training game",
        "base_logit": round(win_model.base_logit, 4),
        "top_factors": [
            {"group": g, "shap": round(v["shap"], 4), "favours": "Blue" if v["shap"] > 0 else "Red",
             "features": sorted(v["features"], key=lambda f: -abs(f["shap"]))[:features_per_group]}
            for g, v in ranked
        ],
    }
