"""Patches for pm_trader PolymarketClient quirks."""

from __future__ import annotations

from pm_trader.api import PolymarketClient, _parse_market
from pm_trader.models import Market, MarketNotFoundError

_PATCHED = False


def patch_polymarket_client() -> None:
    """Retry Gamma market lookup with closed=true.

    Resolved markets often disappear from the default Gamma ``/markets?slug=``
    list but remain available when ``closed=true`` is set. Without this,
    ``resolve_all`` raises MarketNotFoundError on the first stale position and
    aborts the whole pass — leaving overnight books stuck at max_open.
    """
    global _PATCHED
    if _PATCHED or getattr(PolymarketClient.get_market, "_closed_fallback", False):
        _PATCHED = True
        return

    _orig = PolymarketClient.get_market

    def get_market(self: PolymarketClient, slug_or_id: str) -> Market:
        try:
            return _orig(self, slug_or_id)
        except MarketNotFoundError:
            data = self._gamma_get(
                "/markets", params={"slug": slug_or_id, "closed": "true"}
            )
            if isinstance(data, list) and data:
                market_data = data[0]
                self._set_cached(f"market:{slug_or_id}", market_data)
                return _parse_market(market_data)
            raise

    get_market._closed_fallback = True  # type: ignore[attr-defined]
    PolymarketClient.get_market = get_market  # type: ignore[method-assign]
    _PATCHED = True
