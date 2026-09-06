"""Persistent hedge/exit state for arbitrage pairs."""

from __future__ import annotations

import json
import time
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
    first_outcome: str | None = None
    second_outcome: str | None = None
    first_leg_at: float | None = None
    second_limit: float | None = None
    target_shares: float | None = None
    hedge_submitted: bool = False


class ArbExitStore:
    """Tracks sequential hedges and exit rungs per condition_id."""

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
                first_outcome=str(row["first_outcome"]).lower() if row.get("first_outcome") else None,
                second_outcome=str(row["second_outcome"]).lower() if row.get("second_outcome") else None,
                first_leg_at=float(row["first_leg_at"]) if row.get("first_leg_at") is not None else None,
                second_limit=float(row["second_limit"]) if row.get("second_limit") is not None else None,
                target_shares=float(row["target_shares"]) if row.get("target_shares") is not None else None,
                hedge_submitted=bool(row.get("hedge_submitted")),
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

    def mark_first_leg(
        self,
        condition_id: str,
        *,
        market_slug: str,
        first_outcome: str,
        second_outcome: str,
        second_limit: float,
        target_shares: float,
        at: float | None = None,
    ) -> None:
        state = self.ensure(condition_id, market_slug=market_slug)
        state.first_outcome = first_outcome.lower()
        state.second_outcome = second_outcome.lower()
        state.second_limit = float(second_limit)
        state.target_shares = float(target_shares)
        state.first_leg_at = float(at if at is not None else time.time())
        state.hedge_submitted = False
        self._save()

    def mark_hedge_submitted(self, condition_id: str, *, market_slug: str) -> None:
        state = self.ensure(condition_id, market_slug=market_slug)
        state.hedge_submitted = True
        self._save()

    def waiting_hedge(self, condition_id: str, *, now: float | None = None, delay: float = 120) -> bool:
        state = self._states.get(condition_id)
        if not state or state.first_leg_at is None:
            return False
        if state.hedge_submitted:
            return False
        return (now if now is not None else time.time()) - state.first_leg_at < delay

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

    def prune_closed(self, positions: list[Position], *, abort_seconds: float = 1800) -> None:
        open_ids = {
            p.market_condition_id or p.market_slug
            for p in positions
            if p.shares > 0 and not p.is_resolved
        }
        now = time.time()
        stale = []
        for key, state in self._states.items():
            if key in open_ids:
                continue
            if (
                state.first_leg_at is not None
                and (now - state.first_leg_at) < abort_seconds
            ):
                continue
            stale.append(key)
        if not stale:
            return
        for key in stale:
            del self._states[key]
        self._save()
