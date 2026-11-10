import math

import numpy as np

from draft_analyzer import draft
from draft_analyzer.features.asof import ChampionIndex, DraftContext, Engine, GameRecord, PreGame
from draft_analyzer.features.elo import EloBlock
from draft_analyzer.features.roles import RoleBlock
from draft_analyzer.features.team import TeamTendencyBlock

CH = ChampionIndex([f"c{i}" for i in range(30)])


def mk(gid, t, blue_team="A", red_team="B", bpicks=(1, 2, 3, 4, 5), rpicks=(6, 7, 8, 9, 10),
       bbans=(11, 12, 13, 14, 15), rbans=(16, 17, 18, 19, 20), first="Blue", series="s", gis=1,
       win=1, dur=0.05, burned=((), ())):
    if first == "Blue":
        seq = draft.interleave(bbans, bpicks, rbans, rpicks)
    else:
        seq = draft.interleave(rbans, rpicks, bbans, bpicks)
    pre = PreGame(game_id=gid, series_id=series, game_in_series=gis, t=t, patch="1", league="L", season=2025,
                  split="s", playoffs=0, blue_team=blue_team, red_team=red_team,
                  blue_players=tuple(f"{blue_team}{i}" for i in range(5)),
                  red_players=tuple(f"{red_team}{i}" for i in range(5)), first_pick_side=first,
                  fearless="hard", blue_burned=burned[0], red_burned=burned[1],
                  blue_series_wins=0, red_series_wins=0)
    return GameRecord(pre=pre, t_end=t + dur, actions=tuple(seq), blue_roles=tuple(bpicks),
                      red_roles=tuple(rpicks), blue_win=win)


def engine(**kw):
    return Engine(CH, [EloBlock(), RoleBlock(CH.C), TeamTendencyBlock(CH, **kw)])


def run(recs, ks=(0, 6, 20), **kw):
    """-> {game_id: {k: (names, X)}} plus win vectors under key ('win', k)."""
    eng = engine(**kw)
    out = {}

    def visit(g, e):
        d = out.setdefault(g.pre.game_id, {})
        for k in ks:
            ctx = DraftContext(g.pre, g.actions[:k])
            if k < 20:
                d[k] = e.candidate_matrix(ctx)
            d[("win", k)] = e.win_vector(ctx)

    eng.process(recs, visit)
    return out


def col(res, gid, k, name):
    names, X = res[gid][k]
    return X[:, names.index(name)]


def test_hand_computed_rates():
    # A (blue) in g1 picks 1..5, bans 11..15; B bans 16..20 against A. g2 is 60 days later (decay 0.5).
    g1 = mk("g1", 100.0)
    g2 = mk("g2", 160.0, blue_team="A", red_team="B")
    res = run([g1, g2])
    # k=0 -> acting is the first-pick side = Blue = A.
    f = lambda n, c: col(res, "g2", 0, n)[c]
    assert math.isclose(f("team_pick_rate_own", 1), 0.5 / (0.5 + 3), rel_tol=1e-6)
    assert math.isclose(f("team_pick_rate_opp", 6), 0.5 / 3.5, rel_tol=1e-6)
    assert math.isclose(f("team_ban_rate_own", 11), 0.5 / 3.5, rel_tol=1e-6)
    assert math.isclose(f("team_ban_rate_opp", 16), 0.5 / 3.5, rel_tol=1e-6)
    assert math.isclose(f("team_banned_against_own", 16), 0.5 / 3.5, rel_tol=1e-6)   # B banned 16 against A
    assert math.isclose(f("team_banned_against_opp", 11), 0.5 / 3.5, rel_tol=1e-6)   # A banned 11 against B
    gr = 0.5 ** (60 / 14)
    assert math.isclose(f("team_pick_rate_own_recent", 1), gr / (gr + 3), rel_tol=1e-6)
    assert f("team_pick_rate_own", 7) == 0.0
    gl = 0.5 ** (60 / 365)
    assert math.isclose(col(res, "g2", 0, "team_games_own")[0], math.log1p(gl), rel_tol=1e-6)
    # game 1 itself: no history
    assert np.all(col(res, "g1", 0, "team_pick_rate_own") == 0)
    assert col(res, "g1", 0, "team_games_own")[0] == 0.0


def test_comfort_sum_diff():
    g1 = mk("g1", 100.0)
    g2 = mk("g2", 100.2, bpicks=(1, 2, 20, 21, 22), rpicks=(6, 7, 8, 9, 10))
    res = run([g1, g2], ks=(0, 9, 20))
    assert res["g2"][("win", 0)]["team_comfort_sum_diff"] == 0.0
    # after k=9 (6 bans + F,S,S picks): blue has 1 pick, red has 2.
    w = res["g2"][("win", 9)]["team_comfort_sum_diff"]
    r = 1.0 / (1.0 + 3.0)  # one game of history at decay ~1 (0.2 days)
    # blue's first pick is action 6 of g2 = bpicks[0]=1 -> rate ~r ; red picks 6,7 -> ~2r
    assert abs(w - (r - 2 * r)) < 0.01


def test_leakage():
    base = [mk("g1", 100.0), mk("g2", 110.0, blue_team="B", red_team="A", win=0), mk("g3", 120.0)]
    alt = [base[0], base[1], mk("g3", 120.0, bpicks=(20, 21, 22, 23, 24), bbans=(1, 2, 3, 4, 5), win=0),
           mk("g4", 125.0, bpicks=(20, 21, 22, 23, 24))]
    a = run(base)["g3"]
    b = run(alt)["g3"]
    # k=0 (no actions yet) must be identical: only history may matter
    for key in (0, ("win", 0)):
        if isinstance(key, tuple):
            assert a[key]["team_comfort_sum_diff"] == b[key]["team_comfort_sum_diff"]
        else:
            assert np.array_equal(a[key][1], b[key][1])
    # and the earlier game changing does matter
    c = run([mk("g1", 100.0, bpicks=(20, 21, 22, 23, 24)), base[1], base[2]])["g3"]
    assert not np.array_equal(a[0][1], c[0][1])


def test_series_visibility_same_day():
    g1 = mk("g1", 100.00, series="x", gis=1, dur=0.03)
    g2 = mk("g2", 100.05, series="x", gis=2)
    res = run([g1, g2])
    assert col(res, "g2", 0, "team_pick_rate_own")[1] > 0.2
    g1b = mk("g1", 100.00, series="x", gis=1, dur=0.03, bpicks=(20, 21, 22, 23, 24))
    res2 = run([g1b, g2])
    assert col(res2, "g2", 0, "team_pick_rate_own")[1] == 0.0
    # game still in progress is NOT visible
    g1c = mk("g1", 100.00, series="x", dur=0.2)
    assert np.all(col(run([g1c, g2]), "g2", 0, "team_pick_rate_own") == 0)


def test_red_first_pick_orientation():
    g1 = mk("g1", 100.0)  # A blue picks 1..5; B red picks 6..10
    g2 = mk("g2", 110.0, blue_team="B", red_team="A", first="Red")
    res = run([g1, g2], ks=(0, 1, 2))
    # k=0: red (A) acts first -> own = A, opp = B
    assert col(res, "g2", 0, "team_pick_rate_own")[1] > 0
    assert col(res, "g2", 0, "team_pick_rate_opp")[6] > 0
    assert col(res, "g2", 0, "team_pick_rate_own")[6] == 0
    # k=1: blue (B) acts -> flipped
    assert col(res, "g2", 1, "team_pick_rate_own")[6] > 0
    assert col(res, "g2", 1, "team_pick_rate_opp")[1] > 0
    # games context
    assert col(res, "g2", 0, "team_games_own")[0] > 0


def test_shapes_names_finite():
    recs = [mk("g1", 100.0), mk("g2", 101.0, first="Red"), mk("g3", 130.0, bbans=(-1, 12, 13, 14, 15))]
    res = run(recs, ks=(0, 5, 11, 19, 20))
    for gid, d in res.items():
        for k, v in d.items():
            if isinstance(k, tuple):
                assert np.isfinite(v["team_comfort_sum_diff"])
            else:
                names, X = v
                assert X.shape[0] == CH.C and np.all(np.isfinite(X))
                assert [n for n in names if n.startswith("team_")]
    names = res["g3"][0][0]
    assert sum(n.startswith("team_") for n in names) == 10
