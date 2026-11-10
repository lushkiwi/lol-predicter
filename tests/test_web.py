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
    # nothing is loaded from another host
    for path in ("/", "/static/app.js", "/static/style.css"):
        text = client.get(path).text
        assert "https://" not in text and "http://" not in text.replace("http://www.w3.org", ""), path
