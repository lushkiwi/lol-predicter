"""Synthetic games for engine tests (no real data needed)."""
from __future__ import annotations

import dataclasses
import random

from draft_analyzer import draft
from draft_analyzer.features.asof import ChampionIndex, GameRecord, PreGame

N_CHAMPS = 40


def champs() -> ChampionIndex:
    return ChampionIndex([f"Champ{i:02d}" for i in range(N_CHAMPS)])


def make_game(rng: random.Random, game_id: str, series_id: str, n: int, t: float, blue: str, red: str,
              burned: tuple = (), blue_wins: int = 0, red_wins: int = 0, first_pick_side: str | None = None,
              length_days: float = 0.5 / 24, league: str = "LCK", patch: str = "16.01") -> GameRecord:
    pool = [c for c in range(N_CHAMPS) if c not in burned]
    actions = tuple(rng.sample(pool, 20))
    pre = PreGame(
        game_id=game_id, series_id=series_id, game_in_series=n, t=t, patch=patch, league=league, season=2026,
        split="Cup", playoffs=0, blue_team=blue, red_team=red,
        blue_players=tuple(f"{blue}_p{r}" for r in range(5)), red_players=tuple(f"{red}_p{r}" for r in range(5)),
        first_pick_side=first_pick_side or rng.choice(["Blue", "Red"]), fearless="hard",
        blue_burned=tuple(burned), red_burned=tuple(burned), blue_series_wins=blue_wins, red_series_wins=red_wins,
    )
    return finish(pre, actions, rng, t + length_days)


def finish(pre: PreGame, actions: tuple, rng: random.Random, t_end: float, blue_win: int | None = None) -> GameRecord:
    """Attach an outcome (random final roles and winner) to a draft."""
    def picks(side):
        return [c for i, c in enumerate(actions)
                if draft.ACTION_TYPE[i] == "pick" and pre.side_of_slot(draft.ACTION_SLOT[i]) == side]
    b, r = picks("Blue"), picks("Red")
    rng.shuffle(b)
    rng.shuffle(r)
    return GameRecord(pre=pre, t_end=t_end, actions=tuple(actions), blue_roles=tuple(b), red_roles=tuple(r),
                      blue_win=rng.randint(0, 1) if blue_win is None else blue_win)


def make_season(seed: int = 0, n_series: int = 30, teams: int = 6) -> list[GameRecord]:
    """Series of 2-3 games, one hour apart; consecutive series overlap in time (concurrent games)."""
    rng = random.Random(seed)
    names = [f"T{i}" for i in range(teams)]
    games = []
    for s in range(n_series):
        a, b = rng.sample(names, 2)
        t0 = 20000.0 + s * (0.6 / 24)  # next series starts 36 min after the previous one started
        burned: tuple = ()
        wins = {a: 0, b: 0}
        for n in range(1, rng.choice([2, 3]) + 1):
            blue, red = (a, b) if rng.random() < 0.5 else (b, a)
            g = make_game(rng, f"s{s}g{n}", f"S{s}", n, t0 + (n - 1) / 24, blue, red, burned, wins[blue], wins[red],
                          patch="16.01" if s < n_series // 2 else "16.02")
            games.append(g)
            wins[blue if g.blue_win else red] += 1
            burned = burned + tuple(c for i, c in enumerate(g.actions) if draft.ACTION_TYPE[i] == "pick")
    return sorted(games, key=lambda g: (g.pre.t, g.pre.game_id))


def with_outcome_changed(g: GameRecord, rng: random.Random) -> GameRecord:
    """Same pre-game facts and same draft, different result and final roles."""
    return finish(g.pre, g.actions, rng, g.t_end, blue_win=1 - g.blue_win)


def with_draft_changed(g: GameRecord, rng: random.Random) -> GameRecord:
    burned = set(g.pre.blue_burned) | set(g.pre.red_burned)
    pool = [c for c in range(N_CHAMPS) if c not in burned]
    return finish(g.pre, tuple(rng.sample(pool, 20)), rng, g.t_end, blue_win=1 - g.blue_win)
