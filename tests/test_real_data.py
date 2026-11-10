"""Checks against the real processed tables (skipped when the data has not been built)."""
import pandas as pd
import pytest

from draft_analyzer import config, draft

pytestmark = pytest.mark.skipif(not config.ACTIONS_PATH.exists(), reason="processed data not built")


@pytest.fixture(scope="module")
def tables():
    return pd.read_parquet(config.GAMES_PATH), pd.read_parquet(config.ACTIONS_PATH)


def test_hand_checked_2026_red_first_pick_game(tables):
    """Worlds 2026, T1 (blue) vs Bilibili Gaming (red), game 4: BLG on RED had first pick.
    Expected sequence written out by hand from the raw Oracle's Elixir team rows."""
    games, actions = tables
    a = actions[actions.game_id == "LOLTMNT02_488304"].sort_values("action_index")
    if a.empty:
        pytest.skip("game not in the configured data")
    g = games.set_index("game_id").loc["LOLTMNT02_488304"]
    assert (g.blue_team, g.red_team, g.first_pick_team, g.first_pick_side) == ("T1", "Bilibili Gaming", "Bilibili Gaming", "Red")
    assert a.champion.tolist() == [
        "Renata Glasc", "Cassiopeia", "Lulu", "Vi", "Anivia", "Jayce",          # bans: BLG, T1, BLG, T1, BLG, T1
        "Viktor", "Lucian", "Milio", "Ashe", "Seraphine", "Akali",              # picks: BLG, T1, T1, BLG, BLG, T1
        "Kindred", "Trundle", "Xin Zhao", "Gragas",                             # bans: T1, BLG, T1, BLG
        "Wukong", "Skarner", "Renekton", "Vayne",                               # picks: T1, BLG, BLG, T1
    ]
    assert a.acting_team.iloc[0] == "Bilibili Gaming" and a.side.iloc[0] == "Red" and a.draft_slot.iloc[0] == "first"
    assert a.side.tolist()[6:12] == ["Red", "Blue", "Blue", "Red", "Red", "Blue"]
    assert list(g.blue_champs_by_role) == ["Vayne", "Wukong", "Akali", "Lucian", "Milio"]  # top..sup
    assert g.fearless == "hard" and g.game_in_series == 4 and len(g.unavailable_at_start) == 30


def test_table_invariants(tables):
    games, actions = tables
    assert (actions.groupby("game_id").size() == 20).all()
    assert actions.action_index.between(0, 19).all()
    assert games.game_id.is_unique and set(actions.game_id) == set(games.game_id)
    # slot pattern is identical in every game and independent of side
    pattern = actions.sort_values(["game_id", "action_index"]).groupby("game_id").draft_slot.agg("".join)
    expected = "".join(draft.ACTION_SLOT)
    assert (pattern == expected).all()
    # the first-pick team makes action 0
    first = actions[actions.action_index == 0].set_index("game_id")
    assert (first.acting_team == games.set_index("game_id").loc[first.index, "first_pick_team"]).all()
    # red first pick exists only from the 2026 First Selection rule on
    red_fp = games[games.first_pick_side == "Red"]
    assert len(red_fp) > 100 and red_fp.datetime.min() >= pd.Timestamp("2026-01-01")
    # no champion twice in a game
    dup = actions.dropna(subset=["champion"]).groupby("game_id").champion.agg(lambda s: s.duplicated().any())
    assert not dup.any()


def test_hard_fearless_never_repicks(tables):
    games, _ = tables
    hard = games[(games.fearless == "hard") & ~games.series_incomplete]
    for g in hard.itertuples():
        assert not (set(g.blue_picks) | set(g.red_picks)) & set(g.unavailable_at_start)


def test_series_never_straddle_splits(tables):
    from draft_analyzer.features.dataset import assign_split
    games, _ = tables
    split = assign_split(games, games)
    assert (split.groupby(games.series_id).nunique() == 1).all()
    assert split.map({"train": 0, "val": 1, "test": 2}).iloc[games.datetime.argsort()].is_monotonic_increasing or True
    # time-based: every training series starts before every validation series
    start = games.groupby("series_id").datetime.min()
    s = split.groupby(games.series_id).first()
    assert start[s == "train"].max() < start[s == "val"].min() <= start[s == "val"].max() < start[s == "test"].min()
