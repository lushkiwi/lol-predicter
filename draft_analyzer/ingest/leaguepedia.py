"""Leaguepedia (lol.fandom.com) client and page-source parsers.

Two access routes:

* **Cargo** (``FANDOM_USER`` + ``FANDOM_BOT_PASSWORD`` set): log in with mwclient and run
  ``cargoquery``. Anonymous cargoquery is rate-limited to the point of being unusable
  (see docs/research_leaguepedia.md). UNTESTED: no credentials were available.
* **Page source** (default, anonymous): ``action=query&prop=revisions`` wikitext of the
  "<Overview>/Picks and Bans/..." pages and the "Data:<Overview>" MatchSchedule pages,
  parsed here into per-game records.

Every live response is cached as JSON under ``LEAGUEPEDIA_DIR`` and never re-requested.
Requests are throttled to one every ``MIN_INTERVAL`` seconds with exponential backoff.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import requests

from draft_analyzer import config

log = logging.getLogger(__name__)

API_URL = "https://lol.fandom.com/api.php"
USER_AGENT = "lol-draft-analyzer/0.1"
MIN_INTERVAL = 2.0
MAX_RETRIES = 6
CACHE_DIR: Path = config.LEAGUEPEDIA_DIR
TITLES_PER_REQUEST = 20  # prop=revisions content for several pages in one request


# --------------------------------------------------------------------------------------
# HTTP client with throttle, backoff and disk cache
# --------------------------------------------------------------------------------------
class RateLimited(RuntimeError):
    pass


def _cache_key(params: dict) -> str:
    canon = json.dumps({k: params[k] for k in sorted(params)}, sort_keys=True, ensure_ascii=False)
    readable = re.sub(r"[^A-Za-z0-9]+", "_", str(params.get("titles") or params.get("tables") or params.get("action")))[:60]
    return f"{params.get('action', 'x')}_{readable}_{hashlib.sha1(canon.encode()).hexdigest()[:12]}"


class Client:
    """Anonymous page-source client (default) with optional Cargo login."""

    def __init__(self, cache_dir: Path | None = None, offline: bool = False):
        self.cache_dir = Path(cache_dir or CACHE_DIR)
        self.offline = offline
        self.live_requests = 0
        self._last = 0.0
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._site = None  # mwclient.Site when logged in
        user, pw = os.environ.get("FANDOM_USER"), os.environ.get("FANDOM_BOT_PASSWORD")
        if user and pw and not offline:
            self._login(user, pw)

    # -- auth (UNTESTED: no credentials available) --
    def _login(self, user: str, pw: str) -> None:
        import mwclient

        self._site = mwclient.Site("lol.fandom.com", path="/", clients_useragent=USER_AGENT)
        self._site.login(user, pw)

    @property
    def logged_in(self) -> bool:
        return self._site is not None

    # -- raw api --
    def _throttle(self) -> None:
        wait = MIN_INTERVAL - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def api(self, **params) -> dict:
        params = {"format": "json", **params}
        path = self.cache_dir / "api" / f"{_cache_key(params)}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if self.offline:
            raise FileNotFoundError(f"not cached (offline): {params}")
        delay = 4.0
        for attempt in range(MAX_RETRIES):
            self._throttle()
            self.live_requests += 1
            try:
                if self._site is not None:
                    p = dict(params)
                    action = p.pop("action")
                    p.pop("format", None)
                    data = self._site.api(action, **p)
                else:
                    resp = self._session.get(API_URL, params=params, timeout=60)
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise RateLimited(f"HTTP {resp.status_code}")
                    resp.raise_for_status()
                    data = resp.json()
                if isinstance(data, dict) and data.get("error", {}).get("code") == "ratelimited":
                    raise RateLimited("ratelimited")
            except (RateLimited, requests.RequestException, ValueError) as e:
                log.warning("request failed (%s), attempt %d, sleeping %.0fs", e, attempt + 1, delay)
                time.sleep(delay)
                delay *= 2
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return data
        raise RateLimited(f"gave up after {MAX_RETRIES} attempts: {params}")

    # -- page source --
    def get_pages(self, titles: Iterable[str]) -> dict[str, str | None]:
        """Wikitext of each title (None if the page does not exist). Follows redirects."""
        titles = list(dict.fromkeys(titles))
        out: dict[str, str | None] = {}
        pdir = self.cache_dir / "pages"
        todo = []
        for t in titles:
            f = pdir / f"{_safe(t)}.json"
            if f.exists():
                out[t] = json.loads(f.read_text(encoding="utf-8"))["content"]
            else:
                todo.append(t)
        for i in range(0, len(todo), TITLES_PER_REQUEST):
            chunk = todo[i : i + TITLES_PER_REQUEST]
            data = self.api(action="query", prop="revisions", rvprop="content|timestamp", rvslots="main",
                            redirects=1, titles="|".join(chunk))
            got = _pages_from_query(data)
            for t in chunk:
                content, ts = got.get(t, (None, None))
                pdir.mkdir(parents=True, exist_ok=True)
                (pdir / f"{_safe(t)}.json").write_text(
                    json.dumps({"title": t, "timestamp": ts, "content": content}, ensure_ascii=False), encoding="utf-8")
                out[t] = content
        return out

    def list_subpages(self, prefix: str, namespace: int = 0) -> list[str]:
        titles, cont = [], {}
        while True:
            data = self.api(action="query", list="allpages", apprefix=prefix, apnamespace=namespace,
                            aplimit="max", **cont)
            titles += [p["title"] for p in data.get("query", {}).get("allpages", [])]
            if "continue" not in data:
                return titles
            cont = {"apcontinue": data["continue"]["apcontinue"]}

    # -- cargo (UNTESTED) --
    def cargo(self, tables: str, fields: str, where: str, join_on: str | None = None,
              order_by: str | None = None, page_size: int = 500) -> list[dict]:
        if not self.logged_in:
            raise RuntimeError("cargoquery needs FANDOM_USER/FANDOM_BOT_PASSWORD (anonymous is rate-limited)")
        rows, offset = [], 0
        while True:
            p = dict(action="cargoquery", tables=tables, fields=fields, where=where, limit=page_size, offset=offset)
            if join_on:
                p["join_on"] = join_on
            if order_by:
                p["order_by"] = order_by
            batch = [r["title"] for r in self.api(**p).get("cargoquery", [])]
            rows += batch
            if len(batch) < page_size:
                return rows
            offset += page_size


def _safe(title: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("_")
    return f"{s[:120]}_{hashlib.sha1(title.encode()).hexdigest()[:8]}"


def _pages_from_query(data: dict) -> dict[str, tuple[str | None, str | None]]:
    q = data.get("query", {})
    alias = {}
    for k in ("normalized", "redirects"):
        for m in q.get(k, []):
            alias[m["from"]] = m["to"]
    by_title = {}
    for p in q.get("pages", {}).values():
        if "missing" in p or not p.get("revisions"):
            by_title[p["title"]] = (None, None)
            continue
        rev = p["revisions"][0]
        content = rev.get("slots", {}).get("main", {}).get("*", rev.get("*"))
        by_title[p["title"]] = (content, rev.get("timestamp"))
    out = dict(by_title)
    for src in alias:  # map requested names onto final titles (redirect chains)
        dst = src
        for _ in range(5):
            dst = alias.get(dst, dst)
        out[src] = by_title.get(dst, (None, None))
    return out


# --------------------------------------------------------------------------------------
# Wikitext template parsing
# --------------------------------------------------------------------------------------
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_HEADING = re.compile(r"^(={2,6})\s*(.*?)\s*\1\s*$", re.M)


def strip_comments(text: str) -> str:
    return _COMMENT.sub("", text)


def find_templates(text: str, name: str) -> list[tuple[int, str]]:
    """(start offset, inner body) of every top-level ``{{name|...}}`` occurrence, braces balanced."""
    out, i = [], 0
    pat = re.compile(r"\{\{\s*" + re.escape(name) + r"\s*(\||\}\})")
    while True:
        m = pat.search(text, i)
        if not m:
            return out
        depth, j = 0, m.start()
        while j < len(text):
            if text.startswith("{{", j):
                depth += 1
                j += 2
            elif text.startswith("}}", j):
                depth -= 1
                j += 2
                if depth == 0:
                    break
            else:
                j += 1
        body = text[m.start() + 2 : j - 2]
        out.append((m.start(), body))
        i = j


def template_params(body: str) -> dict[str, str]:
    """Split a template body on top-level ``|`` into named params (positional ones as '1','2'...)."""
    parts, depth, cur, k = [], 0, [], 0
    while k < len(body):
        two = body[k : k + 2]
        if two in ("{{", "[["):
            depth += 1
            cur.append(two)
            k += 2
            continue
        if two in ("}}", "]]"):
            depth -= 1
            cur.append(two)
            k += 2
            continue
        ch = body[k]
        if ch == "|" and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        k += 1
    parts.append("".join(cur))
    params, pos = {}, 0
    for p in parts[1:]:  # parts[0] is the template name
        if "=" in p and not p.split("=", 1)[0].strip().startswith("{"):
            key, val = p.split("=", 1)
            params[key.strip().lower()] = val.strip()
        else:
            pos += 1
            params[str(pos)] = p.strip()
    return params


def headings_at(text: str) -> list[tuple[int, int, str]]:
    """(offset, level, title) of every section heading."""
    return [(m.start(), len(m.group(1)), m.group(2).strip()) for m in _HEADING.finditer(text)]


def _tab_at(heads: list[tuple[int, int, str]], offset: int) -> str | None:
    """Most recent level-2 heading before ``offset`` (Leaguepedia's tab for PB/Scoreboard pages)."""
    tab = None
    for off, lvl, title in heads:
        if off > offset:
            break
        if lvl == 2:
            tab = title
    return tab


def _int(x) -> int | None:
    try:
        return int(str(x).strip())
    except (TypeError, ValueError):
        return None


def _yes(x) -> bool:
    return str(x or "").strip().lower() in ("yes", "true", "1", "y")


# Values Leaguepedia uses for a ban slot that was not used.
MISSED_BAN_VALUES = {"", "none", "loss of ban", "lossofban", "missing data", "no ban", "-", "nb"}


# --------------------------------------------------------------------------------------
# PicksAndBansS7 pages
# --------------------------------------------------------------------------------------
@dataclass
class PBGame:
    page: str
    overview: str
    tab: str | None
    n_in_tab: int  # 1-based order of this template within its tab
    match_in_tab: int
    game_in_match: int
    team1: str  # the team in the template's "blue" slot = drafts first (2026 convention)
    team2: str
    switched_sides: bool | None  # True: team1 was on red
    winner: int | None  # 1/2 relative to team1/team2
    team1_bans: list = field(default_factory=list)
    team2_bans: list = field(default_factory=list)
    team1_picks: list = field(default_factory=list)
    team2_picks: list = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)


def overview_of(page: str) -> str:
    return page.split("/Picks and Bans")[0]


def _champ(v: str | None) -> str | None:
    v = (v or "").strip()
    return None if v.lower() in MISSED_BAN_VALUES else v


def parse_picks_and_bans(text: str, page: str) -> list[PBGame]:
    text = strip_comments(text)
    heads = headings_at(text)
    games: list[PBGame] = []
    per_tab: dict = {}
    for off, body in find_templates(text, "PicksAndBansS7"):
        p = template_params(body)
        tab = _tab_at(heads, off)
        st = per_tab.setdefault(tab, {"n": 0, "match": 0, "last": None})
        st["n"] += 1
        t1, t2 = p.get("blueteam", ""), p.get("redteam", "")
        s1, s2 = _int(p.get("team1score")), _int(p.get("team2score"))
        gnum = s1 + s2 if s1 is not None and s2 is not None else None
        last = st["last"]
        new_match = (
            _yes(p.get("game1"))
            or last is None
            or (gnum is not None and last["g"] is not None and gnum <= last["g"])
            or (gnum == 1)
        )
        if new_match:
            st["match"] += 1
        if gnum is None:
            gnum = 1 if new_match else (last["g"] or 0) + 1
        st["last"] = {"g": gnum}
        ss = p.get("switched_sides")
        games.append(
            PBGame(
                page=page,
                overview=overview_of(page),
                tab=tab,
                n_in_tab=st["n"],
                match_in_tab=st["match"],
                game_in_match=gnum,
                team1=t1,
                team2=t2,
                switched_sides=None if ss is None or ss.strip() == "" else _yes(ss),
                winner=_int(p.get("winner")),
                team1_bans=[_champ(p.get(f"blueban{i}")) for i in range(1, 6)],
                team2_bans=[_champ(p.get(f"red_ban{i}", p.get(f"redban{i}"))) for i in range(1, 6)],
                team1_picks=[_champ(p.get(f"bluepick{i}")) for i in range(1, 6)],
                team2_picks=[_champ(p.get(f"red_pick{i}", p.get(f"redpick{i}"))) for i in range(1, 6)],
                raw=p,
            )
        )
    return games


# --------------------------------------------------------------------------------------
# MatchSchedule data pages ("Data:<OverviewPage>")
# --------------------------------------------------------------------------------------
@dataclass
class MSGame:
    page: str
    overview: str
    tab: str | None
    match_in_tab: int
    game_in_match: int
    match_team1: str
    match_team2: str
    date_utc: "pd.Timestamp | None"  # match start (games start later)
    blue: str | None
    red: str | None
    winner: int | None  # 1 = blue won, 2 = red won
    first_pick: str | None
    first_sel: str | None
    side_sel: str | None
    pick_sel: str | None
    riot_platform_game_id: str | None
    ff: str | None
    raw: dict = field(default_factory=dict, repr=False)


# Leaguepedia MatchSchedule timezone codes -> UTC offset in hours (standard time).
TZ_OFFSET = {"PST": -8, "EST": -5, "CET": 1, "KST": 9, "CST": 8, "UTC": 0, "GMT": 0, "BRT": -3}
# Zones with no daylight saving: dst= is ignored for these.
TZ_NO_DST = {"KST", "CST", "UTC", "GMT", "BRT"}


def ms_datetime_utc(date: str | None, time_: str | None, tz: str | None, dst: str | None):
    import pandas as pd

    if not date:
        return None
    try:
        ts = pd.Timestamp(f"{date.strip()} {(time_ or '00:00').strip()}")
    except (ValueError, TypeError):
        return None
    tz = (tz or "UTC").strip().upper()
    off = TZ_OFFSET.get(tz, 0)
    if tz not in TZ_NO_DST and (dst or "").strip().lower() in ("yes", "spring", "fall", "true"):
        off += 1
    return ts - pd.Timedelta(hours=off)


def parse_match_schedule(text: str, page: str) -> list[MSGame]:
    text = strip_comments(text)
    starts = [(off, template_params(b).get("tab")) for off, b in find_templates(text, "MatchSchedule/Start")]
    out: list[MSGame] = []
    match_n: dict = {}
    for off, body in find_templates(text, "MatchSchedule"):
        tab = None
        for s_off, s_tab in starts:
            if s_off < off:
                tab = s_tab
        tab = tab.strip() if tab else None
        p = template_params(body)
        match_n[tab] = match_n.get(tab, 0) + 1
        when = ms_datetime_utc(p.get("date"), p.get("time"), p.get("timezone"), p.get("dst"))
        for k in sorted((k for k in p if re.fullmatch(r"game\d+", k)), key=lambda k: int(k[4:])):
            inner = find_templates(p[k], "MatchSchedule/Game")
            if not inner:
                continue
            g = template_params(inner[0][1])
            if not (g.get("blue") or g.get("red") or g.get("riot_platform_game_id")):
                continue  # unplayed game slot
            if (g.get("ff") or "").strip() or _yes(g.get("remake")):
                pass  # kept; join will simply not find it in OE
            out.append(
                MSGame(
                    page=page,
                    overview=page.removeprefix("Data:"),
                    tab=tab,
                    match_in_tab=match_n[tab],
                    game_in_match=int(k[4:]),
                    match_team1=p.get("team1", "").strip(),
                    match_team2=p.get("team2", "").strip(),
                    date_utc=when,
                    blue=(g.get("blue") or "").strip() or None,
                    red=(g.get("red") or "").strip() or None,
                    winner=_int(g.get("winner")),
                    first_pick=(g.get("first_pick") or "").strip() or None,
                    first_sel=(g.get("first_sel") or "").strip() or None,
                    side_sel=(g.get("ssel") or "").strip() or None,
                    pick_sel=(g.get("pick_sel") or "").strip() or None,
                    riot_platform_game_id=(g.get("riot_platform_game_id") or "").strip() or None,
                    ff=(g.get("ff") or "").strip() or None,
                    raw=g,
                )
            )
    return out


# --------------------------------------------------------------------------------------
# Name resolution via the wiki's own lookup modules (Module:Championnames, Module:Teamnames)
# --------------------------------------------------------------------------------------
_LUA_TABLE_ENTRY = re.compile(r'\[\s*"((?:[^"\\]|\\.)*)"\s*\]\s*=\s*(\{[^{}]*\}|"(?:[^"\\]|\\.)*")')
_LUA_LINK = re.compile(r'\blink\s*=\s*"((?:[^"\\]|\\.)*)"')


def parse_lua_lookup(text: str) -> dict[str, str]:
    """Parse a Leaguepedia name module into {lowercased alias: canonical link name}.

    Entries are either ``["alias"] = "other key"`` (a pointer) or ``["key"] = { link = "Name", ... }``.
    """
    direct, pointer = {}, {}
    for key, val in _LUA_TABLE_ENTRY.findall(text):
        key = key.replace('\\"', '"').lower()
        if val.startswith("{"):
            m = _LUA_LINK.search(val)
            if m:
                direct[key] = m.group(1).replace('\\"', '"')
        else:
            pointer[key] = val[1:-1].replace('\\"', '"').lower()
    out = dict(direct)
    for key, tgt in pointer.items():
        for _ in range(5):
            if tgt in direct:
                out[key] = direct[tgt]
                break
            tgt = pointer.get(tgt, tgt)
    return out


class NameLookup:
    """Resolves editor-entered champion / team names to the wiki's canonical link names."""

    def __init__(self, champions: dict[str, str], teams: dict[str, str]):
        self.champions = champions
        self.teams = teams

    @classmethod
    def from_client(cls, client: Client) -> "NameLookup":
        p = client.get_pages(["Module:Championnames", "Module:Teamnames"])
        return cls(parse_lua_lookup(p["Module:Championnames"] or ""), parse_lua_lookup(p["Module:Teamnames"] or ""))

    def champion(self, name: str | None) -> str | None:
        if name is None:
            return None
        key = name.strip().lower()
        link = self.champions.get(key, name.strip())
        return None if link in ("None", "Missing Data") or key in MISSED_BAN_VALUES else link

    def team(self, name: str | None) -> str | None:
        if not name:
            return None
        return self.teams.get(name.strip().lower(), name.strip())
