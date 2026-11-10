"""Draft sequence, availability, series reconstruction and Fearless logic.

The 20-action professional draft order is encoded ONCE here, in terms of *draft slot*
("first" = the team with first pick, "second" = the other team). Side (blue/red) is
deliberately absent: from the 2026 season the First Selection rule lets the red side draft
first, so nothing in this module may derive order from side.
"""
from __future__ import annotations

from typing import Iterable, Sequence

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------------------
# The draft sequence (single source of truth)
# --------------------------------------------------------------------------------------
N_ACTIONS = 20
#            ban phase 1 | pick phase 1 | ban phase 2 | pick phase 2
_SLOT_CODE = "FSFSFS" + "FSSFFS" + "SFSF" + "SFFS"
_TYPE_CODE = "BBBBBB" + "PPPPPP" + "BBBB" + "PPPP"
_PHASES = ["ban1"] * 6 + ["pick1"] * 6 + ["ban2"] * 4 + ["pick2"] * 4

ACTION_SLOT: tuple[str, ...] = tuple("first" if c == "F" else "second" for c in _SLOT_CODE)
ACTION_TYPE: tuple[str, ...] = tuple("ban" if c == "B" else "pick" for c in _TYPE_CODE)
ACTION_PHASE: tuple[str, ...] = tuple(_PHASES)


def _ordinals() -> tuple[int, ...]:
    """For each action: 0-based index among the acting slot's own bans (or picks)."""
    seen: dict[tuple[str, str], int] = {}
    out = []
    for slot, typ in zip(ACTION_SLOT, ACTION_TYPE):
        out.append(seen.get((slot, typ), 0))
        seen[(slot, typ)] = out[-1] + 1
    return tuple(out)


ACTION_ORDINAL: tuple[int, ...] = _ordinals()
assert len(ACTION_SLOT) == len(ACTION_TYPE) == N_ACTIONS

OTHER_SIDE = {"Blue": "Red", "Red": "Blue"}
OTHER_SLOT = {"first": "second", "second": "first"}


def slot_side(slot: str, first_pick_side: str) -> str:
    """Side of the team occupying ``slot`` given which side has first pick."""
    return first_pick_side if slot == "first" else OTHER_SIDE[first_pick_side]


def interleave(
    first_bans: Sequence, first_picks: Sequence, second_bans: Sequence, second_picks: Sequence
) -> list:
    """Merge per-team ordered bans/picks (5 each; None = missed) into the 20-action sequence."""
    src = {
        ("first", "ban"): list(first_bans),
        ("first", "pick"): list(first_picks),
        ("second", "ban"): list(second_bans),
        ("second", "pick"): list(second_picks),
    }
    for k, v in src.items():
        if len(v) != 5:
            raise ValueError(f"{k} must have exactly 5 entries (use None for missed), got {len(v)}")
    return [src[(ACTION_SLOT[i], ACTION_TYPE[i])][ACTION_ORDINAL[i]] for i in range(N_ACTIONS)]


def deinterleave(actions: Sequence) -> dict[tuple[str, str], list]:
    """Inverse of :func:`interleave`. Accepts a partial sequence (prefix of the 20 actions)."""
    out: dict[tuple[str, str], list] = {
        ("first", "ban"): [], ("first", "pick"): [], ("second", "ban"): [], ("second", "pick"): [],
    }
    for i, champ in enumerate(actions):
        out[(ACTION_SLOT[i], ACTION_TYPE[i])].append(champ)
    return out


def is_missing(x) -> bool:
    return x is None or (isinstance(x, float) and np.isnan(x)) or x is pd.NA


# --------------------------------------------------------------------------------------
# Series reconstruction
# --------------------------------------------------------------------------------------
SERIES_MAX_GAP_HOURS = 30.0


def assign_series(games: pd.DataFrame) -> pd.DataFrame:
    """Group games into series.

    ``games`` needs: game_id, datetime, league, blue_team_id, red_team_id, game_in_series.
    A game joins the previous game's series when it is between the same two teams in the same
    league, its game number is exactly previous + 1, and it started within
    ``SERIES_MAX_GAP_HOURS`` of the previous game. Otherwise it opens a new series; if such a
    game is not numbered 1 the series is flagged ``series_incomplete`` (earlier games are
    missing from the data, so e.g. Fearless-burned champions are unknown).

    Returns a frame indexed like ``games`` with ``series_id`` and ``series_incomplete``.
    """
    order = games.sort_values(["datetime", "game_id"], kind="stable")
    last: dict[tuple, dict] = {}
    series_ids, incomplete = {}, {}
    for row in order.itertuples():
        key = (row.league, frozenset((row.blue_team_id, row.red_team_id)))
        prev = last.get(key)
        gap_h = (row.datetime - prev["dt"]).total_seconds() / 3600 if prev else None
        if prev and row.game_in_series == prev["n"] + 1 and gap_h <= SERIES_MAX_GAP_HOURS:
            sid, inc = prev["sid"], prev["inc"]
        else:
            sid = f"S_{row.game_id}"
            inc = row.game_in_series != 1
        last[key] = {"sid": sid, "n": row.game_in_series, "dt": row.datetime, "inc": inc}
        series_ids[row.Index] = sid
        incomplete[row.Index] = inc
    return pd.DataFrame({"series_id": pd.Series(series_ids), "series_incomplete": pd.Series(incomplete)}).reindex(
        games.index
    )


# --------------------------------------------------------------------------------------
# Fearless
# --------------------------------------------------------------------------------------
FEARLESS_TOL = 0.02  # tolerated repeat rate (data glitches) before a rule is rejected
FEARLESS_MIN_OPPORTUNITIES = 4


def series_pick_history(games: pd.DataFrame) -> pd.DataFrame:
    """Per game: champions picked earlier in the same series, by own team and by opponent.

    Needs: series_id, datetime, blue_team_id, red_team_id, blue_picks, red_picks.
    Returns columns blue_prev_own, blue_prev_opp, red_prev_own, red_prev_opp (sorted lists).
    """
    out = {}
    for _, grp in games.sort_values(["datetime", "game_id"], kind="stable").groupby("series_id", sort=False):
        picked: dict[str, set] = {}
        for row in grp.itertuples():
            b, r = row.blue_team_id, row.red_team_id
            out[row.Index] = {
                "blue_prev_own": sorted(picked.get(b, set())),
                "blue_prev_opp": sorted(picked.get(r, set())),
                "red_prev_own": sorted(picked.get(r, set())),
                "red_prev_opp": sorted(picked.get(b, set())),
            }
            picked.setdefault(b, set()).update(c for c in row.blue_picks if not is_missing(c))
            picked.setdefault(r, set()).update(c for c in row.red_picks if not is_missing(c))
    return pd.DataFrame.from_dict(out, orient="index").reindex(games.index)


def classify_fearless(own_repeat_rate: float, opp_repeat_rate: float, n: int) -> str:
    """Classify a ruleset from empirical repeat rates.

    'hard'  : nobody re-picks anything picked earlier in the series by either team
    'soft'  : a team never re-picks its own earlier champions, but does take the opponent's
    'none'  : teams re-pick their own champions
    'na'    : not enough games 2+ to tell (e.g. best-of-1 stages) -- no series state anyway
    """
    if n < FEARLESS_MIN_OPPORTUNITIES:
        return "na"
    if own_repeat_rate <= FEARLESS_TOL and opp_repeat_rate <= FEARLESS_TOL:
        return "hard"
    if own_repeat_rate <= FEARLESS_TOL:
        return "soft"
    return "none"


def detect_fearless(games: pd.DataFrame, group_cols: Iterable[str]) -> tuple[pd.Series, pd.DataFrame]:
    """Detect the Fearless mode empirically per group (e.g. league/season/split/playoffs).

    Needs the columns of :func:`series_pick_history` output plus blue_picks/red_picks,
    game_in_series, series_incomplete. Returns (per-game mode Series, per-group summary).
    """
    group_cols = list(group_cols)
    rows = []
    for row in games.itertuples():
        if row.game_in_series < 2 or row.series_incomplete:
            continue
        for side in ("blue", "red"):
            picks = {c for c in getattr(row, f"{side}_picks") if not is_missing(c)}
            rows.append(
                {
                    **{c: getattr(row, c) for c in group_cols},
                    "own_repeat": bool(picks & set(getattr(row, f"{side}_prev_own"))),
                    "opp_repeat": bool(picks & set(getattr(row, f"{side}_prev_opp"))),
                }
            )
    if rows:
        opp = pd.DataFrame(rows)
        summary = (
            opp.groupby(group_cols, dropna=False)
            .agg(team_games=("own_repeat", "size"), own_repeat_rate=("own_repeat", "mean"), opp_repeat_rate=("opp_repeat", "mean"))
            .reset_index()
        )
    else:
        summary = pd.DataFrame(columns=group_cols + ["team_games", "own_repeat_rate", "opp_repeat_rate"])
    summary["fearless"] = [
        classify_fearless(o, p, n) for o, p, n in zip(summary.own_repeat_rate, summary.opp_repeat_rate, summary.team_games)
    ]
    mode = games[group_cols].merge(summary[group_cols + ["fearless"]], on=group_cols, how="left")["fearless"]
    mode.index = games.index
    return mode.astype(object).fillna("na"), summary


def burned_for_team(mode: str, prev_own: Sequence[str], prev_opp: Sequence[str]) -> list[str]:
    """Champions a team cannot pick at the start of a game because of Fearless."""
    if mode == "hard":
        return sorted(set(prev_own) | set(prev_opp))
    if mode == "soft":
        return sorted(set(prev_own))
    return []


def unavailable(actions_so_far: Sequence, burned: Iterable[str] = ()) -> set[str]:
    """Champions unavailable for the next action: already picked/banned this game, or burned.

    ``burned`` should be the acting team's Fearless-burned list for pick actions. For ban
    actions pass the *opponent's* burned list when you want to exclude pointless bans.
    """
    used = {c for c in actions_so_far if not is_missing(c)}
    return used | set(burned)
