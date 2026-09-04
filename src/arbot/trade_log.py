from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from arbot.signals import Signal

_MAX_LINES = 1000


def _root_data_dir(path: Path) -> Path:
    from arbot.paths import root_data_dir
    return root_data_dir(path)


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")
    _trim(path)


def _load_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _trim(path: Path) -> None:
    lines = path.read_text().splitlines()
    if len(lines) <= _MAX_LINES:
        return
    path.write_text("\n".join(lines[-_MAX_LINES:]) + "\n")


def append_skipped(
    data_dir: Path,
    *,
    strategy: str,
    signal: Signal,
    error: str,
    source: str = "execution",
) -> None:
    root = _root_data_dir(data_dir)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "strategy": strategy,
        "source": source,
        "action": signal.action,
        "slug": signal.slug,
        "outcome": signal.outcome,
        "amount_usd": signal.amount_usd,
        "shares": signal.shares,
        "reason": signal.reason,
        "error": error,
    }
    _append_jsonl(root / "skipped_trades.jsonl", row)


def load_skipped_trades(data_dir: Path, limit: int = 200) -> list[dict[str, Any]]:
    return list(reversed(_load_jsonl(_root_data_dir(data_dir) / "skipped_trades.jsonl", limit)))


def append_copy_event(
    data_dir: Path,
    *,
    tx_id: str,
    leader_ts: int,
    side: str,
    slug: str,
    title: str,
    status: str,
    trade_id: int | None = None,
    error: str | None = None,
    fill_latency_ms: float | None = None,
    detected_at: float | None = None,
) -> None:
    root = _root_data_dir(data_dir)
    detected = detected_at if detected_at is not None else time.time()
    latency_ms = max(0.0, (detected - leader_ts) * 1000) if leader_ts else None
    row: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tx_id": tx_id,
        "leader_ts": leader_ts,
        "latency_ms": latency_ms,
        "side": side,
        "slug": slug,
        "title": title,
        "status": status,
    }
    if trade_id is not None:
        row["trade_id"] = trade_id
    if error:
        row["error"] = error
    if fill_latency_ms is not None:
        row["fill_latency_ms"] = fill_latency_ms
    _append_jsonl(root / "copy" / "copy_events.jsonl", row)


def load_copy_events(data_dir: Path, limit: int = 500) -> list[dict[str, Any]]:
    return _load_jsonl(_root_data_dir(data_dir) / "copy" / "copy_events.jsonl", limit)


def copy_latency_stats(data_dir: Path) -> dict[str, Any]:
    events = [e for e in load_copy_events(data_dir, 500) if e.get("status") == "filled"]
    latencies = [float(e["latency_ms"]) for e in events if e.get("latency_ms") is not None]
    if not latencies:
        return {"count": 0, "avg_ms": None, "p50_ms": None, "p95_ms": None, "last_ms": None}
    latencies.sort()
    n = len(latencies)
    p50 = latencies[n // 2]
    p95 = latencies[int(n * 0.95)] if n > 1 else latencies[-1]
    return {
        "count": n,
        "avg_ms": sum(latencies) / n,
        "p50_ms": p50,
        "p95_ms": p95,
        "last_ms": latencies[-1],
    }


def copy_latency_by_trade_id(data_dir: Path) -> dict[int, float]:
    out: dict[int, float] = {}
    for e in load_copy_events(data_dir, 500):
        tid = e.get("trade_id")
        lat = e.get("latency_ms")
        if tid is not None and lat is not None:
            out[int(tid)] = float(lat)
    return out


def append_activity(
    data_dir: Path | str,
    *,
    level: str,
    event: str,
    message: str,
    strategy: str = "system",
    **extra: Any,
) -> None:
    root = _root_data_dir(Path(data_dir))
    row: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "event": event,
        "strategy": strategy,
        "message": message,
        "source": "activity",
    }
    for key, value in extra.items():
        if value is not None:
            row[key] = value
    _append_jsonl(root / "activity.jsonl", row)


def load_activity_log(data_dir: Path | str, limit: int = 300) -> list[dict[str, Any]]:
    return list(reversed(_load_jsonl(_root_data_dir(Path(data_dir)) / "activity.jsonl", limit)))


def build_activity_feed(data_dir: Path | str, limit: int = 250) -> list[dict[str, Any]]:
    """Merge activity, skipped trades, and copy events into one chronological feed."""
    rows: list[dict[str, Any]] = []
    for row in load_activity_log(data_dir, limit):
        rows.append({**row, "feed": "activity"})
    from arbot.decision_log import load_decisions

    for row in load_decisions(data_dir, limit):
        rows.append(
            {
                "ts": row.get("ts"),
                "level": row.get("level", "info"),
                "event": row.get("decision", "decision"),
                "strategy": row.get("strategy", "unknown"),
                "message": row.get("skip_summary") or row.get("reason") or "",
                "source": "decision",
                "city": row.get("city"),
                "event_date": row.get("event_date"),
                "bucket": row.get("bucket"),
                "slug": row.get("slug"),
                "action": row.get("action"),
                "decision": row.get("decision"),
                "feed": "decision",
                **{
                    k: v
                    for k, v in row.items()
                    if k
                    not in {
                        "ts",
                        "level",
                        "strategy",
                        "reason",
                        "source",
                        "event",
                        "city",
                        "event_date",
                        "bucket",
                        "slug",
                        "action",
                        "decision",
                    }
                },
            }
        )
    for row in load_skipped_trades(data_dir, limit):
        rows.append(
            {
                "ts": row.get("ts"),
                "level": "error",
                "event": "skipped_trade",
                "strategy": row.get("strategy", "unknown"),
                "message": row.get("error") or row.get("reason") or "skipped",
                "source": row.get("source", "execution"),
                "slug": row.get("slug"),
                "outcome": row.get("outcome"),
                "action": row.get("action"),
                "reason": row.get("reason"),
                "feed": "skipped",
            }
        )
    for row in reversed(load_copy_events(data_dir, limit)):
        status = str(row.get("status") or "")
        level = "info" if status == "filled" else "warn"
        msg = row.get("error") or f"{row.get('side')} {row.get('slug')} ({status})"
        rows.append(
            {
                "ts": row.get("ts"),
                "level": level,
                "event": "copy_trade",
                "strategy": "copy",
                "message": msg,
                "source": "copy",
                "slug": row.get("slug"),
                "side": row.get("side"),
                "status": status,
                "latency_ms": row.get("latency_ms"),
                "fill_latency_ms": row.get("fill_latency_ms"),
                "feed": "copy",
            }
        )
    rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
    return rows[:limit]
