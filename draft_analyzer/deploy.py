"""Assemble the directory that is uploaded to a host.

    python -m draft_analyzer.deploy            # writes ./deploy and checks that it serves
    railway up deploy --path-as-root           # upload it; the host builds the Dockerfile

The repository leaves out the match data and the trained models (they are derived from Oracle's
Elixir data and rebuilt by the pipeline), so a host cannot build the site from GitHub alone.
This copies the code, the Dockerfile and exactly the files the web server reads at run time
into one directory, and nothing else. Run it again after retraining.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config

ROOT = config.ROOT
# Read by predict.LiveSession and web.server; kept in step by the check below.
RUNTIME_FILES = (
    "data/processed/games.parquet",
    "data/processed/history_games.parquet",
    "data/processed/champions.parquet",
    "data/processed/models/models_live.pkl",
    "data/processed/models/engine.pkl",
    "data/raw/ddragon/versions.json",
)
IMAGE_CACHE = "data/raw/ddragon/img"  # optional: saves the host refetching icons after every deploy
TOP_LEVEL = ("Dockerfile", "requirements-serve.txt", "LICENSE")
MANIFEST = "MANIFEST.json"

CHECK = """
from fastapi.testclient import TestClient
from draft_analyzer import config
from draft_analyzer.web.server import app
import pathlib, sys
assert pathlib.Path(config.ROOT).resolve() == pathlib.Path.cwd().resolve(), config.ROOT
c = TestClient(app)
assert c.get("/health").status_code == 200
m = c.get("/api/meta").json()
t = m["teams"][0], m["teams"][1]
s = {"patch": m["default_patch"], "league": t[0]["league"], "blue_team": t[0]["name"], "red_team": t[1]["name"],
     "first_pick_team": t[0]["name"], "blue_players": t[0]["roster"], "red_players": t[1]["roster"], "fearless": "hard", "actions": []}
r = c.post("/api/predict", json=s)
assert r.status_code == 200 and len(r.json()["next_action"]["candidates"]) == 12, r.text[:300]
assert c.get("/").status_code == 200 and c.get("/static/fonts/sofia-sans-latin.woff2").status_code == 200
print(m["data_through"])
"""


def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        return out.stdout.strip() + ("+uncommitted" if dirty else "")
    except Exception:
        return "unknown"


def build(out: Path, images: bool = True, check: bool = True) -> dict:
    out = out.resolve()
    if out.exists():
        if not (out / MANIFEST).exists():  # never delete a directory this script did not make
            raise SystemExit(f"{out} exists and is not a deploy directory (no {MANIFEST}); remove it or choose another --out")
        shutil.rmtree(out)
    missing = [f for f in RUNTIME_FILES + TOP_LEVEL if not (ROOT / f).exists()]
    if missing:
        raise SystemExit("missing, run the pipeline first: " + ", ".join(missing))
    out.mkdir(parents=True)
    shutil.copytree(ROOT / "draft_analyzer", out / "draft_analyzer", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for f in RUNTIME_FILES + TOP_LEVEL:
        (out / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / f, out / f)
    if images and (ROOT / IMAGE_CACHE).exists():
        shutil.copytree(ROOT / IMAGE_CACHE, out / IMAGE_CACHE)
    files = sorted(p for p in out.rglob("*") if p.is_file())
    manifest = {
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "commit": _git_commit(),
        "files": len(files) + 1,
        "megabytes": round(sum(p.stat().st_size for p in files) / 1e6, 1),
        "runtime_files": {f: (out / f).stat().st_size for f in RUNTIME_FILES},
    }
    (out / MANIFEST).write_text(json.dumps(manifest, indent=2))
    if check:  # the directory must serve on its own: its own copy of the code, its own data
        run = subprocess.run([sys.executable, "-c", CHECK], cwd=out, capture_output=True, text=True,
                             env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
        if run.returncode != 0:
            raise SystemExit("the deploy directory does not serve:\n" + run.stderr[-2000:])
        manifest["data_through"] = run.stdout.strip().splitlines()[-1]
        (out / MANIFEST).write_text(json.dumps(manifest, indent=2))
    return manifest


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "deploy"))
    ap.add_argument("--no-images", action="store_true", help="leave out the cached champion images")
    ap.add_argument("--no-check", action="store_true", help="skip starting the server from the directory")
    args = ap.parse_args(argv)
    m = build(Path(args.out), images=not args.no_images, check=not args.no_check)
    print(f"{args.out}: {m['files']} files, {m['megabytes']} MB, code at {m['commit']}"
          + (f", data through {m['data_through']}, serves" if "data_through" in m else ", not checked"))


if __name__ == "__main__":
    main()
