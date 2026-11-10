import json

import pytest

from draft_analyzer.ingest import ddragon

FIXTURE = {"data": {
    "MonkeyKing": {"id": "MonkeyKing", "key": "62", "name": "Wukong", "tags": ["Fighter", "Tank"],
                   "partype": "Mana", "info": {"attack": 8, "defense": 5, "magic": 2, "difficulty": 3},
                   "stats": {"hp": 600, "hpperlevel": 99, "movespeed": 345}},
    "Ahri": {"id": "Ahri", "key": "103", "name": "Ahri", "tags": ["Mage", "Assassin"],
             "partype": "Mana", "info": {"attack": 3, "defense": 4, "magic": 8, "difficulty": 5},
             "stats": {"hp": 590, "hpperlevel": 104, "movespeed": 330}},
}}


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ddragon, "CACHE_DIR", tmp_path)
    return tmp_path


def no_network(*a, **k):
    raise AssertionError("network used")


def test_parse_columns_and_tags():
    df = ddragon.parse_champions(FIXTURE)
    assert list(df["champion"]) == ["Ahri", "Wukong"]
    for col in ["ddragon_id", "ddragon_key", "ddragon_name", "tags", "partype", "info_attack",
                "info_difficulty", "stat_hp", "stat_hpperlevel", "stat_movespeed",
                "tag_Fighter", "tag_Tank", "tag_Mage", "tag_Assassin", "tag_Marksman", "tag_Support"]:
        assert col in df.columns
    w = df[df.champion == "Wukong"].iloc[0]
    assert w.ddragon_key == 62 and w.ddragon_id == "MonkeyKing"
    assert w.tag_Fighter and w.tag_Tank and not w.tag_Mage and not w.tag_Support
    a = df[df.champion == "Ahri"].iloc[0]
    assert a.tag_Mage and a.tag_Assassin and not a.tag_Fighter


def test_alias_applied(monkeypatch):
    monkeypatch.setitem(ddragon.ALIASES, "Wukong", "Wukong2")
    df = ddragon.parse_champions(FIXTURE)
    row = df[df.ddragon_id == "MonkeyKing"].iloc[0]
    assert row.champion == "Wukong2" and row.ddragon_name == "Wukong"


def test_cache_used_instead_of_network(cache, monkeypatch):
    (cache / "champion_1.0.json").write_text(json.dumps(FIXTURE))
    monkeypatch.setattr(ddragon, "_fetch_json", no_network)
    assert len(ddragon.load_champions("1.0")) == 2


def test_fetch_then_cache(cache, monkeypatch):
    calls = []

    def fake(url):
        calls.append(url)
        return ["2.0", "1.0"] if url.endswith("versions.json") else FIXTURE

    monkeypatch.setattr(ddragon, "_fetch_json", fake)
    assert len(ddragon.load_champions()) == 2
    assert (cache / "versions.json").exists() and (cache / "champion_2.0.json").exists()
    assert len(calls) == 2
    ddragon.load_champions()
    assert len(calls) == 2
    ddragon.load_champions(refresh=True)
    assert len(calls) == 4
