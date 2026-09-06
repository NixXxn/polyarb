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
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from pm_trader.engine import Engine
from pm_trader.models import Position

from arbot.config import Settings
from arbot.decision_log import format_skip_summary, log_decision
from arbot.markets import best_ask, best_bid
from arbot.signals import QuantMeta, Signal
from arbot.sizing import account_cash, scaled_size

log = logging.getLogger("arbot")

# Gamma /markets caps each response at 100 regardless of the requested limit.
_GAMMA_PAGE_SIZE = 100
# Cap CLOB book fetches per scan so a wide 24h window cannot blow the poll.
_MAX_MARKETS_TO_QUOTE = 100
# Fast Up/Down books usually sit just over $1 combined; still rest maker bids under.
_MAKER_QUOTE_MAX_ASK = 0.99
_MAKER_QUOTE_MAX_SUM = 1.02
_FAST_SLUG_MARKERS = ("-5m-", "-15m-", "-5min", "-15min")

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


def _is_fast_market(market: _ArbMarket) -> bool:
    slug = market.slug.lower()
    if any(m in slug for m in _FAST_SLUG_MARKERS):
        return True
    if market.hours_to_end is None:
        return True
    return False


def _maker_allowed(market: _ArbMarket, cfg: Any) -> bool:
    """Rest bids only on slower, preferred books — 5m tape is adverse-selection junk."""
    if not (market.preferred or market.lp_reward_score > 0):
        return False
    if _is_fast_market(market):
        return False
    min_hours = float(getattr(cfg, "maker_min_horizon_hours", 1.0) or 0.0)
    if market.hours_to_end is None or market.hours_to_end < min_hours:
        return False
    return True


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


def _gamma_market_pages(
    engine: Engine,
    params: dict[str, Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Page through Gamma /markets. The API ignores limit>100, so offset is required."""
    out: list[dict[str, Any]] = []
    page_size = min(_GAMMA_PAGE_SIZE, max(1, limit))
    offset = 0
    while len(out) < limit:
        chunk = min(page_size, limit - len(out))
        query = {**params, "limit": chunk, "offset": offset}
        try:
            data = engine.api._gamma_get("/markets", params=query)
        except Exception as e:
            log.warning("arbitrage: market fetch failed: %s", e)
            break
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if len(data) < chunk:
            break
        offset += len(data)
    return out


def _merge_gamma_markets(*batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for batch in batches:
        for row in batch:
            slug = str(row.get("slug") or "")
            if not slug or slug in seen:
                continue
            seen.add(slug)
            merged.append(row)
    return merged


def discover_arb_markets(
    engine: Engine,
    settings: Settings,
    *,
    limit: int | None = None,
) -> list[_ArbMarket]:
    """Fetch active binary markets; keep only those that resolve inside max_horizon_hours.

    Gamma's volume ranking is dominated by long-dated books, and each response is
    capped at 100 rows. Pull a soon-ending window (paginated) and merge with the
    volume list so 5m/15m crypto Up/Down markets are not dropped.
    """
    cfg = settings.arbitrage
    lim = int(limit if limit is not None else cfg.scan_limit)
    volume_rows = _gamma_market_pages(
        engine,
        {
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
        },
        limit=lim,
    )
    horizon_rows: list[dict[str, Any]] = []
    if cfg.max_horizon_hours > 0:
        now = datetime.now(timezone.utc)
        horizon_rows = _gamma_market_pages(
            engine,
            {
                "active": "true",
                "closed": "false",
                "order": "endDate",
                "ascending": "true",
                "end_date_min": now.isoformat(),
                "end_date_max": (now + timedelta(hours=cfg.max_horizon_hours)).isoformat(),
            },
            limit=lim,
        )
    data = _merge_gamma_markets(horizon_rows, volume_rows)

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
) -> tuple[_ArbQuote | None, str | None]:
    cfg = settings.arbitrage
    maker_book = _maker_allowed(market, cfg)
    if maker_book:
        quote_max_ask = _MAKER_QUOTE_MAX_ASK
        quote_max_sum = max(cfg.max_maker_ask_sum, _MAKER_QUOTE_MAX_SUM)
    else:
        # Taker-only: require a live lock under the pair cap. Do not rest under a $1.02 5m book.
        quote_max_ask = cfg.max_ask
        quote_max_sum = cfg.max_pair_cost
    try:
        full = engine.api.get_market(market.slug)
        token_a = full.get_token_id(market.outcome_a)
        token_b = full.get_token_id(market.outcome_b)
        book_a = engine.api.get_order_book(token_a)
        book_b = engine.api.get_order_book(token_b)
    except Exception as e:
        log.debug("arbitrage book %s: %s", market.slug, e)
        return None, "book_error"
    ask_a, size_a = best_ask(book_a)
    ask_b, size_b = best_ask(book_b)
    if ask_a is None or ask_b is None:
        return None, "no_ask"
    if size_a < cfg.min_ask_size or size_b < cfg.min_ask_size:
        return None, "ask_size_too_small"
    if ask_a < cfg.min_ask or ask_b < cfg.min_ask:
        return None, "ask_too_cheap"
    if ask_a > quote_max_ask or ask_b > quote_max_ask:
        return None, "ask_too_expensive"
    pair_cost = ask_a + ask_b
    if pair_cost > quote_max_sum + 1e-9:
        return None, "ask_sum_too_high"
    edge = 1.0 - min(pair_cost, cfg.max_pair_cost) - cfg.fee_buffer
    return (
        _ArbQuote(
            market=market,
            ask_a=ask_a,
            ask_b=ask_b,
            size_a=size_a,
            size_b=size_b,
            pair_cost=pair_cost,
            edge=edge,
        ),
        None,
    )


def _complete_pending_hedges(
    engine: Engine,
    settings: Settings,
    *,
    paper_mode: bool,
) -> list[Signal]:
    """After the first-leg wait, rest a limit on the other side to lock the pair."""
    from arbot.arbitrage_state import ArbExitStore

    cfg = settings.arbitrage
    delay = float(getattr(cfg, "hedge_delay_seconds", 120) or 120)
    store = ArbExitStore(engine.db.data_dir)
    positions = engine.db.get_open_positions()
    pairs = _open_pairs(positions)
    now = time.time()
    signals: list[Signal] = []
    bankroll = account_cash(engine, cfg.starting_balance or settings.starting_balance)

    for key, legs in pairs.items():
        if len(legs) >= 2:
            continue
        state = store.get(key)
        if state is None or state.first_leg_at is None or state.hedge_submitted:
            continue
        if now - state.first_leg_at < delay:
            continue
        first_outcome = (state.first_outcome or next(iter(legs))).lower()
        pos = legs.get(first_outcome) or next(iter(legs.values()))
        second_name = state.second_outcome
        if not second_name:
            continue
        try:
            full = engine.api.get_market(pos.market_slug)
            token = full.get_token_id(second_name)
            book = engine.api.get_order_book(token)
        except Exception:
            continue
        ask, size = best_ask(book)
        if ask is None or size < cfg.min_ask_size:
            continue
        first_px = float(pos.avg_entry_price or 0)
        planned = state.second_limit if state.second_limit is not None else (cfg.max_pair_cost - first_px)
        limit = round(max(cfg.min_ask, min(planned, cfg.max_pair_cost - first_px)), 4)
        if first_px + limit > cfg.max_pair_cost + 1e-9:
            limit = round(max(cfg.min_ask, cfg.max_pair_cost - first_px), 4)
        shares = float(state.target_shares or pos.shares)
        shares = min(shares, size, pos.shares)
        take = ask <= limit + 1e-9
        order_type = "fak" if (take or (paper_mode and cfg.paper_fak and ask <= limit + 0.02)) else "limit"
        use_price = round(ask if order_type == "fak" else limit, 4)
        if first_px + use_price > 1.0 + 1e-9:
            # Never complete a hedge that locks a guaranteed loss vs $1 payout.
            continue
        amount = round(shares * use_price, 2)
        if amount < settings.min_position_usd or amount > bankroll:
            continue
        reason = (
            f"hedge second leg {second_name} after {delay:.0f}s "
            f"limit={use_price:.3f} first={first_outcome}@{first_px:.3f} "
            f"sum={first_px + use_price:.3f}"
        )
        _log_arb(
            engine,
            decision="buy",
            reason=reason,
            slug=pos.market_slug,
            limit_a=first_px,
            limit_b=use_price,
            pair_cost=round(first_px + use_price, 4),
            stake_usd=amount,
        )
        store.mark_hedge_submitted(key, market_slug=pos.market_slug)
        signals.append(
            Signal(
                action="buy",
                slug=pos.market_slug,
                outcome=second_name,
                amount_usd=amount,
                order_type=order_type,
                limit_price=use_price,
                paper_fill_at_limit=bool(paper_mode and cfg.paper_fak and order_type == "fak"),
                market_condition_id=pos.market_condition_id or key,
                reason=reason,
            )
        )
    return signals


def analyze_arbitrage(
    engine: Engine,
    settings: Settings,
    *,
    paper_mode: bool = False,
) -> list[Signal]:
    """Buy the cheaper leg now; complete the hedge with a limit after hedge_delay_seconds."""
    cfg = settings.arbitrage
    hedge_signals = _complete_pending_hedges(engine, settings, paper_mode=paper_mode)
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
        return hedge_signals

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
        return hedge_signals

    markets = discover_arb_markets(engine, settings)
    to_quote = markets[:_MAX_MARKETS_TO_QUOTE]
    _log_arb(
        engine,
        decision="scan",
        reason=(
            f"arbitrage scan: {len(markets)} binary candidates / "
            f"quoting={len(to_quote)} / "
            f"budget=${pair_budget:.2f} / open_pairs={open_pair_count}"
        ),
        candidates=len(markets),
        quoting=len(to_quote),
        open_pairs=open_pair_count,
        pair_budget=pair_budget,
    )

    signals: list[Signal] = list(hedge_signals)
    rejects: dict[str, int] = defaultdict(int)
    for market in to_quote:
        if open_pair_count + max(0, len(signals) - len(hedge_signals)) >= cfg.max_open_pairs:
            break
        if market.condition_id and market.condition_id in pairs:
            rejects["already_in"] += 1
            continue
        if any(p.market_slug == market.slug for legs in pairs.values() for p in legs.values()):
            rejects["already_in"] += 1
            continue

        quote, quote_reject = _quote_pair(engine, market, settings)
        if quote is None:
            rejects[quote_reject or "no_quote"] += 1
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
            if not taker_ok and not _maker_allowed(market, cfg):
                rejects["maker_too_fast" if (market.preferred or market.lp_reward_score > 0) else "not_preferred_maker"] += 1
                continue
            bal_lo = float(cfg.maker_balanced_min)
            bal_hi = float(cfg.maker_balanced_max)
            if not taker_ok and not (
                bal_lo <= quote.ask_a <= bal_hi and bal_lo <= quote.ask_b <= bal_hi
            ):
                rejects["book_unbalanced"] += 1
                continue
            order_type = "limit"
            pair_ref = limit_a + limit_b

        # Paper FAK takes the live ask. Maker rests — do not instant-fill a 5¢ edge
        # that is not actually in the book (typical 5m combined ask is ~$1.00+).
        fill_at_limit = bool(paper_mode and cfg.paper_fak and order_type == "limit" and taker_ok)

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
        # First leg: take the cheaper ask now. Second leg waits hedge_delay_seconds.
        if quote.ask_a <= quote.ask_b:
            first_outcome, first_limit, first_amount = market.outcome_a, limit_a, amount_a
            second_outcome, second_limit = market.outcome_b, limit_b
        else:
            first_outcome, first_limit, first_amount = market.outcome_b, limit_b, amount_b
            second_outcome, second_limit = market.outcome_a, limit_a
        first_fill = bool(
            paper_mode and cfg.paper_fak and (order_type == "fak" or taker_ok)
        )
        first_type = "fak" if (use_fak or first_fill) else order_type
        from arbot.arbitrage_state import ArbExitStore

        pair_key = market.condition_id or market.slug
        ArbExitStore(engine.db.data_dir).mark_first_leg(
            pair_key,
            market_slug=market.slug,
            first_outcome=first_outcome,
            second_outcome=second_outcome,
            second_limit=second_limit,
            target_shares=target_shares,
        )
        signals.append(
            Signal(
                action="buy",
                slug=market.slug,
                outcome=first_outcome.lower(),
                amount_usd=first_amount,
                order_type=first_type,
                limit_price=first_limit,
                paper_fill_at_limit=first_fill,
                market_condition_id=market.condition_id or None,
                quant=quant,
                reason=reason + f" first-leg={first_outcome} (hedge {second_outcome} in {int(getattr(cfg, 'hedge_delay_seconds', 120))}s)",
            )
        )

    if not signals and rejects:
        summary = format_skip_summary(dict(rejects))
        _log_arb(
            engine,
            decision="skip",
            reason=f"no_arb_window: {summary}" if summary else "no_arb_window",
            skip_summary=summary or None,
            rejects=dict(rejects),
            markets_scanned=len(to_quote),
            candidates=len(markets),
        )
    return signals


def arbitrage_exits(engine: Engine, settings: Settings) -> list[Signal]:
    """Hold locked pairs to resolution. Only sell both when combined bids lock a gain.

    Overnight wipeouts came from selling the winner in ladder slices and dumping the
    loser, leaving a directional bag that resolved at ~0. Incomplete first-legs wait
    for the 2-minute hedge instead of being orphan-sold on the next scan.
    """
    from arbot.arbitrage_state import ArbExitStore

    cfg = settings.arbitrage
    delay = float(getattr(cfg, "hedge_delay_seconds", 120) or 120)
    abort = float(getattr(cfg, "hedge_abort_seconds", 1800) or 1800)
    pair_exit = float(getattr(cfg, "pair_exit_bid_sum", 0.99) or 0.99)
    hold_complete = bool(getattr(cfg, "hold_complete_pairs", True))
    positions = engine.db.get_open_positions()
    pairs = _open_pairs(positions)
    store = ArbExitStore(engine.db.data_dir)
    store.prune_closed(positions, abort_seconds=abort)
    signals: list[Signal] = []
    min_sell_usd = float(settings.min_position_usd)
    now = time.time()

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
        if len(legs) < 2:
            state = store.get(key)
            age = None
            if state and state.first_leg_at is not None:
                age = now - state.first_leg_at
            if age is None or age < abort:
                # Still inside hedge window (or unknown): do not dump the first leg.
                continue
            for _outcome, pos in legs.items():
                bid, _ask = _leg_book(pos)
                if bid is None or bid < 0.20:
                    continue
                reason = (
                    f"arb hedge abort {pos.outcome} bid={bid:.3f} "
                    f"after {age:.0f}s unhedged on {pos.market_slug}"
                )
                sig = _sell(pos, pos.shares, reason=reason, bid=bid)
                if sig:
                    signals.append(sig)
            continue

        quoted: list[tuple[str, Position, float]] = []
        pair_cost = 0.0
        for _outcome, pos in legs.items():
            bid, _ask = _leg_book(pos)
            if bid is None:
                continue
            quoted.append((_outcome, pos, bid))
            pair_cost += float(pos.avg_entry_price or 0) * float(pos.shares)
        if len(quoted) < 2:
            continue
        bid_sum = quoted[0][2] + quoted[1][2]
        shares = min(quoted[0][1].shares, quoted[1][1].shares)
        cost_per_share = 0.0
        if shares > 0:
            cost_per_share = (quoted[0][1].avg_entry_price or 0) + (quoted[1][1].avg_entry_price or 0)
        # Take both only when the book pays at least the lock (recycle capital).
        # Otherwise hold to resolution ($1/share).
        if hold_complete and bid_sum + 1e-9 < max(pair_exit, cost_per_share):
            continue
        if bid_sum + 1e-9 < cost_per_share:
            continue
        for outcome, pos, bid in quoted:
            reason = (
                f"arb pair exit {outcome} bid={bid:.3f} "
                f"sum={bid_sum:.3f} cost={cost_per_share:.3f}"
            )
            sig = _sell(pos, pos.shares, reason=reason, bid=bid)
            if sig:
                signals.append(sig)

    return signals
