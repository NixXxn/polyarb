from __future__ import annotations

from dataclasses import dataclass, field

from pm_trader.analytics import compute_stats
from pm_trader.engine import Engine
from pm_trader.models import Position

from arbot.markets import polymarket_event_url


@dataclass
class ScanCounts:
    candidates: int = 0
    orders_placed: int = 0
    fills: int = 0
    resolved: int = 0
    risk_exits: int = 0
    pending: int = 0


@dataclass
class StrategyStats:
    name: str
    trades: int
    buys: int
    sells: int
    win_rate: float


@dataclass
class OpenPositionLink:
    strategy: str
    outcome: str
    label: str
    url: str


@dataclass
class CombinedStats:
    cash: float
    positions: float
    total: float
    pnl: float
    unrealized_pnl: float
    roi_pct: float
    trades: int
    buys: int
    sells: int
    win_rate: float
    max_drawdown: float
    fees: float
    avg_trade: float
    by_strategy: list[StrategyStats] = field(default_factory=list)
    open_positions: list[OpenPositionLink] = field(default_factory=list)


def fmt_money(value: float) -> str:
    """European-style money: $1.234,56"""
    sign = "-" if value < 0 else ""
    body = f"{abs(value):,.2f}"
    body = body.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{sign}${body}"


def fmt_pnl(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{fmt_money(abs(value))}"


def mark_positions(engine: Engine) -> float:
    total = 0.0
    for pos in engine.db.get_open_positions():
        try:
            market = engine.api.get_market(pos.market_slug)
            token = market.get_token_id(pos.outcome)
            mid = engine.api.get_midpoint(token)
            total += pos.shares * float(mid)
        except Exception:
            total += pos.shares * pos.avg_entry_price
    return total


def _sell_realized_pnl(trades_chronological: list) -> dict[int, float]:
    """Per-sell realized P&L using running average cost basis."""
    from collections import defaultdict

    shares: dict[tuple[str, str], float] = defaultdict(float)
    cost: dict[tuple[str, str], float] = defaultdict(float)
    out: dict[int, float] = {}
    for t in trades_chronological:
        key = (t.market_condition_id, t.outcome)
        if t.side == "buy":
            shares[key] += t.shares
            cost[key] += t.amount_usd
        elif t.side == "sell":
            held = shares.get(key, 0.0)
            if held > 0:
                avg_entry = cost[key] / held
                sold = min(t.shares, held)
                cost_of_sold = avg_entry * sold
                shares[key] = held - sold
                cost[key] = max(cost[key] - cost_of_sold, 0.0)
            else:
                cost_of_sold = t.avg_price * t.shares
            proceeds = t.amount_usd - t.fee
            out[t.id] = proceeds - cost_of_sold
    return out


def realized_pnl_total(trades_chronological: list) -> float:
    """Sum of realized P&L across all sell trades (open positions excluded)."""
    return sum(_sell_realized_pnl(trades_chronological).values())


def _position_ledger_realized(engine: Engine) -> tuple[float, int, int]:
    """Sum realized P&L from closed positions (sells + resolutions).

    Resolutions credit cash via resolve_position but do not insert sell trades,
    so trade-only stats understate hold-to-resolution strategies (incl. arbitrage).
    """
    try:
        rows = engine.db.conn.execute(
            """
            SELECT realized_pnl
            FROM positions
            WHERE (shares <= 0 OR is_resolved = 1)
              AND realized_pnl IS NOT NULL
            """
        ).fetchall()
    except Exception:
        return 0.0, 0, 0
    total = 0.0
    wins = 0
    closed = 0
    for (pnl,) in rows:
        try:
            value = float(pnl)
        except (TypeError, ValueError):
            continue
        closed += 1
        total += value
        if value > 0:
            wins += 1
    return total, wins, closed


def account_stats(engine: Engine) -> dict:
    account = engine.get_account()
    positions_value = mark_positions(engine)
    trades = engine.db.get_trades(limit=10_000)
    chronological = list(reversed(trades))
    raw = compute_stats(trades, account, positions_value)
    sell_realized = realized_pnl_total(chronological)
    pos_realized, pos_wins, pos_closed = _position_ledger_realized(engine)
    # Prefer position ledger when it captures resolutions; fall back to sells.
    if pos_closed > 0:
        realized = pos_realized
        if pos_closed > 0:
            raw["win_rate"] = pos_wins / pos_closed
            raw["sell_count"] = max(int(raw.get("sell_count", 0)), pos_closed)
    else:
        realized = sell_realized
    total_pnl = float(raw.get("pnl", 0.0))
    raw["unrealized_pnl"] = total_pnl - realized
    raw["realized_pnl"] = realized
    raw["pnl"] = realized
    raw["roi_pct"] = (
        (realized / account.starting_balance * 100) if account.starting_balance else 0.0
    )
    return raw


def _strategy_stats(name: str, raw: dict) -> StrategyStats:
    return StrategyStats(
        name=name,
        trades=int(raw["total_trades"]),
        buys=int(raw["buy_count"]),
        sells=int(raw["sell_count"]),
        win_rate=float(raw["win_rate"]),
    )


def _position_label(pos: Position) -> str:
    question = (pos.market_question or "").strip()
    if question:
        return question
    return pos.market_slug


def _open_position_links(named_engines: list[tuple[str, Engine]]) -> list[OpenPositionLink]:
    links: list[OpenPositionLink] = []
    for name, engine in named_engines:
        for pos in engine.db.get_open_positions():
            if pos.shares <= 0 or pos.is_resolved:
                continue
            links.append(
                OpenPositionLink(
                    strategy=name,
                    outcome=pos.outcome,
                    label=_position_label(pos),
                    url=polymarket_event_url(pos.market_slug),
                )
            )
    return links


def combine_engines(named_engines: list[tuple[str, Engine]]) -> CombinedStats:
    labeled = [(name, account_stats(engine)) for name, engine in named_engines]
    if not labeled:
        return CombinedStats(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    stats_list = [raw for _, raw in labeled]
    cash = sum(s["cash"] for s in stats_list)
    positions = sum(s["positions_value"] for s in stats_list)
    total = sum(s["total_value"] for s in stats_list)
    starting = sum(s["starting_balance"] for s in stats_list)
    realized_pnl = sum(float(s.get("realized_pnl", 0.0)) for s in stats_list)
    unrealized_pnl = sum(float(s.get("unrealized_pnl", 0.0)) for s in stats_list)
    roi = (realized_pnl / starting * 100) if starting else 0.0
    trades = sum(s["total_trades"] for s in stats_list)
    buys = sum(s["buy_count"] for s in stats_list)
    sells = sum(s["sell_count"] for s in stats_list)
    fees = sum(s["total_fees"] for s in stats_list)
    # Size-weighted average trade; win rate / drawdown: merge trades conceptually
    # Use trade-weighted win rate approximation from per-account stats.
    win_rate = (
        sum(s["win_rate"] * s["sell_count"] for s in stats_list) / sells if sells else 0.0
    )
    max_dd = max(s["max_drawdown"] for s in stats_list)
    avg = (sum(s["avg_trade_size"] * s["total_trades"] for s in stats_list) / trades) if trades else 0.0
    return CombinedStats(
        cash=cash,
        positions=positions,
        total=total,
        pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        roi_pct=roi,
        trades=trades,
        buys=buys,
        sells=sells,
        win_rate=win_rate,
        max_drawdown=max_dd,
        fees=fees,
        avg_trade=avg,
        by_strategy=[_strategy_stats(name, raw) for name, raw in labeled],
        open_positions=_open_position_links(named_engines),
    )


def format_scan_update(counts: ScanCounts, stats: CombinedStats) -> str:
    strategy_lines = ""
    if stats.by_strategy:
        strategy_lines = "".join(
            f"{s.name} {s.trades} ({s.buys} buys/{s.sells} sells), "
            f"win rate {s.win_rate * 100:.1f}%;\n"
            for s in stats.by_strategy
        )
    return (
        "Scan complete:\n"
        f"{counts.candidates} candidates;\n"
        f"{counts.orders_placed} orders placed;\n"
        f"{counts.pending} pending;\n"
        f"{counts.fills} fills/expirations; {counts.resolved} resolved;\n"
        f"{counts.risk_exits} risk exits.\n"
        "\n"
        f"Cash {fmt_money(stats.cash)};\n"
        f"positions {fmt_money(stats.positions)};\n"
        f"total {fmt_money(stats.total)}\n"
        f"P&L (realisiert) {fmt_pnl(stats.pnl)};\n"
        f"P&L (unrealisiert) {fmt_pnl(stats.unrealized_pnl)};\n"
        f"ROI (realisiert) {stats.roi_pct:.2f}%.\n"
        f"Kontostand {fmt_money(stats.total)} "
        f"(Cash {fmt_money(stats.cash)} + offen {fmt_money(stats.positions)}).\n"
        f"Trades {stats.trades} ({stats.buys} buys/{stats.sells} sells);\n"
        f"{strategy_lines}"
        f"win rate {stats.win_rate * 100:.1f}%;\n"
        f"max drawdown {stats.max_drawdown * 100:.2f}%;\n"
        f"fees {fmt_money(stats.fees)};\n"
        f"average trade {fmt_money(stats.avg_trade)};"
        f"{_format_open_positions(stats.open_positions)}"
    )


def _format_open_positions(positions: list[OpenPositionLink]) -> str:
    if not positions:
        return ""
    lines = "\n".join(
        f"{p.strategy} {p.outcome} {p.label} {p.url}" for p in positions
    )
    return f"\n\nOpen positions:\n{lines}"
