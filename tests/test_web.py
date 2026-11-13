"""Web API (needs the built models; skipped otherwise)."""
import json
from pathlib import Path

import pytest

from draft_analyzer import config, draft

MODELS = config.DATA_PROCESSED / "models"
pytestmark = pytest.mark.skipif(
    not ((MODELS / "models_live.pkl").exists() and (MODELS / "engine.pkl").exists() and config.GAMES_PATH.exists()),
    reason="models not built",
)


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from draft_analyzer.web.server import app

    return TestClient(app)


@pytest.fixture(scope="module")
def state():
    s = json.loads((Path(__file__).parents[1] / "examples" / "state_new_series.json").read_text())
    s.pop("_comment", None)
    return s


def test_meta(client):
    m = client.get("/api/meta").json()
    assert len(m["champions"]) > 150 and len(m["draft_order"]) == draft.N_ACTIONS
    assert all(len(t["roster"]) == 5 for t in m["teams"]) and m["default_patch"] in m["patches"]
    assert all(abs(sum(c["roles"].values()) - 1) < 0.01 for c in m["champions"])


def test_predict_follows_the_draft(client, state):
    burned = None
    for k in (0, 3, 7, 20):
        # walk the model's own top choice so the draft is always legal
        s = {**state, "actions": [], "top": 5}
        out = None
        for _ in range(k + 1):
            out = client.post("/api/predict", json=s).json()
            if "next_action" not in out:
                break
            s["actions"] = s["actions"] + [out["next_action"]["candidates"][0]["champion"]]
        n = min(k, 20)
        w = out["win"]
        assert 0 < w["p_blue_win"] < 1 and abs(w["p_blue_win"] + w["p_red_win"] - 1) < 1e-3
        assert w["p_blue_win_by_action"][-1] == w["p_blue_win"]
        burned = set(out["game"]["blue_burned"])
        assert len(burned) == 10 and out["game"]["game_in_series"] == 2  # one earlier game, hard Fearless
        if n < 20:
            na = out["next_action"]
            probs = na["probabilities"]
            assert abs(sum(p["probability"] for p in probs) - 1) < 1e-2 and len(na["candidates"]) == 5
            assert na["acting_side"] in ("Blue", "Red")
            if na["action_type"] == "pick":
                assert not burned & {p["champion"] for p in probs}
        else:
            assert "next_action" not in out and len(w["p_blue_win_by_action"]) == 21


def test_predict_rejects_bad_states(client, state):
    r = client.post("/api/predict", json={**state, "actions": ["Olaf", "Olaf"]})
    assert r.status_code == 400 and "twice" in r.json()["detail"]
    r = client.post("/api/predict", json={**state, "actions": ["Not A Champion"]})
    assert r.status_code == 400
    r = client.post("/api/predict", json={k: v for k, v in state.items() if k != "patch"})
    assert r.status_code == 400
    assert client.get("/img/champion/NotAChampion.png").status_code == 404


def test_same_team_on_both_sides_is_rejected(client, state):
    r = client.post("/api/predict", json={**state, "red_team": state["blue_team"], "first_pick_team": state["blue_team"]})
    assert r.status_code == 400 and "different" in r.json()["detail"]


def test_page_and_assets_are_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "/static/app.js" in page.text
    assert client.get("/static/app.js").status_code == 200 and client.get("/static/style.css").status_code == 200
    # a cached script from before an update would break against the new page
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
    # nothing is loaded from another host
    for path in ("/", "/static/app.js", "/static/style.css"):
        text = client.get(path).text
        assert "https://" not in text and "http://" not in text.replace("http://www.w3.org", ""), path
    # the typefaces the stylesheet names are served from here, with their licence
    import re
    fonts = set(re.findall(r'url\("(/static/fonts/[^"]+)"\)', client.get("/static/style.css").text))
    assert len(fonts) == 4 and all(client.get(f).status_code == 200 for f in fonts)
    assert "SIL Open Font License" in client.get("/static/fonts/OFL.txt").text


def test_splash_art_only_for_known_champions(client):
    # only known champion ids are ever fetched from Data Dragon
    assert client.get("/img/splash/NotAChampion.jpg").status_code == 404


def test_about_page_is_served_and_linked_from_the_draft(client):
    r = client.get("/about")
    assert r.status_code == 200 and "Shikhar Joshi" in r.text
    assert 'href="/about"' in client.get("/").text
    assert client.get("/static/about.css").status_code == 200


def test_answers_are_remembered_and_bodies_are_bounded(client, state):
    s = {**state, "actions": ["Azir"]}
    first = client.post("/api/predict", json=s)
    again = client.post("/api/predict", json=s)
    assert first.status_code == again.status_code == 200
    assert again.headers["x-answer"] == "remembered" and again.content == first.content
    assert client.post("/api/predict", json={**s, "actions": ["Vi"]}).headers["x-answer"] == "computed"
    assert client.post("/api/predict", json={**s, "padding": "x" * 70_000}).status_code == 413
    assert client.get("/health").json() == {"status": "ok"}


def test_first_games_share_one_engine_and_keep_their_answers(state, monkeypatch):
    """States without earlier games all read one engine (a copy per matchup cost over 1 GB on the
    public site). Nothing predicted in between may change what such a state is answered."""
    from datetime import datetime as real_datetime

    import draft_analyzer.predict as P

    class Frozen(real_datetime):  # the time of asking enters the features, so hold it still
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 11, 22, tzinfo=tz)

    monkeypatch.setattr(P, "datetime", Frozen)
    first = {**state, "previous_games": [], "actions": ["Azir", "Vi"]}
    expected = P.LiveSession().predict(dict(first), top_k=5, all_probs=True)  # a session that has seen nothing else

    sess = P.LiveSession()
    base = sess.base_engine()
    applied = base.n_applied
    swapped = {**first, "blue_team": first["red_team"], "red_team": first["blue_team"],
               "blue_players": first["red_players"], "red_players": first["blue_players"]}
    sess.predict(dict(swapped), top_k=5)                                          # another matchup, same engine
    sess.predict({**state, "actions": ["Azir"]}, top_k=5)                         # earlier games: an engine of its own
    sess.predict({**first, "datetime": "2027-03-01T00:00:00"}, top_k=5)           # a later clock must not stick
    assert [c["engine"] is base for c in sess._shared.values()] == [True] * 2
    assert [c["engine"] is base for c in sess._cache.values()] == [False]
    assert base.n_applied == applied                                              # no game was applied to it
    sess._shared.clear()                                                          # rebuild the context as well
    assert sess.predict(dict(first), top_k=5, all_probs=True) == expected


def test_rate_limiter_gives_each_address_its_own_budget():
    from draft_analyzer.web.server import RateLimiter

    lim = RateLimiter(per_minute=6)  # one request every 10 s, bursts of 6
    assert [lim.wait_seconds("a", now=0.0) for _ in range(6)] == [0.0] * 6
    assert lim.wait_seconds("a", now=0.0) == pytest.approx(10.0)
    assert lim.wait_seconds("b", now=0.0) == 0.0             # another address is unaffected
    assert lim.wait_seconds("a", now=4.0) == pytest.approx(6.0)
    assert lim.wait_seconds("a", now=10.5) == 0.0            # one token is back
    assert lim.wait_seconds("a", now=10.5) > 0
    assert RateLimiter(per_minute=0).wait_seconds("a", now=0.0) == 0.0  # 0 = no limit


def test_visitor_address_comes_from_the_proxy_only_when_trusted(monkeypatch):
    from types import SimpleNamespace

    from draft_analyzer.web.server import client_address

    req = SimpleNamespace(headers={"x-real-ip": "203.0.113.9", "x-forwarded-for": "1.2.3.4, 198.51.100.2"},
                          client=SimpleNamespace(host="10.0.0.1"))
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    assert client_address(req) == "10.0.0.1"                  # a visitor cannot pick their own address
    monkeypatch.setenv("TRUST_PROXY", "1")
    assert client_address(req) == "203.0.113.9"
    req.headers = {"x-forwarded-for": "1.2.3.4, 198.51.100.2"}
    assert client_address(req) == "198.51.100.2"              # the entry the proxy appended, not the visitor's
