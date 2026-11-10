"""Pairwise champion features: synergy (same team) and counter (opposing teams).

Pro samples per champion pair are tiny, so everything is shrunk hard toward zero.

State (all time-decayed with a long half-life, C x C):
  syn_n / syn_resid : games two champions were picked by the SAME team / sum of (won - Elo exp).
  ctr_n / ctr_resid : games c was picked by one team and d by the OTHER / sum of (c's team won - exp).
  picks[c], games   : decayed pick count of c and decayed game count (for co-pick lift).

Lift expectation under independence. Let p_c = picks[c] / games. Assuming champions are drafted
independently, P(both c and d are picked in a game) = p_c * p_d. (Correction: picks in a game are drawn without replacement, so given c is picked, P(d picked)
is p_d * (n-1)/(n-p_c) with n = picks per game = 10; we use the symmetric geometric mean of the
c and d factors, f_c = (n-1)/(n-p_c), so independent drafting gives lift ~ 0.) Given both are picked, d occupies
one of the 9 other pick slots, 4 of which are on c's team and 5 on the other team, so
    e_syn[c, d] = picks[c] * picks[d] / games * sqrt(f_c f_d) * 4/9
    e_ctr[c, d] = picks[c] * picks[d] / games * sqrt(f_c f_d) * 5/9
(the spec's "/2" is the 2-team approximation; 4/9 and 5/9 are exact for 5 picks per team).
    colift = log((syn_n + k_lift) / (e_syn + k_lift)),  vslift = log((ctr_n + k_lift) / (e_ctr + k_lift)).
"""
from __future__ import annotations

import numpy as np

from .asof import ChampionIndex, Decayed, DraftContext, GameRecord, PreGame, StatBlock

_SAME = 4.0 / 9.0
_OPP = 5.0 / 9.0


class PairwiseBlock(StatBlock):
    name = "pairwise"
    prefix = "pair_"

    def __init__(self, champs: ChampionIndex, half_life: float = 240.0, k_syn: float = 15.0,
                 k_ctr: float = 15.0, k_lift: float = 5.0):
        self.C = champs.C if hasattr(champs, "C") else int(champs)
        C = self.C
        self.k_syn, self.k_ctr, self.k_lift = k_syn, k_ctr, k_lift
        self.syn_n = Decayed((C, C), half_life)
        self.syn_resid = Decayed((C, C), half_life)
        self.ctr_n = Decayed((C, C), half_life)
        self.ctr_resid = Decayed((C, C), half_life)
        self.picks = Decayed((C,), half_life)
        self.games = Decayed((1,), half_life)
        z = np.zeros((C, C))
        self.syn = self.ctr = self.colift = self.vslift = self.syn_nr = z

    # ---------------- per-game cache ----------------
    def begin_game(self, pre: PreGame) -> None:
        t = pre.t
        syn_n, ctr_n = self.syn_n.read(t), self.ctr_n.read(t)
        self.syn_nr = syn_n
        self.syn = self.syn_resid.read(t) / (syn_n + self.k_syn)
        self.ctr = self.ctr_resid.read(t) / (ctr_n + self.k_ctr)
        games = float(self.games.read(t)[0])
        if games > 0:
            pk = self.picks.read(t)
            n_pk = pk.sum() / games  # picks per game (10)
            # picks within a game are drawn without replacement: given c is picked the other n-1
            # slots spread over the remaining champions, so P(d | c) ~ p_d * (n-1)/(n-p_c), not p_d.
            f = np.sqrt(np.maximum(n_pk - 1.0, 0.0) / np.maximum(n_pk - pk / games, 1e-9))
            pp = np.outer(pk * f, pk * f) / games
            self.colift = np.log((syn_n + self.k_lift) / (pp * _SAME + self.k_lift))
            self.vslift = np.log((ctr_n + self.k_lift) / (pp * _OPP + self.k_lift))
            np.fill_diagonal(self.colift, 0.0)
            np.fill_diagonal(self.vslift, 0.0)
        else:
            self.colift = self.vslift = np.zeros((self.C, self.C))

    # ---------------- features ----------------
    def _mean(self, M: np.ndarray, picks) -> np.ndarray:
        if len(picks) == 0:
            return np.zeros(self.C)
        return M[:, picks].mean(axis=1)

    def candidate_features(self, ctx: DraftContext) -> dict[str, np.ndarray]:
        A, B = ctx.picks(ctx.acting_side), ctx.picks(ctx.opp_side)
        m = self._mean
        return {
            "pair_syn_own": m(self.syn, A),
            "pair_ctr_vs_opp": m(self.ctr, B),
            "pair_syn_opp": m(self.syn, B),
            "pair_ctr_vs_own": m(self.ctr, A),
            "pair_colift_own": m(self.colift, A),
            "pair_colift_opp": m(self.colift, B),
            "pair_vslift_opp": m(self.vslift, B),
            "pair_vslift_own": m(self.vslift, A),
            "pair_evidence_own": np.log1p(m(self.syn_nr, A)),
            "pair_evidence_opp": np.log1p(m(self.syn_nr, B)),
        }

    def _syn_sum(self, picks) -> float:
        if len(picks) < 2:
            return 0.0
        S = self.syn[np.ix_(picks, picks)]
        return float((S.sum() - np.trace(S)) / 2.0)

    def win_features(self, ctx: DraftContext) -> dict[str, float]:
        b, r = ctx.picks("Blue"), ctx.picks("Red")
        ctr = float(self.ctr[np.ix_(b, r)].sum()) if b and r else 0.0
        return {"pair_syn_sum_diff": self._syn_sum(b) - self._syn_sum(r), "pair_ctr_sum": ctr}

    # ---------------- state update (only place outcomes are seen) ----------------
    def update(self, g: GameRecord, exp_blue: float) -> None:
        t = g.pre.t
        blue = np.array([c for c in g.picks("Blue") if c >= 0], dtype=np.int64)
        red = np.array([c for c in g.picks("Red") if c >= 0], dtype=np.int64)
        self.games.add_all(t, 1.0)
        rb = g.won("Blue") - exp_blue  # blue's residual; red's is -rb
        for P, r in ((blue, rb), (red, -rb)):
            if len(P) == 0:
                continue
            self.picks.add(t, (P,), 1.0)
            if len(P) > 1:
                I, J = np.meshgrid(P, P, indexing="ij")
                off = I != J
                idx = (I[off], J[off])
                self.syn_n.add(t, idx, 1.0)
                self.syn_resid.add(t, idx, r)
        if len(blue) and len(red):
            I, J = np.meshgrid(blue, red, indexing="ij")
            I, J = I.ravel(), J.ravel()
            self.ctr_n.add(t, (I, J), 1.0)
            self.ctr_n.add(t, (J, I), 1.0)
            self.ctr_resid.add(t, (I, J), rb)
            self.ctr_resid.add(t, (J, I), -rb)
