from __future__ import annotations

import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = ROOT / "config" / "settings.yaml"


def resolve_settings_path(path: Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    raw = os.environ.get("ARBOT_SETTINGS_PATH", "").strip()
    if raw:
        return Path(raw)
    return DEFAULT_SETTINGS_PATH

# Keys editable from the dashboard (written back into settings.yaml).
EDITABLE_ARBITRAGE_KEYS = (
    "starting_balance",
    "max_pair_cost",
    "max_maker_ask_sum",
    "min_edge",
    "fee_buffer",
    "min_liquidity",
    "min_volume_24h",
    "min_ask",
    "max_ask",
    "min_ask_size",
    "position_usd",
    "max_position_usd",
    "max_open_pairs",
    "maker_tick",
    "paper_fak",
    "prefer_crypto_weather",
    "prefer_lp_rewards",
    "scan_limit",
    "max_horizon_hours",
    "maker_min_horizon_hours",
    "maker_balanced_min",
    "maker_balanced_max",
    "exit_ladder_prices",
    "exit_ladder_fraction",
    "lose_leg_bid_max",
    "lose_leg_bid_min",
    "lose_leg_lead_bid",
    "rebalance_enabled",
    "rebalance_move",
    "rebalance_fraction",
    "rebalance_min_lead",
)

EDITABLE_TOP_KEYS = (
    "mode",
    "poll_interval_seconds",
    "starting_balance",
    "min_position_usd",
    "user_agent",
)


@dataclass(frozen=True)
class LiveSettings:
    clob_host: str
    chain_id: int
    signature_type: int
    funder: str


@dataclass(frozen=True)
class ArbitrageSettings:
    max_pair_cost: float
    max_maker_ask_sum: float
    min_edge: float
    fee_buffer: float
    min_liquidity: float
    min_volume_24h: float
    min_ask: float
    max_ask: float
    min_ask_size: float
    position_usd: float
    max_position_usd: float
    max_open_pairs: int
    maker_tick: float
    paper_fak: bool
    prefer_crypto_weather: bool
    prefer_lp_rewards: bool
    scan_limit: int
    max_horizon_hours: int
    maker_min_horizon_hours: float
    maker_balanced_min: float
    maker_balanced_max: float
    starting_balance: float | None
    exit_ladder_prices: tuple[float, ...]
    exit_ladder_fraction: float
    lose_leg_bid_max: float
    lose_leg_bid_min: float
    lose_leg_lead_bid: float
    rebalance_enabled: bool
    rebalance_move: float
    rebalance_fraction: float
    rebalance_min_lead: float


@dataclass(frozen=True)
class Settings:
    mode: str
    poll_interval_seconds: int
    starting_balance: float
    min_position_usd: float
    user_agent: str
    arbitrage: ArbitrageSettings
    live: LiveSettings
    settings_path: Path


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return yaml.safe_load(f) or {}


def _parse_arb_ladder_prices(raw: Any) -> tuple[float, ...]:
    default = (0.50, 0.70, 0.85, 0.95)
    if raw is None:
        return default
    if raw == []:
        return ()
    prices: list[float] = []
    for row in raw:
        try:
            prices.append(float(row))
        except (TypeError, ValueError):
            continue
    return tuple(sorted(set(prices))) if prices else default


def _arb_from_raw(raw: dict[str, Any]) -> ArbitrageSettings:
    return ArbitrageSettings(
        max_pair_cost=float(raw.get("max_pair_cost", 0.97)),
        max_maker_ask_sum=float(raw.get("max_maker_ask_sum", 1.04)),
        min_edge=float(raw.get("min_edge", 0.025)),
        fee_buffer=float(raw.get("fee_buffer", 0.01)),
        min_liquidity=float(raw.get("min_liquidity", 400.0)),
        min_volume_24h=float(raw.get("min_volume_24h", 150.0)),
        min_ask=float(raw.get("min_ask", 0.02)),
        max_ask=float(raw.get("max_ask", 0.95)),
        min_ask_size=float(raw.get("min_ask_size", 3.0)),
        position_usd=float(raw.get("position_usd", 40.0)),
        max_position_usd=float(raw.get("max_position_usd", 100.0)),
        max_open_pairs=int(raw.get("max_open_pairs", 8)),
        maker_tick=float(raw.get("maker_tick", 0.01)),
        paper_fak=bool(raw.get("paper_fak", True)),
        prefer_crypto_weather=bool(raw.get("prefer_crypto_weather", True)),
        prefer_lp_rewards=bool(raw.get("prefer_lp_rewards", True)),
        scan_limit=int(raw.get("scan_limit", 250)),
        max_horizon_hours=int(raw.get("max_horizon_hours", 24)),
        maker_min_horizon_hours=float(raw.get("maker_min_horizon_hours", 1.0)),
        maker_balanced_min=float(raw.get("maker_balanced_min", 0.35)),
        maker_balanced_max=float(raw.get("maker_balanced_max", 0.65)),
        starting_balance=(
            float(raw["starting_balance"]) if raw.get("starting_balance") is not None else None
        ),
        exit_ladder_prices=_parse_arb_ladder_prices(raw.get("exit_ladder_prices")),
        exit_ladder_fraction=float(raw.get("exit_ladder_fraction", 0.25)),
        lose_leg_bid_max=float(raw.get("lose_leg_bid_max", 0.35)),
        lose_leg_bid_min=float(raw.get("lose_leg_bid_min", 0.05)),
        lose_leg_lead_bid=float(raw.get("lose_leg_lead_bid", 0.55)),
        rebalance_enabled=bool(raw.get("rebalance_enabled", True)),
        rebalance_move=float(raw.get("rebalance_move", 0.04)),
        rebalance_fraction=float(raw.get("rebalance_fraction", 0.10)),
        rebalance_min_lead=float(raw.get("rebalance_min_lead", 0.55)),
    )


def load_settings(path: Path | None = None) -> Settings:
    settings_path = resolve_settings_path(path)
    raw = _load_yaml(settings_path)
    live_raw = raw.get("live") or {}
    return Settings(
        mode=str(raw.get("mode") or "paper"),
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 30)),
        starting_balance=float(raw.get("starting_balance", 1000)),
        min_position_usd=float(raw.get("min_position_usd", 2)),
        user_agent=str(raw.get("user_agent") or "polymarket-arb/0.1"),
        arbitrage=_arb_from_raw(raw.get("arbitrage") or {}),
        live=LiveSettings(
            clob_host=str(live_raw.get("clob_host") or "https://clob.polymarket.com"),
            chain_id=int(live_raw.get("chain_id", 137)),
            signature_type=int(live_raw.get("signature_type", 1)),
            funder=str(live_raw.get("funder") or ""),
        ),
        settings_path=settings_path,
    )


def settings_public_dict(settings: Settings) -> dict[str, Any]:
    """JSON-friendly view for the dashboard."""
    arb = {f.name: getattr(settings.arbitrage, f.name) for f in fields(settings.arbitrage)}
    arb["exit_ladder_prices"] = list(settings.arbitrage.exit_ladder_prices)
    return {
        "mode": settings.mode,
        "poll_interval_seconds": settings.poll_interval_seconds,
        "starting_balance": settings.starting_balance,
        "min_position_usd": settings.min_position_usd,
        "user_agent": settings.user_agent,
        "arbitrage": arb,
        "live": asdict(settings.live),
        "settings_path": str(settings.settings_path),
        "editable": {
            "top": list(EDITABLE_TOP_KEYS),
            "arbitrage": list(EDITABLE_ARBITRAGE_KEYS),
            "live": ["clob_host", "chain_id", "signature_type", "funder"],
        },
    }


def update_settings(updates: dict[str, Any], path: Path | None = None) -> Settings:
    """Merge dashboard updates into settings.yaml and reload."""
    settings_path = resolve_settings_path(path)
    raw = _load_yaml(settings_path)
    if not isinstance(raw, dict):
        raw = {}

    for key in EDITABLE_TOP_KEYS:
        if key in updates and updates[key] is not None:
            raw[key] = updates[key]

    arb_updates = updates.get("arbitrage")
    if isinstance(arb_updates, dict):
        arb = dict(raw.get("arbitrage") or {})
        for key in EDITABLE_ARBITRAGE_KEYS:
            if key in arb_updates and arb_updates[key] is not None:
                arb[key] = arb_updates[key]
        raw["arbitrage"] = arb

    live_updates = updates.get("live")
    if isinstance(live_updates, dict):
        live = dict(raw.get("live") or {})
        for key in ("clob_host", "chain_id", "signature_type", "funder"):
            if key in live_updates and live_updates[key] is not None:
                live[key] = live_updates[key]
        raw["live"] = live

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with settings_path.open("w") as f:
        yaml.safe_dump(raw, f, sort_keys=False, default_flow_style=False)
    return load_settings(settings_path)
