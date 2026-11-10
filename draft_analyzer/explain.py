"""Grouped SHAP explanations as structured JSON (input for the Phase 5 LLM layer).

SHAP values are summed within groups of correlated features (see ``features.FEATURE_GROUPS``)
before export; the raw feature values sit underneath each group.

* Next-action model: exact TreeSHAP of the candidate's score (LightGBM ``pred_contrib``, the
  same algorithm as ``shap.TreeExplainer``). Because the output is a softmax across
  candidates, the useful quantity is how a candidate differs from the field, so each group's
  contribution is also reported "relative": the candidate's SHAP minus the probability-
  weighted mean SHAP of all candidates. Context features (same for every candidate) mostly
  cancel in the relative view, which is what one wants when asking "why this champion".
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


def explain_next_action(model, X: np.ndarray, cand_ids: np.ndarray, champion_names: list[str], action_type: str,
                        top_k: int = 10, top_groups: int = 4, features_per_group: int = 4) -> dict:
    """Explain the top-k candidates of one action.

    ``X`` is the (n_available, F) feature matrix in ``model.features`` order and ``cand_ids``
    the champion ids of its rows.
    """
    temp = model.temperature.get(action_type, 1.0)
    contrib = model.booster.predict(X, num_iteration=model.best_iteration, pred_contrib=True) * temp
    phi, base = contrib[:, :-1], contrib[:, -1]
    scores = phi.sum(axis=1) + base
    p = np.exp(scores - scores.max())
    p /= p.sum()
    groups = _group_indices(model.features)
    gnames = list(groups)
    G = np.stack([phi[:, idx].sum(axis=1) for idx in groups.values()], axis=1)  # (n, n_groups)
    G_rel = G - (p[:, None] * G).sum(axis=0, keepdims=True)
    phi_rel = phi - (p[:, None] * phi).sum(axis=0, keepdims=True)
    order = np.argsort(-p)[:top_k]
    out = []
    for rank, i in enumerate(order, 1):
        top = np.argsort(-np.abs(G_rel[i]))[:top_groups]
        factors = []
        for gi in top:
            idx = groups[gnames[gi]]
            feat_order = sorted(idx, key=lambda j: -abs(phi_rel[i, j]))[:features_per_group]
            factors.append({
                "group": gnames[gi],
                "shap_relative": round(float(G_rel[i, gi]), 4),
                "shap_raw": round(float(G[i, gi]), 4),
                "direction": "for" if G_rel[i, gi] > 0 else "against",
                "features": [{"name": model.features[j], "value": round(float(X[i, j]), 4),
                              "shap_relative": round(float(phi_rel[i, j]), 4)} for j in feat_order],
            })
        out.append({"rank": rank, "champion": champion_names[int(cand_ids[i])], "probability": round(float(p[i]), 4),
                    "top_factors": factors})
    return {"action_type": action_type, "n_candidates": int(len(cand_ids)), "units": "log-odds (softmax logit)",
            "candidates": out}


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
