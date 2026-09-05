from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from arbot.paths import root_data_dir as _root_data_dir

RETENTION_DAYS = 3
_MAX_DECISION_LINES = 8000

# JSONL audit files purged by age (state files are excluded).
_PURGE_PATHS = (
    "activity.jsonl",
    "decisions.jsonl",
    "skipped_trades.jsonl",
    "shadow_ledger.jsonl",
    "scan_history.jsonl",
    "copy/copy_events.jsonl",
)


def _parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")
    lines = path.read_text().splitlines()
    if len(lines) > _MAX_DECISION_LINES:
        path.write_text("\n".join(lines[-_MAX_DECISION_LINES:]) + "\n")


def purge_stale_logs(data_dir: Path | str, *, retention_days: int = RETENTION_DAYS) -> dict[str, int]:
    """Drop log lines older than ``retention_days``. Returns removed line counts per file."""
    root = _root_data_dir(Path(data_dir))
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed: dict[str, int] = {}
    for rel in _PURGE_PATHS:
        path = root / rel
        if not path.is_file():
            continue
        kept: list[str] = []
        dropped = 0
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            ts = _parse_ts(row.get("ts"))
            if ts is None or ts >= cutoff:
                kept.append(line)
            else:
                dropped += 1
        if dropped:
            if kept:
                path.write_text("\n".join(kept) + "\n")
            else:
                path.unlink(missing_ok=True)
        removed[rel] = dropped
    return removed


def log_decision(
    data_dir: Path | str,
    *,
    strategy: str,
    decision: str,
    reason: str,
    city: str | None = None,
    event_date: date | str | None = None,
    slug: str | None = None,
    bucket: str | None = None,
    action: str | None = None,
    level: str = "info",
    **extra: Any,
) -> None:
    """Structured strategy decision (buy/skip/exit/dry_run/scan)."""
    root = _root_data_dir(Path(data_dir))
    if isinstance(event_date, date):
        event_date = event_date.isoformat()
    row: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "event": "decision",
        "strategy": strategy,
        "decision": decision,
        "reason": reason,
        "source": "decision",
    }
    if city:
        row["city"] = city
    if event_date:
        row["event_date"] = event_date
    if slug:
        row["slug"] = slug
    if bucket:
        row["bucket"] = bucket
    if action:
        row["action"] = action
    for key, value in extra.items():
        if value is not None:
            row[key] = value
    _append_jsonl(root / "decisions.jsonl", row)


def load_decisions(data_dir: Path | str, limit: int = 500) -> list[dict[str, Any]]:
    root = _root_data_dir(Path(data_dir))
    path = root / "decisions.jsonl"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(rows))


REJECT_LABELS: dict[str, str] = {
    "no_ask": "kein Ask im Orderbuch",
    "ask_size_too_small": "zu wenig Volumen am Ask",
    "ask_too_cheap": "Ask unter Mindestpreis (Markt quasi settled)",
    "ask_too_expensive": "Ask über Tail-Budget (>10¢)",
    "already_in_position": "Position bereits offen",
    "order_book_error": "Orderbuch nicht lesbar",
    "low_model_prob": "Modell-Wahrscheinlichkeit zu niedrig",
    "low_prob_ratio": "Preis/Leistung-Verhältnis zu niedrig",
    "low_edge": "Edge zu klein",
    "forecast_mismatch": "Forecast passt nicht zum Bucket",
    "ask_too_high": "Ask zu hoch",
    "ask_too_low": "Ask zu niedrig",
    "event_outside_horizon": "Event endet außerhalb des 6h-Fensters",
    "no_event_end": "Event ohne Enddatum",
    "event_closed": "Event geschlossen",
    "low_event_volume": "Event-Volumen zu niedrig",
    "prop_market": "Prop-Markt (kein Match)",
    "not_match_market": "kein Match-Markt",
    "market_outside_horizon": "Markt endet außerhalb des Fensters",
    "market_closed": "Markt geschlossen",
    "market_unavailable": "Markt nicht ladbar",
    "no_valid_ask": "kein gültiger Ask",
    "max_open_positions": "max. offene Positionen",
    "already_in_event": "bereits im Event investiert",
    "insufficient_cash": "zu wenig Cash",
    "no_oddspapi_match": "kein OddsPapi-Match",
    "oddspapi_unavailable": "OddsPapi nicht verfügbar",
    "low_oddspapi_edge": "OddsPapi-Edge zu klein",
    "oddspapi_kelly_too_small": "Kelly-Size zu klein",
    "no_quote": "no usable two-leg book",
    "already_in": "already in position",
    "limit_sum_too_high": "limit sum above pair cap",
    "edge_too_small": "edge too small",
    "not_preferred_maker": "maker skipped (not crypto/wx/LP)",
    "maker_too_fast": "maker skipped (resolves too soon)",
    "book_unbalanced": "book too one-sided for maker",
    "size_too_small": "size too small",
    "book_error": "order book fetch failed",
    "no_ask": "no ask on one or both legs",
    "ask_sum_too_high": "combined asks above maker cap",
}


def classify_ask_reject(
    ask: float | None,
    ask_size: float,
    *,
    min_ask: float,
    max_ask: float,
    min_size: float,
) -> str | None:
    if ask is None:
        return "no_ask"
    if ask_size < min_size:
        return "ask_size_too_small"
    if ask < min_ask:
        return "ask_too_cheap"
    if ask > max_ask:
        return "ask_too_expensive"
    return None


def format_skip_summary(rejects: dict[str, int]) -> str:
    parts = []
    for key, count in sorted(rejects.items(), key=lambda item: (-item[1], item[0])):
        label = REJECT_LABELS.get(key, key)
        parts.append(f"{count}× {label}")
    return "; ".join(parts)
