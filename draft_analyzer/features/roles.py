"""Champion role distributions (as-of) and role-fit features.

Picks do not reveal roles during the draft, so a role is modelled as a probability
distribution per champion learned from history (flex picks matter).
"""
from __future__ import annotations

from itertools import permutations

import numpy as np

from .asof import ROLES, Decayed, DraftContext, GameRecord, PreGame, StatBlock

_PERMS = {m: np.array(list(permutations(range(5), m)), dtype=np.int64) for m in range(1, 6)}


class RoleBlock(StatBlock):
    name = "roles"
    prefix = "role_"

    def __init__(self, C: int, half_life: float = 180.0, prior_strength: float = 1.0):
        self.C = C
        self.counts = Decayed((C, 5), half_life)
        self.prior_strength = prior_strength
        self._probs: np.ndarray | None = None

    # --- as-of reads ---
    def probs_at(self, t: float) -> np.ndarray:
        """(C, 5) matrix P(role | champion) from the games applied so far, decayed to ``t``."""
        counts = self.counts.read(t)
        # Dirichlet smoothing toward uniform so unseen champions are "any role"
        return (counts + self.prior_strength / 5.0) / (counts.sum(axis=1, keepdims=True) + self.prior_strength)

    def begin_game(self, pre: PreGame) -> None:
        self._probs = self.probs_at(pre.t)

    def role_probs(self) -> np.ndarray:
        """(C, 5) matrix P(role | champion) as of the current game."""
        return self._probs

    def assignment_posterior(self, picks: list[int]) -> tuple[np.ndarray, np.ndarray]:
        """Given a team's locked picks, return (P(role filled) (5,), P(pick i plays role r) (m,5)).

        Exact enumeration over injective assignments of the m picks to the 5 roles, weighting
        each assignment by the product of P(role | champion).
        """
        m = len(picks)
        if m == 0:
            return np.zeros(5), np.zeros((0, 5))
        perms = _PERMS[m]  # (n_perm, m)
        p = self._probs[picks]  # (m, 5)
        w = np.prod(p[np.arange(m)[None, :], perms], axis=1)  # (n_perm,)
        total = w.sum()
        if total <= 0:
            w = np.ones(len(perms))
            total = w.sum()
        pick_role = np.zeros((m, 5))
        for i in range(m):
            np.add.at(pick_role[i], perms[:, i], w)
        pick_role /= total
        return pick_role.sum(axis=0), pick_role

    def open_roles(self, picks: list[int]) -> np.ndarray:
        """P(role still open) for a team with these locked picks."""
        filled, _ = self.assignment_posterior(picks)
        return 1.0 - filled

    def candidate_role_weights(self, picks: list[int]) -> np.ndarray:
        """(C, 5): for every candidate, P(candidate would play role r | team's open roles).

        Rows are normalised; a candidate whose usual roles are all filled gets a flat row
        scaled by the open-role probabilities.
        """
        open_ = self.open_roles(picks)
        w = self._probs * open_[None, :]
        s = w.sum(axis=1, keepdims=True)
        return np.where(s > 1e-12, w / np.maximum(s, 1e-12), open_[None, :] / max(open_.sum(), 1e-12))

    def candidate_features(self, ctx: DraftContext) -> dict[str, np.ndarray]:
        open_own = self.open_roles(ctx.picks(ctx.acting_side))
        open_opp = self.open_roles(ctx.picks(ctx.opp_side))
        p = self._probs
        feats = {
            "role_fit_own": p @ open_own,  # P(candidate fits a role the acting team still needs)
            "role_fit_opp": p @ open_opp,  # same for the opponent (relevant for bans)
            "role_flex_entropy": -(p * np.log(p + 1e-12)).sum(axis=1),
        }
        for i, r in enumerate(ROLES):
            feats[f"role_p_{r}"] = p[:, i]
        return feats

    def context_features(self, ctx: DraftContext) -> dict[str, float]:
        open_own = self.open_roles(ctx.picks(ctx.acting_side))
        open_opp = self.open_roles(ctx.picks(ctx.opp_side))
        out = {}
        for i, r in enumerate(ROLES):
            out[f"role_open_own_{r}"] = float(open_own[i])
            out[f"role_open_opp_{r}"] = float(open_opp[i])
        return out

    # --- update ---
    def update(self, g: GameRecord, exp_blue: float) -> None:
        for side in ("Blue", "Red"):
            champs = [c for c in g.roles(side)]
            idx = [(c, r) for r, c in enumerate(champs) if c >= 0]
            if idx:
                rows, cols = zip(*idx)
                self.counts.add(g.pre.t, (np.array(rows), np.array(cols)), 1.0)
