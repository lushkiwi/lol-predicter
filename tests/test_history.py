"""History-only games: they feed the configured stat blocks and nothing else."""
import dataclasses
import random

import numpy as np
import pandas as pd

from draft_analyzer import config
from draft_analyzer.features import default_blocks
from draft_analyzer.features.asof import DraftContext, Engine, StatBlock, records_from_games
from draft_analyzer.ingest import oracles_elixir as oe

from . import synth


class Counter(StatBlock):
    def __init__(self, name, needs_draft=True):
        self.name, self.prefix, self.needs_draft = name, f"{name}_", needs_draft
        self.seen = []

    def update(self, g, exp_blue):
        self.seen.append(g.pre.game_id)


def as_history(g, draft_known=True):
    return dataclasses.replace(g, history=True, draft_known=draft_known)


def test_history_games_update_only_history_blocks_and_are_never_visited():
    rng = random.Random(0)
    a = synth.make_game(rng, "a", "SA", 1, 100.0, "T0", "T1")
    h = as_history(synth.make_game(rng, "h", "SH", 1, 100.2, "A0", "A1", league="LCKC"))
    h_nodraft = as_history(synth.make_game(rng, "hn", "SN", 1, 100.3, "A2", "A3", league="LDL"), draft_known=False)
    b = synth.make_game(rng, "b", "SB", 1, 101.0, "T0", "T1")
    with_draft, without_draft, excluded = Counter("x"), Counter("y", needs_draft=False), Counter("z")
    eng = Engine(synth.champs(), [with_draft, without_draft, excluded], history_blocks=("x", "y"))
    visited = []
    eng.process([b, h_nodraft, h, a], lambda g, e: visited.append(g.pre.game_id))
    eng.flush()
    assert visited == ["a", "b"]
    assert with_draft.seen == ["a", "h", "b"]
    assert without_draft.seen == ["a", "h", "hn", "b"]
    assert excluded.seen == ["a", "b"]


def featurise(games, history_blocks):
    ch = synth.champs()
    eng = Engine(ch, default_blocks(ch), history_blocks=history_blocks)
    out, names = {}, {}

    def visit(g, e):
        n, X = e.candidate_matrix(DraftContext(g.pre, g.actions[:6]))
        names["n"] = n
        out[g.pre.game_id] = (X, e.win_vector(DraftContext(g.pre, g.actions)))

    eng.process(games, visit)
    return out, names["n"]


def test_history_reaches_player_features_only_and_only_once_finished():
    season = synth.make_season(seed=3, n_series=12)
    target = season[-1]
    rng = random.Random(7)
    rookies = target.pre.blue_players  # the target's blue players have an earlier lower-tier game
    def lower_tier(game_id, t, length):
        g = synth.make_game(rng, game_id, f"S_{game_id}", 1, t, "Academy A", "Academy B", league="LCKC", length_days=length)
        return as_history(dataclasses.replace(g, pre=dataclasses.replace(g.pre, blue_players=rookies)))

    finished = lower_tier("h_done", target.pre.t - 5.0, 0.02)
    unfinished = lower_tier("h_running", target.pre.t - 0.01, 0.5)  # still being played when the target starts

    base, names = featurise(season, ("elo", "player"))
    late, _ = featurise(season + [unfinished], ("elo", "player"))
    seen, _ = featurise(season + [finished], ("elo", "player"))
    off, _ = featurise(season + [finished], ())

    gid = target.pre.game_id
    np.testing.assert_array_equal(base[gid][0], late[gid][0], err_msg="an unfinished history game leaked")
    assert base[gid][1] == late[gid][1]
    np.testing.assert_array_equal(base[gid][0], off[gid][0], err_msg="history used although no block asked for it")

    changed = {n for j, n in enumerate(names) if not np.array_equal(base[gid][0][:, j], seen[gid][0][:, j])}
    assert changed, "a finished history game of the same players changed nothing"
    assert all(n.startswith("player_") for n in changed), sorted(changed)


def _raw_game(gid, league, year, date, first_pick=True):
    rows = []
    for s, side in enumerate(("Blue", "Red")):
        champs = [f"C{s}{i}" for i in range(5)]
        common = dict(gameid=gid, datacompleteness="complete", league=league, year=year, split="Spring", playoffs=0,
                      date=date, game=1, patch="16.01", side=side, teamname=f"{league} team {s}", teamid=f"{league}_t{s}",
                      gamelength=1800, result=1 - s)
        for i, pos in enumerate(config.POSITIONS):
            rows.append({**common, "participantid": s * 5 + i + 1, "position": pos, "playername": f"{league}p{s}{i}",
                         "playerid": f"{league}_p{s}{i}", "champion": champs[i], "firstPick": np.nan})
        team = {**common, "participantid": 100 * (s + 1), "position": "team", "playername": None, "playerid": None,
                "champion": None, "firstPick": (1 - s) if first_pick else np.nan}
        team.update({f"ban{i + 1}": f"B{s}{i}" for i in range(5)})
        team.update({f"pick{i + 1}": champs[i] for i in range(5)})
        rows.append(team)
    return rows


def test_scope_split_and_history_records(monkeypatch):
    raw = pd.DataFrame(
        _raw_game("g_lck", "LCK", 2026, "2026-02-01 10:00:00")
        + _raw_game("g_promo", "LCS", 2027, "2026-10-05 10:00:00")
        + _raw_game("g_cl", "LCKC", 2026, "2026-02-02 10:00:00")
        + _raw_game("g_ldl", "LDL", 2024, "2024-02-02 10:00:00", first_pick=False)
    )
    games, _ = oe.build_games(raw)
    scope = oe.in_scope_mask(games)
    assert set(games.game_id[scope]) == {"g_lck"}
    assert set(games.game_id[~scope]) == {"g_promo", "g_cl", "g_ldl"}

    names = sorted({c for col in ("blue_bans", "blue_picks", "red_bans", "red_picks") for cs in games[col] for c in cs})
    ch = synth.ChampionIndex(names)
    hist = games[~scope]
    recs = {r.pre.game_id: r for r in records_from_games(hist, ch, history=True)}
    assert set(recs) == {"g_promo", "g_cl", "g_ldl"} and all(r.history for r in recs.values())
    assert recs["g_cl"].draft_known and len(recs["g_cl"].actions) == 20
    assert not recs["g_ldl"].draft_known and recs["g_ldl"].actions == ()
    assert recs["g_ldl"].blue_roles == ch.encode([f"C0{i}" for i in range(5)])
