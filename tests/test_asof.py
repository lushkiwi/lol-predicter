"""The as-of engine: ordering, leakage, series visibility. Runs over ALL registered blocks."""
import math
import random

import numpy as np
import pytest

from draft_analyzer import draft
from draft_analyzer.features import default_blocks
from draft_analyzer.features.asof import Decayed, DraftContext, Engine, GameRecord, StatBlock

from . import synth


def featurise(games):
    """Run a fresh engine; return {game_id: {k: candidate matrix}}, {game_id: {k: win vector}}, names."""
    ch = synth.champs()
    eng = Engine(ch, default_blocks(ch))
    cand, win, names = {}, {}, {}

    def visit(g, e):
        cand[g.pre.game_id], win[g.pre.game_id] = {}, {}
        for k in range(21):
            ctx = DraftContext(g.pre, g.actions[:k])
            win[g.pre.game_id][k] = e.win_vector(ctx)
            if k < 20:
                n, X = e.candidate_matrix(ctx)
                names["n"] = n
                cand[g.pre.game_id][k] = X

    eng.process(games, visit)
    return cand, win, names["n"]


def assert_same(a, b, gid, ks=range(20)):
    for k in ks:
        np.testing.assert_array_equal(a[0][gid][k], b[0][gid][k], err_msg=f"candidate features differ: {gid} k={k}")
    for k in list(ks) + ([20] if 19 in ks else []):
        assert a[1][gid][k] == b[1][gid][k], f"win features differ: {gid} k={k}"


@pytest.fixture(scope="module")
def season():
    return synth.make_season(seed=1)


@pytest.fixture(scope="module")
def base(season):
    return featurise(season)


def test_features_are_finite_and_named(base):
    cand, win, names = base
    assert len(names) == len(set(names)), "duplicate feature names"
    for gid, per_k in cand.items():
        for k, X in per_k.items():
            assert X.shape == (synth.N_CHAMPS, len(names))
            assert np.isfinite(X).all(), f"non-finite feature in {gid} k={k}: {[n for n, c in zip(names, X.T) if not np.isfinite(c).all()]}"
    for gid, per_k in win.items():
        for k, d in per_k.items():
            assert all(math.isfinite(v) for v in d.values())


@pytest.mark.parametrize("i", [5, 20, 41])
def test_no_leakage_from_own_outcome_or_future(season, base, i):
    """Features of game i are unchanged when its result/roles and every later game change."""
    rng = random.Random(99)
    target = season[i]
    changed = season[:i] + [synth.with_outcome_changed(target, rng)] + [synth.with_draft_changed(g, rng) for g in season[i + 1:]]
    assert_same(base, featurise(changed), target.pre.game_id)


@pytest.mark.parametrize("i", [5, 20, 41])
def test_pre_draft_features_do_not_see_own_draft(season, base, i):
    """At k=0 nothing about the game's own draft may be visible."""
    rng = random.Random(7)
    target = season[i]
    changed = season[:i] + [synth.with_draft_changed(target, rng)] + season[i + 1:]
    assert_same(base, featurise(changed), target.pre.game_id, ks=[0])


def test_partial_draft_features_do_not_see_later_actions(season, base):
    """Features before action k depend only on actions 0..k-1 of the game."""
    rng = random.Random(3)
    i, k = 30, 9
    target = season[i]
    burned = set(target.pre.blue_burned) | set(target.pre.red_burned) | set(target.actions[:k])
    tail = rng.sample([c for c in range(synth.N_CHAMPS) if c not in burned], 20 - k)
    alt = synth.finish(target.pre, target.actions[:k] + tuple(tail), rng, target.t_end)
    changed = season[:i] + [alt] + season[i + 1:]
    assert_same(base, featurise(changed), target.pre.game_id, ks=range(k + 1))


def test_concurrent_unfinished_game_is_invisible(season, base):
    """A game that started earlier but has not finished must not be visible."""
    rng = random.Random(5)
    pairs = [(a, b) for a in season for b in season
             if a.pre.t < b.pre.t < a.t_end and a.pre.series_id != b.pre.series_id]
    assert pairs, "fixture should contain overlapping games"
    a, b = pairs[len(pairs) // 2]
    i = season.index(a)
    changed = season[:i] + [synth.with_draft_changed(a, rng)] + season[i + 1:]
    assert_same(base, featurise(changed), b.pre.game_id)


def test_game_two_sees_game_one(season, base):
    """Same-day earlier games of a series are valid history and must be used."""
    rng = random.Random(11)
    g2 = next(g for g in season[10:] if g.pre.game_in_series == 2)
    g1 = next(g for g in season if g.pre.series_id == g2.pre.series_id and g.pre.game_in_series == 1)
    assert g2.pre.t - g1.pre.t < 0.1 and g1.t_end <= g2.pre.t  # one hour apart, same day
    i = season.index(g1)
    # change game 1's bans/outcome but keep its picks, so game 2's burned list stays valid
    keep = [c if draft.ACTION_TYPE[j] == "pick" else None for j, c in enumerate(g1.actions)]
    pool = [c for c in range(synth.N_CHAMPS) if c not in set(g1.actions) | set(g1.pre.blue_burned)]
    new_bans = iter(rng.sample(pool, 10))
    actions = tuple(c if c is not None else next(new_bans) for c in keep)
    alt = synth.finish(g1.pre, actions, rng, g1.t_end, blue_win=1 - g1.blue_win)
    other = featurise(season[:i] + [alt] + season[i + 1:])
    gid = g2.pre.game_id
    assert not np.array_equal(base[0][gid][0], other[0][gid][0]), "game 2 features ignore game 1"


def test_engine_applies_games_only_after_they_finish():
    class Counter(StatBlock):
        name, prefix = "counter", "cnt_"

        def __init__(self):
            self.seen = []

        def update(self, g, exp_blue):
            self.seen.append(g.pre.game_id)

    rng = random.Random(0)
    a = synth.make_game(rng, "a", "SA", 1, 100.0, "T0", "T1", length_days=1.0)   # ends at 101.0
    b = synth.make_game(rng, "b", "SB", 1, 100.5, "T2", "T3", length_days=0.1)   # ends at 100.6
    c = synth.make_game(rng, "c", "SC", 1, 100.7, "T4", "T5", length_days=0.1)
    d = synth.make_game(rng, "d", "SD", 1, 101.0, "T0", "T2", length_days=0.1)   # starts exactly when a ends
    block = Counter()
    eng = Engine(synth.champs(), [block])
    seen_at = {}
    eng.process([d, c, b, a], lambda g, e: seen_at.__setitem__(g.pre.game_id, list(block.seen)))
    assert seen_at == {"a": [], "b": [], "c": ["b"], "d": ["b", "c", "a"]}
    eng.flush()
    assert block.seen == ["b", "c", "a", "d"]


def test_decayed_counter():
    d = Decayed(3, half_life=10.0)
    d.add(0.0, 0, 1.0)
    np.testing.assert_allclose(d.read(10.0), [0.5, 0, 0])
    d.add(10.0, [0, 1], 1.0)
    np.testing.assert_allclose(d.read(10.0), [1.5, 1.0, 0])
    np.testing.assert_allclose(d.read(20.0), [0.75, 0.5, 0])
    np.testing.assert_allclose(d.read(5.0), [1.5, 1.0, 0])  # never "un-decays"
    plain = Decayed(2, None)
    plain.add(0.0, 1, 2.0)
    np.testing.assert_allclose(plain.read(1000.0), [0, 2.0])


def test_available_mask_uses_slot_not_side():
    rng = random.Random(2)
    g = synth.make_game(rng, "x", "SX", 2, 10.0, "T0", "T1", burned=(0, 1, 2), first_pick_side="Red")
    ctx = DraftContext(g.pre, g.actions[:6])  # next: first pick of the draft -> RED acts
    assert ctx.acting_side == "Red" and ctx.is_pick and ctx.slot == "first"
    mask = ctx.available_mask(synth.N_CHAMPS)
    assert not mask[[0, 1, 2]].any() and not mask[list(g.actions[:6])].any()
    assert mask.sum() == synth.N_CHAMPS - 9
    assert DraftContext(g.pre, g.actions[:1]).acting_side == "Blue"  # second slot bans second
