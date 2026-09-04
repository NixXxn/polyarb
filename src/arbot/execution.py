from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pm_trader.engine import Engine

log = logging.getLogger("arbot")

_live_client_singleton: Any | None = None


def get_shared_live_client(resolved: Any) -> Any:
    """Reuse one CLOB client per process (warm TLS + derived API creds)."""
    global _live_client_singleton
    if _live_client_singleton is None:
        from arbot.live import PyClobLiveClient

        _live_client_singleton = PyClobLiveClient(resolved)
        log.info("Live CLOB client warmed")
    return _live_client_singleton


@dataclass
class ExecutionContext:
    """Per-scan caches — cuts redundant Gamma/CLOB round-trips during bursts."""

    market_cache: dict[str, Any] = field(default_factory=dict)
    book_cache: dict[str, Any] = field(default_factory=dict)
    tick_cache: dict[str, str] = field(default_factory=dict)
    wallet_balance: float | None = None
    balance_checked: bool = False

    def get_market(self, engine: Engine, slug: str) -> Any:
        if slug not in self.market_cache:
            self.market_cache[slug] = engine.api.get_market(slug)
        return self.market_cache[slug]

    def get_order_book(self, engine: Engine, token_id: str) -> Any:
        key = str(token_id)
        if key not in self.book_cache:
            self.book_cache[key] = engine.api.get_order_book(key)
        return self.book_cache[key]

    def get_tick_size(self, engine: Engine, token_id: str, fallback: str = "0.01") -> str:
        key = str(token_id)
        if key in self.tick_cache:
            return self.tick_cache[key]
        try:
            tick = _tick_str(engine.api.get_tick_size(key))
        except Exception:
            tick = fallback
        self.tick_cache[key] = tick
        return tick

    def invalidate_book(self, token_id: str) -> None:
        self.book_cache.pop(str(token_id), None)


def _tick_str(tick: Any) -> str:
    try:
        value = float(tick)
    except (TypeError, ValueError):
        return "0.01"
    if value >= 0.1:
        return "0.1"
    if value >= 0.01:
        return "0.01"
    if value >= 0.001:
        return "0.001"
    return "0.0001"


def log_fill_latency(label: str, started: float) -> None:
    ms = (time.perf_counter() - started) * 1000
    log.info("%s latency %.1f ms", label, ms)
