"""Join Leaguepedia draft records to Oracle's Elixir games and cross-check the draft order.

Run: ``python -m draft_analyzer.ingest.join`` (uses cached pages; fetches missing ones).

Pipeline
1. For each sampled "Picks and Bans" page/tab, parse the PicksAndBansS7 templates (draft) and the
   matching ``Data:<OverviewPage>`` MatchSchedule games (date, sides, first_pick, Riot game id).
2. Resolve every editor-entered team name with the wiki's Module:Teamnames, then map it onto the
   Oracle's Elixir (OE) team name of that league-season (:class:`TeamResolver`).
3. Link each PB game to its schedule game inside the same tab by (team pair, game number).
4. Join to OE: Riot platform game id == OE game_id; otherwise date (+-36h) + both teams + game
   number in series.
5. Compare first pick team, sides, per-team ban and pick order, the 20-action sequence, winner.
"""
from __future__ import annotations

import argparse
import logging
import re
import unicodedata
from dataclasses import dataclass

import pandas as pd

from draft_analyzer import config, draft
from draft_analyzer.ingest import leaguepedia as lp

log = logging.getLogger(__name__)

OUT_PATH = config.DATA_PROCESSED / "leaguepedia_crosscheck.parquet"
DATE_TOLERANCE = pd.Timedelta(hours=36)

# --------------------------------------------------------------------------------------
# Sample: whole tabs of Picks-and-Bans pages, stratified by season and league.
# (oe_league, oe_year, picks-and-bans page, tabs or None for all tabs, max matches per tab)
# --------------------------------------------------------------------------------------
SAMPLE: list[tuple[str, int, str, list[str] | None, int | None]] = [
    # 2026 (First Selection rule)
    ("LCK", 2026, "LCK/2026 Season/Cup/Picks and Bans/Group Stage", ["Week 1"], None),
    ("LCK", 2026, "LCK/2026 Season/Rounds 1-2/Picks and Bans/1-3", ["Week 1"], None),
    ("LEC", 2026, "LEC/2026 Season/Versus Season/Picks and Bans/Individual Games", ["Week 1"], None),
    ("LPL", 2026, "LPL/2026 Season/Split 1/Picks and Bans/1-2", ["Week 1"], 8),
    ("LCS", 2026, "LCS/2026 Season/Lock-In/Picks and Bans/Swiss Stage and Last Chance", ["Swiss Round 1"], None),
    ("LCP", 2026, "LCP/2026 Season/Split 1/Picks and Bans/Individual Games", ["Week 1"], None),
    ("MSI", 2026, "2026 Mid-Season Invitational/Picks and Bans/Play-In Stage", None, None),
    # 2025
    ("LCK", 2025, "LCK/2025 Season/Rounds 1-2/Picks and Bans/1-3", ["Week 1"], 6),
    ("LEC", 2025, "LEC/2025 Season/Winter Season/Picks and Bans/Individual Games", ["Week 1"], None),
    ("LPL", 2025, "LPL/2025 Season/Split 2/Picks and Bans/1-3", ["Week 1"], 5),
    ("LTA N", 2025, "LTA North/2025 Season/Split 2/Picks and Bans/Individual Games", ["Week 1"], None),
    ("WLDs", 2025, "2025 Season World Championship/Main Event/Picks and Bans/Swiss Stage", ["Round 1"], None),
    # 2024
    ("LCK", 2024, "LCK/2024 Season/Spring Season/Picks and Bans/1-3", ["Week 1"], 5),
    ("LEC", 2024, "LEC/2024 Season/Summer Season/Picks and Bans/Individual Games", ["Week 1"], None),
    ("LCS", 2024, "LCS/2024 Season/Summer Season/Picks and Bans/Individual Games", ["Week 1"], None),
    ("LPL", 2024, "LPL/2024 Season/Spring Season/Picks and Bans/1-3", ["Week 1"], 5),
    ("WLDs", 2024, "2024 Season World Championship/Main Event/Picks and Bans/Bracket Stage", ["Quarterfinals"], None),
]

# --------------------------------------------------------------------------------------
# Team-name normalization
# --------------------------------------------------------------------------------------
# Leaguepedia link name (after Module:Teamnames) -> OE name, where neither an exact
# normalized match nor unique containment finds it. OE back-fills a franchise's *current* name
# onto older seasons; Leaguepedia keeps the name the team had at the time.
TEAM_ALIASES: dict[str, str] = {
    "Kwangdong Freecs": "DN SOOPers",
    "DN Freecs": "DN SOOPers",
    "OKSavingsBank BRION": "HANJIN BRION",
    "KRX": "Kiwoom DRX",  # wiki has a separate "KRX" entry for the 2026 rebrand
    "SHFT": "Shifters",  # wiki has a separate "SHFT" entry
}


def norm_team(name: str | None) -> str:
    """Comparison key: accents folded, parenthetical qualifiers dropped, lowercase alphanumerics."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\(.*?\)", "", s)
    return re.sub(r"[^0-9a-z]+", "", s.lower())


class TeamResolver:
    """Maps Leaguepedia team names onto a fixed set of OE team names (one league-season)."""

    def __init__(self, oe_names, lookup: lp.NameLookup | None = None):
        self.oe_names = sorted(set(oe_names))
        self.by_key = {norm_team(n): n for n in self.oe_names}
        self.lookup = lookup
        self.log: dict[str, tuple[str | None, str]] = {}  # raw -> (oe name, method)

    def resolve(self, raw: str | None) -> str | None:
        if not raw:
            return None
        if raw in self.log:
            return self.log[raw][0]
        wiki = self.lookup.team(raw) if self.lookup else raw.strip()
        method = "exact" if wiki == raw.strip() else "wiki-module"
        out = self.by_key.get(norm_team(wiki))
        if out is None and wiki in TEAM_ALIASES and TEAM_ALIASES[wiki] in self.oe_names:
            out, method = TEAM_ALIASES[wiki], "alias"
        if out is None:
            k = norm_team(wiki)
            cands = [n for kk, n in self.by_key.items() if len(k) >= 3 and (k in kk or kk in k)]
            if len(cands) == 1:
                out, method = cands[0], "containment"
        if out is not None and out != raw.strip() and method == "exact":
            method = "normalized"
        self.log[raw] = (out, method if out else "unresolved")
        return out


# --------------------------------------------------------------------------------------
# Leaguepedia game records
# --------------------------------------------------------------------------------------
@dataclass
class LPGame:
    oe_league: str
    oe_year: int
    page: str
    tab: str | None
    match_in_tab: int
    game_in_match: int
    first_team: str | None  # OE name of PB team1 (= drafts first)
    second_team: str | None
    first_team_raw: str
    second_team_raw: str
    switched_sides: bool | None
    pb_winner: str | None
    first_bans: list
    first_picks: list
    second_bans: list
    second_picks: list
    ms_linked: bool = False
    date_utc: pd.Timestamp | None = None
    blue: str | None = None
    red: str | None = None
    ms_first_pick: str | None = None
    ms_winner: str | None = None
    riot_id: str | None = None

    @property
    def lp_blue(self) -> str | None:
        """Blue side: schedule's blue if linked, else PB convention (team1 unless switched_sides)."""
        if self.blue:
            return self.blue
        if self.first_team is None:
            return None
        return self.second_team if self.switched_sides else self.first_team

    def sequence(self) -> list:
        return draft.interleave(self.first_bans, self.first_picks, self.second_bans, self.second_picks)


def _occurrence(tab, pair, match_in_tab, seen: dict) -> int:
    """0-based index of this match among the matches of the same team pair in the same tab."""
    lst = seen.setdefault((tab, pair), [])
    if match_in_tab not in lst:
        lst.append(match_in_tab)
    return lst.index(match_in_tab)


def build_lp_games(client: lp.Client, lookup: lp.NameLookup, oe_games: pd.DataFrame,
                   sample=SAMPLE) -> tuple[list[LPGame], dict]:
    pages = [s[2] for s in sample]
    data_pages = ["Data:" + lp.overview_of(p) for p in pages]
    src = client.get_pages(pages + data_pages)
    resolvers: dict[tuple, TeamResolver] = {}
    out: list[LPGame] = []
    for league, year, page, tabs, max_matches in sample:
        key = (league, year)
        if key not in resolvers:
            sub = oe_games[(oe_games.league == league) & (oe_games.oe_year == year)]
            resolvers[key] = TeamResolver(set(sub.blue_team) | set(sub.red_team), lookup)
        res = resolvers[key]
        if src.get(page) is None:
            log.warning("missing page %s", page)
            continue
        pb = [g for g in lp.parse_picks_and_bans(src[page], page) if tabs is None or g.tab in tabs]
        if max_matches:
            pb = [g for g in pb if g.match_in_tab <= max_matches]
        dp = "Data:" + lp.overview_of(page)
        ms = lp.parse_match_schedule(src[dp], dp) if src.get(dp) else []
        # A pair can meet twice in one tab (e.g. LPL weeks): the k-th PB match of a pair links to
        # the k-th schedule match of that pair.
        ms_index: dict[tuple, list[lp.MSGame]] = {}
        ms_occ: dict = {}
        for m in ms:
            pair = frozenset((res.resolve(m.blue), res.resolve(m.red)))
            occ = _occurrence(m.tab, pair, m.match_in_tab, ms_occ)
            ms_index.setdefault((m.tab, pair, occ, m.game_in_match), []).append(m)
        pb_occ: dict = {}
        for g in pb:
            t1, t2 = res.resolve(g.team1), res.resolve(g.team2)
            occ = _occurrence(g.tab, frozenset((t1, t2)), g.match_in_tab, pb_occ)
            ch = lookup.champion
            rec = LPGame(
                oe_league=league, oe_year=year, page=page, tab=g.tab, match_in_tab=g.match_in_tab,
                game_in_match=g.game_in_match, first_team=t1, second_team=t2,
                first_team_raw=g.team1, second_team_raw=g.team2, switched_sides=g.switched_sides,
                pb_winner={1: t1, 2: t2}.get(g.winner),
                first_bans=[ch(x) for x in g.team1_bans], first_picks=[ch(x) for x in g.team1_picks],
                second_bans=[ch(x) for x in g.team2_bans], second_picks=[ch(x) for x in g.team2_picks],
            )
            cands = ms_index.get((g.tab, frozenset((t1, t2)), occ, g.game_in_match), [])
            if len(cands) == 1:
                m = cands[0]
                rec.ms_linked = True
                rec.date_utc = m.date_utc
                rec.blue, rec.red = res.resolve(m.blue), res.resolve(m.red)
                rec.ms_first_pick = res.resolve(m.first_pick)
                rec.ms_winner = {1: rec.blue, 2: rec.red}.get(m.winner)
                rec.riot_id = m.riot_platform_game_id
            out.append(rec)
    names = {f"{k[0]} {k[1]}": dict(r.log) for k, r in resolvers.items()}
    return out, names


# --------------------------------------------------------------------------------------
# Join to OE
# --------------------------------------------------------------------------------------
def match_oe(rec: LPGame, oe_games: pd.DataFrame, oe_ids: set[str]) -> tuple[str | None, str]:
    """Returns (OE game_id or None, method / reason)."""
    if rec.riot_id and rec.riot_id in oe_ids:
        return rec.riot_id, "riot_id"
    note = "riot id not in OE; " if rec.riot_id else ""
    if rec.first_team is None or rec.second_team is None:
        return None, note + f"team unresolved ({rec.first_team_raw!r} / {rec.second_team_raw!r})"
    if rec.date_utc is None:
        return None, note + "no date (PB game not linked to a schedule game)"
    pair = {rec.first_team, rec.second_team}
    sub = oe_games[
        (oe_games.league == rec.oe_league)
        & (oe_games.game_in_series == rec.game_in_match)
        & ((oe_games.datetime - rec.date_utc).abs() <= DATE_TOLERANCE)
    ]
    sub = sub[[{b, r} == pair for b, r in zip(sub.blue_team, sub.red_team)]]
    if len(sub) == 0:
        return None, note + "no OE game with same teams, game number and date"
    if len(sub) > 1:
        sub = sub.assign(gap=(sub.datetime - rec.date_utc).abs()).sort_values("gap")
        if sub.gap.iloc[0] == sub.gap.iloc[1]:
            return None, note + "ambiguous fallback"
    return sub.game_id.iloc[0], note + "date+teams+game"


def _lst(x) -> list:
    return [None if draft.is_missing(v) else v for v in list(x)]


def compare(rec: LPGame, oe: pd.Series, oe_seq: list) -> dict:
    diffs = []
    row = {}
    row["oe_first_pick_team"] = oe.first_pick_team
    row["lp_first_pick_team"] = rec.first_team
    row["first_pick_agree"] = rec.first_team == oe.first_pick_team
    if not row["first_pick_agree"]:
        diffs.append(f"first pick: LP={rec.first_team} OE={oe.first_pick_team}")
    row["lp_ms_first_pick"] = rec.ms_first_pick
    row["ms_first_pick_agree"] = None if rec.ms_first_pick is None else rec.ms_first_pick == oe.first_pick_team
    row["lp_internal_first_pick_agree"] = None if rec.ms_first_pick is None else rec.ms_first_pick == rec.first_team
    row["lp_blue_team"] = rec.lp_blue
    row["side_agree"] = rec.lp_blue == oe.blue_team
    if not row["side_agree"]:
        diffs.append(f"blue side: LP={rec.lp_blue} OE={oe.blue_team}")
    pb_blue = rec.second_team if rec.switched_sides else rec.first_team
    row["pb_side_agree"] = pb_blue == oe.blue_team
    lp_team = {rec.first_team: (rec.first_bans, rec.first_picks), rec.second_team: (rec.second_bans, rec.second_picks)}
    for kind, k in (("ban", 0), ("pick", 1)):
        ok = True
        for side in ("blue", "red"):
            team = getattr(oe, f"{side}_team")
            oe_l = _lst(getattr(oe, f"{side}_{kind}s"))
            lp_l = list(lp_team.get(team, ([None] * 5, [None] * 5))[k])
            for i, (a, b) in enumerate(zip(lp_l, oe_l)):
                if a != b:
                    ok = False
                    diffs.append(f"{side} ({team}) {kind}{i + 1}: LP={a} OE={b}")
        row[f"{kind}_order_agree"] = ok
    lp_seq = rec.sequence()
    row["lp_sequence"] = [str(x) for x in lp_seq]
    row["oe_sequence"] = [str(x) for x in oe_seq]
    row["sequence_agree"] = lp_seq == oe_seq
    row["seq_diff_positions"] = [i for i, (a, b) in enumerate(zip(lp_seq, oe_seq)) if a != b]
    win = rec.ms_winner or rec.pb_winner
    row["winner_agree"] = win == oe.winner_team
    if not row["winner_agree"]:
        diffs.append(f"winner: LP={win} OE={oe.winner_team}")
    row["diffs"] = diffs
    return row


# Extra pages fetched (all tabs) to look at OE games whose picks look role-ordered. Every game on
# these and on the SAMPLE pages that is not in the stratified sample is compared too
# (stratum "extended"); fetching them cost one request.
EXTRA_PAGES: list[tuple[str, int, str]] = [
    ("LEC", 2026, "LEC/2026 Season/Spring Season/Picks and Bans/Weeks 1-4"),
    ("LPL", 2026, "LPL/2026 Season/Split 1/Picks and Bans/3-4"),
    ("LCP", 2026, "LCP/2026 Season/Split 2/Picks and Bans/Individual Games"),
    ("LCS", 2026, "LCS/2026 Season/Summer Season/Picks and Bans/Individual Games"),
    ("LEC", 2026, "LEC/2026 Season/Summer Season/Picks and Bans/Weeks 1-3"),
    ("LEC", 2025, "LEC/2025 Season/Spring Season/Picks and Bans/Weeks 1-4"),
    ("LPL", 2025, "LPL/2025 Season/Split 2/Picks and Bans/4-5"),
]


def role_order_suspects(games: pd.DataFrame) -> set[str]:
    """OE games where BOTH teams' pick1..5 equal their top/jng/mid/bot/sup champions.

    By chance this should be very rare (it needs both teams to draft exactly in role order), so
    these are candidates for OE having stored role order instead of draft order.
    """
    def same(a, b):
        return list(a) == list(b)

    m = [same(bp, bc) and same(rp, rc) for bp, bc, rp, rc in
         zip(games.blue_picks, games.blue_champs_by_role, games.red_picks, games.red_champs_by_role)]
    return set(games.game_id[m])


def _rows(recs: list[LPGame], games: pd.DataFrame, seqs: dict, stratum: str, used: dict) -> list[dict]:
    by_id = games.set_index("game_id")
    oe_ids = set(games.game_id)
    rows = []
    for r in recs:
        base = {
            "stratum": stratum,
            "lp_page": r.page, "lp_tab": r.tab, "lp_match_in_tab": r.match_in_tab,
            "lp_game_in_match": r.game_in_match, "league": r.oe_league, "oe_year": r.oe_year,
            "lp_first_team_raw": r.first_team_raw, "lp_second_team_raw": r.second_team_raw,
            "lp_switched_sides": r.switched_sides, "lp_ms_linked": r.ms_linked,
            "lp_date_utc": r.date_utc, "lp_riot_id": r.riot_id,
        }
        gid, how = match_oe(r, games, oe_ids)
        if gid is not None and gid in used:
            how, gid = f"OE game {gid} already matched to another LP game", None
        base.update(matched=gid is not None, match_method=how, game_id=gid)
        if gid is not None:
            used[gid] = True
            oe = by_id.loc[gid]
            base.update(oe_first_pick_side=oe.first_pick_side, oe_blue_team=oe.blue_team,
                        oe_red_team=oe.red_team, oe_datetime=oe.datetime, split=oe.split,
                        oe_irregular=bool(oe.irregular), oe_irregular_notes=oe.irregular_notes)
            base.update(compare(r, oe, seqs[gid]))
        rows.append(base)
    return rows


def crosscheck(client: lp.Client | None = None, sample=SAMPLE, extended: bool = True) -> tuple[pd.DataFrame, dict]:
    """One row per Leaguepedia game: the stratified sample (stratum 'sample') and, if ``extended``,
    every other game on the fetched pages plus EXTRA_PAGES (stratum 'extended')."""
    client = client or lp.Client()
    lookup = lp.NameLookup.from_client(client)
    games = pd.read_parquet(config.GAMES_PATH)
    acts = pd.read_parquet(config.ACTIONS_PATH, columns=["game_id", "action_index", "champion"])
    seqs = {
        gid: [None if draft.is_missing(c) else c for c in grp.sort_values("action_index").champion]
        for gid, grp in acts.groupby("game_id")
    }
    recs, names = build_lp_games(client, lookup, games, sample)
    used: dict = {}
    rows = _rows(recs, games, seqs, "sample", used)
    if extended:
        in_sample = {(r.page, r.tab, r.match_in_tab, r.game_in_match) for r in recs}
        pages = [(lg, y, p, None, None) for lg, y, p, _, _ in sample] + [(lg, y, p, None, None) for lg, y, p in EXTRA_PAGES]
        all_recs, _ = build_lp_games(client, lookup, games, pages)
        rest = [r for r in all_recs if (r.page, r.tab, r.match_in_tab, r.game_in_match) not in in_sample]
        rows += _rows(rest, games, seqs, "extended", used)
    df = pd.DataFrame(rows)
    sus = role_order_suspects(games)
    df["oe_role_order_suspect"] = df.game_id.isin(sus)
    return df, {"names": names, "n_role_order_suspects": len(sus), "live_requests": client.live_requests}


AGREE_COLS = ["first_pick_agree", "side_agree", "ban_order_agree", "pick_order_agree", "sequence_agree", "winner_agree"]


def _summary_block(df: pd.DataFrame, title: str) -> list[str]:
    m = df[df.matched]
    lines = [f"== {title}: {len(df)} Leaguepedia games, matched {len(m)} ({len(m) / max(len(df), 1):.1%})",
             "match methods: " + repr(m.match_method.value_counts().to_dict())]
    for c in AGREE_COLS:
        lines.append(f"{c:18s} {m[c].mean():.2%}  ({int(m[c].sum())}/{len(m)})")
    for k in ("oe_year", "oe_first_pick_side", "league"):
        lines.append(m.groupby(k)[AGREE_COLS].mean().round(4).assign(n=m.groupby(k).size()).to_string())
    um = df[~df.matched]
    if len(um):
        lines.append("unmatched:\n" + um[["lp_page", "lp_tab", "lp_match_in_tab", "lp_game_in_match",
                                          "lp_first_team_raw", "lp_second_team_raw", "match_method"]].to_string())
    for r in m[~m[AGREE_COLS].all(axis=1)].itertuples():
        lines.append(f"DISAGREE {r.game_id} {r.league} {r.oe_year} {r.lp_tab} m{r.lp_match_in_tab}g{r.lp_game_in_match}"
                     f"{' [OE role-order suspect]' if r.oe_role_order_suspect else ''}: " + "; ".join(r.diffs))
    return lines


def summarize(df: pd.DataFrame) -> str:
    lines = _summary_block(df[df.stratum == "sample"], "stratified sample")
    if (df.stratum == "extended").any():
        lines += _summary_block(df, "sample + all other games on the fetched pages")
    sus = df[df.matched & df.oe_role_order_suspect]
    lines.append(f"OE role-order suspects compared: {len(sus)}; Leaguepedia pick order agrees with OE in "
                 f"{int(sus.pick_order_agree.sum())}: " + ", ".join(sus.game_id))
    return "\n".join(lines)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--offline", action="store_true", help="use cached pages only")
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    df, info = crosscheck(lp.Client(offline=args.offline))
    df.to_parquet(args.out, index=False)
    print(summarize(df))
    print("team names needing more than an exact match:")
    for lg, d in info["names"].items():
        odd = {k: v for k, v in d.items() if v[1] != "exact"}
        if odd:
            print(f"  {lg}: {odd}")
    print(f"OE role-order suspects (both teams' picks == role order) in all of OE: {info['n_role_order_suspects']}")
    print(f"live requests this run: {info['live_requests']}; wrote {args.out}")


if __name__ == "__main__":
    main()
