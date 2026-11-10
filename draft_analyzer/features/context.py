"""Draft/series context features and in-series memory."""
from __future__ import annotations

import numpy as np

from .. import draft
from .asof import DraftContext, GameRecord, StatBlock

_PHASE_ID = {"ban1": 0, "pick1": 1, "ban2": 2, "pick2": 3}
_FEARLESS_ID = {"na": 0, "none": 0, "soft": 1, "hard": 2}


class ContextBlock(StatBlock):
    """Where we are in the draft and the series, plus what happened earlier in this series.

    Series memory only contains FINISHED earlier games of the series (it is filled in
    ``update``), so game 2 sees game 1 but nothing leaks from the current game.
    """

    name = "context"
    prefix = "ctx_"  # also emits series_* features (their own SHAP/ablation group)

    def __init__(self, C: int):
        self.C = C
        self.series: dict[str, list[GameRecord]] = {}

    def _series_counts(self, ctx: DraftContext) -> tuple[np.ndarray, np.ndarray]:
        """Times each champion was banned earlier this series by the acting team / the opponent."""
        own = np.zeros(self.C)
        opp = np.zeros(self.C)
        acting, other = ctx.pre.team(ctx.acting_side), ctx.pre.team(ctx.opp_side)
        for g in self.series.get(ctx.pre.series_id, ()):
            for side in ("Blue", "Red"):
                bans = [c for c in g.bans(side) if c >= 0]
                if not bans:
                    continue
                team = g.pre.team(side)
                if team == acting:
                    own[bans] += 1
                elif team == other:
                    opp[bans] += 1
        return own, opp

    def candidate_features(self, ctx: DraftContext) -> dict[str, np.ndarray]:
        own, opp = self._series_counts(ctx)
        return {"series_ban_own_prev": own, "series_ban_opp_prev": opp}

    def context_features(self, ctx: DraftContext) -> dict[str, float]:
        pre = ctx.pre
        a, o = ctx.acting_side, ctx.opp_side
        return {
            "ctx_action_index": float(ctx.k),
            "ctx_is_pick": float(ctx.is_pick),
            "ctx_phase": float(_PHASE_ID[ctx.phase]),
            "ctx_slot_first": float(ctx.slot == "first"),
            "ctx_side_blue": float(a == "Blue"),
            "ctx_n_picks_own": float(len(ctx.picks(a))),
            "ctx_n_picks_opp": float(len(ctx.picks(o))),
            "ctx_international": float(pre.is_international),
            "ctx_playoffs": float(pre.playoffs),
            "series_game_number": float(pre.game_in_series),
            "series_wins_own": float(pre.series_wins(a)),
            "series_wins_opp": float(pre.series_wins(o)),
            "series_fearless": float(_FEARLESS_ID.get(pre.fearless, 0)),
            "series_n_burned_own": float(len(pre.burned(a))),
            "series_n_burned_opp": float(len(pre.burned(o))),
        }

    def win_features(self, ctx: DraftContext) -> dict[str, float]:
        pre = ctx.pre
        return {
            "ctx_blue_first_pick": 1.0 if pre.first_pick_side == "Blue" else -1.0,
            "series_wins_diff": float(pre.blue_series_wins - pre.red_series_wins),
        }

    def update(self, g: GameRecord, exp_blue: float) -> None:
        self.series.setdefault(g.pre.series_id, []).append(g)
