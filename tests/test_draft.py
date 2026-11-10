"""Draft order, slot/side assignment, series reconstruction, Fearless detection."""
import pandas as pd
import pytest

from draft_analyzer import draft
from draft_analyzer.ingest import oracles_elixir as oe


def test_sequence_shape_and_counts():
    assert draft.N_ACTIONS == 20
    assert "".join("F" if s == "first" else "S" for s in draft.ACTION_SLOT) == "FSFSFS" "FSSFFS" "SFSF" "SFFS"
    assert draft.ACTION_TYPE == ("ban",) * 6 + ("pick",) * 6 + ("ban",) * 4 + ("pick",) * 4
    for slot in ("first", "second"):
        for typ in ("ban", "pick"):
            idx = [i for i in range(20) if draft.ACTION_SLOT[i] == slot and draft.ACTION_TYPE[i] == typ]
            assert len(idx) == 5
            assert [draft.ACTION_ORDINAL[i] for i in idx] == [0, 1, 2, 3, 4]
    assert draft.ACTION_PHASE[5] == "ban1" and draft.ACTION_PHASE[6] == "pick1"
    assert draft.ACTION_PHASE[12] == "ban2" and draft.ACTION_PHASE[19] == "pick2"


def test_interleave_roundtrip():
    fb, fp = [f"fb{i}" for i in range(5)], [f"fp{i}" for i in range(5)]
    sb, sp = [f"sb{i}" for i in range(5)], [f"sp{i}" for i in range(5)]
    seq = draft.interleave(fb, fp, sb, sp)
    assert seq == [
        "fb0", "sb0", "fb1", "sb1", "fb2", "sb2",
        "fp0", "sp0", "sp1", "fp1", "fp2", "sp2",
        "sb3", "fb3", "sb4", "fb4",
        "sp3", "fp3", "fp4", "sp4",
    ]
    back = draft.deinterleave(seq)
    assert back[("first", "ban")] == fb and back[("second", "pick")] == sp
    with pytest.raises(ValueError):
        draft.interleave(fb[:4], fp, sb, sp)


def test_slot_side_is_independent_of_side():
    assert draft.slot_side("first", "Blue") == "Blue" and draft.slot_side("second", "Blue") == "Red"
    assert draft.slot_side("first", "Red") == "Red" and draft.slot_side("second", "Red") == "Blue"


def _raw_game(gid, date, game, blue, red, first_pick_side, blue_picks, red_picks, blue_bans, red_bans,
              blue_win=1, league="LCK", year=2026, split="Cup", missing_ban=None):
    """Minimal Oracle's-Elixir-shaped rows (10 players + 2 team rows) for one game."""
    rows = []
    for side, team, picks, bans, res in (("Blue", blue, blue_picks, blue_bans, blue_win), ("Red", red, red_picks, red_bans, 1 - blue_win)):
        base = dict(gameid=gid, datacompleteness="complete", league=league, year=year, split=split, playoffs=0,
                    date=date, game=game, patch="16.01", side=side, teamname=team, teamid=f"id:{team}",
                    firstPick=int(side == first_pick_side), gamelength=1800, result=res)
        bans = list(bans)
        if missing_ban == side:
            bans[3] = None
        ban_cols = {f"ban{i+1}": b for i, b in enumerate(bans)}
        # player rows: roles deliberately in a different order than pick order
        for pos, champ in zip(("top", "jng", "mid", "bot", "sup"), reversed(picks)):
            rows.append({**base, **ban_cols, "position": pos, "playername": f"{team}_{pos}", "playerid": f"p:{team}_{pos}",
                         "champion": champ, "participantid": 0})
        rows.append({**base, **ban_cols, "position": "team", "playername": None, "playerid": None, "champion": None,
                     "participantid": 100 if side == "Blue" else 200, **{f"pick{i+1}": p for i, p in enumerate(picks)}})
    return rows


def _champs(prefix):
    return [f"{prefix}{i}" for i in range(5)]


@pytest.fixture
def tiny_raw():
    rows = []
    # Series 1 (hard fearless, 3 games). Game 2: RED has first pick (2026 First Selection).
    rows += _raw_game("g1", "2026-01-14 08:00:00", 1, "A", "B", "Blue", _champs("a1_"), _champs("b1_"), _champs("x1_"), _champs("y1_"))
    rows += _raw_game("g2", "2026-01-14 09:00:00", 2, "B", "A", "Red", _champs("b2_"), _champs("a2_"), _champs("x2_"), _champs("y2_"), blue_win=0)
    rows += _raw_game("g3", "2026-01-14 10:00:00", 3, "A", "B", "Red", _champs("a3_"), _champs("b3_"), _champs("x3_"), _champs("y3_"), missing_ban="Red")
    # Series 2: same teams next day -> new series even though teams match
    rows += _raw_game("g4", "2026-01-15 08:00:00", 1, "A", "B", "Blue", _champs("a4_"), _champs("b4_"), _champs("x4_"), _champs("y4_"))
    rows += _raw_game("g5", "2026-01-15 09:00:00", 2, "A", "B", "Blue", _champs("a5_"), _champs("b5_"), _champs("x5_"), _champs("y5_"))
    # Different pair interleaved in time must not break series 1
    rows += _raw_game("h1", "2026-01-14 08:30:00", 1, "C", "D", "Blue", _champs("c1_"), _champs("d1_"), _champs("u1_"), _champs("v1_"))
    # Orphan game 2 (game 1 missing from data)
    rows += _raw_game("o2", "2026-01-16 09:00:00", 2, "C", "D", "Blue", _champs("c2_"), _champs("d2_"), _champs("u2_"), _champs("v2_"))
    return pd.DataFrame(rows)


def test_series_reconstruction(tiny_raw):
    games, _ = oe.build_games(tiny_raw)
    g = games.set_index("game_id")
    assert g.loc["g1", "series_id"] == g.loc["g2", "series_id"] == g.loc["g3", "series_id"]
    assert g.loc["g4", "series_id"] == g.loc["g5", "series_id"] != g.loc["g1", "series_id"]
    assert g.loc["h1", "series_id"] not in set(g.loc[["g1", "g4"], "series_id"])
    assert bool(g.loc["o2", "series_incomplete"]) and not bool(g.loc["g2", "series_incomplete"])
    # series score before each game is from the perspective of that game's blue/red teams
    assert (g.loc["g2", "blue_series_wins_before"], g.loc["g2", "red_series_wins_before"]) == (0, 1)  # A (now red) won g1
    assert (g.loc["g3", "blue_series_wins_before"], g.loc["g3", "red_series_wins_before"]) == (2, 0)  # A won g1 and g2


def test_red_side_first_pick_sequence(tiny_raw):
    games, _ = oe.build_games(tiny_raw)
    actions = oe.build_draft_actions(games)
    a = actions[actions.game_id == "g2"].sort_values("action_index")
    assert len(a) == 20
    # g2: blue = B, red = A, RED has first pick -> A is the 'first' slot
    first = a[a.draft_slot == "first"]
    assert set(first.side) == {"Red"} and set(first.acting_team) == {"A"}
    assert a.iloc[0].champion == "y2_0" and a.iloc[0].action_type == "ban" and a.iloc[0].side == "Red"
    assert a.iloc[1].champion == "x2_0" and a.iloc[1].side == "Blue"
    assert list(a.champion.iloc[6:12]) == ["a2_0", "b2_0", "b2_1", "a2_1", "a2_2", "b2_2"]
    assert list(a.side.iloc[6:12]) == ["Red", "Blue", "Blue", "Red", "Red", "Blue"]
    assert list(a.champion.iloc[12:16]) == ["x2_3", "y2_3", "x2_4", "y2_4"]
    assert list(a.champion.iloc[16:20]) == ["b2_3", "a2_3", "a2_4", "b2_4"]
    assert games.set_index("game_id").loc["g2", "first_pick_team"] == "A"
    # blue first pick game for contrast
    a1 = actions[actions.game_id == "g1"].sort_values("action_index")
    assert a1.iloc[0].side == "Blue" and a1.iloc[6].champion == "a1_0"


def test_missed_ban_kept_as_null_action(tiny_raw):
    games, _ = oe.build_games(tiny_raw)
    actions = oe.build_draft_actions(games)
    a = actions[actions.game_id == "g3"]
    assert len(a) == 20 and a.champion.isna().sum() == 1
    row = games.set_index("game_id").loc["g3"]
    assert row.irregular and "red_missed_bans:1" in row.irregular_notes and row.n_actions == 19


def test_roles_come_from_player_rows_not_pick_order(tiny_raw):
    games, _ = oe.build_games(tiny_raw)
    row = games.set_index("game_id").loc["g1"]
    assert list(row.blue_picks) == _champs("a1_")
    assert list(row.blue_champs_by_role) == list(reversed(_champs("a1_")))  # top..sup
    assert list(row.blue_players) == [f"p:A_{p}" for p in ("top", "jng", "mid", "bot", "sup")]


def test_fearless_detection_and_burned_lists(tiny_raw):
    games, summary = oe.build_games(tiny_raw)
    g = games.set_index("game_id")
    assert set(summary.fearless) == {"hard"}  # nobody repeats anything in the fixture
    assert list(g.loc["g1", "unavailable_at_start"]) == []
    assert set(g.loc["g2", "unavailable_at_start"]) == set(_champs("a1_") + _champs("b1_"))
    assert len(g.loc["g3", "unavailable_at_start"]) == 20
    assert list(g.loc["g4", "unavailable_at_start"]) == []  # new series resets


def test_classify_fearless_modes():
    assert draft.classify_fearless(0.0, 0.0, 50) == "hard"
    assert draft.classify_fearless(0.0, 0.6, 50) == "soft"
    assert draft.classify_fearless(0.75, 0.6, 50) == "none"
    assert draft.classify_fearless(0.0, 0.0, 2) == "na"
    assert draft.burned_for_team("soft", ["A"], ["B"]) == ["A"]
    assert draft.burned_for_team("hard", ["A"], ["B"]) == ["A", "B"]
    assert draft.burned_for_team("none", ["A"], ["B"]) == []


def test_unavailable():
    assert draft.unavailable(["A", None, "B"], burned=["C"]) == {"A", "B", "C"}
