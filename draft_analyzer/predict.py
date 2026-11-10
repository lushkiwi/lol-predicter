"""CLI demo.

Replay a historical draft up to an action index (features are as-of that game):

    python -m draft_analyzer.predict --game-id LOLTMNT02_488304 --upto 6

Follow a live draft from a hand-written state file (see examples/state_example.json):

    python -m draft_analyzer.predict --state state.json

Both print the top 10 next-action candidates with probabilities, the current win probability
(full model and "draft effect" = full minus the Elo+side+first-pick baseline) and the top
factors. ``--json`` prints the full structured explanation instead (input for an LLM layer).
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from datetime import datetime, timezone
from itertools import permutations

import numpy as np
import pandas as pd

from . import config, draft
from .explain import explain_next_action, explain_win
from .features import all_records, build_engine
from .features.asof import DraftContext, Engine, GameRecord, PreGame, records_from_games, to_days

MODELS = config.DATA_PROCESSED / "models"


class StateError(ValueError):
    """A problem with a hand-written draft state (bad champion name, too many actions, ...)."""


def load_models(live: bool) -> dict:
    with open(MODELS / ("models_live.pkl" if live else "models.pkl"), "rb") as f:
        return pickle.load(f)


# --------------------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------------------
def predict_state(engine: Engine, models: dict, pre: PreGame, actions: tuple, top_k: int = 10,
                  all_probs: bool = False) -> dict:
    """Predictions and explanations for the draft state (pre-game facts + actions so far).

    The engine must already hold the history as of ``pre.t``.
    """
    engine.begin_game(pre)
    ctx = DraftContext(pre, tuple(actions))
    names = engine.champs.names
    out: dict = {
        "game": {"game_id": pre.game_id, "league": pre.league, "patch": pre.patch, "game_in_series": pre.game_in_series,
                 "blue_team": pre.blue_team, "red_team": pre.red_team, "first_pick_side": pre.first_pick_side,
                 "fearless": pre.fearless, "n_burned": len(set(pre.blue_burned) | set(pre.red_burned))},
        "draft_so_far": [
            {"index": i, "type": draft.ACTION_TYPE[i], "slot": draft.ACTION_SLOT[i],
             "side": pre.side_of_slot(draft.ACTION_SLOT[i]), "champion": names[c] if c >= 0 else None}
            for i, c in enumerate(actions)
        ],
    }
    win = models["win"]
    out["win"] = explain_win(win["model"], win["baseline"], engine.win_vector(ctx))
    # what the picks so far have changed: same model, same game, before any draft action
    p0 = float(win["model"].predict(pd.DataFrame([engine.win_vector(DraftContext(pre, ()))]))[0])
    out["win"]["p_blue_win_before_draft"] = round(p0, 4)
    out["win"]["change_since_draft_start_blue"] = round(out["win"]["p_blue_win"] - p0, 4)
    if all_probs:  # the same model after each action so far (index 0 = before the draft)
        path = pd.DataFrame([engine.win_vector(DraftContext(pre, tuple(actions[:k]))) for k in range(len(actions) + 1)])
        out["win"]["p_blue_win_by_action"] = [round(float(x), 4) for x in win["model"].predict(path)]
    if not ctx.complete:
        na = models["next_action"]
        model = na["by_type"].get(ctx.action_type, na["joint"]) if na.get("by_type") else na["joint"]
        feat_names, X = engine.candidate_matrix(ctx)
        cols = [feat_names.index(f) for f in model.features]
        mask = ctx.available_mask(engine.C)
        ids = np.flatnonzero(mask)
        nxt = explain_next_action(model, X[ids][:, cols], ids, names, ctx.action_type, top_k=top_k, all_probs=all_probs)
        nxt.update({"action_index": ctx.k, "phase": ctx.phase, "draft_slot": ctx.slot, "acting_side": ctx.acting_side})
        out["next_action"] = nxt
    return out


# --------------------------------------------------------------------------------------
# Replay mode
# --------------------------------------------------------------------------------------
def replay(game_id: str, upto: int, top_k: int = 10) -> dict:
    games = pd.read_parquet(config.GAMES_PATH)
    if game_id not in set(games["game_id"]):
        raise SystemExit(f"unknown game id {game_id!r}")
    engine = build_engine()
    recs = all_records(engine.champs, games)  # in-scope games plus history-only games
    target = next(r for r in recs if r.pre.game_id == game_id and not r.history)
    engine.replay(r for r in recs if r.pre.t < target.pre.t)
    upto = max(0, min(int(upto), draft.N_ACTIONS))
    out = predict_state(engine, load_models(live=False), target.pre, target.actions[:upto], top_k)
    row = games.set_index("game_id").loc[game_id]
    out["game"].update({"blue_team": row.blue_team, "red_team": row.red_team, "datetime": str(row.datetime)})
    truth = {"winner_side": row.winner_side}
    if upto < draft.N_ACTIONS:
        c = target.actions[upto]
        truth["actual_next"] = engine.champs.names[c] if c >= 0 else None
        ranked = [x["champion"] for x in out["next_action"]["candidates"]]
        truth["actual_next_in_top_k"] = truth["actual_next"] in ranked
    out["truth"] = truth
    return out


# --------------------------------------------------------------------------------------
# Live / manual mode
# --------------------------------------------------------------------------------------
class NameResolver:
    """Map team / player display names in a state file to dataset ids."""

    def __init__(self, games: pd.DataFrame, history: pd.DataFrame | None = None):
        g = games.sort_values("datetime")
        if history is not None and len(history):  # history first, so in-scope names win on a clash
            g = pd.concat([history.sort_values("datetime"), g], ignore_index=True)
        self.team_ids: dict[str, str] = {}
        self.team_roster: dict[str, dict[str, str]] = {}
        self.player_ids: dict[str, str] = {}
        for side in ("blue", "red"):
            for name, tid, pids, pnames in zip(g[f"{side}_team"], g[f"{side}_team_id"], g[f"{side}_players"], g[f"{side}_player_names"]):
                self.team_ids[name.lower()] = tid
                for pid, pname in zip(pids, pnames):
                    if pname is not None and pid is not None:
                        self.team_roster.setdefault(tid, {})[pname.lower()] = pid
                        self.player_ids[pname.lower()] = pid
        self.warnings: list[str] = []

    def team(self, name: str) -> str:
        tid = self.team_ids.get(name.lower())
        if tid is None:
            self.warnings.append(f"team {name!r} not found in the data: treated as a new team (no history)")
            return f"name:{name}"
        return tid

    def players(self, team_id: str, names: list[str]) -> tuple:
        out = []
        for n in names:
            pid = self.team_roster.get(team_id, {}).get(n.lower()) or self.player_ids.get(n.lower())
            if pid is None:
                self.warnings.append(f"player {n!r} not found in the data: treated as a new player (no history)")
                pid = f"name:{n}"
            out.append(pid)
        return tuple(out)


def _encode(engine: Engine, champs: list) -> tuple:
    try:
        return engine.champs.encode(champs)
    except KeyError as e:
        raise StateError(f"state file: {e}. Use Data Dragon display names, e.g. \"Kai'Sa\", \"Jarvan IV\".")


def _guess_roles(engine: Engine, picks: list[int]) -> tuple:
    """Most likely role assignment for 5 picks (used for earlier games given without roles)."""
    probs = engine.block("roles").role_probs()
    best = max(permutations(range(5)), key=lambda perm: float(np.prod([probs[c, r] for c, r in zip(picks, perm)])))
    by_role = [0] * 5
    for c, r in zip(picks, best):
        by_role[r] = c
    return tuple(by_role)


def _pre_from_state(engine: Engine, resolver: NameResolver, s: dict, series_id: str, n: int, t: float,
                    picked: dict, wins: dict) -> PreGame:
    """``picked`` maps team id -> champion ids that team picked earlier in the series."""
    blue, red = resolver.team(s["blue_team"]), resolver.team(s["red_team"])
    fp = s["first_pick_team"]
    if s["blue_team"].strip().lower() == s["red_team"].strip().lower():
        raise StateError("state file: blue_team and red_team must be different teams")
    if fp.lower() not in (s["blue_team"].lower(), s["red_team"].lower()):
        raise StateError("state file: first_pick_team must equal blue_team or red_team")
    mode = s.get("fearless", "hard")
    both = set(_encode(engine, s.get("burned", [])))  # explicit list: unavailable to both teams

    def burned(own: str, opp: str, key: str) -> tuple:
        # hard: everything picked by either team; soft: only the team's own earlier picks
        auto = draft.burned_for_team(mode, picked.get(own, ()), picked.get(opp, ()))
        return tuple(sorted(set(auto) | both | set(_encode(engine, s.get(key, [])))))

    return PreGame(
        game_id=s.get("game_id", f"live_{series_id}_{n}"), series_id=series_id, game_in_series=n, t=t,
        patch=str(s["patch"]), league=s["league"], season=int(s.get("season", datetime.now(timezone.utc).year)),
        split=s.get("split"), playoffs=int(s.get("playoffs", 0)), blue_team=blue, red_team=red,
        blue_players=resolver.players(blue, s["blue_players"]), red_players=resolver.players(red, s["red_players"]),
        first_pick_side="Blue" if fp.lower() == s["blue_team"].lower() else "Red",
        fearless=mode, blue_burned=burned(blue, red, "blue_burned"), red_burned=burned(red, blue, "red_burned"),
        blue_series_wins=wins.get(blue, 0), red_series_wins=wins.get(red, 0),
    )


class LiveSession:
    """Models, name resolver and engine kept in memory, to answer many live states quickly
    (the CLI makes one call; the web UI one per click).

    The engine is restored from its pickle once per series context (everything in the state
    except ``actions``), because earlier games given in ``previous_games`` are applied to it.
    """

    def __init__(self, cache_size: int = 4):
        from collections import OrderedDict

        from .ingest.oracles_elixir import load_history

        self._engine_bytes = (MODELS / "engine.pkl").read_bytes()
        self.models = load_models(live=True)
        self.games = pd.read_parquet(config.GAMES_PATH)
        self.resolver = NameResolver(self.games, load_history())
        self._cache: OrderedDict = OrderedDict()
        self._cache_size = cache_size

    def new_engine(self) -> Engine:
        engine: Engine = pickle.loads(self._engine_bytes)
        engine.flush()
        return engine

    def _series_context(self, state: dict) -> dict:
        key = json.dumps({k: v for k, v in state.items() if k != "actions"}, sort_keys=True, default=str)
        ctx = self._cache.get(key)
        if ctx is not None:
            self._cache.move_to_end(key)
            self.resolver.warnings = list(ctx["warnings"])
            return ctx
        engine, resolver = self.new_engine(), self.resolver
        resolver.warnings = []
        t_now = to_days(state["datetime"]) if state.get("datetime") else to_days(datetime.now(timezone.utc).replace(tzinfo=None))
        t_now = max(t_now, engine.t_now)
        series_id = state.get("series_id", "live_series")
        prev = state.get("previous_games", [])
        picked: dict = {}  # team id -> set of champion ids picked earlier in this series
        wins: dict = {}
        n_known = 0
        if state.get("series_id"):
            known = self.games[self.games["series_id"] == state["series_id"]].sort_values("datetime")
            n_known = len(known)
            for g in known.itertuples():
                winner = g.blue_team_id if g.winner_side == "Blue" else g.red_team_id
                wins[winner] = wins.get(winner, 0) + 1
                picked.setdefault(g.blue_team_id, set()).update(c for c in _encode(engine, g.blue_picks) if c >= 0)
                picked.setdefault(g.red_team_id, set()).update(c for c in _encode(engine, g.red_picks) if c >= 0)
        for i, pg in enumerate(prev, 1):
            merged = {**{k: v for k, v in state.items() if k not in ("actions", "previous_games", "burned", "blue_burned", "red_burned")}, **pg}
            t = t_now - (len(prev) - i + 1) / 24.0
            pre = _pre_from_state(engine, resolver, merged, series_id, n_known + i, t, picked, dict(wins))
            actions = _encode(engine, pg["actions"])
            if len(actions) != draft.N_ACTIONS:
                raise StateError(f"previous_games[{i - 1}]: need all 20 actions (use null for a missed ban)")
            if pg["winner"].lower() not in (merged["blue_team"].lower(), merged["red_team"].lower()):
                raise StateError(f"previous_games[{i - 1}]: winner must be one of the two teams")
            engine.begin_game(pre)
            rec = GameRecord(pre=pre, t_end=t + 0.5 / 24, actions=actions, blue_roles=(), red_roles=(), blue_win=0)
            blue_win = int(pg["winner"].lower() == merged["blue_team"].lower())
            rec = GameRecord(pre=pre, t_end=t + 0.5 / 24, actions=actions,
                             blue_roles=_guess_roles(engine, list(rec.picks("Blue"))),
                             red_roles=_guess_roles(engine, list(rec.picks("Red"))), blue_win=blue_win)
            engine.submit(rec)
            engine.flush()
            winner = pre.blue_team if blue_win else pre.red_team
            wins[winner] = wins.get(winner, 0) + 1
            picked.setdefault(pre.blue_team, set()).update(c for c in rec.picks("Blue") if c >= 0)
            picked.setdefault(pre.red_team, set()).update(c for c in rec.picks("Red") if c >= 0)
        n = int(state.get("game_in_series", n_known + len(prev) + 1))
        pre = _pre_from_state(engine, resolver, state, series_id, n, t_now, picked, wins)
        ctx = {"engine": engine, "pre": pre, "warnings": list(resolver.warnings)}
        self._cache[key] = ctx
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return ctx

    def predict(self, state: dict, top_k: int = 10, all_probs: bool = False) -> dict:
        ctx = self._series_context(state)
        engine, pre = ctx["engine"], ctx["pre"]
        actions = _encode(engine, state.get("actions", []))
        if len(actions) > draft.N_ACTIONS:
            raise StateError("state file: more than 20 actions")
        taken = [c for c in actions if c >= 0]
        if len(taken) != len(set(taken)):
            raise StateError("state file: a champion appears twice in actions")
        out = predict_state(engine, self.models, pre, actions, top_k, all_probs=all_probs)
        out["game"].update({"blue_team": state["blue_team"], "red_team": state["red_team"]})
        names = engine.champs.names
        out["game"]["blue_burned"] = [names[c] for c in pre.blue_burned]
        out["game"]["red_burned"] = [names[c] for c in pre.red_burned]
        out["warnings"] = list(ctx["warnings"])
        return out


def live(state: dict, top_k: int = 10) -> dict:
    """Predict from a manual state. Earlier games of the series are taken from the dataset
    (when ``series_id`` names a series in it) and/or from ``previous_games``; the latter are
    applied to the engine first, so Fearless burns, series memory and team/player stats
    include them."""
    return LiveSession().predict(state, top_k)


# --------------------------------------------------------------------------------------
# Printing
# --------------------------------------------------------------------------------------
def render(out: dict) -> str:
    g = out["game"]
    fp_team = g["blue_team"] if g["first_pick_side"] == "Blue" else g["red_team"]
    lines = [
        f"{g['league']}  patch {g['patch']}  game {g['game_in_series']} of series   {g.get('datetime', '')}",
        f"  Blue: {g['blue_team']}    Red: {g['red_team']}    first pick: {fp_team} ({g['first_pick_side']})"
        f"    Fearless: {g['fearless']} ({g['n_burned']} champions burned)",
        "",
        "Draft so far:" if out["draft_so_far"] else "Draft so far: (nothing yet)",
    ]
    for a in out["draft_so_far"]:
        lines.append(f"  {a['index']:>2}  {a['side']:<4} {a['type']:<4} {a['champion'] or '(missed)'}")
    na = out.get("next_action")
    if na:
        lines += ["", f"Next: action {na['action_index']} = {na['acting_side']} {na['action_type']} "
                      f"({na['phase']}, {na['draft_slot']} slot), {na['n_candidates']} champions available",
                  f"  {'#':>2}  {'champion':<16} {'prob':>6}   top factors (relative log-odds vs the field)"]
        for c in na["candidates"]:
            fac = ";  ".join(f"{f['group']} {f['shap_relative']:+.2f}" for f in c["top_factors"][:3])
            lines.append(f"  {c['rank']:>2}  {c['champion']:<16} {c['probability']:>6.1%}   {fac}")
    w = out["win"]
    lines += ["", f"Win probability:  Blue {w['p_blue_win']:.1%}  /  Red {w['p_red_win']:.1%}",
              f"  baseline (Elo + side + first pick): Blue {w['p_blue_win_baseline']:.1%}   "
              f"draft effect (full model minus baseline; NOT VALIDATED, see below): {w['draft_effect_blue']:+.1%} for Blue",
              f"  same model before the draft started: Blue {w['p_blue_win_before_draft']:.1%}   "
              f"change from the picks so far: {w['change_since_draft_start_blue']:+.1%} for Blue",
              "  note: on held-out games the full model is not measurably more accurate than the baseline,",
              "        so the draft effect and the change from the picks are the model's opinion, not a tested signal.",
              "  top factors (log-odds of a blue win vs the average game):"]
    for f in w["top_factors"]:
        detail = ", ".join(f"{x['name']}={x['value']:.3g}" for x in f["features"][:3])
        lines.append(f"    {f['group']:<24} {f['shap']:+.3f} -> {f['favours']:<4}  [{detail}]")
    t = out.get("truth")
    if t:
        lines.append("")
        if "actual_next" in t:
            lines.append(f"Actual next action: {t['actual_next']}  (in top {len(na['candidates'])}: {t['actual_next_in_top_k']})")
        lines.append(f"Actual winner: {t['winner_side']}")
    for wmsg in out.get("warnings", []):
        lines.append(f"warning: {wmsg}")
    return "\n".join(lines)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game-id", help="replay mode: Oracle's Elixir game id")
    ap.add_argument("--upto", type=int, default=0, help="replay mode: number of draft actions already made (0-20)")
    ap.add_argument("--state", help="live mode: path to a state JSON file")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--json", action="store_true", help="print the full structured output")
    args = ap.parse_args(argv)
    if bool(args.game_id) == bool(args.state):
        ap.error("give exactly one of --game-id or --state")
    try:
        out = replay(args.game_id, args.upto, args.top) if args.game_id else live(json.load(open(args.state)), args.top)
    except StateError as e:
        raise SystemExit(str(e))
    print(json.dumps(out, indent=2) if args.json else render(out))


if __name__ == "__main__":
    main(sys.argv[1:])
