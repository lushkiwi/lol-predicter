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
    GET  /health                 200 once the models are loaded

The server keeps the models and the as-of engine in memory (``predict.LiveSession``), so a
prediction takes a fraction of a second. By default it listens on localhost only.

Hosting it publicly is a matter of environment variables (see ``Dockerfile``):
    HOST, PORT            where to listen (a host sets PORT; HOST=0.0.0.0 inside a container)
    TRUST_PROXY=1         take the visitor's address from the proxy's X-Real-IP header (only behind a host's proxy)
    PREDICT_PER_MINUTE    new predictions one address may ask for per minute (default 40, 0 = no limit;
                          answers the server already remembers are free)

A prediction is computed by one request at a time (about 1.8 CPU-seconds, mostly the exact
explanations). Three things keep that from becoming a queue: answers are remembered, so a state
somebody already asked for (everyone's first view, the popular lines of a draft) costs nothing;
each address has a per-minute budget; and when too many requests are already waiting the server
says so at once instead of making everyone wait longer.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

import pandas as pd
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import config, draft
from ..ingest import ddragon
from ..predict import LiveSession, StateError

STATIC = Path(__file__).parent / "static"
ROLE_LABELS = ("top", "jungle", "mid", "bot", "support")
RECENT_TEAM_DAYS = 400

MAX_STATE_BYTES = 64 * 1024  # a full five-game series with rosters is under 10 kB
MAX_WAITING = 8              # requests allowed to queue for the model before new ones are told to retry
CACHE_ENTRIES = 600          # remembered answers, about 30 kB each

app = FastAPI(title="LoL draft analyzer")
_lock = threading.Lock()
_state: dict = {}


class RateLimiter:
    """Token bucket per visitor address: ``per_minute`` requests a minute, bursts up to that many.
    In memory only, which is right for one small server; it forgets on restart."""

    def __init__(self, per_minute: float, max_clients: int = 20000):
        self.rate, self.capacity, self.max_clients = per_minute / 60.0, float(per_minute), max_clients
        self._buckets: dict[str, tuple[float, float]] = {}
        self._guard = threading.Lock()

    def wait_seconds(self, client: str, now: float | None = None) -> float:
        """0 if the request may go ahead (and it is counted), else how long to wait."""
        if self.capacity <= 0:
            return 0.0
        now = time.monotonic() if now is None else now
        with self._guard:
            tokens, last = self._buckets.get(client, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < 1.0:
                self._buckets[client] = (tokens, now)
                return (1.0 - tokens) / self.rate
            if len(self._buckets) >= self.max_clients and client not in self._buckets:
                self._buckets.clear()  # crude, but bounds memory under a flood of addresses
            self._buckets[client] = (tokens - 1.0, now)
            return 0.0


limiter = RateLimiter(float(os.environ.get("PREDICT_PER_MINUTE", "40")))


class AnswerCache:
    """The last ``size`` distinct prediction answers, as the bytes that were sent. The models and
    the data do not change while the server runs, so an answer never goes stale."""

    def __init__(self, size: int):
        self.size, self.hits, self.misses = size, 0, 0
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._guard = threading.Lock()

    def get(self, key: str) -> bytes | None:
        with self._guard:
            body = self._items.get(key)
            if body is None:
                self.misses += 1
                return None
            self._items.move_to_end(key)
            self.hits += 1
            return body

    def put(self, key: str, body: bytes) -> None:
        with self._guard:
            self._items[key] = body
            self._items.move_to_end(key)
            while len(self._items) > self.size:
                self._items.popitem(last=False)


answers = AnswerCache(CACHE_ENTRIES)
_waiting = 0
_waiting_guard = threading.Lock()


def client_address(request: Request) -> str:
    """The visitor's address. Behind a host's proxy every request comes from the proxy, so with
    TRUST_PROXY=1 the address the proxy reports is used: X-Real-IP (Railway sets it), else the
    last entry of X-Forwarded-For (earlier entries are whatever the visitor chose to send).
    Never set TRUST_PROXY when visitors reach the server directly: they could then pick their address."""
    if os.environ.get("TRUST_PROXY") == "1":
        real = request.headers.get("x-real-ip", "").strip()
        if real:
            return real
        forwarded = [x.strip() for x in request.headers.get("x-forwarded-for", "").split(",") if x.strip()]
        if forwarded:
            return forwarded[-1]
    return request.client.host if request.client else "unknown"


def session() -> LiveSession:
    sess = _state.get("session")
    if sess is None:  # the lock is only for the first load; later calls must not queue behind a prediction
        with _lock:
            if "session" not in _state:
                _state["session"] = LiveSession()
            sess = _state["session"]
    return sess


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
        "n_games": int(len(g)),
        "win_note": "On held-out games the full win model is not measurably more accurate than the baseline "
                    "(team strength + side + first pick), so the draft's effect on win probability is the model's "
                    "opinion, not a tested signal.",
    }


@app.get("/health")
def health() -> dict:
    """For the host's deploy check: answers once the models are loaded and a draft can be scored."""
    session()
    return {"status": "ok"}


@app.get("/api/meta")
def meta() -> dict:
    sess = session()
    with _lock:
        if "meta" not in _state:
            _state["meta"] = build_meta(sess)
        return _state["meta"]


@app.post("/api/predict")
def predict(state: dict, request: Request) -> Response:
    global _waiting
    if int(request.headers.get("content-length") or 0) > MAX_STATE_BYTES:
        raise HTTPException(status_code=413, detail="the draft state is too large")
    key = json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)
    body = answers.get(key)
    if body is not None:  # free: it does not count against the address's budget
        return Response(content=body, media_type="application/json", headers={"X-Answer": "remembered"})
    wait = limiter.wait_seconds(client_address(request))
    if wait > 0:
        raise HTTPException(status_code=429, detail=f"too many predictions from this address; try again in {wait:.0f} s",
                            headers={"Retry-After": str(int(wait) + 1)})
    sess = session()
    top = int(state.pop("top", 12))
    with _waiting_guard:
        if _waiting >= MAX_WAITING:
            raise HTTPException(status_code=503, detail="busy: many drafts are being predicted right now; try again in a moment",
                                headers={"Retry-After": "3"})
        _waiting += 1
    try:
        with _lock:
            body = answers.get(key)  # somebody ahead in the queue may have asked for the same state
            if body is None:
                out = sess.predict(state, top_k=top, all_probs=True)
                body = json.dumps(out, separators=(",", ":"), allow_nan=False).encode()
                answers.put(key, body)
    except StateError as e:
        raise HTTPException(status_code=400, detail=str(e).removeprefix("state file: "))
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"missing field in state: {e}")
    finally:
        with _waiting_guard:
            _waiting -= 1
    return Response(content=body, media_type="application/json", headers={"X-Answer": "computed"})


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


class _RevalidatedStatic(StaticFiles):
    """Static files the browser must revalidate, so an updated page never runs against an old script."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", _RevalidatedStatic(directory=STATIC), name="static")


def main(argv=None) -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description="Web UI for the draft analyzer (local by default; HOST and PORT for hosting)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    args = ap.parse_args(argv)
    print(f"Draft analyzer UI: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
