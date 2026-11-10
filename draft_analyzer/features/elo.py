"""Team Elo (as-of), with regression toward the league mean at split boundaries."""
from __future__ import annotations

import math

from .. import config
from .asof import DraftContext, GameRecord, PreGame, StatBlock


class EloBlock(StatBlock):
    """Plain Elo on game results.

    * New teams start at their league's current mean rating (1500 if the league is new).
    * When a team starts a new *domestic* split (league, season, split), its rating is pulled a
      fraction ``regress`` toward its league's mean (roster changes). A change of season pulls
      ``regress_season`` instead. International events never trigger regression.
    * All leagues start at 1500, so cross-region differences are learned only from the few
      international games: cross-region ratings are weak (documented caveat).
    * No side term here: side and first pick are handled by the win model on top.
    """

    name = "elo"
    prefix = "elo_"

    def __init__(self, k: float = 28.0, regress: float = 0.0, regress_season: float = 0.35, init: float = 1500.0):
        self.k, self.regress, self.regress_season, self.init = k, regress, regress_season, init
        self.rating: dict[str, float] = {}
        self.games: dict[str, int] = {}
        self.home_league: dict[str, str] = {}
        self.last_split: dict[str, tuple] = {}

    # --- as-of reads ---
    def league_mean(self, league: str) -> float:
        vals = [r for t, r in self.rating.items() if self.home_league.get(t) == league]
        return sum(vals) / len(vals) if vals else self.init

    def current(self, team: str, pre: PreGame) -> float:
        """Rating of ``team`` as it enters the game described by ``pre``."""
        if team not in self.rating:
            return self.league_mean(pre.league) if not pre.is_international else self.init
        r = self.rating[team]
        if not pre.is_international:
            last = self.last_split.get(team)
            if last is not None and last != pre.split_key:
                frac = self.regress_season if last[1] != pre.season else self.regress
                r += frac * (self.league_mean(self.home_league.get(team, pre.league)) - r)
        return r

    def expected_blue(self, pre: PreGame) -> float:
        diff = self.current(pre.blue_team, pre) - self.current(pre.red_team, pre)
        return 1.0 / (1.0 + 10.0 ** (-diff / 400.0))

    def context_features(self, ctx: DraftContext) -> dict[str, float]:
        pre = ctx.pre
        own, opp = pre.team(ctx.acting_side), pre.team(ctx.opp_side)
        r_own, r_opp = self.current(own, pre), self.current(opp, pre)
        return {
            "elo_own": r_own,
            "elo_opp": r_opp,
            "elo_diff_acting": (r_own - r_opp) / 400.0,
        }

    def win_features(self, ctx: DraftContext) -> dict[str, float]:
        pre = ctx.pre
        rb, rr = self.current(pre.blue_team, pre), self.current(pre.red_team, pre)
        return {
            "elo_diff": (rb - rr) / 400.0,
            "elo_games_min": math.log1p(min(self.games.get(pre.blue_team, 0), self.games.get(pre.red_team, 0))),
        }

    # --- update ---
    def update(self, g: GameRecord, exp_blue: float) -> None:
        pre = g.pre
        rb, rr = self.current(pre.blue_team, pre), self.current(pre.red_team, pre)
        e = 1.0 / (1.0 + 10.0 ** (-(rb - rr) / 400.0))
        delta = self.k * (g.blue_win - e)
        self.rating[pre.blue_team] = rb + delta
        self.rating[pre.red_team] = rr - delta
        for team in (pre.blue_team, pre.red_team):
            self.games[team] = self.games.get(team, 0) + 1
            if not pre.is_international:
                self.home_league[team] = pre.league
                self.last_split[team] = pre.split_key
            else:
                self.home_league.setdefault(team, pre.league)
