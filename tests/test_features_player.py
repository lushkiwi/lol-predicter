import dataclasses
import math

import numpy as np

from draft_analyzer import draft
from draft_analyzer.features.asof import ChampionIndex, DraftContext, Engine, GameRecord, PreGame
from draft_analyzer.features.elo import EloBlock
from draft_analyzer.features.roles import RoleBlock
from draft_analyzer.features.player import PlayerMasteryBlock

CH = ChampionIndex([f"c{i}" for i in range(30)])


def mk(gid, t, blue_team="A", red_team="B", bpicks=(1, 2, 3, 4, 5), rpicks=(6, 7, 8, 9, 10),
       bbans=(11, 12, 13, 14, 15), rbans=(16, 17, 18, 19, 20), first="Blue", series="s", gis=1,
       win=1, dur=0.05, burned=((), ()), players=None):
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
    if players is not None:
        pre = dataclasses.replace(pre, blue_players=players[0], red_players=players[1])
    return GameRecord(pre=pre, t_end=t + dur, actions=tuple(seq), blue_roles=tuple(bpicks),
                      red_roles=tuple(rpicks), blue_win=win)


def engine(**kw):
    return Engine(CH, [EloBlock(), RoleBlock(CH.C), PlayerMasteryBlock(CH, **kw)])


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


def test_hand_computed():
    D = 180.0  # one half-life for games and for the role block
    g1 = mk("g1", 100.0)
    g2 = mk("g2", 100.0 + D, bbans=(2, 12, 13, 14, 15), bpicks=(21, 22, 23, 24, 25), burned=((1,), ()))
    res = run([g1, g2], ks=(0, 6, 9, 20), pool_min_games=0.4)
    f = lambda k, n: col(res, "g2", k, n)
    pt = (0.5 + 0.2) / (0.5 + 1.0)  # P(top | c1) after one decayed game
    assert math.isclose(f(0, "player_games_own")[1], math.log1p(pt * 0.5), rel_tol=1e-6)
    assert math.isclose(f(0, "player_games_own_max")[1], math.log1p(0.5), rel_tol=1e-6)
    assert math.isclose(f(0, "player_career_own")[1], math.log1p(pt * 1.0), rel_tol=1e-6)
    assert math.isclose(f(0, "player_share_own")[1], pt * 0.5 / (0.5 + 5.0), rel_tol=1e-6)
    assert math.isclose(f(0, "player_win_resid_own")[1], pt * 0.25 / 8.5, rel_tol=1e-6)
    # opponent B: champion 6 played top by B0, lost (resid -0.5)
    assert math.isclose(f(0, "player_win_resid_opp")[6], pt * -0.25 / 8.5, rel_tol=1e-6)
    assert math.isclose(f(0, "player_games_opp")[6], math.log1p(pt * 0.5), rel_tol=1e-6)
    # pool: A has 5 champs (1..5), champion 1 burned -> 4; B has 5
    assert f(0, "player_pool_left_own")[0] == 4.0 and f(0, "player_pool_left_opp")[0] == 5.0
    # k=6: six bans taken (blue banned 2) -> own pool 3; opp: red's champs 6..10 untouched by bans 2,12..15,16..20? 16..20 not in pool -> 5
    assert f(6, "player_pool_left_own")[0] == 3.0 and f(6, "player_pool_left_opp")[0] == 5.0
    # nothing known about a never-played champion
    assert f(0, "player_games_own")[29] == 0.0 and f(0, "player_share_own")[29] == 0.0
    # win features: k=0 exactly zero; k=9 blue has pick 21? -> picks are g2's (21,22,23); use no-history champs
    assert res["g2"][("win", 0)]["player_mastery_sum_diff"] == 0.0
    assert res["g2"][("win", 0)]["player_win_resid_sum_diff"] == 0.0


def test_win_features_hand_computed():
    g1 = mk("g1", 100.0)
    g2 = mk("g2", 100.2)  # same drafts: k=9 -> blue picks [1], red picks [6, 7]
    res = run([g1, g2], ks=(0, 9))
    w = res["g2"][("win", 9)]
    d = 0.5 ** (0.2 / 180)
    assert math.isclose(w["player_mastery_sum_diff"], math.log1p(d) - 2 * math.log1p(d), rel_tol=1e-6)
    assert math.isclose(w["player_win_resid_sum_diff"], 3 * (0.5 * d) / (d + 8.0), rel_tol=1e-6)


def test_leakage():
    base = [mk("g1", 100.0), mk("g2", 110.0, blue_team="B", red_team="A", win=0), mk("g3", 120.0)]
    alt = [base[0], base[1], mk("g3", 120.0, bpicks=(20, 21, 22, 23, 24), bbans=(1, 2, 3, 4, 5), win=0),
           mk("g4", 125.0, bpicks=(20, 21, 22, 23, 24))]
    a, b = run(base)["g3"], run(alt)["g3"]
    assert np.array_equal(a[0][1], b[0][1])
    assert a[("win", 0)] == b[("win", 0)]
    c = run([mk("g1", 100.0, win=0), base[1], base[2]])["g3"]
    assert not np.array_equal(a[0][1], c[0][1])  # earlier outcome matters (resid)


def test_series_visibility_same_day():
    g1 = mk("g1", 100.00, series="x", gis=1, dur=0.03)
    g2 = mk("g2", 100.05, series="x", gis=2)
    assert col(run([g1, g2]), "g2", 0, "player_games_own_max")[1] > 0.6
    g1b = mk("g1", 100.00, series="x", gis=1, dur=0.03, bpicks=(20, 21, 22, 23, 24))
    assert col(run([g1b, g2]), "g2", 0, "player_games_own_max")[1] == 0.0
    g1c = mk("g1", 100.00, series="x", dur=0.2)
    assert np.all(col(run([g1c, g2]), "g2", 0, "player_games_own_max") == 0)


def test_red_first_pick_orientation():
    g1 = mk("g1", 100.0)
    g2 = mk("g2", 110.0, blue_team="B", red_team="A", first="Red")
    res = run([g1, g2], ks=(0, 1))
    assert col(res, "g2", 0, "player_games_own_max")[1] > 0 and col(res, "g2", 0, "player_games_own_max")[6] == 0
    assert col(res, "g2", 0, "player_games_opp_max")[6] > 0
    assert col(res, "g2", 1, "player_games_own_max")[6] > 0 and col(res, "g2", 1, "player_games_own_max")[1] == 0


def test_role_weighting_flex():
    # champion 1 played top by A0 only; with top already filled for A's picks, weight shifts away
    g1 = mk("g1", 100.0)
    g2 = mk("g2", 100.2)
    res = run([g1, g2], ks=(0, 9))
    own0 = col(res, "g2", 0, "player_games_own")[1]
    # at k=9 red (second) acts on pick idx 9; red picked 6,7 so far -> weights use red's open roles
    assert own0 > 0
    assert col(res, "g2", 9, "player_games_own_max")[1] >= 0


def test_missing_players_and_champs_skipped():
    g1 = mk("g1", 100.0, players=((None, "A1", float("nan"), "A3", "A4"), tuple(f"B{i}" for i in range(5))),
            bpicks=(-1, 2, 3, 4, 5))
    g2 = mk("g2", 101.0, players=((None, "A1", "A2", "A3", "A4"), tuple(f"B{i}" for i in range(5))))
    res = run([g1, g2], ks=(0, 5, 20))
    m = col(res, "g2", 0, "player_games_own_max")
    assert m[3] == 0.0 and m[1] == 0.0 and m[2] > 0 and m[4] > 0 and np.all(np.isfinite(m))


def test_shapes_names_finite():
    recs = [mk("g1", 100.0), mk("g2", 101.0, first="Red"), mk("g3", 130.0, bbans=(-1, 12, 13, 14, 15))]
    res = run(recs, ks=(0, 5, 11, 19, 20))
    for gid, d in res.items():
        for k, v in d.items():
            if isinstance(k, tuple):
                assert np.isfinite(v["player_mastery_sum_diff"]) and np.isfinite(v["player_win_resid_sum_diff"])
            else:
                names, X = v
                assert X.shape[0] == CH.C and np.all(np.isfinite(X))
    names = res["g3"][0][0]
    assert sum(n.startswith("player_") for n in names) == 12
