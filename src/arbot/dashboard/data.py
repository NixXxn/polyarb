from __future__ import annotations

import csv
import io
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pm_trader.engine import Engine

from arbot.accounts import STRATEGY, make_engine, reset_all_strategies
from arbot.config import ROOT, load_settings, settings_public_dict, update_settings
from arbot.decision_log import load_decisions
from arbot.live_sync import load_live_open_orders, load_live_sync_meta
from arbot.markets import polymarket_event_url
from arbot.mode import ResolvedMode, load_dotenv_file, resolve_mode
from arbot.paths import data_dir_from_env
from arbot.report import account_stats, combine_engines
from arbot.scan_history import load_scan_history
from arbot.trade_log import build_activity_feed, load_skipped_trades


def server_tzinfo() -> timezone | ZoneInfo:
    """IANA zone from $TZ, otherwise the process local zone."""
    raw = (os.environ.get("TZ") or "").strip()
    if raw:
        try:
            return ZoneInfo(raw)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def server_clock(now: datetime | None = None) -> dict[str, Any]:
    tz = server_tzinfo()
    current = (now or datetime.now(timezone.utc)).astimezone(tz)
    offset = current.utcoffset() or timedelta(0)
    offset_minutes = int(offset.total_seconds() // 60)
    hours, minutes = divmod(abs(offset_minutes), 60)
    sign = "+" if offset_minutes >= 0 else "-"
    utc_offset = f"{sign}{hours:02d}:{minutes:02d}"
    abbr = current.tzname() or "UTC"
    iana = getattr(tz, "key", None) or (os.environ.get("TZ") or "").strip() or abbr
    label = "UTC" if offset_minutes == 0 and abbr in {"UTC", "GMT"} else f"{abbr} (UTC{utc_offset})"
    return {
        "timezone": iana,
        "timezone_abbr": abbr,
        "display_timezone": label,
        "utc_offset": utc_offset,
        "utc_offset_minutes": offset_minutes,
        "server_time": current.strftime(f"%Y-%m-%d %H:%M:%S {abbr}"),
        "server_time_iso": current.isoformat(),
    }


def format_dashboard_ts(value: Any, *, tz: timezone | ZoneInfo | None = None) -> str:
    """Format stored timestamps in the server timezone.

    Activity/decision/scan logs write ``datetime.now(timezone.utc).isoformat()``.
    Ledger ``created_at`` values are typically naive UTC. Naive datetimes are
    treated as UTC, then converted to the process timezone (or ``TZ``).
    """
    if value is None:
        return ""
    zone = tz or server_tzinfo()
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return ""
        # Already formatted for display, e.g. "2026-09-05 16:50:10 CEST".
        if "T" not in text and " " in text and not text[-1].isdigit():
            return text
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(zone)
    abbr = local.tzname() or "UTC"
    return local.strftime(f"%Y-%m-%d %H:%M:%S {abbr}")


def _stamp_row(row: dict[str, Any], *keys: str) -> dict[str, Any]:
    out = dict(row)
    for key in keys:
        if key in out and out[key] is not None:
            out[key] = format_dashboard_ts(out[key])
    return out


_RESET_STATS_FILES = (
    "activity.jsonl",
    "decisions.jsonl",
    "skipped_trades.jsonl",
    "scan_history.jsonl",
    "live_sync_state.json",
    "arb_exit_state.json",
)


def _resolve_dashboard(
    data_dir: Path | None = None,
    mode: str | None = None,
) -> tuple[Any, ResolvedMode]:
    load_dotenv_file(ROOT / ".env")
    settings = load_settings()
    resolved = resolve_mode(
        settings_mode=settings.mode,
        cli_mode=mode,
        confirm_live=False,
        data_dir=data_dir or data_dir_from_env(),
        clob_host=settings.live.clob_host,
        chain_id=settings.live.chain_id,
        signature_type=settings.live.signature_type,
        funder=settings.live.funder,
        require_credentials=False,
    )
    return settings, resolved


def reset_strategy_budgets(
    *,
    data_dir: Path | None = None,
    mode: str | None = None,
    balance: float,
) -> dict[str, Any]:
    settings, resolved = _resolve_dashboard(data_dir, mode)
    results = reset_all_strategies(resolved.data_dir, balance)
    return {
        "ok": True,
        "mode": resolved.mode,
        "data_dir": str(resolved.data_dir),
        "accounts": [
            {"name": name, "cash": cash, "starting_balance": starting}
            for name, cash, starting in results
        ],
        "settings": settings_public_dict(settings),
    }


def set_strategy_budget(
    *,
    strategy: str = STRATEGY,
    balance: float,
    data_dir: Path | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    settings, resolved = _resolve_dashboard(data_dir, mode)
    if strategy != STRATEGY:
        raise ValueError(f"Unknown strategy {strategy!r}")
    engine = make_engine(STRATEGY, resolved.data_dir, balance, reset=True)
    acct = engine.get_account()
    payload = {
        "ok": True,
        "name": STRATEGY,
        "cash": acct.cash,
        "starting_balance": acct.starting_balance,
    }
    engine.close()
    return payload


def reset_all_statistics(
    *,
    data_dir: Path | None = None,
    mode: str | None = None,
    balance: float | None = None,
) -> dict[str, Any]:
    settings, resolved = _resolve_dashboard(data_dir, mode)
    root = resolved.data_dir
    removed: list[str] = []
    for name in _RESET_STATS_FILES:
        path = root / name
        if path.is_file():
            path.unlink()
            removed.append(str(path))
    strat_dir = root / STRATEGY
    for name in ("arb_exit_state.json", "position_exit_state.json"):
        path = strat_dir / name
        if path.is_file():
            path.unlink()
            removed.append(str(path))
    if balance is not None:
        reset_all_strategies(root, float(balance))
    return {"ok": True, "removed": removed, "settings": settings_public_dict(settings)}


def save_dashboard_settings(
    updates: dict[str, Any],
    *,
    data_dir: Path | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    settings, resolved = _resolve_dashboard(data_dir, mode)
    updated = update_settings(updates, settings.settings_path)
    return {
        "ok": True,
        "mode": resolved.mode,
        "settings": settings_public_dict(updated),
    }


def fetch_dashboard(
    data_dir: Path | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    settings, resolved = _resolve_dashboard(data_dir, mode)
    balance = float(settings.arbitrage.starting_balance or settings.starting_balance)
    engine = make_engine(STRATEGY, resolved.data_dir, balance)
    try:
        raw = account_stats(engine)
        combined = combine_engines([(STRATEGY, engine)])
        trades = engine.db.get_trades(limit=200)
        positions = []
        for pos in engine.db.get_open_positions():
            if pos.shares <= 0 or pos.is_resolved:
                continue
            positions.append(
                {
                    "strategy": STRATEGY,
                    "outcome": pos.outcome,
                    "market_slug": pos.market_slug,
                    "market_question": pos.market_question,
                    "shares": pos.shares,
                    "avg_entry_price": pos.avg_entry_price,
                    "total_cost": pos.total_cost,
                    "url": polymarket_event_url(pos.market_slug),
                }
            )
        trade_rows = [
            _stamp_row(
                {
                    "id": t.id,
                    "strategy": STRATEGY,
                    "side": t.side,
                    "market_slug": t.market_slug,
                    "market_question": t.market_question,
                    "outcome": t.outcome,
                    "avg_price": t.avg_price,
                    "amount_usd": t.amount_usd,
                    "shares": t.shares,
                    "fee": t.fee,
                    "order_type": t.order_type,
                    "created_at": t.created_at,
                    "url": polymarket_event_url(t.market_slug),
                },
                "created_at",
            )
            for t in trades
        ]
        live_sync = load_live_sync_meta(resolved.data_dir) if resolved.is_live else None
        if live_sync:
            live_sync = _stamp_row(live_sync, "last_sync")
        clock = server_clock()
        return {
            "ok": True,
            "mode": resolved.mode,
            "data_dir": str(resolved.data_dir),
            "timezone": clock["display_timezone"],
            "timezone_name": clock["timezone"],
            "timezone_abbr": clock["timezone_abbr"],
            "utc_offset": clock["utc_offset"],
            "utc_offset_minutes": clock["utc_offset_minutes"],
            "server_time": clock["server_time"],
            "server_time_iso": clock["server_time_iso"],
            "settings": settings_public_dict(settings),
            "portfolio": {
                "cash": combined.cash,
                "positions": combined.positions,
                "total": combined.total,
                "pnl": combined.pnl,
                "unrealized_pnl": combined.unrealized_pnl,
                "roi_pct": combined.roi_pct,
                "trades": combined.trades,
                "buys": combined.buys,
                "sells": combined.sells,
                "win_rate": combined.win_rate,
                "by_strategy": [
                    {
                        "name": STRATEGY,
                        "label": "Arbitrage",
                        "cash": raw.get("cash"),
                        "starting_balance": raw.get("starting_balance"),
                        "pnl": raw.get("realized_pnl"),
                        "unrealized_pnl": raw.get("unrealized_pnl"),
                        "trades": raw.get("total_trades"),
                        "win_rate": raw.get("win_rate"),
                    }
                ],
            },
            "positions": positions,
            "trades": trade_rows,
            "activity": [
                _stamp_row(row, "ts", "created_at")
                for row in build_activity_feed(resolved.data_dir, limit=80)
            ],
            "decisions": [
                _stamp_row(row, "ts") for row in load_decisions(resolved.data_dir, limit=80)
            ],
            "skipped": [
                _stamp_row(row, "ts")
                for row in load_skipped_trades(resolved.data_dir, limit=40)
            ],
            "scan_history": [
                _stamp_row(row, "ts")
                for row in load_scan_history(resolved.data_dir, limit=60)
            ],
            "live_orders": load_live_open_orders(resolved.data_dir) if resolved.is_live else [],
            "live_sync": live_sync,
        }
    finally:
        engine.close()


def export_dashboard_csv(
    kind: str,
    *,
    data_dir: Path | None = None,
    mode: str | None = None,
) -> tuple[str, str]:
    """Return (csv_text, filename) for trades or activity."""
    settings, resolved = _resolve_dashboard(data_dir, mode)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    kind = (kind or "trades").strip().lower()
    if kind == "activity":
        rows = build_activity_feed(resolved.data_dir, limit=2000)
        fields = ["ts", "event", "decision", "strategy", "message", "slug", "action"]
        filename = f"arbot-activity-{stamp}.csv"
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})
        return buf.getvalue(), filename

    balance = float(settings.arbitrage.starting_balance or settings.starting_balance)
    engine = make_engine(STRATEGY, resolved.data_dir, balance)
    try:
        trades = engine.db.get_trades(limit=10000)
        fields = [
            "created_at",
            "side",
            "outcome",
            "avg_price",
            "amount_usd",
            "shares",
            "fee",
            "order_type",
            "market_slug",
            "market_question",
        ]
        filename = f"arbot-trades-{stamp}.csv"
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fields)
        writer.writeheader()
        for t in trades:
            writer.writerow(
                {
                    "created_at": t.created_at,
                    "side": t.side,
                    "outcome": t.outcome,
                    "avg_price": t.avg_price,
                    "amount_usd": t.amount_usd,
                    "shares": t.shares,
                    "fee": t.fee,
                    "order_type": t.order_type,
                    "market_slug": t.market_slug,
                    "market_question": t.market_question,
                }
            )
        return buf.getvalue(), filename
    finally:
        engine.close()
