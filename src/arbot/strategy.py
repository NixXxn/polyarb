"""Arbitrage / spread-capture: buy both sides when combined cost < $1 (locked edge).

Two-legged strategy — buy YES+NO (or Up+Down) so one side always pays $1/share.
When ask_yes + ask_no + fees < 1, the locked edge is independent of the outcome.
Prefers fast crypto / weather markets and ranks LP-reward markets higher when present.

After entry, hybrid active exits take over: laddered take-profit on the leading leg,
lose-leg salvage when the hedge bid collapses, and momentum rebalance trims on mid moves.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from pm_trader.engine import Engine
from pm_trader.models import Position

from arbot.config import Settings
from arbot.decision_log import log_decision
from arbot.markets import best_ask, best_bid
from arbot.signals import QuantMeta, Signal
from arbot.sizing import account_cash, scaled_size

log = logging.getLogger("arbot")

# Prefer fast-moving crypto + weather; still allow other binary markets at lower rank.
_PREFERRED_MARKERS = (
    "bitcoin",
    "btc-",
    "btc ",
    "ethereum",
    "eth-",
    "solana",
    "sol-",
    "xrp",
    "doge",
    "crypto",
    "updown",
    "up-down",
    "highest-temperature",
    "lowest-temperature",
    "temperature-in-",
)

# Skip slow / noisy prop markets where two-leg arb is rarely fillable.
_SKIP_MARKERS = (
    "player-props",
    "more-markets",
    "-spread-",
    "-total-",
    "-handicap-",
    "-o-u-",
    "first-blood",
    "correct-score",
)


@dataclass(frozen=True)
class _ArbMarket:
    condition_id: str
    slug: str
    question: str
    outcome_a: str  # "Yes" / "Up"
    outcome_b: str  # "No" / "Down"
    liquidity: float
    volume_24h: float
    lp_reward_score: float
    preferred: bool
    hours_to_end: float | None = None


@dataclass(frozen=True)
class _ArbQuote:
    market: _ArbMarket
    ask_a: float
    ask_b: float
    size_a: float
    size_b: float
    pair_cost: float
    edge: float


def _parse_json_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _is_preferred(slug: str, question: str) -> bool:
    blob = f"{slug} {question}".lower()
    return any(m in blob for m in _PREFERRED_MARKERS)


def _should_skip(slug: str, question: str) -> bool:
    blob = f"{slug} {question}".lower()
    return any(m in blob for m in _SKIP_MARKERS)


def _hours_until_end(market: dict[str, Any]) -> float | None:
    raw = market.get("endDate") or market.get("endDateIso") or market.get("end_date")
    if raw is None or raw == "":
        return None
    try:
        if isinstance(raw, (int, float)):
            ts = float(raw)
            if ts > 1e12:
                ts /= 1000.0
            end = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            end = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None
    return (end - datetime.now(timezone.utc)).total_seconds() / 3600.0


def _lp_reward_score(market: dict[str, Any]) -> float:
    """Rank markets that advertise CLOB/LP rewards higher (maker incentives)."""
    score = 0.0
    rewards = market.get("clobRewards") or market.get("rewards") or []
    if isinstance(rewards, dict):
        rewards = [rewards]
    if not isinstance(rewards, list):
        return 0.0
    for row in rewards:
        if not isinstance(row, dict):
            continue
        for key in ("rewardsDailyRate", "rewardsAmount", "ratePerDay", "dailyRate"):
            try:
                score = max(score, float(row.get(key) or 0))
            except (TypeError, ValueError):
                continue
    try:
        score = max(score, float(market.get("competitive") or 0) * 0.01)
    except (TypeError, ValueError):
        pass
    return score


def _binary_outcomes(market: dict[str, Any]) -> tuple[str, str] | None:
    outcomes = [str(o) for o in _parse_json_list(market.get("outcomes"))]
    lowered = {o.lower(): o for o in outcomes}
    if "yes" in lowered and "no" in lowered:
        return lowered["yes"], lowered["no"]
    if "up" in lowered and "down" in lowered:
        return lowered["up"], lowered["down"]
    return None


def _log_arb(
    engine: Engine,
    *,
    decision: str,
    reason: str,
    **extra: Any,
) -> None:
    log_decision(
        engine.db.data_dir,
        strategy="arbitrage",
        decision=decision,
        reason=reason,
        **extra,
    )


def discover_arb_markets(
    engine: Engine,
    settings: Settings,
    *,
    limit: int | None = None,
) -> list[_ArbMarket]:
    """Fetch active binary markets; keep only those that resolve inside max_horizon_hours."""
    cfg = settings.arbitrage
    lim = int(limit if limit is not None else cfg.scan_limit)
    try:
        data = engine.api._gamma_get(
            "/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": lim,
                "order": "volume24hr",
                "ascending": "false",
            },
        )
    except Exception as e:
        log.warning("arbitrage: market fetch failed: %s", e)
        return []
    if not isinstance(data, list):
        return []

    out: list[_ArbMarket] = []
    for m in data:
        try:
            slug = str(m.get("slug") or "")
            if not slug:
                continue
            question = str(m.get("question") or "")
            if _should_skip(slug, question):
                continue
            pair = _binary_outcomes(m)
            if pair is None:
                continue
            liq = float(m.get("liquidity") or m.get("liquidityNum") or 0)
            vol = float(m.get("volume24hr") or 0)
            if liq < cfg.min_liquidity and vol < cfg.min_volume_24h:
                continue
            preferred = _is_preferred(slug, question)
            if cfg.prefer_crypto_weather and not preferred and vol < cfg.min_volume_24h * 3:
                # Keep some non-preferred depth for pure arb, but require more volume.
                if vol < cfg.min_volume_24h * 5:
                    continue
            hours = _hours_until_end(m)
            if cfg.max_horizon_hours > 0:
                if hours is None or hours < 0 or hours > cfg.max_horizon_hours:
                    continue
            out.append(
                _ArbMarket(
                    condition_id=str(m.get("conditionId") or ""),
                    slug=slug,
                    question=question,
                    outcome_a=pair[0],
                    outcome_b=pair[1],
                    liquidity=liq,
                    volume_24h=vol,
                    lp_reward_score=_lp_reward_score(m),
                    preferred=preferred,
                    hours_to_end=hours,
                )
            )
        except Exception:
            continue

    def _rank(row: _ArbMarket) -> tuple:
        # Soonest resolution first, then volume. Fast books recycle capital.
        horizon_key = -(row.hours_to_end if row.hours_to_end is not None else 1e9)
        reward = row.lp_reward_score if cfg.prefer_lp_rewards else 0.0
        return (
            horizon_key,
            1 if row.preferred else 0,
            reward,
            row.volume_24h,
            row.liquidity,
        )

    out.sort(key=_rank, reverse=True)
    return out


def _open_pairs(positions: list[Position]) -> dict[str, dict[str, Position]]:
    """Group open legs by condition_id → outcome → position."""
    grouped: dict[str, dict[str, Position]] = defaultdict(dict)
    for pos in positions:
        if pos.shares <= 0 or pos.is_resolved:
            continue
        key = pos.market_condition_id or pos.market_slug
        grouped[key][pos.outcome.lower()] = pos
    return grouped


def _quote_pair(
    engine: Engine,
    market: _ArbMarket,
    settings: Settings,
) -> _ArbQuote | None:
    cfg = settings.arbitrage
    try:
        full = engine.api.get_market(market.slug)
        token_a = full.get_token_id(market.outcome_a)
        token_b = full.get_token_id(market.outcome_b)
        book_a = engine.api.get_order_book(token_a)
        book_b = engine.api.get_order_book(token_b)
    except Exception as e:
        log.debug("arbitrage book %s: %s", market.slug, e)
        return None
    ask_a, size_a = best_ask(book_a)
    ask_b, size_b = best_ask(book_b)
    if ask_a is None or ask_b is None:
        return None
    if size_a < cfg.min_ask_size or size_b < cfg.min_ask_size:
        return None
    if not (cfg.min_ask <= ask_a <= cfg.max_ask and cfg.min_ask <= ask_b <= cfg.max_ask):
        return None
    pair_cost = ask_a + ask_b
    # Allow maker posts when asks are only slightly over $1 (spread capture).
    if pair_cost > cfg.max_maker_ask_sum + 1e-9:
        return None
    edge = 1.0 - min(pair_cost, cfg.max_pair_cost) - cfg.fee_buffer
    return _ArbQuote(
        market=market,
        ask_a=ask_a,
        ask_b=ask_b,
        size_a=size_a,
        size_b=size_b,
        pair_cost=pair_cost,
        edge=edge,
    )


def analyze_arbitrage(
    engine: Engine,
    settings: Settings,
    *,
    paper_mode: bool = False,
) -> list[Signal]:
    """Emit paired YES+NO (or Up+Down) buys when combined ask locks an edge under $1."""
    cfg = settings.arbitrage
    positions = engine.db.get_open_positions()
    pairs = _open_pairs(positions)
    open_pair_count = sum(1 for legs in pairs.values() if len(legs) >= 1)
    if open_pair_count >= cfg.max_open_pairs:
        _log_arb(
            engine,
            decision="skip",
            reason="max_open_pairs",
            open_pairs=open_pair_count,
            max_open_pairs=cfg.max_open_pairs,
        )
        return []

    bankroll = account_cash(engine, cfg.starting_balance or settings.starting_balance)
    remaining_slots = max(1, cfg.max_open_pairs - open_pair_count)
    pair_budget = scaled_size(
        cfg.position_usd,
        cash=bankroll,
        starting_balance=cfg.starting_balance or settings.starting_balance,
        remaining_slots=remaining_slots,
        min_usd=settings.min_position_usd * 2,
        max_usd=cfg.max_position_usd,
    )
    if pair_budget is None:
        _log_arb(engine, decision="skip", reason="insufficient_cash", cash=bankroll)
        return []

    markets = discover_arb_markets(engine, settings)
    _log_arb(
        engine,
        decision="scan",
        reason=(
            f"arbitrage scan: {len(markets)} binary candidates / "
            f"budget=${pair_budget:.2f} / open_pairs={open_pair_count}"
        ),
        candidates=len(markets),
        open_pairs=open_pair_count,
        pair_budget=pair_budget,
    )

    signals: list[Signal] = []
    rejects: dict[str, int] = defaultdict(int)
    for market in markets:
        if open_pair_count + (len(signals) // 2) >= cfg.max_open_pairs:
            break
        if market.condition_id and market.condition_id in pairs:
            rejects["already_in"] += 1
            continue
        if any(p.market_slug == market.slug for legs in pairs.values() for p in legs.values()):
            rejects["already_in"] += 1
            continue

        quote = _quote_pair(engine, market, settings)
        if quote is None:
            rejects["no_quote"] += 1
            continue

        taker_cap = cfg.max_pair_cost
        if paper_mode:
            # Paper: take any gross ask-sum under $1 (sim has no separate fee drag on both legs).
            taker_cap = max(cfg.max_pair_cost, 0.995)
        taker_ok = quote.pair_cost + (0.0 if paper_mode else cfg.fee_buffer) <= taker_cap + 1e-9
        taker_ok = taker_ok and (1.0 - quote.pair_cost) >= (cfg.min_edge * (0.5 if paper_mode else 1.0))
        use_fak = bool(paper_mode and cfg.paper_fak and taker_ok)

        if use_fak:
            limit_a = round(quote.ask_a, 4)
            limit_b = round(quote.ask_b, 4)
            order_type = "fak"
            pair_ref = quote.pair_cost
        else:
            # Maker spread-capture: post both legs so limits sum to max_pair_cost.
            # Split budget proportional to asks (cheaper leg gets more shares notionally).
            target_sum = cfg.max_pair_cost
            # Keep limits at/below ask so they can rest as bids into the book.
            raw_a = min(quote.ask_a, target_sum * (quote.ask_a / quote.pair_cost))
            raw_b = target_sum - raw_a
            if raw_b > quote.ask_b:
                raw_b = quote.ask_b
                raw_a = target_sum - raw_b
            tick = cfg.maker_tick
            limit_a = round(max(cfg.min_ask, min(raw_a, quote.ask_a) - (0 if taker_ok else 0)), 4)
            limit_b = round(max(cfg.min_ask, min(raw_b, quote.ask_b)), 4)
            # Shave a tick on both when asks are above target (true maker).
            if not taker_ok:
                limit_a = round(max(cfg.min_ask, min(quote.ask_a - tick, target_sum * 0.5)), 4)
                limit_b = round(max(cfg.min_ask, target_sum - limit_a), 4)
                if limit_b >= quote.ask_b:
                    limit_b = round(max(cfg.min_ask, quote.ask_b - tick), 4)
                    limit_a = round(max(cfg.min_ask, target_sum - limit_b), 4)
            if limit_a + limit_b > cfg.max_pair_cost + 1e-9:
                rejects["limit_sum_too_high"] += 1
                continue
            if (1.0 - (limit_a + limit_b)) < cfg.min_edge:
                rejects["edge_too_small"] += 1
                continue
            # Prefer maker on crypto/weather/LP; skip dull general books without taker edge.
            if not taker_ok and not (market.preferred or market.lp_reward_score > 0):
                rejects["not_preferred_maker"] += 1
                continue
            order_type = "limit"
            pair_ref = limit_a + limit_b

        # Paper maker: fill both legs at posted limits so locked edge is bookable.
        fill_at_limit = bool(paper_mode and cfg.paper_fak and order_type == "limit")

        # Equal shares so $1 payout covers both legs regardless of winner.
        target_shares = pair_budget / pair_ref
        max_by_book = min(quote.size_a, quote.size_b)
        target_shares = min(target_shares, max_by_book)
        amount_a = round(target_shares * limit_a, 2)
        amount_b = round(target_shares * limit_b, 2)
        if amount_a < settings.min_position_usd or amount_b < settings.min_position_usd:
            rejects["size_too_small"] += 1
            continue
        if amount_a + amount_b > bankroll:
            rejects["insufficient_cash"] += 1
            break

        locked_edge = 1.0 - (limit_a + limit_b)
        reason = (
            f"arb pair {market.outcome_a}/{market.outcome_b} "
            f"sum={limit_a + limit_b:.3f} edge={locked_edge:.3f} "
            f"${amount_a + amount_b:.2f} shares≈{target_shares:.1f} "
            f"({'LP+' if market.lp_reward_score > 0 else ''}"
            f"{'crypto/wx' if market.preferred else 'general'})"
        )
        _log_arb(
            engine,
            decision="buy",
            reason=reason,
            slug=market.slug,
            ask_a=quote.ask_a,
            ask_b=quote.ask_b,
            limit_a=limit_a,
            limit_b=limit_b,
            pair_cost=round(limit_a + limit_b, 4),
            edge=round(locked_edge, 4),
            stake_usd=round(amount_a + amount_b, 2),
            lp_reward_score=market.lp_reward_score,
            preferred=market.preferred,
        )

        quant = QuantMeta(
            p=locked_edge,
            sigma=0.0,
            f_star=locked_edge,
            kelly_fraction=0.0,
            source="arbitrage",
        )
        signals.append(
            Signal(
                action="buy",
                slug=market.slug,
                outcome=market.outcome_a.lower(),
                amount_usd=amount_a,
                order_type=order_type,
                limit_price=limit_a,
                paper_fill_at_limit=fill_at_limit,
                market_condition_id=market.condition_id or None,
                quant=quant,
                reason=reason + f" leg={market.outcome_a}",
            )
        )
        signals.append(
            Signal(
                action="buy",
                slug=market.slug,
                outcome=market.outcome_b.lower(),
                amount_usd=amount_b,
                order_type=order_type,
                limit_price=limit_b,
                paper_fill_at_limit=fill_at_limit,
                market_condition_id=market.condition_id or None,
                quant=quant,
                reason=reason + f" leg={market.outcome_b}",
            )
        )

    if not signals and rejects:
        _log_arb(
            engine,
            decision="skip",
            reason="no_arb_window",
            rejects=dict(rejects),
            markets_scanned=len(markets),
        )
    return signals


def arbitrage_exits(engine: Engine, settings: Settings) -> list[Signal]:
    """Hybrid active exits after arb entry: lose-leg salvage, ladder TP, momentum trim.

    Incomplete (orphan) pairs are still unwound. Complete pairs no longer hold both
    legs to resolution — capital turns over via laddered winner sells + lose-leg exits.
    """
    from arbot.arbitrage_state import ArbExitStore

    cfg = settings.arbitrage
    positions = engine.db.get_open_positions()
    pairs = _open_pairs(positions)
    store = ArbExitStore(engine.db.data_dir)
    store.prune_closed(positions)
    signals: list[Signal] = []
    min_sell_usd = float(settings.min_position_usd)

    def _leg_book(pos: Position) -> tuple[float | None, float | None]:
        try:
            market = engine.api.get_market(pos.market_slug)
            token = market.get_token_id(pos.outcome)
            book = engine.api.get_order_book(token)
        except Exception:
            return None, None
        bid, _ = best_bid(book)
        ask, _ = best_ask(book)
        return bid, ask

    def _mid(bid: float | None, ask: float | None) -> float | None:
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        return bid if bid is not None else ask

    def _sell(
        pos: Position,
        shares: float,
        *,
        reason: str,
        bid: float,
        partial: bool = False,
        ladder_level: float | None = None,
    ) -> Signal | None:
        sell_shares = min(float(pos.shares), float(shares))
        if sell_shares <= 0:
            return None
        if sell_shares * bid < min_sell_usd and sell_shares < pos.shares - 1e-9:
            # Skip dust partials; allow full exits even if small.
            return None
        _log_arb(
            engine,
            decision="sell",
            reason=reason,
            slug=pos.market_slug,
            outcome=pos.outcome,
            shares=round(sell_shares, 4),
            bid=bid,
            partial_exit=partial,
            ladder_level=ladder_level,
        )
        return Signal(
            action="sell",
            slug=pos.market_slug,
            outcome=pos.outcome,
            shares=sell_shares,
            order_type="fak",
            limit_price=bid,
            partial_exit=partial,
            ladder_multiple=ladder_level,
            market_condition_id=pos.market_condition_id,
            reason=reason,
        )

    for key, legs in pairs.items():
        # Orphan: incomplete fill — exit remaining directional risk unless lose-leg
        # already harvested intentionally (winner left for ladder TP).
        if len(legs) < 2:
            if key and store.lose_leg_sold(key):
                # Remaining winner: still apply ladder / rebalance below via single-leg path.
                pass
            else:
                for outcome, pos in legs.items():
                    bid, _ask = _leg_book(pos)
                    if bid is None or bid < 0.01:
                        continue
                    reason = (
                        f"arb orphan exit {outcome} bid={bid:.3f} "
                        f"(incomplete pair on {pos.market_slug})"
                    )
                    sig = _sell(pos, pos.shares, reason=reason, bid=bid)
                    if sig:
                        signals.append(sig)
                continue

        # Complete pair (or winner left after lose-leg): quote both sides.
        quoted: list[tuple[str, Position, float, float | None]] = []
        for outcome, pos in legs.items():
            bid, ask = _leg_book(pos)
            if bid is None:
                continue
            quoted.append((outcome, pos, bid, ask))
        if len(quoted) < 1:
            continue

        condition_id = key if key else (quoted[0][1].market_condition_id or quoted[0][1].market_slug)
        market_slug = quoted[0][1].market_slug

        for outcome, pos, bid, ask in quoted:
            store.set_baseline(condition_id, outcome, pos.shares, market_slug=market_slug)
            mid = _mid(bid, ask)
            if mid is not None and store.last_mid(condition_id, outcome) is None:
                store.set_last_mid(condition_id, outcome, mid, market_slug=market_slug)

        # Identify leader / laggard by bid.
        quoted_sorted = sorted(quoted, key=lambda row: row[2], reverse=True)
        win_outcome, win_pos, win_bid, win_ask = quoted_sorted[0]
        lose_row = quoted_sorted[-1] if len(quoted_sorted) >= 2 else None

        # 1) Losing-leg exit — salvage hedge when trend is clear.
        if (
            lose_row is not None
            and not store.lose_leg_sold(condition_id)
            and win_bid >= cfg.lose_leg_lead_bid
            and cfg.lose_leg_bid_min <= lose_row[2] <= cfg.lose_leg_bid_max
        ):
            lose_outcome, lose_pos, lose_bid, _lose_ask = lose_row
            reason = (
                f"arb lose-leg exit {lose_outcome} bid={lose_bid:.3f} "
                f"(lead {win_outcome}@{win_bid:.3f} >= {cfg.lose_leg_lead_bid:.2f})"
            )
            store.mark_lose_leg_sold(condition_id, market_slug=market_slug)
            sig = _sell(lose_pos, lose_pos.shares, reason=reason, bid=lose_bid)
            if sig:
                signals.append(sig)

        # Single remaining or winner leg for TP / rebalance.
        lead_outcome, lead_pos, lead_bid, lead_ask = win_outcome, win_pos, win_bid, win_ask
        if lead_pos.shares <= 0:
            continue

        # 2) Laddered take-profit on the leading leg (absolute price rungs).
        baseline = store.baseline(condition_id, lead_outcome) or lead_pos.shares
        tranche = baseline * cfg.exit_ladder_fraction
        laddered = False
        for level in cfg.exit_ladder_prices:
            if lead_bid + 1e-9 < level:
                break
            if store.ladder_hit(condition_id, lead_outcome, level):
                continue
            sell_shares = min(lead_pos.shares, tranche)
            if sell_shares <= 0:
                continue
            reason = (
                f"arb ladder TP {int(cfg.exit_ladder_fraction * 100)}% "
                f"@ {level:.2f} bid={lead_bid:.3f} ({lead_outcome})"
            )
            store.mark_ladder(condition_id, lead_outcome, level, market_slug=market_slug)
            sig = _sell(
                lead_pos,
                sell_shares,
                reason=reason,
                bid=lead_bid,
                partial=True,
                ladder_level=level,
            )
            if sig:
                signals.append(sig)
                # Reduce local view so subsequent rungs don't oversell same scan.
                lead_pos = SimpleNamespace(  # type: ignore[assignment]
                    shares=max(0.0, float(lead_pos.shares) - sell_shares),
                    market_slug=lead_pos.market_slug,
                    outcome=lead_pos.outcome,
                    market_condition_id=lead_pos.market_condition_id,
                    avg_entry_price=lead_pos.avg_entry_price,
                    total_cost=getattr(lead_pos, "total_cost", 0.0),
                    is_resolved=False,
                )
                laddered = True
                if lead_pos.shares <= 0:
                    break

        # 3) Momentum rebalance — trim leader on significant mid advances.
        if (
            cfg.rebalance_enabled
            and not laddered
            and lead_pos.shares > 0
            and lead_bid >= cfg.rebalance_min_lead
        ):
            mid = _mid(lead_bid, lead_ask)
            prev = store.last_mid(condition_id, lead_outcome)
            if mid is not None and prev is not None and (mid - prev) >= cfg.rebalance_move:
                sell_shares = lead_pos.shares * cfg.rebalance_fraction
                reason = (
                    f"arb rebalance trim {int(cfg.rebalance_fraction * 100)}% "
                    f"{lead_outcome} mid {prev:.3f}->{mid:.3f} "
                    f"(Δ>={cfg.rebalance_move:.2f})"
                )
                store.set_last_mid(condition_id, lead_outcome, mid, market_slug=market_slug)
                sig = _sell(
                    lead_pos,
                    sell_shares,
                    reason=reason,
                    bid=lead_bid,
                    partial=True,
                    ladder_level=round(mid, 4),
                )
                if sig:
                    signals.append(sig)
            elif mid is not None:
                store.set_last_mid(condition_id, lead_outcome, mid, market_slug=market_slug)

    return signals
