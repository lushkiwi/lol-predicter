"""Local web UI: click through a draft and see the model's next pick/ban probabilities and the
win probability update after every action.

    python -m draft_analyzer.web            # http://127.0.0.1:8000
    python -m draft_analyzer.web --port 8010

Endpoints
    GET  /                       the page (static/index.html)
    GET  /api/meta               champions, teams with their latest rosters, leagues, patches, draft order
    POST /api/predict            body: a draft state (same fields as the CLI state file); returns predictions
    GET  /img/champion/<id>.png  champion icon from Data Dragon, cached on disk after the first request
    GET  /img/splash/<id>.jpg    champion splash art (face-centred crop) from Data Dragon, cached the same way
    GET  /classic                the previous look of the page, kept for comparison

The server keeps the models and the as-of engine in memory (``predict.LiveSession``), so a
prediction takes a fraction of a second. It listens on localhost only.
"""
from __future__ import annotations

import argparse
import threading
from pathlib import Path

import pandas as pd
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import config, draft
from ..ingest import ddragon
from ..predict import LiveSession, StateError

STATIC = Path(__file__).parent / "static"
ROLE_LABELS = ("top", "jungle", "mid", "bot", "support")
RECENT_TEAM_DAYS = 400

app = FastAPI(title="LoL draft analyzer")
_lock = threading.Lock()
_state: dict = {}


def session() -> LiveSession:
    with _lock:
        if "session" not in _state:
            _state["session"] = LiveSession()
        return _state["session"]


def build_meta(sess: LiveSession) -> dict:
    champs = pd.read_parquet(config.CHAMPIONS_PATH)
    engine = sess.new_engine()
    roles = engine.block("roles")
    counts = roles.counts.read(engine.t_now)  # (C, 5) recent games per champion and role
    role_probs = (counts + roles.prior_strength / 5.0) / (counts.sum(axis=1, keepdims=True) + roles.prior_strength)
    by_name = champs.set_index("champion")
    champions = []
    for i, name in enumerate(engine.champs.names):
        row = by_name.loc[name]
        probs = [round(float(x), 3) for x in role_probs[i]]
        champions.append({"name": name, "id": row["ddragon_id"], "tags": list(row["tags"]),
                          "roles": dict(zip(ROLE_LABELS, probs)),
                          "primary_role": ROLE_LABELS[max(range(5), key=lambda r: probs[r])]})

    g = sess.games.sort_values("datetime")
    recent = g[g["datetime"] >= g["datetime"].max() - pd.Timedelta(days=RECENT_TEAM_DAYS)]
    teams: dict[str, dict] = {}
    for row in recent.itertuples():
        for side in ("blue", "red"):
            name = getattr(row, f"{side}_team")
            t = teams.setdefault(name, {"name": name, "league": row.league})
            if row.league not in config.INTERNATIONAL_LEAGUES:
                t["league"] = row.league  # latest domestic league
            roster = list(getattr(row, f"{side}_player_names"))
            if all(p is not None for p in roster):
                t["roster"] = roster
            t["last_game"] = str(row.datetime)[:10]
    team_list = sorted((t for t in teams.values() if "roster" in t), key=lambda t: (t["league"], t["name"].lower()))
    patches = list(dict.fromkeys(recent["patch"].tolist()[::-1]))[:12]
    return {
        "ddragon_version": ddragon.latest_version(),
        "champions": champions,
        "teams": team_list,
        "leagues": [x for x in config.LEAGUES if x in set(recent["league"])],
        "patches": patches,
        "default_patch": patches[0],
        "roles": list(ROLE_LABELS),
        "draft_order": [{"index": i, "type": draft.ACTION_TYPE[i], "slot": draft.ACTION_SLOT[i], "phase": draft.ACTION_PHASE[i]}
                        for i in range(draft.N_ACTIONS)],
        "data_through": str(g["datetime"].max())[:10],
        "win_note": "On held-out games the full win model is not measurably more accurate than the baseline "
                    "(team strength + side + first pick), so the draft's effect on win probability is the model's "
                    "opinion, not a tested signal.",
    }


@app.get("/api/meta")
def meta() -> dict:
    sess = session()
    with _lock:
        if "meta" not in _state:
            _state["meta"] = build_meta(sess)
        return _state["meta"]


@app.post("/api/predict")
def predict(state: dict) -> JSONResponse:
    sess = session()
    top = int(state.pop("top", 12))
    try:
        with _lock:
            out = sess.predict(state, top_k=top, all_probs=True)
    except StateError as e:
        raise HTTPException(status_code=400, detail=str(e).removeprefix("state file: "))
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"missing field in state: {e}")
    return JSONResponse(out)


@app.get("/img/champion/{name}.png")
def champion_icon(name: str) -> FileResponse:
    m = meta()
    if name not in {c["id"] for c in m["champions"]}:
        raise HTTPException(status_code=404, detail="unknown champion")
    version = m["ddragon_version"]
    path = config.DDRAGON_DIR / "img" / version / f"{name}.png"
    if not path.exists():  # cached forever once fetched
        resp = requests.get(f"{ddragon.BASE_URL}/cdn/{version}/img/champion/{name}.png",
                            headers={"User-Agent": ddragon.USER_AGENT}, timeout=30)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Data Dragon did not return the icon")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})


@app.get("/img/splash/{name}.jpg")
def champion_splash(name: str) -> FileResponse:
    """Splash art for the pick slots. Not versioned by Data Dragon, so it is cached by champion id alone."""
    if name not in {c["id"] for c in meta()["champions"]}:
        raise HTTPException(status_code=404, detail="unknown champion")
    path = config.DDRAGON_DIR / "img" / "splash" / f"{name}.jpg"
    if not path.exists():  # cached forever once fetched
        resp = requests.get(f"{ddragon.BASE_URL}/cdn/img/champion/centered/{name}_0.jpg",
                            headers={"User-Agent": ddragon.USER_AGENT}, timeout=30)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Data Dragon did not return the splash art")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/classic")
def classic() -> FileResponse:
    return FileResponse(STATIC / "classic" / "index.html", headers={"Cache-Control": "no-cache"})


class _RevalidatedStatic(StaticFiles):
    """Static files the browser must revalidate, so an updated page never runs against an old script."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", _RevalidatedStatic(directory=STATIC), name="static")


def main(argv=None) -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description="Local web UI for the draft analyzer")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    print(f"Draft analyzer UI: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
