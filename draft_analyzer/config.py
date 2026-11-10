"""Central configuration: leagues, years, paths.

Override leagues/years without editing code via env vars (also read from ``.env``):
``DRAFT_LEAGUES="LCK,LPL"`` and ``DRAFT_YEARS="2025,2026"``.
"""
from __future__ import annotations

import os
from pathlib import Path

try:  # optional: load secrets/overrides from .env
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:  # pragma: no cover - dotenv is optional
    pass

ROOT = Path(__file__).resolve().parents[1]
DATA_RAW = ROOT / "data" / "raw"
DATA_PROCESSED = ROOT / "data" / "processed"
REPORTS = ROOT / "reports"
OE_DIR = DATA_RAW / "oe"
LEAGUEPEDIA_DIR = DATA_RAW / "leaguepedia"
DDRAGON_DIR = DATA_RAW / "ddragon"

GAMES_PATH = DATA_PROCESSED / "games.parquet"
ACTIONS_PATH = DATA_PROCESSED / "draft_actions.parquet"
HISTORY_GAMES_PATH = DATA_PROCESSED / "history_games.parquet"
CHAMPIONS_PATH = DATA_PROCESSED / "champions.parquet"


def _env_list(name: str, default: tuple) -> tuple:
    raw = os.environ.get(name)
    if not raw:
        return default
    return tuple(x.strip() for x in raw.split(",") if x.strip())


# Oracle's Elixir season files to load (the OE "year" is a season label, not a calendar year).
YEARS: tuple[int, ...] = tuple(int(y) for y in _env_list("DRAFT_YEARS", (2024, 2025, 2026)))

# Oracle's Elixir league codes, verified against the data (see docs/M1_report.md).
# LCS (2024, 2026) and LTA / LTA N / LTA S (2025) are the same Americas slot across seasons.
INTERNATIONAL_LEAGUES: tuple[str, ...] = ("MSI", "WLDs", "FST", "EWC")
LEAGUES: tuple[str, ...] = _env_list(
    "DRAFT_LEAGUES",
    ("LCK", "LPL", "LEC", "LCS", "LTA", "LTA N", "LTA S", "LCP") + INTERNATIONAL_LEAGUES,
)

# Events inside LEAGUES that are out of scope, as (league, OE season). "LCS 2027" is the
# October 2026 promotion tournament between amateur teams. These games are history only.
OUT_OF_SCOPE_EVENTS: tuple[tuple[str, int], ...] = (("LCS", 2027),)

# History-only leagues: their finished games update the as-of statistics of HISTORY_BLOCKS
# (so players and teams arriving from them have a track record) but they are never predicted,
# trained on or scored. "*" means every other league in the Oracle's Elixir files.
# Compared on validation in reports/history_leagues.md: no choice beats using no history; feeding
# player statistics only is a clean tie and is the default. Elo is left out on purpose: each
# league is its own rating pool, so lower-tier ratings are not comparable across leagues.
TIER2_LEAGUES: tuple[str, ...] = ("LCKC", "LDL", "NACL", "EM")
# Leagues the 2025 LCP and LTA South teams played in before (and, for some, after) those leagues existed.
PREDECESSOR_LEAGUES: tuple[str, ...] = ("PCS", "VCS", "LJL", "CBLOL", "LLA")
HISTORY_LEAGUES: tuple[str, ...] = _env_list("DRAFT_HISTORY_LEAGUES", TIER2_LEAGUES + PREDECESSOR_LEAGUES)
HISTORY_BLOCKS: tuple[str, ...] = _env_list("DRAFT_HISTORY_BLOCKS", ("player",))

# Google Drive file ids of the yearly Oracle's Elixir CSVs (folder 1gLSw0RLjBbtaNy0dgnGQDAZOHIgCe-HH).
OE_FILE_IDS: dict[int, str] = {
    2024: "1IjIEhLc9n8eLKeY-yh_YigKVWbhgGBsN",
    2025: "1v6LRphp2kYciU4SXp0PCjEMuev1bDejc",
    2026: "1hnpbrUpBMS1TZI7IovfpKeZfWJH1Aptm",
}
OE_FILENAME = "{year}_LoL_esports_match_data_from_OraclesElixir.csv"

POSITIONS: tuple[str, ...] = ("top", "jng", "mid", "bot", "sup")
