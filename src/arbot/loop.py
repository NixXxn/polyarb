from __future__ import annotations

import logging
import time
from pathlib import Path

from pm_trader.engine import Engine
from pm_trader.models import OrderRejectedError, SimError

from arbot.accounts import STRATEGY
from arbot.config import Settings
from arbot.decision_log import log_decision, purge_stale_logs
from arbot.execution import ExecutionContext, log_fill_latency
from arbot.live import LiveTrader
from arbot.report import ScanCounts, combine_engines, format_scan_update
from arbot.scan_history import append_scan
from arbot.signals import Signal
from arbot.strategy import analyze_arbitrage, arbitrage_exits
from arbot.trade_log import append_activity, append_skipped

log = logging.getLogger("arbot")


def _resolve(engine: Engine) -> int:
    try:
        before = {p.id for p in engine.db.get_open_positions()}
        engine.resolve_all()
        after = {p.id for p in engine.db.get_open_positions()}
        return max(0, len(before) - len(after))
    except Exception as e:
        log.warning("resolve_all failed: %s", e)
        return 0


def _rollback_partial_exit(
    engine: Engine, signal: Signal, ctx: ExecutionContext | None = None
) -> None:
    if not signal.partial_exit or signal.ladder_multiple is None:
        return
    condition_id = signal.market_condition_id
    if not condition_id:
        try:
            market = (ctx or ExecutionContext()).get_market(engine, signal.slug)
            condition_id = market.condition_id
        except Exception:
            return
    try:
        from arbot.arbitrage_state import ArbExitStore

        ArbExitStore(engine.db.data_dir).unmark_ladder(
            condition_id, signal.outcome, float(signal.ladder_multiple)
        )
    except Exception:
        pass


def _paper_fill_limit_buy(
    engine: Engine,
    signal: Signal,
    *,
    strategy: str,
    started: float,
) -> bool:
    amount = float(signal.amount_usd or 0)
    price = float(signal.limit_price or 0)
    if amount <= 0 or price <= 0:
        raise OrderRejectedError("paper fill-at-limit buy needs amount and limit_price")
    shares = amount / price
    market = engine.api.get_market(signal.slug)
    outcome = engine._validate_outcome(signal.outcome, market)
    account = engine._require_account()
    fee = 0.0
    total_outflow = amount + fee
    if total_outflow > account.cash:
        from pm_trader.models import InsufficientBalanceError

        raise InsufficientBalanceError(required=total_outflow, available=account.cash)
    engine.db.update_cash(account.cash - total_outflow)
    trade = engine.db.insert_trade(
        market_condition_id=market.condition_id,
        market_slug=market.slug,
        market_question=market.question,
        outcome=outcome,
        side="buy",
        order_type="fak",
        avg_price=price,
        amount_usd=amount,
        shares=shares,
        fee_rate_bps=0,
        fee=fee,
        slippage=0.0,
        levels_filled=1,
        is_partial=False,
    )
    engine._update_position_after_buy(
        market=market,
        outcome=outcome,
        new_shares=shares,
        cost=total_outflow,
        avg_fill_price=price,
    )
    log.info(
        "PAPER FILL-AT-LIMIT BUY %s %s @ %.4f shares=%.2f — %s",
        signal.slug,
        outcome,
        price,
        shares,
        signal.reason,
    )
    log_fill_latency(f"PAPER LIMIT-FILL {signal.slug}", started)
    log_decision(
        engine.db.data_dir,
        strategy=strategy,
        decision="executed",
        reason=signal.reason,
        slug=signal.slug,
        action=signal.action,
        amount_usd=signal.amount_usd,
        shares=shares,
        limit_price=price,
        trade_id=getattr(trade, "id", None),
    )
    return True


def execute_signal(
    engine: Engine,
    signal: Signal,
    dry_run: bool,
    live: LiveTrader | None = None,
    ctx: ExecutionContext | None = None,
    strategy: str = STRATEGY,
) -> bool:
    if dry_run:
        log_decision(
            engine.db.data_dir,
            strategy=strategy,
            decision="dry_run",
            reason=signal.reason,
            slug=signal.slug,
            action=signal.action,
            amount_usd=signal.amount_usd,
            shares=signal.shares,
        )
        log.info(
            "DRY-RUN %s %s %s usd=%s shares=%s — %s",
            signal.action,
            signal.slug,
            signal.outcome,
            signal.amount_usd,
            signal.shares,
            signal.reason,
        )
        return False
    try:
        started = time.perf_counter()
        if live is not None:
            filled = live.fill(engine, signal, ctx=ctx, strategy=strategy)
            log_fill_latency(f"LIVE {signal.action.upper()} {signal.slug}", started)
            if filled:
                log_decision(
                    engine.db.data_dir,
                    strategy=strategy,
                    decision="executed",
                    reason=signal.reason,
                    slug=signal.slug,
                    action=signal.action,
                    amount_usd=signal.amount_usd,
                    shares=signal.shares,
                )
            return filled
        if (
            signal.paper_fill_at_limit
            and signal.order_type == "limit"
            and signal.limit_price is not None
            and signal.action == "buy"
        ):
            return _paper_fill_limit_buy(engine, signal, strategy=strategy, started=started)
        if signal.order_type == "limit" and signal.limit_price is not None:
            before = engine.db.get_trades(limit=1)
            before_trade_id = before[0].id if before else None
            if signal.action == "buy":
                amount = float(signal.amount_usd or 0)
                if amount <= 0:
                    raise OrderRejectedError("limit buy amount is zero")
                engine.place_limit_order(
                    signal.slug,
                    signal.outcome,
                    "buy",
                    amount,
                    signal.limit_price,
                    order_type="gtc",
                )
            else:
                shares = float(signal.shares or 0)
                if shares <= 0:
                    raise OrderRejectedError("limit sell shares is zero")
                engine.place_limit_order(
                    signal.slug,
                    signal.outcome,
                    "sell",
                    shares,
                    signal.limit_price,
                    order_type="gtc",
                )
            engine.check_orders()
            after = engine.db.get_trades(limit=1)
            after_trade_id = after[0].id if after else None
            filled = after_trade_id != before_trade_id
            log_fill_latency(f"PAPER LIMIT {signal.action.upper()} {signal.slug}", started)
            if filled:
                log_decision(
                    engine.db.data_dir,
                    strategy=strategy,
                    decision="executed",
                    reason=signal.reason,
                    slug=signal.slug,
                    action=signal.action,
                    amount_usd=signal.amount_usd,
                    shares=signal.shares,
                )
                return True
            append_activity(
                engine.db.data_dir,
                level="info",
                event="limit_order_submitted",
                strategy=strategy,
                message="limit order placed but not immediately filled",
                slug=signal.slug,
                outcome=signal.outcome,
                action=signal.action,
                limit_price=signal.limit_price,
            )
            log_decision(
                engine.db.data_dir,
                strategy=strategy,
                decision="limit_submitted",
                reason=signal.reason,
                slug=signal.slug,
                action=signal.action,
                amount_usd=signal.amount_usd,
                shares=signal.shares,
                limit_price=signal.limit_price,
            )
            return False
        if signal.action == "buy":
            result = engine.buy(
                signal.slug, signal.outcome, float(signal.amount_usd or 0), order_type="fak"
            )
            log.info(
                "BUY %s @ %.3f shares=%.2f fee=%.4f — %s",
                signal.slug,
                result.trade.avg_price,
                result.trade.shares,
                result.trade.fee,
                signal.reason,
            )
            log_fill_latency(f"PAPER BUY {signal.slug}", started)
        else:
            result = engine.sell(
                signal.slug, signal.outcome, float(signal.shares or 0), order_type="fak"
            )
            log.info(
                "SELL %s @ %.3f shares=%.2f — %s",
                signal.slug,
                result.trade.avg_price,
                result.trade.shares,
                signal.reason,
            )
            log_fill_latency(f"PAPER SELL {signal.slug}", started)
        log_decision(
            engine.db.data_dir,
            strategy=strategy,
            decision="executed",
            reason=signal.reason,
            slug=signal.slug,
            action=signal.action,
            amount_usd=signal.amount_usd,
            shares=signal.shares,
        )
        return True
    except (OrderRejectedError, SimError) as e:
        _rollback_partial_exit(engine, signal, ctx=ctx)
        append_skipped(engine.db.data_dir, strategy=strategy, signal=signal, error=str(e))
        append_activity(
            engine.db.data_dir,
            level="warn",
            event="order_skipped",
            strategy=strategy,
            message=str(e),
            slug=signal.slug,
            outcome=signal.outcome,
            action=signal.action,
            reason=signal.reason,
        )
        log_decision(
            engine.db.data_dir,
            strategy=strategy,
            decision="skipped",
            reason=f"{signal.reason} | {e}",
            slug=signal.slug,
            action=signal.action,
        )
        log.warning("skip %s %s: %s", signal.action, signal.slug, e)
        return False


def scan_once(
    settings: Settings,
    engine: Engine,
    *,
    dry_run: bool = False,
    live: LiveTrader | None = None,
) -> ScanCounts:
    counts = ScanCounts()
    ctx = ExecutionContext()
    if live is not None:
        try:
            live.sync_live_orders(engine, strategy=STRATEGY)
        except Exception as e:
            log.warning("live sync failed: %s", e)
    else:
        counts.resolved += _resolve(engine)
        try:
            engine.check_orders()
        except Exception:
            pass

    for sig in arbitrage_exits(engine, settings):
        filled = execute_signal(engine, sig, dry_run, live=live, ctx=ctx, strategy=STRATEGY)
        if filled:
            counts.risk_exits += 1
            counts.fills += 1

    for sig in analyze_arbitrage(engine, settings, paper_mode=live is None and not dry_run):
        filled = execute_signal(engine, sig, dry_run, live=live, ctx=ctx, strategy=STRATEGY)
        counts.orders_placed += 1
        if filled:
            counts.fills += 1

    return counts


def run_loop(
    settings: Settings,
    engine: Engine,
    *,
    dry_run: bool = False,
    once: bool = False,
    live: LiveTrader | None = None,
    data_dir: Path | None = None,
) -> None:
    log.info(
        "Arbitrage loop starting mode=%s poll=%ss dry_run=%s",
        "live" if live else "paper",
        settings.poll_interval_seconds,
        dry_run,
    )
    while True:
        try:
            purge_stale_logs(engine.db.data_dir)
            counts = scan_once(settings, engine, dry_run=dry_run, live=live)
            combined = combine_engines([(STRATEGY, engine)])
            msg = format_scan_update(counts, combined)
            log.info("\n%s", msg)
            append_scan(
                data_dir or engine.db.data_dir,
                counts=counts,
                stats=combined,
            )
            append_activity(
                engine.db.data_dir,
                level="info",
                event="scan_complete",
                strategy=STRATEGY,
                message=msg.split("\n", 1)[0],
                orders=counts.orders_placed,
                fills=counts.fills,
                exits=counts.risk_exits,
            )
        except Exception as e:
            log.exception("scan failed: %s", e)
            append_activity(
                engine.db.data_dir,
                level="error",
                event="scan_failed",
                strategy=STRATEGY,
                message=str(e),
            )
        if once:
            return
        time.sleep(max(5, int(settings.poll_interval_seconds)))
