"""Point-in-time ("as-of") feature engine.

Leakage is prevented structurally:

* Feature code only ever sees a :class:`PreGame` (facts known before the draft starts) plus
  the draft actions made so far (:class:`DraftContext`). Outcomes live on :class:`GameRecord`,
  which is only handed to ``StatBlock.update``.
* :class:`Engine` walks games in start-time order and applies a finished game to the stat
  blocks only once its END time is <= the start time of the game being featurised. So a game
  never sees itself, later games, or games still in progress, while game 2 of a series does
  see game 1 (no date-only cutoffs anywhere).

Every stat block keeps numpy arrays indexed by champion id so candidate features for all
champions are computed with vector ops.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

import numpy as np
import pandas as pd

from .. import config, draft

SECONDS_PER_DAY = 86400.0
ROLES = config.POSITIONS  # top, jng, mid, bot, sup


# --------------------------------------------------------------------------------------
# Champion vocabulary
# --------------------------------------------------------------------------------------
class ChampionIndex:
    def __init__(self, names: Sequence[str]):
        self.names = list(names)
        self.idx = {n: i for i, n in enumerate(self.names)}
        self.C = len(self.names)

    @classmethod
    def load(cls, path=None) -> "ChampionIndex":
        df = pd.read_parquet(path or config.CHAMPIONS_PATH)
        return cls(sorted(df["champion"].tolist()))

    def encode(self, names: Iterable, strict: bool = True) -> tuple[int, ...]:
        out = []
        for n in names:
            if draft.is_missing(n):
                out.append(-1)
            elif n in self.idx:
                out.append(self.idx[n])
            elif strict:
                raise KeyError(f"unknown champion {n!r}")
            else:
                out.append(-1)
        return tuple(out)

    def decode(self, ids: Iterable[int]) -> list:
        return [None if i < 0 else self.names[i] for i in ids]


# --------------------------------------------------------------------------------------
# Game records
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PreGame:
    """Everything known before the draft starts. Contains no outcome information."""

    game_id: str
    series_id: str
    game_in_series: int
    t: float  # start time, days since unix epoch
    patch: str
    league: str
    season: int
    split: str | None
    playoffs: int
    blue_team: str  # team ids
    red_team: str
    blue_players: tuple  # player ids in role order top, jng, mid, bot, sup (starting roster)
    red_players: tuple
    first_pick_side: str  # 'Blue' | 'Red' -- NOT implied by side from 2026 on
    fearless: str  # 'hard' | 'soft' | 'none' | 'na'
    blue_burned: tuple  # champion ids the blue team cannot pick (Fearless)
    red_burned: tuple
    blue_series_wins: int
    red_series_wins: int

    def team(self, side: str) -> str:
        return self.blue_team if side == "Blue" else self.red_team

    def players(self, side: str) -> tuple:
        return self.blue_players if side == "Blue" else self.red_players

    def burned(self, side: str) -> tuple:
        return self.blue_burned if side == "Blue" else self.red_burned

    def series_wins(self, side: str) -> int:
        return self.blue_series_wins if side == "Blue" else self.red_series_wins

    def side_of_slot(self, slot: str) -> str:
        return draft.slot_side(slot, self.first_pick_side)

    @property
    def split_key(self) -> tuple:
        return (self.league, self.season, self.split)

    @property
    def is_international(self) -> bool:
        return self.league in config.INTERNATIONAL_LEAGUES


@dataclass(frozen=True)
class GameRecord:
    """A finished game: pre-game facts plus the draft and the result."""

    pre: PreGame
    t_end: float
    actions: tuple  # 20 champion ids in draft order, -1 = missed ban
    blue_roles: tuple  # champion id by final role top..sup
    red_roles: tuple
    blue_win: int

    def picks(self, side: str) -> tuple:
        """Champion ids picked by ``side`` in that team's pick order."""
        return tuple(
            c for i, c in enumerate(self.actions)
            if draft.ACTION_TYPE[i] == "pick" and self.pre.side_of_slot(draft.ACTION_SLOT[i]) == side
        )

    def bans(self, side: str) -> tuple:
        return tuple(
            c for i, c in enumerate(self.actions)
            if draft.ACTION_TYPE[i] == "ban" and self.pre.side_of_slot(draft.ACTION_SLOT[i]) == side
        )

    def roles(self, side: str) -> tuple:
        return self.blue_roles if side == "Blue" else self.red_roles

    def won(self, side: str) -> int:
        return self.blue_win if side == "Blue" else 1 - self.blue_win


def to_days(ts) -> float:
    return pd.Timestamp(ts).timestamp() / SECONDS_PER_DAY


def records_from_games(games: pd.DataFrame, champs: ChampionIndex) -> list[GameRecord]:
    """Convert games.parquet rows into GameRecords, sorted by start time."""
    out = []
    for g in games.sort_values(["datetime", "game_id"], kind="stable").itertuples():
        if g.first_pick_side not in ("Blue", "Red"):
            continue
        pre = PreGame(
            game_id=g.game_id, series_id=g.series_id, game_in_series=int(g.game_in_series),
            t=to_days(g.datetime), patch=g.patch, league=g.league, season=int(g.oe_year),
            split=None if draft.is_missing(g.split) else g.split, playoffs=int(g.playoffs),
            blue_team=g.blue_team_id, red_team=g.red_team_id,
            blue_players=tuple(g.blue_players), red_players=tuple(g.red_players),
            first_pick_side=g.first_pick_side, fearless=g.fearless,
            blue_burned=champs.encode(g.blue_unavailable_at_start), red_burned=champs.encode(g.red_unavailable_at_start),
            blue_series_wins=int(g.blue_series_wins_before), red_series_wins=int(g.red_series_wins_before),
        )
        per = {"first": g.first_pick_side.lower(), "second": draft.OTHER_SIDE[g.first_pick_side].lower()}
        seq = draft.interleave(
            getattr(g, f"{per['first']}_bans"), getattr(g, f"{per['first']}_picks"),
            getattr(g, f"{per['second']}_bans"), getattr(g, f"{per['second']}_picks"),
        )
        out.append(
            GameRecord(
                pre=pre, t_end=to_days(g.end_datetime), actions=champs.encode(seq),
                blue_roles=champs.encode(g.blue_champs_by_role), red_roles=champs.encode(g.red_champs_by_role),
                blue_win=int(g.winner_side == "Blue"),
            )
        )
    return out


# --------------------------------------------------------------------------------------
# Draft context (the only thing feature code sees)
# --------------------------------------------------------------------------------------
@dataclass
class DraftContext:
    """State of a draft before action ``k = len(actions)`` (k == 20 means draft complete)."""

    pre: PreGame
    actions: tuple = ()
    _picks: dict = field(init=False, repr=False)
    _bans: dict = field(init=False, repr=False)

    def __post_init__(self):
        self._picks = {"Blue": [], "Red": []}
        self._bans = {"Blue": [], "Red": []}
        for i, c in enumerate(self.actions):
            side = self.pre.side_of_slot(draft.ACTION_SLOT[i])
            if c >= 0:
                (self._picks if draft.ACTION_TYPE[i] == "pick" else self._bans)[side].append(c)

    # --- the next action ---
    @property
    def k(self) -> int:
        return len(self.actions)

    @property
    def complete(self) -> bool:
        return self.k >= draft.N_ACTIONS

    @property
    def action_type(self) -> str:
        return draft.ACTION_TYPE[self.k]

    @property
    def is_pick(self) -> bool:
        return draft.ACTION_TYPE[self.k] == "pick"

    @property
    def phase(self) -> str:
        return draft.ACTION_PHASE[self.k]

    @property
    def slot(self) -> str:
        return draft.ACTION_SLOT[self.k]

    @property
    def acting_side(self) -> str:
        return self.pre.side_of_slot(self.slot)

    @property
    def opp_side(self) -> str:
        return draft.OTHER_SIDE[self.acting_side]

    # --- what has happened so far ---
    def picks(self, side: str) -> list:
        return self._picks[side]

    def bans(self, side: str) -> list:
        return self._bans[side]

    def taken(self) -> list:
        return [c for c in self.actions if c >= 0]

    def available_mask(self, C: int) -> np.ndarray:
        """Champions that can be chosen by the next action.

        Excludes everything already picked/banned this game. A pick also excludes the acting
        team's Fearless-burned champions; a ban excludes champions the opponent could not
        pick anyway (banning them is pointless).
        """
        mask = np.ones(C, dtype=bool)
        taken = self.taken()
        if taken:
            mask[taken] = False
        burned = self.pre.burned(self.acting_side if self.is_pick else self.opp_side)
        if burned:
            mask[list(burned)] = False
        return mask

    def advance(self, champion: int) -> "DraftContext":
        return DraftContext(self.pre, self.actions + (champion,))


# --------------------------------------------------------------------------------------
# Time-decayed counters
# --------------------------------------------------------------------------------------
class Decayed:
    """Exponentially time-decayed accumulator (array of any shape).

    ``add(t, idx, val)`` decays the whole array to time ``t`` (days) and then adds.
    ``read(t)`` returns the array decayed to ``t`` without mutating. With
    ``half_life=None`` it is a plain (non-decayed) counter.
    """

    __slots__ = ("v", "t", "lam")

    def __init__(self, shape, half_life: float | None):
        self.v = np.zeros(shape, dtype=np.float64)
        self.t: float | None = None
        self.lam = 0.0 if half_life is None else math.log(2.0) / half_life

    def _factor(self, t: float) -> float:
        if self.t is None or self.lam == 0.0 or t <= self.t:
            return 1.0
        return math.exp(-self.lam * (t - self.t))

    def decay_to(self, t: float) -> None:
        f = self._factor(t)
        if f != 1.0:
            self.v *= f
        if self.t is None or t > self.t:
            self.t = t

    def add(self, t: float, idx, val=1.0) -> None:
        self.decay_to(t)
        if isinstance(idx, (list, tuple)) and (len(idx) == 0 or np.isscalar(idx[0])):
            idx = np.asarray(idx, dtype=np.intp)  # a list/tuple of ids, not a multi-dim index
        if isinstance(idx, (tuple, np.ndarray)):
            np.add.at(self.v, idx, val)  # ndarray of ids, or a tuple of index arrays (one per axis)
        else:
            self.v[idx] += val

    def add_all(self, t: float, val) -> None:
        self.decay_to(t)
        self.v += val

    def read(self, t: float) -> np.ndarray:
        return self.v * self._factor(t)


class DecayedMap:
    """dict of key -> :class:`Decayed`, created on first use."""

    def __init__(self, shape, half_life: float | None):
        self.shape, self.half_life = shape, half_life
        self.d: dict = {}

    def get(self, key) -> Decayed:
        obj = self.d.get(key)
        if obj is None:
            obj = self.d[key] = Decayed(self.shape, self.half_life)
        return obj

    def read(self, key, t: float) -> np.ndarray:
        obj = self.d.get(key)
        return np.zeros(self.shape) if obj is None else obj.read(t)

    def __contains__(self, key) -> bool:
        return key in self.d


def shrink(num: np.ndarray, den: np.ndarray, prior, k: float) -> np.ndarray:
    """Empirical-Bayes style shrinkage of the rate num/den toward ``prior`` with strength k."""
    return (num + k * prior) / (den + k)


# --------------------------------------------------------------------------------------
# Stat blocks and the engine
# --------------------------------------------------------------------------------------
class StatBlock:
    """Base class. Subclasses keep running statistics and expose features.

    Rules for subclasses:
    * ``update`` is the ONLY place state may change, and the only method that sees outcomes.
    * Feature methods are pure reads of state as of ``ctx.pre.t``. ``begin_game`` may cache
      per-game arrays (it is called once per game before any feature call for that game).
    * Feature names must start with ``self.prefix`` (used for SHAP grouping).
    * ``candidate_features`` returns arrays of shape (C,), one value per champion.
    * ``win_features`` returns values oriented BLUE minus RED (or blue-perspective).
    """

    name = "base"
    prefix = "base_"
    engine: "Engine" = None  # set by Engine

    def begin_game(self, pre: PreGame) -> None:
        pass

    def candidate_features(self, ctx: DraftContext) -> dict[str, np.ndarray]:
        return {}

    def context_features(self, ctx: DraftContext) -> dict[str, float]:
        return {}

    def win_features(self, ctx: DraftContext) -> dict[str, float]:
        return {}

    def update(self, g: GameRecord, exp_blue: float) -> None:
        """Apply a finished game. ``exp_blue`` is the Elo expectation of a blue win as of the
        game's start (computed before the game was known)."""


class Engine:
    def __init__(self, champs: ChampionIndex, blocks: Sequence[StatBlock]):
        self.champs = champs
        self.C = champs.C
        self.blocks = list(blocks)
        self.by_name = {}
        for b in self.blocks:
            b.engine = self
            self.by_name[b.name] = b
        self._pending: list = []
        self._seq = 0
        self.n_applied = 0
        self.t_now = -math.inf

    def block(self, name: str) -> StatBlock:
        return self.by_name[name]

    # --- time ---
    def advance(self, t: float) -> None:
        """Apply every pending game that finished at or before ``t``."""
        while self._pending and self._pending[0][0] <= t:
            _, _, g, exp_blue = heapq.heappop(self._pending)
            for b in self.blocks:
                b.update(g, exp_blue)
            self.n_applied += 1
        self.t_now = max(self.t_now, t)

    def flush(self) -> None:
        """Apply every pending game regardless of time (end of data / live snapshot)."""
        t_last = max((item[0] for item in self._pending), default=self.t_now)
        self.advance(t_last)

    def replay(self, games: Iterable[GameRecord]) -> None:
        """Feed finished games through the engine without computing features (fast)."""
        for g in sorted(games, key=lambda r: (r.pre.t, r.pre.game_id)):
            self.advance(g.pre.t)
            self.submit(g)

    def begin_game(self, pre: PreGame) -> None:
        self.advance(pre.t)
        for b in self.blocks:
            b.begin_game(pre)

    def expected_blue(self, pre: PreGame) -> float:
        elo = self.by_name.get("elo")
        return 0.5 if elo is None else elo.expected_blue(pre)

    def submit(self, g: GameRecord) -> None:
        """Queue a game whose features have been computed; it is applied once finished."""
        exp_blue = self.expected_blue(g.pre)
        heapq.heappush(self._pending, (g.t_end, self._seq, g, exp_blue))
        self._seq += 1

    def process(self, games: Iterable[GameRecord], visit: Callable[[GameRecord, "Engine"], None] | None = None) -> None:
        """Walk games chronologically. ``visit`` is called with state as-of the game start."""
        for g in sorted(games, key=lambda r: (r.pre.t, r.pre.game_id)):
            self.begin_game(g.pre)
            if visit is not None:
                visit(g, self)
            self.submit(g)

    # --- features ---
    def candidate_parts(self, ctx: DraftContext) -> tuple[list[str], np.ndarray, list[str], np.ndarray]:
        """Features for the next action of ``ctx``.

        Returns (candidate feature names, float32 (C, Fc), context feature names, float32 (Fx,)).
        Context features are identical for every candidate, so they are kept separate.
        """
        cnames, cols, xnames, xvals = [], [], [], []
        for b in self.blocks:
            for n, v in b.candidate_features(ctx).items():
                cnames.append(n)
                cols.append(np.asarray(v, dtype=np.float32))
            for n, v in b.context_features(ctx).items():
                xnames.append(n)
                xvals.append(v)
        return cnames, np.stack(cols, axis=1), xnames, np.asarray(xvals, dtype=np.float32)

    def candidate_matrix(self, ctx: DraftContext) -> tuple[list[str], np.ndarray]:
        """(feature names, float32 (C, F)): candidate features followed by broadcast context."""
        cnames, xc, xnames, xx = self.candidate_parts(ctx)
        return cnames + xnames, np.hstack([xc, np.broadcast_to(xx, (self.C, len(xx)))])

    def win_vector(self, ctx: DraftContext) -> dict[str, float]:
        out: dict[str, float] = {}
        for b in self.blocks:
            out.update(b.win_features(ctx))
        return out
