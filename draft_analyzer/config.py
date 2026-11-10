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

# Google Drive file ids of the yearly Oracle's Elixir CSVs (folder 1gLSw0RLjBbtaNy0dgnGQDAZOHIgCe-HH).
OE_FILE_IDS: dict[int, str] = {
    2024: "1IjIEhLc9n8eLKeY-yh_YigKVWbhgGBsN",
    2025: "1v6LRphp2kYciU4SXp0PCjEMuev1bDejc",
    2026: "1hnpbrUpBMS1TZI7IovfpKeZfWJH1Aptm",
}
OE_FILENAME = "{year}_LoL_esports_match_data_from_OraclesElixir.csv"

POSITIONS: tuple[str, ...] = ("top", "jng", "mid", "bot", "sup")
