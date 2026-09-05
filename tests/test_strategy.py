from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from arbot.config import load_settings
from arbot.strategy import _ArbMarket, analyze_arbitrage, arbitrage_exits
from helpers import FakeLevel


def test_analyze_arbitrage_emits_paired_legs(monkeypatch, tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.db.get_open_positions.return_value = []
    engine.get_account.return_value = SimpleNamespace(cash=1000.0)

    market = _ArbMarket(
        condition_id="0xarb",
        slug="will-btc-hit-100k",
        question="Will Bitcoin hit 100k?",
        outcome_a="Yes",
        outcome_b="No",
        liquidity=5000,
        volume_24h=20000,
        lp_reward_score=2.5,
        preferred=True,
    )

    import arbot.strategy as arb_mod

    monkeypatch.setattr(arb_mod, "discover_arb_markets", lambda *_a, **_k: [market])
    monkeypatch.setattr(
        arb_mod,
        "_quote_pair",
        lambda *_a, **_k: SimpleNamespace(
            market=market,
            ask_a=0.42,
            ask_b=0.48,
            size_a=100.0,
            size_b=100.0,
            pair_cost=0.90,
            edge=0.09,
        ),
    )

    sigs = analyze_arbitrage(engine, settings, paper_mode=True)
    assert len(sigs) == 2
    assert {s.outcome for s in sigs} == {"yes", "no"}
    assert all(s.order_type == "fak" for s in sigs)


def test_analyze_arbitrage_skip_reason_includes_reject_counts(monkeypatch, tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    engine.db.get_open_positions.return_value = []
    engine.get_account.return_value = SimpleNamespace(cash=1000.0)

    market = _ArbMarket(
        condition_id="0xdead",
        slug="will-eth-hit-10k",
        question="Will Ethereum hit 10k?",
        outcome_a="Yes",
        outcome_b="No",
        liquidity=5000,
        volume_24h=20000,
        lp_reward_score=0.0,
        preferred=False,
    )

    import arbot.strategy as arb_mod

    monkeypatch.setattr(arb_mod, "discover_arb_markets", lambda *_a, **_k: [market, market])
    monkeypatch.setattr(arb_mod, "_quote_pair", lambda *_a, **_k: None)

    assert analyze_arbitrage(engine, settings, paper_mode=True) == []
    from arbot.decision_log import load_decisions

    skips = [row for row in load_decisions(tmp_path) if row.get("decision") == "skip"]
    assert skips
    assert skips[0]["reason"].startswith("no_arb_window:")
    assert "2× no usable two-leg book" in skips[0]["reason"]
    assert skips[0]["skip_summary"] == "2× no usable two-leg book"


def test_arbitrage_exits_lose_leg(monkeypatch, tmp_path):
    settings = load_settings()
    engine = MagicMock()
    engine.db.data_dir = tmp_path
    win = SimpleNamespace(
        shares=40.0,
        is_resolved=False,
        market_condition_id="0xpair",
        market_slug="btc-updown",
        outcome="up",
        avg_entry_price=0.45,
        total_cost=18.0,
    )
    lose = SimpleNamespace(
        shares=40.0,
        is_resolved=False,
        market_condition_id="0xpair",
        market_slug="btc-updown",
        outcome="down",
        avg_entry_price=0.50,
        total_cost=20.0,
    )
    engine.db.get_open_positions.return_value = [win, lose]
    full = MagicMock()
    full.get_token_id.side_effect = lambda o: f"tok-{o.lower()}"
    engine.api.get_market.return_value = full

    def book_for(token):
        if "up" in token:
            return SimpleNamespace(asks=[FakeLevel(0.72, 20)], bids=[FakeLevel(0.71, 20)])
        return SimpleNamespace(asks=[FakeLevel(0.30, 20)], bids=[FakeLevel(0.28, 20)])

    engine.api.get_order_book.side_effect = book_for
    sigs = arbitrage_exits(engine, settings)
    assert any(s.outcome == "down" and "lose-leg" in s.reason for s in sigs)
    assert any(s.outcome == "up" and s.partial_exit for s in sigs)
