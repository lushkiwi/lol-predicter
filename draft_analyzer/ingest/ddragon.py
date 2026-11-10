"""Data Dragon ingestion: champion metadata table keyed by Oracle's Elixir names."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import requests

BASE_URL = "https://ddragon.leagueoflegends.com"
USER_AGENT = "lol-draft-analyzer/0.1"
CACHE_DIR = Path("data/raw/ddragon")
OE_CACHE = Path("data/raw/oe/_all_years_cache.parquet")
TAGS = ["Fighter", "Tank", "Mage", "Assassin", "Marksman", "Support"]

# Data Dragon `name` -> Oracle's Elixir spelling, only where they differ.
ALIASES: dict[str, str] = {}


def _fetch_json(url: str) -> dict | list:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _cached_json(filename: str, url: str, refresh: bool = False):
    path = Path(CACHE_DIR) / filename
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    data = _fetch_json(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def get_versions(refresh: bool = False) -> list[str]:
    return _cached_json("versions.json", f"{BASE_URL}/api/versions.json", refresh)


def latest_version(refresh: bool = False) -> str:
    return get_versions(refresh)[0]


def parse_champions(raw: dict) -> pd.DataFrame:
    rows = []
    for c in raw["data"].values():
        row = {
            "champion": ALIASES.get(c["name"], c["name"]),
            "ddragon_id": c["id"],
            "ddragon_key": int(c["key"]),
            "ddragon_name": c["name"],
            "tags": list(c.get("tags", [])),
        }
        for t in TAGS:
            row[f"tag_{t}"] = t in c.get("tags", [])
        for k, v in c.get("info", {}).items():
            row[f"info_{k}"] = v
        row["partype"] = c.get("partype")
        for k, v in c.get("stats", {}).items():
            row[f"stat_{k}"] = v
        rows.append(row)
    return pd.DataFrame(rows).sort_values("champion").reset_index(drop=True)


def load_champions(version: str | None = None, refresh: bool = False) -> pd.DataFrame:
    if version is None:
        version = latest_version(refresh)
    raw = _cached_json(
        f"champion_{version}.json",
        f"{BASE_URL}/cdn/{version}/data/en_US/champion.json",
        refresh,
    )
    return parse_champions(raw)


def oe_champion_names(path: Path = OE_CACHE) -> set[str]:
    cols = ["champion"] + [f"ban{i}" for i in range(1, 6)] + [f"pick{i}" for i in range(1, 6)]
    df = pd.read_parquet(path, columns=cols)
    names: set[str] = set()
    for c in cols:
        names |= set(df[c].dropna().astype(str))
    return names


def build_champions_table(out_path: str = "data/processed/champions.parquet") -> pd.DataFrame:
    df = load_champions()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    return df


if __name__ == "__main__":
    table = build_champions_table()
    print(f"version {latest_version()}: {len(table)} champions")
    if OE_CACHE.exists():
        oe = oe_champion_names()
        dd = set(table["champion"])
        print("In OE, not in DDragon:", sorted(oe - dd))
        print("In DDragon, not in OE:", sorted(dd - oe))
