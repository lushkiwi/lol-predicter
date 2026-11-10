"""Leaguepedia parser / join tests. No network: fixtures are real page-source excerpts."""
import json
import re
from pathlib import Path

import pandas as pd
import pytest

from draft_analyzer import config, draft
from draft_analyzer.ingest import join as J
from draft_analyzer.ingest import leaguepedia as lp

FIX = Path(__file__).parent / "fixtures"
PB_PAGE = "LCK/2026 Season/Cup/Picks and Bans/Group Stage"
DATA_PAGE = "Data:LCK/2026 Season/Cup"
PB25_PAGE = "LCK/2025 Season/Rounds 1-2/Picks and Bans/1-3"


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def lookup():
    return lp.NameLookup(lp.parse_lua_lookup(fx("lp_championnames_excerpt.lua")),
                         lp.parse_lua_lookup(fx("lp_teamnames_excerpt.lua")))


@pytest.fixture(scope="module")
def pb():
    return lp.parse_picks_and_bans(fx("lp_pb_lck2026_cup_week1.wiki"), PB_PAGE)


@pytest.fixture(scope="module")
def ms():
    return lp.parse_match_schedule(fx("lp_data_lck2026_cup_week1.wiki"), DATA_PAGE)


# --------------------------------------------------------------------------------------
# wikitext helpers
# --------------------------------------------------------------------------------------
def test_template_params_nested():
    body = "MatchSchedule|team1=KT|game1={{MatchSchedule/Game|blue=A|red=B}}|x=[[a|b]]|pos"
    p = lp.template_params(body)
    assert p["team1"] == "KT"
    assert p["game1"] == "{{MatchSchedule/Game|blue=A|red=B}}"
    assert p["x"] == "[[a|b]]"
    assert p["1"] == "pos"


def test_find_templates_balanced():
    text = "a {{T|x={{U|y}}}} b {{T|z}} {{TT|no}}"
    found = [b for _, b in lp.find_templates(text, "T")]
    assert found == ["T|x={{U|y}}", "T|z"]


# --------------------------------------------------------------------------------------
# PicksAndBansS7 parser
# --------------------------------------------------------------------------------------
def test_parse_pb_fixture(pb):
    assert len(pb) == 5
    assert all(g.tab == "Week 1" and g.overview == "LCK/2026 Season/Cup" for g in pb)
    assert [(g.match_in_tab, g.game_in_match) for g in pb] == [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2)]
    g1 = pb[0]
    assert (g1.team1, g1.team2, g1.switched_sides, g1.winner) == ("KT Rolster", "DN SOOPers", True, 2)
    assert g1.team1_bans == ["Neeko", "Vi", "Sylas", "Rakan", "Aphelios"]
    assert g1.team2_picks == ["Rumble", "Wukong", "Ahri", "Corki", "Pantheon"]
    # editor abbreviations survive parsing and are resolved later
    assert pb[1].team1_bans[3] == "RG" and pb[3].team1_picks[1] == "XZ"


def test_pb_layout_order_matches_draft_interleave(pb):
    """Editors lay picks out one per line in draft order; that order must equal draft.interleave.

    (Bans are written two per line, "blue" slot first, so the layout only encodes the order of
    ban phase 1 -- F,S,F,S,F,S -- and not ban phase 2, where the second drafter bans first.)
    """
    text = lp.strip_comments(fx("lp_pb_lck2026_cup_week1.wiki"))
    for (_, body), g in zip(lp.find_templates(text, "PicksAndBansS7"), pb):
        seq = draft.interleave(g.team1_bans, g.team1_picks, g.team2_bans, g.team2_picks)
        picks = re.findall(r"\|\s*(bluepick\d|red_pick\d)\s*=", body)
        assert [g.raw[k] for k in picks] == [c for c, t in zip(seq, draft.ACTION_TYPE) if t == "pick"]
        bans1 = re.findall(r"\|\s*(blueban[123]|red_ban[123])\s*=", body)
        assert [g.raw[k] for k in bans1] == [c for c, ph in zip(seq, draft.ACTION_PHASE) if ph == "ban1"]


def test_missed_ban_values():
    assert lp._champ("NONE") is None and lp._champ("Loss of Ban") is None and lp._champ(" ") is None
    assert lp._champ("Azir") == "Azir"


# --------------------------------------------------------------------------------------
# MatchSchedule parser
# --------------------------------------------------------------------------------------
def test_parse_match_schedule_fixture(ms):
    assert len(ms) == 5
    g = ms[0]
    assert (g.tab, g.match_in_tab, g.game_in_match) == ("Week 1", 1, 1)
    assert (g.blue, g.red, g.first_pick, g.first_sel, g.side_sel, g.pick_sel) == (
        "DN SOOPers", "KT", "KT", "KT", "DN SOOPers", "KT")
    assert g.riot_platform_game_id == "LOLTMNT03_337058"
    assert g.date_utc == pd.Timestamp("2026-01-14 08:00")  # 00:00 PST
    assert [m.riot_platform_game_id for m in ms[3:]] == ["LOLTMNT03_337109", "LOLTMNT03_336084"]
    assert ms[3].first_pick == "BRION" and ms[3].blue == "Dplus"


def test_ms_datetime_dst():
    assert lp.ms_datetime_utc("2025-04-02", "10:11", "CET", "yes") == pd.Timestamp("2025-04-02 08:11")
    assert lp.ms_datetime_utc("2025-04-02", "17:00", "KST", "yes") == pd.Timestamp("2025-04-02 08:00")


# --------------------------------------------------------------------------------------
# Name lookups and normalization
# --------------------------------------------------------------------------------------
def test_lua_lookup_and_champion_names(lookup):
    c = lookup.champion
    assert c("RG") == "Renata Glasc" and c("XZ") == "Xin Zhao" and c("ksa") == "K'Sante" and c("wu") == "Wukong"
    assert c("None") is None and c("loss of ban") is None and c(None) is None
    assert c("Azir") == "Azir"  # not in the excerpt: passes through
    assert lookup.team("KT") == "KT Rolster" and lookup.team("Dplus Kia") == "Dplus KIA"
    assert lookup.team("dn freecs") == "DN Freecs"  # pointer chain dn freecs -> dnf -> link


def test_norm_team():
    assert J.norm_team("Dplus KIA") == J.norm_team("Dplus Kia") == "dpluskia"
    assert J.norm_team("LYON (2024 American Team)") == "lyon"
    assert J.norm_team("LØS") == J.norm_team("LØS")
    assert J.norm_team(None) == ""


def test_team_resolver(lookup):
    oe = ["KT Rolster", "DN SOOPers", "Dplus Kia", "HANJIN BRION", "Kiwoom DRX", "Gen.G", "BNK FEARX"]
    r = J.TeamResolver(oe, lookup)
    assert r.resolve("KT") == "KT Rolster" and r.log["KT"][1] == "wiki-module"
    assert r.resolve("Dplus") == "Dplus Kia"
    assert r.resolve("BRION") == "HANJIN BRION" and r.log["BRION"][1] == "containment"
    assert r.resolve("DRX") == "Kiwoom DRX"
    assert r.resolve("DN Freecs") == "DN SOOPers" and r.log["DN Freecs"][1] == "alias"
    assert r.resolve("KRX") == "Kiwoom DRX"
    assert r.resolve("FearX") == "BNK FEARX"
    assert r.resolve("Some Other Team") is None and r.log["Some Other Team"][1] == "unresolved"


def test_team_resolver_containment_must_be_unique():
    r = J.TeamResolver(["Karmine Corp", "Karmine Corp Blue"])
    assert r.resolve("Karmine Corp") == "Karmine Corp"  # exact wins
    assert r.resolve("Karmine") is None  # ambiguous containment is refused


# --------------------------------------------------------------------------------------
# PB <-> schedule linking and OE join (synthetic OE frame)
# --------------------------------------------------------------------------------------
class FakeClient:
    live_requests = 0

    def __init__(self, pages):
        self.pages = pages

    def get_pages(self, titles):
        return {t: self.pages.get(t) for t in titles}


def _oe_frame():
    rows = [
        ("LOLTMNT03_337058", "2026-01-14 08:13", 1, "DN SOOPers", "KT Rolster"),
        ("LOLTMNT03_337069", "2026-01-14 09:10", 2, "DN SOOPers", "KT Rolster"),
        ("LOLTMNT03_337081", "2026-01-14 10:05", 3, "KT Rolster", "DN SOOPers"),
        ("X_1", "2026-01-14 11:00", 1, "Dplus Kia", "HANJIN BRION"),  # ids deliberately not Riot's
        ("X_2", "2026-01-14 12:00", 2, "Dplus Kia", "HANJIN BRION"),
    ]
    df = pd.DataFrame(rows, columns=["game_id", "datetime", "game_in_series", "blue_team", "red_team"])
    df["datetime"] = pd.to_datetime(df.datetime)
    df["league"], df["oe_year"] = "LCK", 2026
    return df


def test_build_lp_games_links_schedule(lookup):
    client = FakeClient({PB_PAGE: fx("lp_pb_lck2026_cup_week1.wiki"), DATA_PAGE: fx("lp_data_lck2026_cup_week1.wiki")})
    recs, names = J.build_lp_games(client, lookup, _oe_frame(), [("LCK", 2026, PB_PAGE, ["Week 1"], None)])
    assert len(recs) == 5 and all(r.ms_linked for r in recs)
    r = recs[0]
    assert (r.first_team, r.second_team, r.blue, r.red) == ("KT Rolster", "DN SOOPers", "DN SOOPers", "KT Rolster")
    assert r.ms_first_pick == "KT Rolster" and r.riot_id == "LOLTMNT03_337058"
    assert recs[3].first_bans[:2] == ["Qiyana", "Malphite"] and recs[3].first_picks[1] == "Xin Zhao"
    assert recs[1].first_bans[3] == "Renata Glasc"


def test_match_oe_direct_and_fallback(lookup):
    client = FakeClient({PB_PAGE: fx("lp_pb_lck2026_cup_week1.wiki"), DATA_PAGE: fx("lp_data_lck2026_cup_week1.wiki")})
    oe = _oe_frame()
    recs, _ = J.build_lp_games(client, lookup, oe, [("LCK", 2026, PB_PAGE, ["Week 1"], None)])
    ids = set(oe.game_id)
    assert J.match_oe(recs[0], oe, ids) == ("LOLTMNT03_337058", "riot_id")
    gid, how = J.match_oe(recs[3], oe, ids)  # Riot id not in this OE frame -> fallback
    assert gid == "X_1" and how == "riot id not in OE; date+teams+game"
    assert J.match_oe(recs[4], oe, ids)[0] == "X_2"
    # wrong game number -> no fallback match
    import dataclasses
    bad = dataclasses.replace(recs[4], riot_id=None, game_in_match=3)
    assert J.match_oe(bad, oe, ids) == (None, "no OE game with same teams, game number and date")
    # too far in time -> no match
    late = dataclasses.replace(recs[4], riot_id=None, date_utc=recs[4].date_utc + pd.Timedelta(days=3))
    assert J.match_oe(late, oe, ids)[0] is None
    nodate = dataclasses.replace(recs[4], riot_id=None, date_utc=None)
    assert "no date" in J.match_oe(nodate, oe, ids)[1]


# --------------------------------------------------------------------------------------
# Hand-checked games: full 20-action sequence from the fixture == expected == OE
# --------------------------------------------------------------------------------------
HAND_CHECKED = {
    # LCK Cup 2026 opener, game 1: KT Rolster drafts first from RED (DN SOOPers chose blue side).
    "LOLTMNT03_337058": (PB_PAGE, "lp_pb_lck2026_cup_week1.wiki", 0, "KT Rolster", "Red", [
        "Neeko", "Jayce", "Vi", "Azir", "Sylas", "Orianna",
        "Malphite", "Rumble", "Wukong", "Yunara", "Taliyah", "Ahri",
        "Ambessa", "Rakan", "Nautilus", "Aphelios",
        "Corki", "Alistar", "K'Sante", "Pantheon"]),
    # Same series, game 3: DN SOOPers drafts first from RED.
    "LOLTMNT03_337081": (PB_PAGE, "lp_pb_lck2026_cup_week1.wiki", 2, "DN SOOPers", "Red", [
        "Poppy", "Vi", "Jayce", "Neeko", "Azir", "Orianna",
        "Ambessa", "Aphelios", "Jarvan IV", "Nocturne", "Ziggs", "Thresh",
        "Nautilus", "Akali", "Leona", "Mel",
        "Rek'Sai", "Aurora", "Rell", "Zoe"]),
    # Dplus Kia vs HANJIN BRION game 1: BRION drafts first from RED; "XZ" = Xin Zhao.
    "LOLTMNT03_337109": (PB_PAGE, "lp_pb_lck2026_cup_week1.wiki", 3, "HANJIN BRION", "Red", [
        "Qiyana", "Azir", "Malphite", "Jayce", "Rumble", "Orianna",
        "Neeko", "Jarvan IV", "Ryze", "Xin Zhao", "Taliyah", "Ambessa",
        "Renekton", "Bard", "Kai'Sa", "Lucian",
        "Karma", "Yunara", "Rek'Sai", "Ezreal"]),
    # LCK 2025 Rounds 1-2, HLE vs Gen.G game 1: pre-2026, blue drafts first.
    "LOLTMNT02_242547": (PB25_PAGE, "lp_pb_lck2025_week1_game1.wiki", 0, "Hanwha Life Esports", "Blue", [
        "Varus", "Kalista", "Skarner", "Yone", "Rumble", "Gwen",
        "Naafiri", "Jayce", "Miss Fortune", "Ambessa", "Ezreal", "Vi",
        "Ryze", "Rell", "Sylas", "Poppy",
        "Ahri", "Aurora", "Alistar", "Rakan"]),
}


@pytest.mark.parametrize("game_id", list(HAND_CHECKED))
def test_hand_checked_sequence_from_fixture(game_id, lookup):
    page, fname, idx, first_team, _, expected = HAND_CHECKED[game_id]
    g = lp.parse_picks_and_bans(fx(fname), page)[idx]
    c = lookup.champion
    seq = draft.interleave([c(x) for x in g.team1_bans], [c(x) for x in g.team1_picks],
                           [c(x) for x in g.team2_bans], [c(x) for x in g.team2_picks])
    assert seq == expected
    assert J.TeamResolver([first_team], lookup).resolve(g.team1) == first_team


@pytest.mark.parametrize("game_id", list(HAND_CHECKED))
def test_hand_checked_sequence_matches_oe(game_id):
    if not (config.ACTIONS_PATH.exists() and config.GAMES_PATH.exists()):
        pytest.skip("processed OE parquet not present")
    _, _, _, first_team, first_side, expected = HAND_CHECKED[game_id]
    acts = pd.read_parquet(config.ACTIONS_PATH, filters=[("game_id", "==", game_id)])
    if acts.empty:
        pytest.skip(f"{game_id} not in draft_actions.parquet")
    acts = acts.sort_values("action_index")
    assert [None if draft.is_missing(x) else x for x in acts.champion] == expected
    games = pd.read_parquet(config.GAMES_PATH, filters=[("game_id", "==", game_id)])
    assert games.first_pick_team.iloc[0] == first_team
    assert games.first_pick_side.iloc[0] == first_side


# --------------------------------------------------------------------------------------
# Client: cache, throttle/backoff (network mocked)
# --------------------------------------------------------------------------------------
class _Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise lp.requests.HTTPError(str(self.status_code))


def _page_payload(title, text):
    return {"query": {"pages": {"1": {"title": title, "revisions": [
        {"timestamp": "2026-01-01T00:00:00Z", "slots": {"main": {"*": text}}}]}}}}


def test_client_caches_and_backs_off(tmp_path, monkeypatch):
    monkeypatch.delenv("FANDOM_USER", raising=False)
    sleeps = []
    monkeypatch.setattr(lp.time, "sleep", lambda s: sleeps.append(s))
    client = lp.Client(cache_dir=tmp_path)
    calls = []
    responses = [_Resp({"error": {"code": "ratelimited"}}), _Resp({}, 503), _Resp(_page_payload("A", "hello"))]

    def fake_get(url, params=None, timeout=None):
        calls.append(params)
        return responses[len(calls) - 1]

    monkeypatch.setattr(client._session, "get", fake_get)
    assert client.get_pages(["A"]) == {"A": "hello"}
    assert len(calls) == 3 and client.live_requests == 3
    assert any(s >= 4 for s in sleeps) and any(s >= 8 for s in sleeps)  # exponential backoff
    assert client._session.headers["User-Agent"] == "lol-draft-analyzer/0.1"
    # second call: served from the per-page cache, no request
    assert client.get_pages(["A"]) == {"A": "hello"} and len(calls) == 3
    # a fresh offline client reads the same cache and refuses to go to the network
    off = lp.Client(cache_dir=tmp_path, offline=True)
    assert off.get_pages(["A"]) == {"A": "hello"}
    with pytest.raises(FileNotFoundError):
        off.get_pages(["B"])
    cached = list((tmp_path / "api").glob("*.json"))
    assert len(cached) == 1 and json.loads(cached[0].read_text())["query"]


def test_cargo_requires_login(tmp_path, monkeypatch):
    monkeypatch.delenv("FANDOM_USER", raising=False)
    with pytest.raises(RuntimeError):
        lp.Client(cache_dir=tmp_path, offline=True).cargo("PicksAndBansS7", "GameId", "1=1")
