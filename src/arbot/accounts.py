from __future__ import annotations

from pathlib import Path

from pm_trader.engine import Engine
from pm_trader.models import NotInitializedError

from arbot.paths import DEFAULT_DATA_DIR, DEFAULT_LIVE_DATA_DIR, STRATEGY, data_dir_from_env

__all__ = [
    "DEFAULT_DATA_DIR",
    "DEFAULT_LIVE_DATA_DIR",
    "STRATEGY",
    "STRATEGY_NAMES",
    "account_dir",
    "make_engine",
    "data_dir_from_env",
    "reset_all_strategies",
]

STRATEGY_NAMES = (STRATEGY,)


def account_dir(account: str = STRATEGY, data_dir: Path | None = None) -> Path:
    base = data_dir or DEFAULT_DATA_DIR
    if ".." in account or "/" in account or "\\" in account:
        raise ValueError(f"Invalid account name: {account!r}")
    return base / account


def make_engine(
    account: str = STRATEGY,
    data_dir: Path | None = None,
    starting_balance: float = 1_000.0,
    reset: bool = False,
) -> Engine:
    from arbot.api_patch import patch_polymarket_client

    patch_polymarket_client()
    engine = Engine(account_dir(account, data_dir_from_env(data_dir)))
    if reset:
        engine.reset()
        engine.init_account(starting_balance)
        return engine
    try:
        engine.get_account()
    except NotInitializedError:
        engine.init_account(starting_balance)
    return engine


def reset_all_strategies(
    data_dir: Path,
    balance: float,
    *,
    strategies: tuple[str, ...] = STRATEGY_NAMES,
) -> list[tuple[str, float, float]]:
    results: list[tuple[str, float, float]] = []
    for name in strategies:
        engine = make_engine(name, data_dir, balance, reset=True)
        acct = engine.get_account()
        results.append((name, acct.cash, acct.starting_balance))
        engine.close()
    return results
