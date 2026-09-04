"""Persistent exit state for arbitrage pairs (ladder / lose-leg / rebalance)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pm_trader.models import Position


@dataclass
class ArbPairState:
    market_slug: str
    baseline_shares: dict[str, float] = field(default_factory=dict)
    ladder_levels_hit: dict[str, list[float]] = field(default_factory=dict)
    lose_leg_sold: bool = False
    last_mid: dict[str, float] = field(default_factory=dict)


class ArbExitStore:
    """Tracks active arb exits per condition_id."""

    def __init__(self, data_dir: Path | str) -> None:
        root = Path(data_dir)
        self._path = root / "arb_exit_state.json"
        self._states: dict[str, ArbPairState] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(raw, dict):
            return
        for key, row in raw.items():
            if not isinstance(row, dict):
                continue
            slug = row.get("market_slug")
            if not isinstance(slug, str):
                continue
            baseline = {
                str(k).lower(): float(v)
                for k, v in (row.get("baseline_shares") or {}).items()
                if isinstance(v, (int, float))
            }
            ladders_raw = row.get("ladder_levels_hit") or {}
            ladders: dict[str, list[float]] = {}
            if isinstance(ladders_raw, dict):
                for ok, levels in ladders_raw.items():
                    ladders[str(ok).lower()] = sorted(
                        {float(x) for x in (levels or []) if isinstance(x, (int, float))}
                    )
            last_mid = {
                str(k).lower(): float(v)
                for k, v in (row.get("last_mid") or {}).items()
                if isinstance(v, (int, float))
            }
            self._states[key] = ArbPairState(
                market_slug=slug,
                baseline_shares=baseline,
                ladder_levels_hit=ladders,
                lose_leg_sold=bool(row.get("lose_leg_sold")),
                last_mid=last_mid,
            )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: asdict(v) for k, v in self._states.items()}
        self._path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))

    def get(self, condition_id: str) -> ArbPairState | None:
        return self._states.get(condition_id)

    def ensure(self, condition_id: str, *, market_slug: str) -> ArbPairState:
        state = self._states.get(condition_id)
        if state is None:
            state = ArbPairState(market_slug=market_slug)
            self._states[condition_id] = state
        else:
            state.market_slug = market_slug
        return state

    def set_baseline(self, condition_id: str, outcome: str, shares: float, *, market_slug: str) -> None:
        state = self.ensure(condition_id, market_slug=market_slug)
        key = outcome.lower()
        if key not in state.baseline_shares or state.baseline_shares[key] <= 0:
            state.baseline_shares[key] = float(shares)
            self._save()

    def baseline(self, condition_id: str, outcome: str) -> float | None:
        state = self._states.get(condition_id)
        if not state:
            return None
        return state.baseline_shares.get(outcome.lower())

    def ladder_hit(self, condition_id: str, outcome: str, level: float) -> bool:
        state = self._states.get(condition_id)
        if not state:
            return False
        return float(level) in state.ladder_levels_hit.get(outcome.lower(), [])

    def mark_ladder(self, condition_id: str, outcome: str, level: float, *, market_slug: str) -> None:
        state = self.ensure(condition_id, market_slug=market_slug)
        key = outcome.lower()
        levels = state.ladder_levels_hit.setdefault(key, [])
        if float(level) not in levels:
            levels.append(float(level))
            levels.sort()
            self._save()

    def unmark_ladder(self, condition_id: str, outcome: str, level: float) -> None:
        state = self._states.get(condition_id)
        if not state:
            return
        key = outcome.lower()
        levels = state.ladder_levels_hit.get(key) or []
        state.ladder_levels_hit[key] = [x for x in levels if x != float(level)]
        self._save()

    def mark_lose_leg_sold(self, condition_id: str, *, market_slug: str) -> None:
        state = self.ensure(condition_id, market_slug=market_slug)
        state.lose_leg_sold = True
        self._save()

    def lose_leg_sold(self, condition_id: str) -> bool:
        state = self._states.get(condition_id)
        return bool(state and state.lose_leg_sold)

    def last_mid(self, condition_id: str, outcome: str) -> float | None:
        state = self._states.get(condition_id)
        if not state:
            return None
        return state.last_mid.get(outcome.lower())

    def set_last_mid(self, condition_id: str, outcome: str, mid: float, *, market_slug: str) -> None:
        state = self.ensure(condition_id, market_slug=market_slug)
        state.last_mid[outcome.lower()] = float(mid)
        self._save()

    def prune_closed(self, positions: list[Position]) -> None:
        open_ids = {
            p.market_condition_id
            for p in positions
            if p.shares > 0 and not p.is_resolved and p.market_condition_id
        }
        stale = [k for k in self._states if k not in open_ids]
        if not stale:
            return
        for key in stale:
            del self._states[key]
        self._save()
