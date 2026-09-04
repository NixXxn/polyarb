from __future__ import annotations

from pm_trader.models import OrderBook


def event_slug_from_market_slug(market_slug: str) -> str:
    return market_slug.rsplit("-", 1)[0] if market_slug.count("-") >= 1 else market_slug


def polymarket_event_url(market_slug: str) -> str:
    return f"https://polymarket.com/event/{event_slug_from_market_slug(market_slug)}"


def best_ask(book: OrderBook) -> tuple[float | None, float]:
    if not book.asks:
        return None, 0.0
    level = min(book.asks, key=lambda x: x.price)
    return level.price, level.size


def best_bid(book: OrderBook) -> tuple[float | None, float]:
    if not book.bids:
        return None, 0.0
    level = max(book.bids, key=lambda x: x.price)
    return level.price, level.size
