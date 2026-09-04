from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from pm_trader.engine import Engine
from pm_trader.models import OrderRejectedError

from arbot.trade_log import append_activity

log = logging.getLogger("arbot")

_MAX_SEEN_TRADES = 3000
_FAILED_STATUSES = {"FAILED", "TRADE_STATUS_FAILED", "TRADE_STATUS_RETRYING"}


class ClobQueryClient(Protocol):
    def get_open_orders(self) -> list[dict[str, Any]]: ...

    def get_trades(self) -> list[dict[str, Any]]: ...


@dataclass
class SyncResult:
    fills_applied: int = 0
    open_orders: int = 0
    bootstrap: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass
class LiveSyncState:
    initialized: bool = False
    seen_trade_ids: list[str] = field(default_factory=list)
    order_strategy: dict[str, str] = field(default_factory=dict)
    last_sync: str | None = None
    open_orders_snapshot: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, data_dir: Path | str) -> LiveSyncState:
        path = _state_path(data_dir)
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        seen = raw.get("seen_trade_ids") or []
        orders = raw.get("order_strategy") or {}
        snapshot = raw.get("open_orders_snapshot") or []
        return cls(
            initialized=bool(raw.get("initialized")),
            seen_trade_ids=[str(x) for x in seen][-_MAX_SEEN_TRADES:],
            order_strategy={str(k): str(v) for k, v in orders.items()},
            last_sync=raw.get("last_sync"),
            open_orders_snapshot=list(snapshot) if isinstance(snapshot, list) else [],
        )

    def save(self, data_dir: Path | str) -> None:
        path = _state_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        if len(self.seen_trade_ids) > _MAX_SEEN_TRADES:
            self.seen_trade_ids = self.seen_trade_ids[-_MAX_SEEN_TRADES:]
        payload = {
            "initialized": self.initialized,
            "seen_trade_ids": self.seen_trade_ids,
            "order_strategy": self.order_strategy,
            "last_sync": self.last_sync,
            "open_orders_snapshot": self.open_orders_snapshot,
        }
        path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))

    def has_seen(self, trade_id: str) -> bool:
        return trade_id in self.seen_trade_ids

    def mark_seen(self, trade_id: str) -> None:
        if trade_id in self.seen_trade_ids:
            return
        self.seen_trade_ids.append(trade_id)

    def register_order(self, order_id: str, strategy: str) -> None:
        if order_id:
            self.order_strategy[str(order_id)] = strategy

    def strategy_for_trade(self, trade: dict[str, Any]) -> str | None:
        order_id = trade.get("taker_order_id") or trade.get("order_id") or trade.get("orderID")
        if order_id:
            return self.order_strategy.get(str(order_id))
        return None


def _state_path(data_dir: Path | str) -> Path:
    root = Path(data_dir)
    if root.name == "arbitrage":
        root = root.parent
    return root / "live_sync_state.json"


def _root_data_dir(data_dir: Path | str) -> Path:
    root = Path(data_dir)
    if root.name == "arbitrage":
        return root.parent
    return root


def register_clob_response(
    data_dir: Path | str,
    *,
    strategy: str,
    resp: dict[str, Any],
) -> None:
    state = LiveSyncState.load(data_dir)
    order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
    if order_id:
        state.register_order(str(order_id), strategy)
    for tid in resp.get("tradeIDs") or []:
        state.mark_seen(str(tid))
    for h in resp.get("transactionsHashes") or []:
        state.mark_seen(f"tx:{h}")
    state.save(data_dir)


def _parse_decimal_amount(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        amount = float(text)
    except (TypeError, ValueError):
        return 0.0
    if "." not in text and abs(amount) >= 1_000_000:
        amount /= 1_000_000
    return amount


def _normalize_outcome(raw: Any) -> str:
    text = str(raw or "yes").strip().lower()
    if text in {"yes", "y", "true", "1"}:
        return "yes"
    if text in {"no", "n", "false", "0"}:
        return "no"
    return text


def parse_clob_trade(trade: dict[str, Any]) -> dict[str, Any] | None:
    trade_id = trade.get("id")
    if not trade_id:
        return None
    status = str(trade.get("status") or "").upper()
    if status in _FAILED_STATUSES:
        return None
    side_raw = str(trade.get("side") or "").upper()
    if side_raw not in {"BUY", "SELL"}:
        return None
    price = _parse_decimal_amount(trade.get("price"))
    shares = _parse_decimal_amount(trade.get("size"))
    if price <= 0 or shares <= 0:
        return None
    condition_id = trade.get("market") or trade.get("condition_id")
    if not condition_id:
        return None
    usd = shares * price
    return {
        "id": str(trade_id),
        "side": "buy" if side_raw == "BUY" else "sell",
        "price": price,
        "shares": shares,
        "usd": usd,
        "condition_id": str(condition_id),
        "outcome": _normalize_outcome(trade.get("outcome")),
        "asset_id": trade.get("asset_id"),
        "taker_order_id": trade.get("taker_order_id"),
        "status": status,
        "match_time": trade.get("match_time"),
    }


def _summarize_open_order(order: dict[str, Any]) -> dict[str, Any]:
    original = _parse_decimal_amount(
        order.get("original_size") or order.get("size") or order.get("originalSize")
    )
    matched = _parse_decimal_amount(
        order.get("size_matched") or order.get("sizeMatched") or order.get("matched_size")
    )
    price = _parse_decimal_amount(order.get("price"))
    return {
        "id": str(order.get("id") or order.get("orderID") or ""),
        "side": str(order.get("side") or "").upper(),
        "price": price,
        "original_size": original,
        "size_matched": matched,
        "remaining": max(original - matched, 0.0),
        "status": str(order.get("status") or "").lower(),
        "asset_id": order.get("asset_id") or order.get("token_id"),
        "market": order.get("market"),
    }


def _ledger_has_recent_match(
    engine: Engine,
    *,
    condition_id: str,
    outcome: str,
    side: str,
    shares: float,
    usd: float,
) -> bool:
    for trade in engine.db.get_trades(limit=30):
        if trade.market_condition_id != condition_id:
            continue
        if trade.outcome.lower() != outcome.lower():
            continue
        if trade.side != side:
            continue
        if abs(trade.shares - shares) <= max(0.01, shares * 0.02):
            if abs(trade.amount_usd - usd) <= max(0.02, usd * 0.02):
                return True
    return False


def sync_live_orders(
    client: ClobQueryClient,
    engine: Engine,
    *,
    strategy: str,
) -> SyncResult:
    """Poll CLOB open orders + trades and reconcile resting fills into the local ledger."""
    data_dir = engine.db.data_dir
    state = LiveSyncState.load(data_dir)
    result = SyncResult()

    try:
        trades = client.get_trades()
    except Exception as e:
        msg = f"get_trades failed: {e}"
        result.errors.append(msg)
        append_activity(
            data_dir,
            level="error",
            event="live_sync",
            strategy=strategy,
            message=msg,
        )
        trades = []

    try:
        open_orders = client.get_open_orders()
    except Exception as e:
        msg = f"get_open_orders failed: {e}"
        result.errors.append(msg)
        append_activity(
            data_dir,
            level="error",
            event="live_sync",
            strategy=strategy,
            message=msg,
        )
        open_orders = []

    if not state.initialized:
        for trade in trades:
            parsed = parse_clob_trade(trade)
            if parsed:
                state.mark_seen(parsed["id"])
        state.initialized = True
        state.save(data_dir)
        result.bootstrap = True
        append_activity(
            data_dir,
            level="info",
            event="live_sync_bootstrap",
            strategy=strategy,
            message=f"Marked {len(trades)} existing CLOB trades as seen",
            trade_count=len(trades),
        )
        trades = []

    for trade in trades:
        parsed = parse_clob_trade(trade)
        if parsed is None:
            continue
        trade_id = parsed["id"]
        if state.has_seen(trade_id):
            continue
        trade_strategy = state.strategy_for_trade(trade)
        if trade_strategy is not None and trade_strategy != strategy:
            state.mark_seen(trade_id)
            continue
        if trade_strategy is None and not _trade_belongs_to_engine(engine, parsed):
            continue
        if _ledger_has_recent_match(
            engine,
            condition_id=parsed["condition_id"],
            outcome=parsed["outcome"],
            side=parsed["side"],
            shares=parsed["shares"],
            usd=parsed["usd"],
        ):
            state.mark_seen(trade_id)
            continue
        try:
            from arbot.live import record_fill

            market = engine.api.get_market(parsed["condition_id"])
            outcome = engine._validate_outcome(parsed["outcome"], market)
            record_fill(
                engine,
                market=market,
                outcome=outcome,
                side=parsed["side"],
                avg_price=parsed["price"],
                shares=parsed["shares"],
                usd=parsed["usd"],
                order_type="fak",
            )
            state.mark_seen(trade_id)
            result.fills_applied += 1
            append_activity(
                data_dir,
                level="info",
                event="live_fill_synced",
                strategy=strategy,
                message=(
                    f"{parsed['side'].upper()} {market.slug} "
                    f"@ {parsed['price']:.4f} x {parsed['shares']:.2f}"
                ),
                slug=market.slug,
                outcome=outcome,
                side=parsed["side"],
                price=parsed["price"],
                shares=parsed["shares"],
                usd=parsed["usd"],
                clob_trade_id=trade_id,
            )
        except (OrderRejectedError, Exception) as e:
            msg = f"apply trade {trade_id}: {e}"
            result.errors.append(msg)
            append_activity(
                data_dir,
                level="error",
                event="live_fill_sync_failed",
                strategy=strategy,
                message=msg,
                clob_trade_id=trade_id,
            )

    summaries = [_summarize_open_order(o) for o in open_orders]
    summaries = [s for s in summaries if s["id"]]
    state.open_orders_snapshot = summaries
    state.last_sync = datetime.now(timezone.utc).isoformat()
    state.save(data_dir)
    result.open_orders = len(summaries)

    if result.fills_applied or result.errors:
        append_activity(
            data_dir,
            level="warn" if result.errors else "info",
            event="live_sync",
            strategy=strategy,
            message=(
                f"synced {result.fills_applied} fill(s), "
                f"{result.open_orders} open order(s)"
                + (f", {len(result.errors)} error(s)" if result.errors else "")
            ),
            fills_applied=result.fills_applied,
            open_orders=result.open_orders,
            errors=result.errors or None,
        )
    return result


def _trade_belongs_to_engine(engine: Engine, parsed: dict[str, Any]) -> bool:
    condition_id = parsed["condition_id"]
    outcome = parsed["outcome"]
    side = parsed["side"]
    pos = engine.db.get_position(condition_id, outcome)
    if side == "sell":
        return pos is not None and pos.shares > 0
    if pos is not None and pos.shares > 0:
        return True
    for trade in engine.db.get_trades(limit=50):
        if trade.market_condition_id == condition_id:
            return True
    return False


def load_live_open_orders(data_dir: Path | str) -> list[dict[str, Any]]:
    state = LiveSyncState.load(data_dir)
    return list(state.open_orders_snapshot)


def load_live_sync_meta(data_dir: Path | str) -> dict[str, Any]:
    state = LiveSyncState.load(data_dir)
    return {
        "initialized": state.initialized,
        "last_sync": state.last_sync,
        "open_orders": len(state.open_orders_snapshot),
        "seen_trades": len(state.seen_trade_ids),
        "tracked_orders": len(state.order_strategy),
    }
