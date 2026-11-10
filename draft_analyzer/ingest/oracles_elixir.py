"""Oracle's Elixir ingestion: download (cached), load, and build the unified tables.

Schema facts verified against the 2024-2026 files (see docs/M1_report.md):
- 12 rows per game: 10 player rows (position top/jng/mid/bot/sup) + 2 team rows (position 'team').
- Team rows carry ``firstPick`` (0/1), ``ban1..ban5`` and ``pick1..pick5`` in each team's own
  ban/pick order, fully populated for the configured leagues. A missed ban is NaN in place.
- Player rows carry the final role (``position``) of each champion.
- ``date`` is the game start timestamp; ``gamelength`` is in seconds.
- ``patch`` must be read as a string ("16.10" != "16.1").
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .. import config, draft

USECOLS = [
    "gameid", "datacompleteness", "league", "year", "split", "playoffs", "date", "game", "patch",
    "participantid", "side", "position", "playername", "playerid", "teamname", "teamid",
    "firstPick", "champion", "ban1", "ban2", "ban3", "ban4", "ban5",
    "pick1", "pick2", "pick3", "pick4", "pick5", "gamelength", "result",
]  # deliberately excludes every in-game stat column (post-game leakage)
BAN_COLS = [f"ban{i}" for i in range(1, 6)]
PICK_COLS = [f"pick{i}" for i in range(1, 6)]


# --------------------------------------------------------------------------------------
# Download / load
# --------------------------------------------------------------------------------------
def csv_path(year: int) -> Path:
    return config.OE_DIR / config.OE_FILENAME.format(year=year)


def download(years=None, cookies_from_browser: str | None = None) -> list[Path]:
    """Download yearly CSVs with gdown. Files already on disk are never re-downloaded.

    Google Drive often rate-limits anonymous downloads of these files; pass
    ``cookies_from_browser="chrome"`` to download as the signed-in browser user, or drop the
    CSVs manually into ``data/raw/oe/``.
    """
    import subprocess
    import sys

    config.OE_DIR.mkdir(parents=True, exist_ok=True)
    paths = []
    for year in years or config.YEARS:
        path = csv_path(year)
        if not (path.exists() and path.stat().st_size > 0):
            cmd = [sys.executable, "-m", "gdown", config.OE_FILE_IDS[year], "-O", str(path), "--retries", "3"]
            if cookies_from_browser:
                cmd += ["--cookies-from-browser", cookies_from_browser]
            subprocess.run(cmd, check=True)
        paths.append(path)
    return paths


def load_raw(years=None, leagues=None) -> pd.DataFrame:
    """Load the pre-game columns for the configured leagues/years (parquet-cached per year)."""
    frames = []
    for year in years or config.YEARS:
        src = csv_path(year)
        if not src.exists():
            raise FileNotFoundError(
                f"{src} missing. Run `python -m draft_analyzer.ingest.oracles_elixir --download` "
                "or place the CSV there manually."
            )
        cache = src.with_suffix(".pregame.parquet")
        if not cache.exists() or cache.stat().st_mtime < src.stat().st_mtime:
            df = pd.read_csv(src, usecols=USECOLS, dtype={"patch": str, "gameid": str}, low_memory=False)
            df.to_parquet(cache, index=False)
        frames.append(pd.read_parquet(cache))
    raw = pd.concat(frames, ignore_index=True)
    leagues = leagues or config.LEAGUES
    return raw[raw["league"].isin(leagues)].reset_index(drop=True)


# --------------------------------------------------------------------------------------
# Games table
# --------------------------------------------------------------------------------------
def _clean_list(values) -> list:
    return [None if draft.is_missing(v) else str(v) for v in values]


def _patch_parts(patch: str) -> tuple[int, int]:
    major, _, minor = str(patch).partition(".")
    return int(major), int(minor or 0)


def build_games(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One row per game. Returns (games, fearless_summary)."""
    team = raw[raw["position"] == "team"]
    players = raw[raw["position"] != "team"]

    records = []
    dropped = []
    player_groups = {gid: g for gid, g in players.groupby("gameid", sort=False)}
    for gid, g in team.groupby("gameid", sort=False):
        sides = {r.side: r for r in g.itertuples()}
        if set(sides) != {"Blue", "Red"} or len(g) != 2:
            dropped.append((gid, "not exactly one Blue and one Red team row"))
            continue
        b, r = sides["Blue"], sides["Red"]
        notes = []
        fp = {"Blue": b.firstPick, "Red": r.firstPick}
        if fp["Blue"] == 1 and fp["Red"] == 0:
            fp_side = "Blue"
        elif fp["Red"] == 1 and fp["Blue"] == 0:
            fp_side = "Red"
        else:
            fp_side = None
            notes.append("first_pick_unknown")
        if b.result + r.result != 1:
            notes.append("no_single_winner")
        rec = {
            "game_id": gid,
            "league": b.league,
            "oe_year": int(b.year),
            "split": None if draft.is_missing(b.split) else b.split,
            "playoffs": int(b.playoffs),
            "datetime": pd.Timestamp(b.date),
            "gamelength": float(b.gamelength) if not draft.is_missing(b.gamelength) else np.nan,
            "game_in_series": int(b.game),
            "patch": b.patch,
            "datacompleteness": b.datacompleteness,
            "blue_team": b.teamname, "red_team": r.teamname,
            "blue_team_id": b.teamid if not draft.is_missing(b.teamid) else f"name:{b.teamname}",
            "red_team_id": r.teamid if not draft.is_missing(r.teamid) else f"name:{r.teamname}",
            "first_pick_side": fp_side,
            "winner_side": "Blue" if b.result == 1 else "Red",
        }
        pg = player_groups.get(gid)
        for side, row in (("blue", b), ("red", r)):
            rec[f"{side}_bans"] = _clean_list(getattr(row, c) for c in BAN_COLS)
            rec[f"{side}_picks"] = _clean_list(getattr(row, c) for c in PICK_COLS)
            sp = pg[pg["side"] == side.capitalize()].set_index("position") if pg is not None else None
            if sp is None or set(sp.index) != set(config.POSITIONS) or len(sp) != 5:
                notes.append(f"{side}_roster_irregular")
                rec[f"{side}_players"] = [None] * 5
                rec[f"{side}_player_names"] = [None] * 5
                rec[f"{side}_champs_by_role"] = [None] * 5
            else:
                sp = sp.loc[list(config.POSITIONS)]
                ids = [pid if not draft.is_missing(pid) else f"name:{name}" for pid, name in zip(sp["playerid"], sp["playername"])]
                rec[f"{side}_players"] = ids
                rec[f"{side}_player_names"] = _clean_list(sp["playername"])
                rec[f"{side}_champs_by_role"] = _clean_list(sp["champion"])
                if set(rec[f"{side}_champs_by_role"]) != set(rec[f"{side}_picks"]):
                    notes.append(f"{side}_picks_mismatch_players")
            n_missed = sum(c is None for c in rec[f"{side}_bans"])
            if n_missed:
                notes.append(f"{side}_missed_bans:{n_missed}")
            if any(c is None for c in rec[f"{side}_picks"]):
                notes.append(f"{side}_missing_picks")
        # A champion cannot be banned twice or banned after being taken. Where OE shows that,
        # Leaguepedia records a missed ban (see docs/M1_crosscheck.md), so the later occurrence
        # in draft order is treated as a missed ban.
        if fp_side is not None:
            first, second = fp_side.lower(), draft.OTHER_SIDE[fp_side].lower()
            seq = draft.interleave(rec[f"{first}_bans"], rec[f"{first}_picks"], rec[f"{second}_bans"], rec[f"{second}_picks"])
            seen: set = set()
            for i, c in enumerate(seq):
                if c is None:
                    continue
                if c in seen and draft.ACTION_TYPE[i] == "ban":
                    side = first if draft.ACTION_SLOT[i] == "first" else second
                    rec[f"{side}_bans"][draft.ACTION_ORDINAL[i]] = None
                    notes.append(f"{side}_duplicate_ban_treated_as_missed:{c}")
                seen.add(c)
        champs = [c for s in ("blue", "red") for k in ("bans", "picks") for c in rec[f"{s}_{k}"] if c is not None]
        if len(champs) != len(set(champs)):
            notes.append("duplicate_champion_in_draft")
        # Informational: both teams' picks listed exactly in role order (top..sup). Usually a
        # genuine draft, occasionally an OE entry that lost the draft order (2 of 12 checked).
        rec["picks_in_role_order"] = all(
            rec[f"{s}_picks"] == rec[f"{s}_champs_by_role"] and None not in rec[f"{s}_picks"] for s in ("blue", "red")
        )
        rec["n_actions"] = len(champs)
        rec["n_missed_bans"] = sum(c is None for s in ("blue", "red") for c in rec[f"{s}_bans"])
        rec["irregular"] = bool(notes)
        rec["irregular_notes"] = ";".join(notes)
        records.append(rec)

    games = pd.DataFrame.from_records(records)
    games["end_datetime"] = games["datetime"] + pd.to_timedelta(games["gamelength"].fillna(0), unit="s")
    parts = games["patch"].map(_patch_parts)
    games["patch_major"] = parts.str[0].astype(int)
    games["patch_minor"] = parts.str[1].astype(int)
    games["first_pick_team"] = np.where(games["first_pick_side"] == "Blue", games["blue_team"], games["red_team"])
    games["first_pick_team_id"] = np.where(games["first_pick_side"] == "Blue", games["blue_team_id"], games["red_team_id"])
    games.loc[games["first_pick_side"].isna(), ["first_pick_team", "first_pick_team_id"]] = None
    games["winner_team"] = np.where(games["winner_side"] == "Blue", games["blue_team"], games["red_team"])

    games = games.sort_values(["datetime", "game_id"], kind="stable").reset_index(drop=True)

    # series
    games = games.join(draft.assign_series(games))
    grp = games.groupby("series_id", sort=False)
    blue_won = (games["winner_side"] == "Blue").astype(int)
    # series score before each game, from the perspective of this game's blue/red teams
    bw, rw = [], []
    for _, g in grp:
        wins: dict[str, int] = {}
        for row in g.itertuples():
            bw.append((row.Index, wins.get(row.blue_team_id, 0), wins.get(row.red_team_id, 0)))
            winner = row.blue_team_id if row.winner_side == "Blue" else row.red_team_id
            wins[winner] = wins.get(winner, 0) + 1
    score = pd.DataFrame(bw, columns=["idx", "blue_series_wins_before", "red_series_wins_before"]).set_index("idx")
    games = games.join(score)
    games["series_n_games"] = grp["game_id"].transform("size")
    del blue_won, rw

    # Fearless: detect per (league, season, split, playoffs), then per-team burned lists
    games = games.join(draft.series_pick_history(games))
    mode, summary = draft.detect_fearless(games, ["league", "oe_year", "split", "playoffs"])
    games["fearless"] = mode
    for side in ("blue", "red"):
        games[f"{side}_unavailable_at_start"] = [
            draft.burned_for_team(m, own, opp)
            for m, own, opp in zip(games["fearless"], games[f"{side}_prev_own"], games[f"{side}_prev_opp"])
        ]
    games["unavailable_at_start"] = [
        sorted(set(a) | set(b)) for a, b in zip(games["blue_unavailable_at_start"], games["red_unavailable_at_start"])
    ]
    games.attrs["dropped"] = dropped
    return games, summary


# --------------------------------------------------------------------------------------
# Draft actions table
# --------------------------------------------------------------------------------------
def build_draft_actions(games: pd.DataFrame) -> pd.DataFrame:
    """One row per draft action (always 20 per game; ``champion`` is null for a missed ban)."""
    rows = []
    for g in games.itertuples():
        if g.first_pick_side is None:
            continue  # cannot order the draft; reported as irregular in games
        side_of = {"first": g.first_pick_side, "second": draft.OTHER_SIDE[g.first_pick_side]}
        per = {slot: side.lower() for slot, side in side_of.items()}
        seq = draft.interleave(
            getattr(g, f"{per['first']}_bans"), getattr(g, f"{per['first']}_picks"),
            getattr(g, f"{per['second']}_bans"), getattr(g, f"{per['second']}_picks"),
        )
        for i, champ in enumerate(seq):
            slot = draft.ACTION_SLOT[i]
            side = side_of[slot]
            rows.append(
                (
                    g.game_id, g.series_id, g.game_in_series, g.datetime, g.patch, g.league, g.split,
                    g.blue_team, g.red_team, g.first_pick_team, i, draft.ACTION_TYPE[i], draft.ACTION_PHASE[i],
                    slot, side, g.blue_team if side == "Blue" else g.red_team,
                    g.blue_team_id if side == "Blue" else g.red_team_id, champ, g.winner_side,
                )
            )
    cols = [
        "game_id", "series_id", "game_in_series", "datetime", "patch", "league", "split",
        "blue_team", "red_team", "first_pick_team", "action_index", "action_type", "phase",
        "draft_slot", "side", "acting_team", "acting_team_id", "champion", "winner_side",
    ]
    return pd.DataFrame(rows, columns=cols)


def build_all(write: bool = True) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    raw = load_raw()
    games, fearless_summary = build_games(raw)
    actions = build_draft_actions(games)
    if write:
        config.DATA_PROCESSED.mkdir(parents=True, exist_ok=True)
        games.to_parquet(config.GAMES_PATH, index=False)
        actions.to_parquet(config.ACTIONS_PATH, index=False)
        fearless_summary.to_csv(config.DATA_PROCESSED / "fearless_summary.csv", index=False)
    return games, actions, fearless_summary


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Build games.parquet and draft_actions.parquet from Oracle's Elixir")
    ap.add_argument("--download", action="store_true", help="download missing CSVs first")
    ap.add_argument("--cookies-from-browser", default=None)
    args = ap.parse_args()
    if args.download:
        download(cookies_from_browser=args.cookies_from_browser)
    games, actions, summary = build_all()
    print(f"games: {len(games)}  series: {games.series_id.nunique()}  actions: {len(actions)}")
    print(f"irregular games: {int(games.irregular.sum())}; dropped: {games.attrs.get('dropped')}")
    print(summary.to_string())
