import math

import numpy as np
import pandas as pd

from draft_analyzer import draft
from draft_analyzer.features.asof import ChampionIndex, DraftContext, Engine, GameRecord, PreGame
from draft_analyzer.features.champion import ChampionMetaBlock
from draft_analyzer.features.elo import EloBlock
from draft_analyzer.features.roles import RoleBlock

CH = ChampionIndex([f"c{i}" for i in range(30)])
ATTRS = pd.DataFrame({
    "champion": ["c0", "c1", "zz_not_in_index"],
    "tag_Fighter": [True, False, True], "tag_Tank": [False, True, False], "tag_Mage": [False, False, True],
    "tag_Assassin": [False, False, False], "tag_Marksman": [True, False, False], "tag_Support": [False, True, False],
    "stat_attackrange": [550, 125, 600],
})
NO_DECAY = dict(hl_long=None, hl_recent=None, hl_league=None)


def mk(gid, t, blue_team="A", red_team="B", bpicks=(1, 2, 3, 4, 5), rpicks=(6, 7, 8, 9, 10),
       bbans=(11, 12, 13, 14, 15), rbans=(16, 17, 18, 19, 20), first="Blue", series=None, gis=1,
       win=1, dur=0.05, burned=((), ()), patch="1", league="L"):
    if first == "Blue":
        seq = draft.interleave(bbans, bpicks, rbans, rpicks)
    else:
        seq = draft.interleave(rbans, rpicks, bbans, bpicks)
    pre = PreGame(game_id=gid, series_id=series or gid, game_in_series=gis, t=t, patch=patch, league=league,
                  season=2025, split="s", playoffs=0, blue_team=blue_team, red_team=red_team,
                  blue_players=tuple(f"{blue_team}{i}" for i in range(5)),
                  red_players=tuple(f"{red_team}{i}" for i in range(5)), first_pick_side=first,
                  fearless="hard", blue_burned=tuple(burned[0]), red_burned=tuple(burned[1]),
                  blue_series_wins=0, red_series_wins=0)
    return GameRecord(pre=pre, t_end=t + dur, actions=tuple(seq), blue_roles=tuple(bpicks),
                      red_roles=tuple(rpicks), blue_win=win)


def run(recs, ks=(0, 1, 6, 7, 8, 20), fixed_actions=None, **kw):
    """-> {game_id: {k: (names, X), ('win', k): dict, 'ctx': dict}}.

    ``fixed_actions`` maps game_id -> action tuple used to build contexts (leakage test)."""
    kw.setdefault("attributes", ATTRS)
    eng = Engine(CH, [EloBlock(), RoleBlock(CH.C), ChampionMetaBlock(CH, **kw)])
    out = {}

    def visit(g, e):
        acts = (fixed_actions or {}).get(g.pre.game_id, g.actions)
        d = out.setdefault(g.pre.game_id, {})
        for k in ks:
            ctx = DraftContext(g.pre, acts[:k])
            if k < 20:
                d[k] = e.candidate_matrix(ctx)
            d[("win", k)] = e.win_vector(ctx)

    eng.process(recs, visit)
    return out


def col(res, gid, k, name):
    names, X = res[gid][k]
    return X[:, names.index(name)].astype(float)


def hand_games():
    g1 = mk("g1", 0.0, "A", "B", win=1, league="L", patch="1")
    g2 = mk("g2", 1.0, "C", "D", rbans=(21, 22, 23, 24, 25), win=0, league="M", patch="1")
    g3 = mk("g3", 2.0, "E", "F", bpicks=(2, 3, 4, 5, 26), win=1, league="L", patch="2", burned=((1,), (1,)))
    g4 = mk("g4", 3.0, "G", "H", bpicks=(2, 3, 4, 5, 26), league="M", patch="2")
    return [g1, g2, g3, g4]


HAND_KW = dict(k_base=0.0, k_patch=2.0, k_league=2.0, k_resid=1.0, k_resid_patch=1.0, k_phase=2.0,
               k_action=4.0, **NO_DECAY)


def test_hand_computed_values():
    res = run(hand_games(), **HAND_KW)
    f = lambda n, k=0: col(res, "g4", k, n)
    close = lambda a, b: math.isclose(a, b, rel_tol=1e-5, abs_tol=1e-7)
    # long-run, availability-adjusted: c1 burned in g3 -> eligible in 2 games, present in both
    assert close(f("meta_presence_long")[1], 1.0)
    assert close(f("meta_presence_long")[26], 1 / 3)
    assert close(f("meta_presence_long")[21], 1 / 3)
    assert close(f("meta_presence_long")[16], 2 / 3)
    assert f("meta_presence_long")[0] == 0.0
    # patch "2" has only g3: shrink toward long-run with k_patch=2
    assert close(f("meta_presence")[26], (1 + 2 / 3) / 3)
    assert close(f("meta_presence")[21], (0 + 2 / 3) / 3)
    assert close(f("meta_presence")[1], 1.0)  # not eligible on the patch -> pure prior
    assert close(f("meta_pick_rate")[26], (1 + 2 / 3) / 3) and f("meta_ban_rate")[26] == 0.0
    assert close(f("meta_ban_rate")[11], 1.0)
    assert f("meta_presence_patch_raw")[1] == 0.0 and close(f("meta_presence_patch_raw")[26], 1.0)
    # league "M" has only g2
    assert close(f("meta_presence_league")[21], (1 + 2 / 3) / 3)
    assert close(f("meta_presence_league")[26], (0 + 2 / 3) / 3)
    # phase/slot (k=0 -> ban1, first slot): c11 banned there 3/3 times; global bucket share 9/60
    share11 = (3 + 2 * 0.15) / (3 + 2)
    assert close(f("meta_rate_phase_slot")[11], share11)
    assert close(f("meta_rate_phase_slot")[2], 1.0 * (0 + 2 * 0.15) / (3 + 2))
    assert close(f("meta_rate_action")[11], (3 + 4 * share11 / 3) / (3 + 4))
    assert np.allclose(f("meta_rate_type", 0), f("meta_ban_rate", 0))
    assert np.allclose(f("meta_rate_type", 6), f("meta_pick_rate", 6))
    # win residuals: every game has exp_blue = 0.5 (fresh teams)
    assert close(f("meta_win_resid")[2], 0.5 / (3 + 1))
    assert close(f("meta_win_resid")[26], 0.5 / (1 + 1))
    assert close(f("meta_win_resid")[6], -0.5 / (3 + 1))
    assert f("meta_win_resid")[1] == 0.0
    assert close(f("meta_win_resid_patch")[2], 0.5 / 2) and f("meta_win_resid_patch")[1] == 0.0
    # context
    assert close(f("meta_games_patch")[0], math.log1p(1)) and close(f("meta_patch_age_days")[0], 1.0)
    # attributes
    assert f("tag_fighter")[0] == 1 and f("tag_marksman")[0] == 1 and f("tag_ranged")[0] == 1
    assert f("tag_tank")[1] == 1 and f("tag_support")[1] == 1 and f("tag_ranged")[1] == 0
    assert all(f(n)[5] == 0 for n in ("tag_fighter", "tag_mage", "tag_ranged"))
    # win features: k=8 (actions 6, 7 done) -> blue (first slot) picked c2, red picked c6
    w = res["g4"][("win", 8)]
    assert close(w["meta_win_resid_sum_diff"], 0.125 - (-0.125))
    assert close(w["meta_win_resid_patch_sum_diff"], 0.25 - (-0.25))
    assert close(w["meta_presence_sum_diff"], 0.0)
    assert all(v == 0.0 for v in res["g4"][("win", 0)].values())


def test_new_patch_falls_back_to_long_run_and_first_game_is_zero():
    gs = hand_games()
    gs.append(mk("g5", 4.0, "I", "J", patch="3"))
    res = run(gs, **HAND_KW)
    assert np.allclose(col(res, "g5", 0, "meta_presence"), col(res, "g5", 0, "meta_presence_long"))
    assert np.all(col(res, "g5", 0, "meta_presence_patch_raw") == 0)
    assert col(res, "g5", 0, "meta_games_patch")[0] == 0 and col(res, "g5", 0, "meta_patch_age_days")[0] == 0
    names, X = res["g1"][0]
    for n in names:
        if n.startswith("meta_"):
            assert np.all(X[:, names.index(n)] == 0), n


def test_time_decay_of_residual():
    g1 = mk("g1", 0.0, "A", "B", win=1)
    g2 = mk("g2", 10.0, "C", "D", win=0)
    g3 = mk("g3", 20.0, "E", "F")
    res = run([g1, g2, g3], hl_long=10.0, k_resid=1.0)
    w1, w2 = 0.5 ** 2, 0.5 ** 1
    expect = (0.5 * w1 - 0.5 * w2) / (w1 + w2 + 1.0)
    assert math.isclose(col(res, "g3", 0, "meta_win_resid")[1], expect, rel_tol=1e-5)


def test_leakage():
    base = [mk("g1", 0.0, "A", "B"), mk("g2", 1.0, "C", "D", bpicks=(21, 22, 23, 24, 25)),
            mk("g3", 2.0, "A", "C"), mk("g4", 3.0, "B", "D")]
    alt = base[:2] + [
        mk("g3", 2.0, "A", "C", bpicks=(26, 27, 28, 29, 0), win=0, rbans=(1, 2, 3, 4, 5), dur=0.01),
        mk("g4", 2.001, "B", "D", bpicks=(11, 12, 13, 14, 15), bbans=(1, 2, 3, 4, 5), win=0, patch="2"),
    ]
    fixed = {"g3": base[2].actions}
    ra, rb = run(base, fixed_actions=fixed), run(alt, fixed_actions=fixed)
    for k in (0, 1, 6, 7):
        na, Xa = ra["g3"][k]
        nb, Xb = rb["g3"][k]
        assert na == nb and np.array_equal(Xa, Xb)
    for k in (0, 7, 20):
        assert ra["g3"][("win", k)] == rb["g3"][("win", k)]


def test_series_visibility_same_day():
    hour = 1 / 24
    a = [mk("s1g1", 0.0, "A", "B", series="S", gis=1, dur=0.6 * hour),
         mk("s1g2", hour, "B", "A", series="S", gis=2, burned=((1, 2, 3, 4, 5, 6, 7, 8, 9, 10),) * 2)]
    b = [mk("s1g1", 0.0, "A", "B", series="S", gis=1, dur=0.6 * hour, bpicks=(21, 22, 23, 24, 25)),
         a[1]]
    ra, rb = run(a), run(b)
    assert col(rb, "s1g2", 0, "meta_presence")[21] > col(ra, "s1g2", 0, "meta_presence")[21] + 0.1
    assert not np.array_equal(ra["s1g2"][0][1], rb["s1g2"][0][1])
    assert col(ra, "s1g2", 0, "meta_games_patch")[0] > 0


def test_red_first_pick_orientation():
    # g1: red has first pick and bans c11 at action 0 (first-slot ban1); blue bans c16 at action 1.
    g1 = mk("g1", 0.0, "A", "B", first="Red", rbans=(11, 12, 13, 14, 15), bbans=(16, 17, 18, 19, 20),
            rpicks=(1, 2, 3, 4, 5), bpicks=(6, 7, 8, 9, 10), win=0)
    g2 = mk("g2", 1.0, "C", "D", first="Red", rbans=(11, 12, 13, 14, 15), bbans=(16, 17, 18, 19, 20),
            rpicks=(1, 2, 3, 4, 5), bpicks=(6, 7, 8, 9, 10))
    res = run([g1, g2], **NO_DECAY)
    ps0, ps1 = col(res, "g2", 0, "meta_rate_phase_slot"), col(res, "g2", 1, "meta_rate_phase_slot")
    assert ps0[11] > ps1[11] and ps1[16] > ps0[16]
    # red won g1 with picks 1..5 -> positive residual; win features are blue minus red
    wr = col(res, "g2", 0, "meta_win_resid")
    assert wr[1] > 0 > wr[6]
    w = res["g2"][("win", 8)]  # action 6: red (first slot) picks c1; action 7: blue picks c6
    assert math.isclose(w["meta_win_resid_sum_diff"], wr[6] - wr[1], rel_tol=1e-5)
    assert w["meta_win_resid_sum_diff"] < 0


def test_shapes_names_and_finite():
    gs = hand_games() + [mk("g5", 5.0, "A", "C", first="Red", patch="3", burned=((2, 3), (2, 3)))]
    res = run(gs)  # default hyperparameters
    for gid in ("g1", "g3", "g5"):
        for k in (0, 1, 6, 7):
            names, X = res[gid][k]
            own = [n for n in names if n.startswith(("meta_", "tag_"))]
            assert len(own) == 21 and X.shape == (CH.C, len(names)) and np.isfinite(X).all()
        for k in (0, 7, 20):
            w = res[gid][("win", k)]
            mine = {n: v for n, v in w.items() if n.startswith("meta_")}
            assert len(mine) == 3 and all(np.isfinite(v) for v in mine.values())
    # fixed set of keys regardless of action type
    assert res["g5"][0][0] == res["g5"][6][0]
    blk = ChampionMetaBlock(CH, attributes=ATTRS)
    assert blk.prefix == "meta_" and blk.name == "champion"
