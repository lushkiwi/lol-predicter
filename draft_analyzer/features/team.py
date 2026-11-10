"""Team draft tendencies (as-of): what each team picks, bans, and gets banned against."""
from __future__ import annotations

import math

import numpy as np

from .asof import ChampionIndex, DecayedMap, DraftContext, GameRecord, PreGame, StatBlock

_SIDES = ("Blue", "Red")


class TeamTendencyBlock(StatBlock):
    """Time-decayed per-team pick / ban / banned-against rates (per decayed team game).

    Rates are count / (games + prior_games), i.e. shrunk toward 0 so thin histories give small
    values. A short half-life copy of the pick rates captures current form.
    """

    name = "team"
    prefix = "team_"

    def __init__(self, champs: ChampionIndex, half_life: float = 60.0, recent_half_life: float = 14.0,
                 games_half_life: float = 365.0, prior_games: float = 3.0):
        self.C = champs.C
        self.prior_games = prior_games
        C = self.C
        self.picks = DecayedMap((C,), half_life)
        self.bans = DecayedMap((C,), half_life)
        self.against = DecayedMap((C,), half_life)
        self.games = DecayedMap((1,), half_life)
        self.picks_recent = DecayedMap((C,), recent_half_life)
        self.games_recent = DecayedMap((1,), recent_half_life)
        self.games_long = DecayedMap((1,), games_half_life)
        self._cache: dict = {}

    # --- per-game cache ---
    def begin_game(self, pre: PreGame) -> None:
        k = self.prior_games
        self._cache = {}
        for side in _SIDES:
            team = pre.team(side)
            g = self.games.read(team, pre.t)[0]
            gr = self.games_recent.read(team, pre.t)[0]
            gl = self.games_long.read(team, pre.t)[0]
            self._cache[side] = {
                "pick": self.picks.read(team, pre.t) / (g + k),
                "ban": self.bans.read(team, pre.t) / (g + k),
                "against": self.against.read(team, pre.t) / (g + k),
                "pick_recent": self.picks_recent.read(team, pre.t) / (gr + k),
                "games": math.log1p(max(gl, 0.0)),
            }

    def candidate_features(self, ctx: DraftContext) -> dict[str, np.ndarray]:
        o, p = self._cache[ctx.acting_side], self._cache[ctx.opp_side]
        return {
            "team_pick_rate_own": o["pick"],
            "team_pick_rate_opp": p["pick"],
            "team_ban_rate_own": o["ban"],
            "team_ban_rate_opp": p["ban"],
            "team_banned_against_own": o["against"],
            "team_banned_against_opp": p["against"],
            "team_pick_rate_own_recent": o["pick_recent"],
            "team_pick_rate_opp_recent": p["pick_recent"],
        }

    def context_features(self, ctx: DraftContext) -> dict[str, float]:
        return {
            "team_games_own": self._cache[ctx.acting_side]["games"],
            "team_games_opp": self._cache[ctx.opp_side]["games"],
        }

    def win_features(self, ctx: DraftContext) -> dict[str, float]:
        s = {}
        for side in _SIDES:
            picks = ctx.picks(side)
            s[side] = float(self._cache[side]["pick"][picks].sum()) if picks else 0.0
        return {"team_comfort_sum_diff": s["Blue"] - s["Red"]}

    # --- update ---
    def update(self, g: GameRecord, exp_blue: float) -> None:
        t = g.pre.t
        for side, other in (("Blue", "Red"), ("Red", "Blue")):
            team, opp_team = g.pre.team(side), g.pre.team(other)
            picks = [c for c in g.picks(side) if c >= 0]
            bans = [c for c in g.bans(side) if c >= 0]
            if picks:
                self.picks.get(team).add(t, picks, 1.0)
                self.picks_recent.get(team).add(t, picks, 1.0)
            if bans:
                self.bans.get(team).add(t, bans, 1.0)
                self.against.get(opp_team).add(t, bans, 1.0)
            for m in (self.games, self.games_recent, self.games_long):
                m.get(team).add(t, 0, 1.0)
