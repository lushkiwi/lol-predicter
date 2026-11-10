"""Champion meta strength (as-of): availability-adjusted presence, draft-position rates,
win residuals vs Elo, plus static Data Dragon attributes.

Eligibility (the denominator of every rate)
-------------------------------------------
Since 2025 most series are hard Fearless: a champion used earlier in the series is burned and
cannot be picked again, so raw presence understates strong champions late in series. Every
rate here therefore counts only *eligible* games: champion ``c`` is eligible in a game iff it
is in neither ``pre.blue_burned`` nor ``pre.red_burned`` at game start. Both numerator and
denominator are restricted to eligible games (so rates stay in [0, 1]; in soft Fearless a
champion burned for one team only is excluded from that game entirely, ~1% of games).
Phase/slot and action-index rates use the same game-start eligibility for simplicity (they do
not account for champions taken earlier in the same draft).

Rates (shape (C,), "per eligible game")
---------------------------------------
* ``meta_presence_long`` P(picked or banned), time-decayed over all leagues (half-life
  ``hl_long``). ``meta_presence_recent`` the same with ``hl_recent``. Both are lightly shrunk
  (strength ``k_base``) toward the all-champion mean only to avoid 0/0.
* ``meta_presence`` / ``meta_pick_rate`` / ``meta_ban_rate``: current-patch counts (undecayed,
  keyed by the patch string so out-of-order patches across leagues are fine) shrunk toward the
  long-run decayed rate: ``(n_patch + k_patch * long_rate) / (eligible_games_patch + k_patch)``.
  A brand-new patch (no finished games) falls back exactly to the long-run rate.
  ``meta_presence_patch_raw`` is the unshrunk patch rate (0 with no patch games).
* ``meta_presence_league``: decayed presence in ``pre.league`` (``hl_league``) shrunk toward
  the global long-run rate with ``k_league``.
* ``meta_rate_phase_slot``: rate of being chosen in the next action's (phase, slot) bucket
  (4 phases x first/second slot = 8 buckets). Decomposed as long-run presence x share, where
  share = P(bucket | chosen) shrunk (``k_phase``, in units of the champion's decayed
  presence count) toward the global share of actions falling in that bucket.
* ``meta_rate_action``: same at the exact action index (20 buckets), share shrunk with the
  heavier ``k_action`` toward phase-slot share x global within-bucket share of that action.
* ``meta_rate_type``: ``meta_pick_rate`` if the next action is a pick, else ``meta_ban_rate``.
* ``meta_win_resid``: decayed sum over the champion's picks of (won - Elo expected) divided by
  (decayed picks + ``k_resid``), i.e. shrunk toward 0. ``meta_win_resid_patch``: current patch
  only, undecayed, ``k_resid_patch``.
* ``tag_*``: static Data Dragon class tags and ``tag_ranged`` (attack range >= 300).

Context: ``meta_games_patch`` = log1p(finished games on the patch), ``meta_patch_age_days`` =
days since the earliest finished game on the patch started (0 for a new patch).

Win features (blue minus red, locked picks only): ``meta_win_resid_sum_diff``,
``meta_win_resid_patch_sum_diff``, ``meta_presence_sum_diff``.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from .. import config, draft
from .asof import ChampionIndex, Decayed, DecayedMap, DraftContext, GameRecord, PreGame, StatBlock

_PHASE_ID = {"ban1": 0, "pick1": 1, "ban2": 2, "pick2": 3}
_SLOT_ID = {"first": 0, "second": 1}
# bucket (phase, slot) of every action index
_ACTION_BUCKET = np.array(
    [_PHASE_ID[draft.ACTION_PHASE[i]] * 2 + _SLOT_ID[draft.ACTION_SLOT[i]] for i in range(draft.N_ACTIONS)]
)
_BUCKET_SIZE = np.bincount(_ACTION_BUCKET, minlength=8).astype(np.float64)

# rows of the long-run decayed state
_ELIG, _PRES, _PICK, _BAN, _WRN, _WRC = range(6)
_PS0 = 6  # 8 phase-slot rows
_ACT0 = _PS0 + 8  # 20 action rows
_NROWS = _ACT0 + draft.N_ACTIONS

TAGS = ("Fighter", "Tank", "Mage", "Assassin", "Marksman", "Support")
TAG_NAMES = tuple(f"tag_{t.lower()}" for t in TAGS) + ("tag_ranged",)


def _attribute_matrix(champs: ChampionIndex, attributes: pd.DataFrame | None, ranged_threshold: float) -> np.ndarray:
    """(C, 7) float matrix of tag flags aligned to ``champs.names``; missing champions -> 0."""
    out = np.zeros((champs.C, len(TAG_NAMES)))
    if attributes is None:
        path = Path(config.CHAMPIONS_PATH)
        if not path.exists():
            return out
        attributes = pd.read_parquet(path)
    df = attributes.drop_duplicates("champion").set_index("champion")
    for j, t in enumerate(TAGS):
        col = f"tag_{t}"
        if col in df.columns:
            out[:, j] = df[col].reindex(champs.names).fillna(False).astype(float).to_numpy()
    if "stat_attackrange" in df.columns:
        rng = pd.to_numeric(df["stat_attackrange"].reindex(champs.names), errors="coerce").fillna(0.0)
        out[:, -1] = (rng.to_numpy() >= ranged_threshold).astype(float)
    return out


def _shrink(num, den, prior, k: float) -> np.ndarray:
    """``(num + k * prior) / (den + k)``; returns ``prior`` where ``den + k == 0`` (never NaN)."""
    den_k = den + k
    val = (num + k * prior) / np.maximum(den_k, 1e-12)
    return np.where(den_k > 0, val, prior)


class _PatchStats:
    __slots__ = ("v", "n_games", "first_t")

    def __init__(self, C: int):
        self.v = np.zeros((6, C))  # elig, pres, pick, ban, wr_num, wr_cnt (undecayed)
        self.n_games = 0
        self.first_t = math.inf


class ChampionMetaBlock(StatBlock):
    name = "champion"
    prefix = "meta_"  # static attributes use "tag_"

    def __init__(
        self,
        champs: ChampionIndex,
        attributes: pd.DataFrame | None = None,
        hl_long: float | None = 45.0,
        hl_recent: float | None = 10.0,
        hl_league: float | None = 45.0,
        k_patch: float = 30.0,
        k_league: float = 30.0,
        k_phase: float = 10.0,
        k_action: float = 30.0,
        k_resid: float = 20.0,
        k_resid_patch: float = 40.0,
        k_base: float = 1.0,
        ranged_threshold: float = 300.0,
    ):
        self.champs = champs
        self.C = C = champs.C
        self.k_patch, self.k_league, self.k_phase, self.k_action = k_patch, k_league, k_phase, k_action
        self.k_resid, self.k_resid_patch, self.k_base = k_resid, k_resid_patch, k_base
        self.long = Decayed((_NROWS, C), hl_long)
        self.recent = Decayed((2, C), hl_recent)  # elig, pres
        self.league = DecayedMap((2, C), hl_league)  # elig, pres
        self.patches: dict[str, _PatchStats] = {}
        self.attrs = _attribute_matrix(champs, attributes, ranged_threshold)
        self._attr_cols = {n: self.attrs[:, j].copy() for j, n in enumerate(TAG_NAMES)}
        self._cache: dict = {}

    # ------------------------------------------------------------------ as-of reads
    @staticmethod
    def _base_rate(num: np.ndarray, den: np.ndarray, k: float) -> np.ndarray:
        tot = den.sum()
        prior = num.sum() / tot if tot > 0 else 0.0
        return _shrink(num, den, prior, k)

    def begin_game(self, pre: PreGame) -> None:
        t = pre.t
        L = self.long.read(t)
        elig = L[_ELIG]
        pres_long = self._base_rate(L[_PRES], elig, self.k_base)
        pick_long = self._base_rate(L[_PICK], elig, self.k_base)
        ban_long = self._base_rate(L[_BAN], elig, self.k_base)
        R = self.recent.read(t)
        pres_recent = _shrink(R[1], R[0], pres_long, self.k_base)

        ps = self.patches.get(pre.patch)
        if ps is None:
            P = np.zeros((6, self.C))
            n_patch, age = 0, 0.0
        else:
            P = ps.v
            n_patch, age = ps.n_games, max(0.0, t - ps.first_t)
        kp = self.k_patch
        presence = _shrink(P[_PRES], P[_ELIG], pres_long, kp)
        pick = _shrink(P[_PICK], P[_ELIG], pick_long, kp)
        ban = _shrink(P[_BAN], P[_ELIG], ban_long, kp)
        raw = np.divide(P[_PRES], P[_ELIG], out=np.zeros(self.C), where=P[_ELIG] > 0)

        Lg = self.league.read(pre.league, t)
        pres_league = _shrink(Lg[1], Lg[0], pres_long, self.k_league)

        # draft-position shares: P(bucket | champion chosen), shrunk toward global shares
        ps_cnt = L[_PS0:_ACT0]  # (8, C)
        act_cnt = L[_ACT0:]  # (20, C)
        ps_tot = ps_cnt.sum(axis=1)
        g_ps = ps_tot / ps_tot.sum() if ps_tot.sum() > 0 else _BUCKET_SIZE / draft.N_ACTIONS
        ps_share = _shrink(ps_cnt, L[_PRES], g_ps[:, None], self.k_phase)
        act_tot = act_cnt.sum(axis=1)
        bucket_tot = ps_tot[_ACTION_BUCKET]
        within = np.where(bucket_tot > 0, act_tot / np.maximum(bucket_tot, 1e-12), 1.0 / _BUCKET_SIZE[_ACTION_BUCKET])
        act_prior = ps_share[_ACTION_BUCKET] * within[:, None]
        act_share = _shrink(act_cnt, L[_PRES], act_prior, self.k_action)

        wr = _shrink(L[_WRN], L[_WRC], 0.0, self.k_resid)
        wr_patch = _shrink(P[_WRN], P[_WRC], 0.0, self.k_resid_patch)

        self._cache = {
            "rate_ps": pres_long[None, :] * ps_share,
            "rate_act": pres_long[None, :] * act_share,
            "presence": presence,
            "pick": pick,
            "ban": ban,
            "wr": wr,
            "wr_patch": wr_patch,
            "base": {
                "meta_presence": presence,
                "meta_pick_rate": pick,
                "meta_ban_rate": ban,
                "meta_presence_long": pres_long,
                "meta_presence_recent": pres_recent,
                "meta_presence_patch_raw": raw,
                "meta_presence_league": pres_league,
            },
            "ctx": {"meta_games_patch": math.log1p(n_patch), "meta_patch_age_days": float(age)},
        }

    def candidate_features(self, ctx: DraftContext) -> dict[str, np.ndarray]:
        c = self._cache
        k = ctx.k
        out = dict(c["base"])
        out["meta_rate_phase_slot"] = c["rate_ps"][_ACTION_BUCKET[k]]
        out["meta_rate_action"] = c["rate_act"][k]
        out["meta_rate_type"] = c["pick"] if ctx.is_pick else c["ban"]
        out["meta_win_resid"] = c["wr"]
        out["meta_win_resid_patch"] = c["wr_patch"]
        out.update(self._attr_cols)
        return out

    def context_features(self, ctx: DraftContext) -> dict[str, float]:
        return dict(self._cache["ctx"])

    def win_features(self, ctx: DraftContext) -> dict[str, float]:
        c = self._cache
        b, r = ctx.picks("Blue"), ctx.picks("Red")

        def diff(a: np.ndarray) -> float:
            return float(a[b].sum() - a[r].sum()) if (b or r) else 0.0

        return {
            "meta_win_resid_sum_diff": diff(c["wr"]),
            "meta_win_resid_patch_sum_diff": diff(c["wr_patch"]),
            "meta_presence_sum_diff": diff(c["presence"]),
        }

    # ------------------------------------------------------------------ update
    def update(self, g: GameRecord, exp_blue: float) -> None:
        pre, t, C = g.pre, g.pre.t, self.C
        elig = np.ones(C)
        burned = [c for c in pre.blue_burned + pre.red_burned if c >= 0]
        if burned:
            elig[burned] = 0.0

        delta = np.zeros((_NROWS, C))
        delta[_ELIG] = elig
        for i, c in enumerate(g.actions):
            if c < 0 or elig[c] == 0.0:
                continue
            delta[_PRES, c] = 1.0
            delta[_PICK if draft.ACTION_TYPE[i] == "pick" else _BAN, c] = 1.0
            delta[_PS0 + _ACTION_BUCKET[i], c] += 1.0
            delta[_ACT0 + i, c] += 1.0
        for side, exp in (("Blue", exp_blue), ("Red", 1.0 - exp_blue)):
            res = g.won(side) - exp
            for c in g.picks(side):
                if c >= 0:
                    delta[_WRN, c] += res
                    delta[_WRC, c] += 1.0

        self.long.add_all(t, delta)
        self.recent.add_all(t, delta[[_ELIG, _PRES]])
        self.league.get(pre.league).add_all(t, delta[[_ELIG, _PRES]])
        ps = self.patches.get(pre.patch)
        if ps is None:
            ps = self.patches[pre.patch] = _PatchStats(C)
        ps.v += delta[:6]
        ps.n_games += 1
        ps.first_t = min(ps.first_t, t)
