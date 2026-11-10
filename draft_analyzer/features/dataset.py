"""Build model-ready datasets with a single chronological pass of the as-of engine.

Next-action data: one row per (draft action, available candidate champion). Candidate
features go to a float32 memmap (they are big); context features are stored once per action.
Win data: one row per (game, draft checkpoint).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .. import config, draft
from .asof import ChampionIndex, DraftContext, Engine, records_from_games

FEATURE_DIR = config.DATA_PROCESSED / "features"
WIN_CHECKPOINTS = (0, 6, 12, 16, 20)  # start, after ban1, after pick1, after ban2, complete

# Time-based splits, by SERIES start (a series is never split). See docs/M2_report.md.
VAL_START = pd.Timestamp("2026-07-20")   # last regional split of 2026 (+ playoffs)
TEST_START = pd.Timestamp("2026-09-17")  # Worlds 2026 onward: touched once, at the end
TUNE_START = pd.Timestamp("2026-04-01")  # tail of the training period, for early stopping / tuning


def assign_split(frame: pd.DataFrame, games: pd.DataFrame) -> pd.Series:
    """'train' / 'val' / 'test' per row of ``frame`` (needs series_id), by series start time."""
    start = games.groupby("series_id")["datetime"].min()
    s = frame["series_id"].map(start)
    out = pd.Series("train", index=frame.index)
    out[s >= VAL_START] = "val"
    out[s >= TEST_START] = "test"
    return out


@dataclass
class NextActionData:
    cand_names: list[str]
    ctx_names: list[str]
    X_cand: np.ndarray  # (N, Fc) float32 (memmap)
    X_ctx: np.ndarray  # (A, Fx) float32
    row_action: np.ndarray  # (N,) int32 -> index into actions
    row_champ: np.ndarray  # (N,) int16 champion id
    row_label: np.ndarray  # (N,) bool
    actions: pd.DataFrame  # A rows; 'start' and 'n_cand' delimit each action's candidate rows

    @property
    def feature_names(self) -> list[str]:
        return self.cand_names + self.ctx_names

    def matrix(self, rows: np.ndarray | slice | None = None, features: list[str] | None = None) -> np.ndarray:
        """Dense float32 design matrix for the given candidate rows (candidate + context)."""
        if rows is None:
            rows = slice(None)
        names = self.feature_names
        keep = names if features is None else features
        ci = [i for i, n in enumerate(self.cand_names) if n in set(keep)]
        xi = [i for i, n in enumerate(self.ctx_names) if n in set(keep)]
        xc = np.asarray(self.X_cand[rows])[:, ci] if ci else np.zeros((len(self.row_action[rows]), 0), np.float32)
        xx = self.X_ctx[self.row_action[rows]][:, xi]
        out = np.hstack([xc, xx])
        order = [self.cand_names[i] for i in ci] + [self.ctx_names[i] for i in xi]
        if features is not None and order != list(features):
            pos = {n: j for j, n in enumerate(order)}
            out = out[:, [pos[n] for n in features]]
        return out

    def select_features(self, features: list[str] | None) -> list[str]:
        """Feature names in the canonical (candidate then context) order, filtered."""
        if features is None:
            return self.feature_names
        s = set(features)
        return [n for n in self.feature_names if n in s]


def build(games: pd.DataFrame | None = None, engine: Engine | None = None, out_dir: Path | None = None,
          verbose: bool = True, history: pd.DataFrame | None = None) -> tuple[NextActionData, pd.DataFrame, Engine]:
    """Run the engine over all games and write the datasets. Returns (next-action, win, engine).

    ``history``: history-only games, replayed in the same chronological pass but never
    featurised. Defaults to the configured history leagues when ``games`` is not given, and
    to none when it is.
    """
    from . import build_engine

    if games is None:
        from ..ingest.oracles_elixir import load_history

        games = pd.read_parquet(config.GAMES_PATH)
        history = load_history() if history is None else history
    engine = engine or build_engine()
    out_dir = Path(out_dir or FEATURE_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    recs = records_from_games(games, engine.champs)
    C = engine.C

    # upper bound on candidate rows
    n_max = sum(sum(1 for c in r.actions if c >= 0) for r in recs) * C
    state = {"X": None, "n": 0, "cand_names": None, "ctx_names": None}
    ctx_rows, act_rows, win_rows = [], [], []
    row_action = np.zeros(n_max, dtype=np.int32)
    row_champ = np.zeros(n_max, dtype=np.int16)
    row_label = np.zeros(n_max, dtype=bool)
    all_ids = np.arange(C)

    def visit(g, eng):
        pre = g.pre
        for k in range(draft.N_ACTIONS + 1):
            ctx = DraftContext(pre, g.actions[:k])
            if k in WIN_CHECKPOINTS:
                win_rows.append({"game_id": pre.game_id, "series_id": pre.series_id, "k": k, "y": g.blue_win,
                                 **eng.win_vector(ctx)})
            if k == draft.N_ACTIONS:
                break
            true = g.actions[k]
            if true < 0:
                continue  # missed ban: nothing to predict
            cnames, xc, xnames, xx = eng.candidate_parts(ctx)
            if state["X"] is None:
                state["cand_names"], state["ctx_names"] = cnames, xnames
                state["X"] = np.lib.format.open_memmap(out_dir / "X_cand.npy", mode="w+", dtype=np.float32,
                                                       shape=(n_max, len(cnames)))
            mask = ctx.available_mask(C)
            forced = not mask[true]
            mask[true] = True
            ids = all_ids[mask]
            n0, n1 = state["n"], state["n"] + len(ids)
            state["X"][n0:n1] = xc[mask]
            row_action[n0:n1] = len(act_rows)
            row_champ[n0:n1] = ids
            row_label[n0:n1] = ids == true
            state["n"] = n1
            ctx_rows.append(xx)
            act_rows.append((pre.game_id, pre.series_id, k, draft.ACTION_TYPE[k], draft.ACTION_PHASE[k],
                             draft.ACTION_SLOT[k], ctx.acting_side, pre.league, true, n0, len(ids), forced))
        if verbose and len(win_rows) % (len(WIN_CHECKPOINTS) * 1000) == 0:
            print(f"  {len(win_rows) // len(WIN_CHECKPOINTS)} games", flush=True)

    hist_recs = records_from_games(history, engine.champs, history=True) if history is not None else []
    engine.process(recs + hist_recs, visit)
    n = state["n"]
    state["X"].flush()
    actions = pd.DataFrame(act_rows, columns=["game_id", "series_id", "action_index", "action_type", "phase", "draft_slot",
                                              "side", "league", "true_champ", "start", "n_cand", "label_forced"])
    dt = games.set_index("game_id")["datetime"]
    actions["datetime"] = actions["game_id"].map(dt)
    actions["split"] = assign_split(actions, games)
    win = pd.DataFrame(win_rows)
    win["datetime"] = win["game_id"].map(dt)
    win["league"] = win["game_id"].map(games.set_index("game_id")["league"])
    win["split"] = assign_split(win, games)

    np.save(out_dir / "X_ctx.npy", np.vstack(ctx_rows))
    np.save(out_dir / "row_action.npy", row_action[:n])
    np.save(out_dir / "row_champ.npy", row_champ[:n])
    np.save(out_dir / "row_label.npy", row_label[:n])
    actions.to_parquet(out_dir / "actions.parquet", index=False)
    win.to_parquet(out_dir / "win.parquet", index=False)
    (out_dir / "meta.json").write_text(json.dumps({
        "cand_names": state["cand_names"], "ctx_names": state["ctx_names"], "n_rows": int(n),
        "champions": engine.champs.names, "n_history_games": len(hist_recs),
        "history_leagues": sorted({r.pre.league for r in hist_recs}), "history_blocks": sorted(engine.history_blocks)}))
    return load(out_dir), win, engine


def load(out_dir: Path | None = None) -> NextActionData:
    out_dir = Path(out_dir or FEATURE_DIR)
    meta = json.loads((out_dir / "meta.json").read_text())
    X = np.load(out_dir / "X_cand.npy", mmap_mode="r")[: meta["n_rows"]]
    return NextActionData(
        cand_names=meta["cand_names"], ctx_names=meta["ctx_names"], X_cand=X,
        X_ctx=np.load(out_dir / "X_ctx.npy"), row_action=np.load(out_dir / "row_action.npy"),
        row_champ=np.load(out_dir / "row_champ.npy"), row_label=np.load(out_dir / "row_label.npy"),
        actions=pd.read_parquet(out_dir / "actions.parquet"),
    )


def load_win(out_dir: Path | None = None) -> pd.DataFrame:
    return pd.read_parquet(Path(out_dir or FEATURE_DIR) / "win.parquet")


def results_frame(data: NextActionData, scores: np.ndarray, action_idx: np.ndarray | None = None) -> pd.DataFrame:
    """Turn per-candidate scores (logits; higher = more likely) into the evaluation frame.

    ``scores`` covers the candidate rows of the actions in ``action_idx`` (default: all), laid
    out contiguously in action order. Probabilities are a softmax within each action. Ties in
    rank are resolved pessimistically (the true champion is ranked last among equals).
    """
    acts = data.actions if action_idx is None else data.actions.iloc[action_idx]
    sizes = acts["n_cand"].to_numpy()
    starts = np.concatenate([[0], np.cumsum(sizes)[:-1]])
    rows = rows_of(data, action_idx)
    label = data.row_label[rows]
    mx = np.maximum.reduceat(scores, starts)
    e = np.exp(scores - np.repeat(mx, sizes))
    p = e / np.repeat(np.add.reduceat(e, starts), sizes)
    true_score = scores[label]
    assert len(true_score) == len(acts), "each action must have exactly one true candidate"
    rank = np.add.reduceat((scores >= np.repeat(true_score, sizes)).astype(np.int64), starts)
    out = acts[["game_id", "series_id", "action_index", "action_type", "phase"]].reset_index(drop=True).copy()
    out["true_rank"] = rank
    out["true_prob"] = p[label]
    return out


def rows_of(data: NextActionData, action_idx: np.ndarray | None) -> np.ndarray | slice:
    """Candidate-row indices for the given actions (contiguous blocks, in action order)."""
    if action_idx is None:
        return slice(None)
    a = data.actions.iloc[action_idx]
    starts, sizes = a["start"].to_numpy(), a["n_cand"].to_numpy()
    return np.concatenate([np.arange(s, s + n) for s, n in zip(starts, sizes)]) if len(a) else np.zeros(0, np.int64)


if __name__ == "__main__":
    import time

    t = time.time()
    data, win, _ = build()
    print(f"built in {time.time() - t:.0f}s: {len(data.actions)} actions, {len(data.row_action)} candidate rows, "
          f"{len(data.cand_names)} candidate + {len(data.ctx_names)} context features; win rows {len(win)}")
    print(data.actions.groupby("split").agg(actions=("game_id", "size"), games=("game_id", "nunique"), series=("series_id", "nunique")))
