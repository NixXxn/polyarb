from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from arbot.report import CombinedStats, ScanCounts

_MAX_LINES = 500


def append_scan(data_dir: Path, counts: ScanCounts, stats: CombinedStats) -> None:
    from arbot.paths import root_data_dir
    path = root_data_dir(data_dir) / "scan_history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "candidates": counts.candidates,
        "orders_placed": counts.orders_placed,
        "fills": counts.fills,
        "resolved": counts.resolved,
        "risk_exits": counts.risk_exits,
        "pending": counts.pending,
        "cash": stats.cash,
        "positions": stats.positions,
        "total": stats.total,
        "pnl": stats.pnl,
        "roi_pct": stats.roi_pct,
    }
    with path.open("a") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")
    _trim(path)


def load_scan_history(data_dir: Path, limit: int = 120) -> list[dict]:
    from arbot.paths import root_data_dir
    path = root_data_dir(data_dir) / "scan_history.jsonl"
    if not path.is_file():
        return []
    lines = path.read_text().splitlines()
    rows: list[dict] = []
    for line in lines[-limit:]:
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
