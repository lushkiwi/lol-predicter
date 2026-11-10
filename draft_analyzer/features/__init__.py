"""Feature engine assembly."""
from __future__ import annotations

from .asof import ChampionIndex, Engine

# SHAP / ablation groups, keyed by feature-name prefix (see StatBlock.prefix).
FEATURE_GROUPS = {
    "meta_": "champion meta strength",
    "tag_": "champion attributes",
    "role_": "role fit",
    "team_": "team tendency",
    "player_": "player comfort",
    "pair_": "synergy/counter",
    "elo_": "team strength",
    "series_": "series context",
    "ctx_": "draft context",
    "base_": "baseline statistics (never used by the M3 models)",
}


def group_of(feature: str) -> str:
    for prefix, group in FEATURE_GROUPS.items():
        if feature.startswith(prefix):
            return group
    return "other"


def default_blocks(champs: ChampionIndex, include: set[str] | None = None) -> list:
    """All stat blocks in dependency order (elo and roles first; others may read them)."""
    from .context import ContextBlock
    from .elo import EloBlock
    from .roles import RoleBlock

    blocks = [EloBlock(), RoleBlock(champs.C), ContextBlock(champs.C)]
    optional = [
        ("champion", "ChampionMetaBlock"),
        ("team", "TeamTendencyBlock"),
        ("player", "PlayerMasteryBlock"),
        ("pairwise", "PairwiseBlock"),
    ]
    import importlib

    for module, cls in optional:
        try:
            mod = importlib.import_module(f"{__name__}.{module}")
        except ModuleNotFoundError:
            continue
        blocks.append(getattr(mod, cls)(champs))
    from ..models.baselines import BaselineBlock

    blocks.append(BaselineBlock(champs))
    if include is not None:
        blocks = [b for b in blocks if b.name in include]
    return blocks


def build_engine(champs: ChampionIndex | None = None, include: set[str] | None = None) -> Engine:
    champs = champs or ChampionIndex.load()
    return Engine(champs, default_blocks(champs, include))
