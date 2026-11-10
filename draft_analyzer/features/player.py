"""Player champion mastery (as-of), weighted by the role a candidate would be played in."""
from __future__ import annotations

import numpy as np

from .. import draft
from .asof import ChampionIndex, DecayedMap, DraftContext, GameRecord, PreGame, StatBlock

_SIDES = ("Blue", "Red")


class PlayerMasteryBlock(StatBlock):
    """Per-player decayed/career games and win residuals on each champion.

    Residual = won - Elo expectation of the player's team, accumulated with time decay.
    """

    name = "player"
    prefix = "player_"

    def __init__(self, champs: ChampionIndex, half_life: float = 180.0, share_prior: float = 5.0,
                 resid_prior: float = 8.0, pool_min_games: float = 3.0):
        self.C = champs.C
        self.share_prior, self.resid_prior, self.pool_min_games = share_prior, resid_prior, pool_min_games
        C = self.C
        self.games = DecayedMap((C,), half_life)
        self.career = DecayedMap((C,), None)
        self.resid = DecayedMap((C,), half_life)
        self.total = DecayedMap((1,), half_life)
        self._cache: dict = {}
        self._w: dict = {}

    def begin_game(self, pre: PreGame) -> None:
        t, C = pre.t, self.C
        self._cache, self._w = {}, {}
        for side in _SIDES:
            G = np.zeros((5, C)); K = np.zeros((5, C)); R = np.zeros((5, C)); T = np.zeros(5)
            for r, p in enumerate(pre.players(side)):
                if draft.is_missing(p) or p not in self.games:
                    continue
                G[r] = self.games.read(p, t)
                K[r] = self.career.read(p, t)
                R[r] = self.resid.read(p, t)
                T[r] = self.total.read(p, t)[0]
            burned = list(pre.burned(side))
            avail = np.ones(C, dtype=bool)
            if burned:
                avail[burned] = False
            self._cache[side] = {
                "G": G,
                "K": K,
                "share": G / (T[:, None] + self.share_prior),
                "resid": R / (G + self.resid_prior),
                "Gmax": G.max(axis=0),
                "pool": (G >= self.pool_min_games).any(axis=0) & avail,
            }

    def _weights(self, ctx: DraftContext, side: str) -> np.ndarray:
        key = (side, tuple(ctx.picks(side)))
        w = self._w.get(key)
        if w is None:
            w = self._w[key] = self.engine.block("roles").candidate_role_weights(ctx.picks(side))
        return w

    def candidate_features(self, ctx: DraftContext) -> dict[str, np.ndarray]:
        out = {}
        for tag, side in (("own", ctx.acting_side), ("opp", ctx.opp_side)):
            c, W = self._cache[side], self._weights(ctx, side)
            out[f"player_games_{tag}"] = np.log1p(np.einsum("cr,rc->c", W, c["G"]))
            out[f"player_games_{tag}_max"] = np.log1p(c["Gmax"])
            out[f"player_career_{tag}"] = np.log1p(np.einsum("cr,rc->c", W, c["K"]))
            out[f"player_share_{tag}"] = np.einsum("cr,rc->c", W, c["share"])
            out[f"player_win_resid_{tag}"] = np.einsum("cr,rc->c", W, c["resid"])
        return out

    def context_features(self, ctx: DraftContext) -> dict[str, float]:
        taken = ctx.taken()
        out = {}
        for tag, side in (("own", ctx.acting_side), ("opp", ctx.opp_side)):
            pool = self._cache[side]["pool"]
            n = int(pool.sum())
            if taken:
                n -= int(pool[sorted(set(taken))].sum())
            out[f"player_pool_left_{tag}"] = float(n)
        return out

    def win_features(self, ctx: DraftContext) -> dict[str, float]:
        mastery, resid = {}, {}
        for side in _SIDES:
            c, picks = self._cache[side], ctx.picks(side)
            if not picks:
                mastery[side] = resid[side] = 0.0
                continue
            G = c["G"][:, picks]  # (5, m)
            best = G.argmax(axis=0)
            cols = np.arange(len(picks))
            has = G[best, cols] > 0
            mastery[side] = float(np.log1p(c["Gmax"][picks]).sum())
            resid[side] = float(np.where(has, c["resid"][:, picks][best, cols], 0.0).sum())
        return {
            "player_mastery_sum_diff": mastery["Blue"] - mastery["Red"],
            "player_win_resid_sum_diff": resid["Blue"] - resid["Red"],
        }

    def update(self, g: GameRecord, exp_blue: float) -> None:
        t = g.pre.t
        for side in _SIDES:
            exp = exp_blue if side == "Blue" else 1.0 - exp_blue
            res = g.won(side) - exp
            for p, c in zip(g.pre.players(side), g.roles(side)):
                if draft.is_missing(p) or c < 0:
                    continue
                self.games.get(p).add(t, c, 1.0)
                self.career.get(p).add(t, c, 1.0)
                self.resid.get(p).add(t, c, res)
                self.total.get(p).add(t, 0, 1.0)
