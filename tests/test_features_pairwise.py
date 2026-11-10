import math

import numpy as np

from draft_analyzer import draft
from draft_analyzer.features.asof import ChampionIndex, DraftContext, Engine, GameRecord, PreGame
from draft_analyzer.features.elo import EloBlock
from draft_analyzer.features.pairwise import PairwiseBlock
from draft_analyzer.features.roles import RoleBlock

C = 30
CH = ChampionIndex([f"c{i}" for i in range(C)])


def pre(t, gid="g", first="Blue", blue="B", red="R", series="s"):
    return PreGame(
        game_id=gid, series_id=series, game_in_series=1, t=t, patch="1", league="L", season=2025, split="x",
        playoffs=0, blue_team=blue, red_team=red, blue_players=("a",) * 5, red_players=("b",) * 5,
        first_pick_side=first, fearless="none", blue_burned=(), red_burned=(), blue_series_wins=0,
        red_series_wins=0,
    )


def game(t, blue, red, blue_win, gid="g", first="Blue", dur=0.03, series="s"):
    p = pre(t, gid, first, series=series)
    acts, cnt = [], {"Blue": 0, "Red": 0}
    for i in range(20):
        if draft.ACTION_TYPE[i] == "ban":
            acts.append(-1)
        else:
            side = p.side_of_slot(draft.ACTION_SLOT[i])
            acts.append((blue if side == "Blue" else red)[cnt[side]])
            cnt[side] += 1
    return GameRecord(pre=p, t_end=t + dur, actions=tuple(acts), blue_roles=tuple(blue), red_roles=tuple(red),
                      blue_win=blue_win)


def make_engine(**kw):
    return Engine(CH, [EloBlock(), RoleBlock(C), PairwiseBlock(CH, **kw)])


def feats_at(recs, gid, k, **kw):
    eng = make_engine(**kw)
    out = {}

    def visit(g, e):
        if g.pre.game_id == gid:
            names, X = e.candidate_matrix(DraftContext(g.pre, g.actions[:k]))
            out["cand"] = dict(zip(names, X.T))
            out["win"] = e.win_vector(DraftContext(g.pre, g.actions[:k]))

    eng.process(recs, visit)
    return out


B1, R1 = (0, 1, 2, 3, 4), (5, 6, 7, 8, 9)
K_AFTER_ALL_PICKS = 20


def first_pick_k(first="Blue"):
    return next(i for i in range(20) if draft.ACTION_TYPE[i] == "pick")


def test_hand_values_and_symmetry():
    g1 = game(10.0, B1, R1, 1, "g1")
    g2 = game(20.0, (0, 1, 10, 11, 12), (5, 13, 14, 15, 16), 0, "g2")
    g3 = game(30.0, (20, 21, 22, 23, 24), (25, 26, 27, 28, 29), 1, "g3")
    eng = make_engine(half_life=None)
    pw = eng.block("pairwise")
    captured = {}

    def visit(g, e):
        if g.pre.game_id == "g3":
            captured["syn"], captured["ctr"] = pw.syn.copy(), pw.ctr.copy()
            captured["colift"], captured["vslift"] = pw.colift.copy(), pw.vslift.copy()
            # candidate 0: own picks (1,), opp picks (5,)  -> build ctx with picks for both sides
            p3 = g.pre
            acts = list(g.actions)
            captured["cand"] = e.candidate_matrix(DraftContext(p3, tuple(acts[:19])))

    eng.process([g1, g2, g3], visit)
    syn, ctr = captured["syn"], captured["ctr"]
    # pair (0,1): g1 blue won, exp .5 -> +0.5 ; g2 blue lost, exp .5 (Elo after g1: B beat R, but teams
    # are the same B,R; use the real exp) -> compute from the elo block instead
    e1 = 0.5
    elo = EloBlock()
    elo_eng = Engine(CH, [elo])
    exps = {}
    elo_eng.process([g1, g2], lambda g, e: exps.__setitem__(g.pre.game_id, e.expected_blue(g.pre)))
    r1, r2 = 1 - e1, 0 - exps["g2"]
    assert math.isclose(syn[0, 1], (r1 + r2) / (2 + 15), rel_tol=1e-9)
    assert np.allclose(syn, syn.T)
    # (0,2): only g1
    assert math.isclose(syn[0, 2], r1 / (1 + 15), rel_tol=1e-9)
    # counters: c0 (blue) vs c5 (red) in g1 and g2 -> c0 side residuals r1, r2
    assert math.isclose(ctr[0, 5], (r1 + r2) / (2 + 15), rel_tol=1e-9)
    assert np.allclose(ctr, -ctr.T)
    # lift: syn_n[0,1]=2, picks0=2, picks1=2, games=2 (then g3 is not applied yet)
    f = (10 - 1) / (10 - 2 / 2)  # picks per game n=10, p_c = 1 (picked in both games)
    e = 2 * 2 / 2 * f * 4 / 9
    assert math.isclose(captured["colift"][0, 1], math.log((2 + 5) / (e + 5)), rel_tol=1e-9)
    e = 2 * 2 / 2 * f * 5 / 9  # c0 vs c5: ctr_n = 2
    assert math.isclose(captured["vslift"][0, 5], math.log((2 + 5) / (e + 5)), rel_tol=1e-9)
    assert np.allclose(np.diag(captured["colift"]), 0)


def test_candidate_means_and_win_features():
    g1 = game(10.0, B1, R1, 1, "g1")
    g2 = game(20.0, (0, 1, 2, 3, 4), (5, 6, 7, 8, 9), 1, "g2")
    eng = make_engine(half_life=None)
    pw = eng.block("pairwise")
    out = {}

    def visit(g, e):
        if g.pre.game_id == "g2":
            # after blue's first two picks and red's first two picks (slot order unknown): use full picks
            ctx = DraftContext(g.pre, g.actions[:19])
            names, X = e.candidate_matrix(ctx)
            out["f"] = dict(zip(names, X.T))
            out["ctx"] = ctx
            out["w"] = e.win_vector(DraftContext(g.pre, g.actions))
            out["syn"], out["ctr"] = pw.syn.copy(), pw.ctr.copy()
            out["w0"] = e.win_vector(DraftContext(g.pre, ()))

    eng.process([g1, g2], visit)
    ctx, syn, ctr, f = out["ctx"], out["syn"], out["ctr"], out["f"]
    A, Bp = ctx.picks(ctx.acting_side), ctx.picks(ctx.opp_side)
    assert np.allclose(f["pair_syn_own"], syn[:, A].mean(axis=1))
    assert np.allclose(f["pair_ctr_vs_opp"], ctr[:, Bp].mean(axis=1))
    assert np.allclose(f["pair_ctr_vs_own"], ctr[:, A].mean(axis=1))
    assert f["pair_evidence_own"][20] == 0.0
    assert f["pair_evidence_own"][5] > 0.0 and f["pair_evidence_opp"][0] > 0.0
    # win features
    s_blue = sum(syn[a, b] for i, a in enumerate(B1) for b in B1[i + 1:])
    s_red = sum(syn[a, b] for i, a in enumerate(R1) for b in R1[i + 1:])
    assert math.isclose(out["w"]["pair_syn_sum_diff"], s_blue - s_red, abs_tol=1e-12)
    assert math.isclose(out["w"]["pair_ctr_sum"], sum(ctr[a, b] for a in B1 for b in R1), abs_tol=1e-12)
    assert out["w"]["pair_ctr_sum"] > 0  # blue won g1 over red
    assert {n: v for n, v in out["w0"].items() if n.startswith("pair_")} == {"pair_syn_sum_diff": 0.0, "pair_ctr_sum": 0.0}


def test_zero_when_no_picks():
    g1 = game(10.0, B1, R1, 1, "g1")
    g2 = game(20.0, B1, R1, 1, "g2")
    f = feats_at([g1, g2], "g2", 0)["cand"]
    for n, v in f.items():
        if n.startswith("pair_"):
            assert np.all(v == 0.0), n


def test_leakage():
    base = [game(10.0, B1, R1, 1, "g1"), game(20.0, (0, 1, 10, 11, 12), (5, 13, 14, 15, 16), 0, "g2")]
    tgt = game(30.0, (0, 1, 2, 20, 21), (5, 6, 22, 23, 24), 1, "g3")
    alt_tgt = game(30.0, (0, 1, 2, 20, 21), (5, 6, 22, 23, 24), 0, "g3")  # outcome changed
    later = game(40.0, B1, R1, 0, "g4")
    later2 = game(40.0, (9, 8, 7, 6, 5), (0, 1, 2, 3, 4), 1, "g4")
    for k in (0, 8, 12, 19):
        a = feats_at(base + [tgt, later], "g3", k)
        b = feats_at(base + [alt_tgt, later2], "g3", k)
        for n in a["cand"]:
            assert np.array_equal(a["cand"][n], b["cand"][n]), n
        assert a["win"] == b["win"]


def test_series_visibility_same_day():
    g1 = game(100.0, B1, R1, 1, "g1", dur=0.03)
    g1b = game(100.0, B1, R1, 0, "g1", dur=0.03)
    g2 = game(100.05, B1, R1, 1, "g2")  # starts one hour after g1 started (g1 ended 43 min in)
    k = 19
    a = feats_at([g1, g2], "g2", k)["cand"]
    b = feats_at([g1b, g2], "g2", k)["cand"]
    c = feats_at([g2], "g2", k)["cand"]
    assert any(not np.array_equal(a[n], b[n]) for n in a)
    assert any(not np.array_equal(a[n], c[n]) for n in a)


def test_red_first_pick_orientation():
    g1 = game(10.0, B1, R1, 1, "g1")
    # red has first pick; red picks c5 first, so next action is Blue's first pick
    g2 = game(20.0, B1, R1, 1, "g2", first="Red")
    k = first_pick_k() + 1
    ctx = DraftContext(g2.pre, g2.actions[:k])
    assert ctx.acting_side == "Blue" and ctx.picks("Red") == [5] and ctx.picks("Blue") == []
    f = feats_at([g1, g2], "g2", k)["cand"]
    # acting team (blue) has no picks -> own features exactly 0
    for n in ("pair_syn_own", "pair_ctr_vs_own", "pair_colift_own", "pair_vslift_own", "pair_evidence_own"):
        assert np.all(f[n] == 0.0)
    # opp (red) has c5; c6 was red's teammate in g1 (red lost -> negative), c0 was against red (ctr_vs_opp > 0)
    assert f["pair_syn_opp"][6] < 0
    assert f["pair_ctr_vs_opp"][0] > 0
    assert f["pair_colift_opp"][6] > f["pair_colift_opp"][20]
    assert f["pair_evidence_opp"][6] > 0 and f["pair_evidence_opp"][20] == 0


def test_shapes_names_finite():
    recs = [game(10.0 + i, B1, R1, i % 2, f"g{i}") for i in range(5)]
    for k in (0, 5, 12, 19):
        out = feats_at(recs, "g4", k)
        for n, v in out["cand"].items():
            if n.startswith("pair_"):
                assert v.shape == (C,) and np.all(np.isfinite(v))
        pw = {n: v for n, v in out["win"].items() if n.startswith("pair_")}
        assert set(pw) == {"pair_syn_sum_diff", "pair_ctr_sum"} and all(np.isfinite(list(pw.values())))
    names = PairwiseBlock(CH).candidate_features
    assert callable(names)
    assert PairwiseBlock.prefix == "pair_" and PairwiseBlock.name == "pairwise"


def _random_game(rng, t, planted=False):
    if planted and rng.random() < 0.4:
        rest = rng.choice(np.arange(2, C), 8, replace=False)
        blue, red = [0, 1, *rest[:3]], list(rest[3:8])
    else:
        ch = rng.choice(C, 10, replace=False)
        blue, red = list(ch[:5]), list(ch[5:])
        if planted:  # keep 0 and 1 apart so the planted pair is only drafted together deliberately
            pass
    return game(t, tuple(int(x) for x in blue), tuple(int(x) for x in red), int(rng.random() < 0.5), "x")


def _sim(planted):
    rng = np.random.default_rng(0)
    blk = PairwiseBlock(CH, half_life=None)
    for i in range(4000):
        g = _random_game(rng, float(i), planted)
        blk.update(g, 0.5)
    blk.begin_game(pre(5000.0))
    return blk


def test_lift_zero_for_independent_drafting():
    blk = _sim(planted=False)
    off = ~np.eye(C, dtype=bool)
    assert abs(blk.colift[off].mean()) < 0.03
    assert abs(blk.vslift[off].mean()) < 0.03
    assert np.abs(blk.colift[off]).max() < 0.5
    assert np.abs(blk.vslift[off]).max() < 0.5


def test_lift_positive_for_planted_pair():
    blk = _sim(planted=True)
    assert blk.colift[0, 1] > 0.8 and blk.colift[1, 0] == blk.colift[0, 1]
    assert blk.vslift[0, 1] < 0.0  # planted pair is never on opposite teams (only random games)
    others = blk.colift[2:, 2:]
    assert blk.colift[0, 1] > others.max()
