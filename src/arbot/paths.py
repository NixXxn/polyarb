from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DATA_DIR = Path.home() / ".pm-arb"
DEFAULT_LIVE_DATA_DIR = Path.home() / ".pm-arb-live"
STRATEGY = "arbitrage"


def data_dir_from_env(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    raw = os.environ.get("ARBOT_DATA_DIR") or os.environ.get("PAPERTRADER_DATA_DIR") or ""
    raw = raw.strip()
    if raw:
        return Path(raw)
    return DEFAULT_DATA_DIR


def root_data_dir(path: Path | str) -> Path:
    root = Path(path)
    if root.name == STRATEGY:
        return root.parent
    return root
