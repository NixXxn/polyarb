from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from arbot.accounts import DEFAULT_DATA_DIR, DEFAULT_LIVE_DATA_DIR


class ModeError(ValueError):
    """Invalid or unsafe trading-mode configuration."""


PAPER = "paper"
LIVE = "live"
_PAPER_ALIASES = {PAPER, "test", "sim", "simulation"}
_LIVE_ALIASES = {LIVE, "prod", "production"}


def normalize_mode(value: str | None) -> str:
    raw = (value or PAPER).strip().lower()
    if raw in _PAPER_ALIASES:
        return PAPER
    if raw in _LIVE_ALIASES:
        return LIVE
    raise ModeError(f"Unknown mode {value!r}. Use paper or live.")


def env_flag(name: str, env: dict[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    return str(source.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def env_value(name: str, env: dict[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    return str(source.get(name, "")).strip()


def load_dotenv_file(path: Path, env: dict[str, str] | None = None) -> None:
    target = env if env is not None else os.environ
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in target:
            target[key] = value


@dataclass(frozen=True)
class ResolvedMode:
    mode: str
    data_dir: Path
    private_key: str = ""
    funder: str = ""
    signature_type: int = 1
    clob_host: str = "https://clob.polymarket.com"
    chain_id: int = 137

    @property
    def is_live(self) -> bool:
        return self.mode == LIVE


def resolve_mode(
    *,
    settings_mode: str,
    cli_mode: str | None,
    confirm_live: bool,
    data_dir: Path,
    clob_host: str,
    chain_id: int,
    signature_type: int,
    funder: str = "",
    env: dict[str, str] | None = None,
    require_credentials: bool = True,
) -> ResolvedMode:
    source = env if env is not None else os.environ
    mode = normalize_mode(
        cli_mode
        or env_value("ARBOT_MODE", source)
        or env_value("PAPERTRADER_MODE", source)
        or settings_mode
    )
    if mode == PAPER:
        return ResolvedMode(
            mode=PAPER,
            data_dir=data_dir,
            clob_host=clob_host,
            chain_id=chain_id,
            signature_type=signature_type,
            funder=funder,
        )

    confirmed = (
        confirm_live
        or env_flag("ARBOT_LIVE", source)
        or env_flag("PAPERTRADER_LIVE", source)
    )
    if require_credentials and not confirmed:
        raise ModeError(
            "Live mode requires --confirm-live or ARBOT_LIVE=1 / PAPERTRADER_LIVE=1."
        )
    key = (
        env_value("ARBOT_PRIVATE_KEY", source)
        or env_value("PAPERTRADER_PRIVATE_KEY", source)
        or env_value("PK", source)
    )
    if require_credentials and not key:
        raise ModeError("Live mode requires ARBOT_PRIVATE_KEY (or PAPERTRADER_PRIVATE_KEY).")
    live_dir = data_dir
    if data_dir == DEFAULT_DATA_DIR:
        live_dir = DEFAULT_LIVE_DATA_DIR
    live_funder = (
        env_value("ARBOT_FUNDER", source)
        or env_value("PAPERTRADER_FUNDER", source)
        or funder
    )
    sig = env_value("ARBOT_SIGNATURE_TYPE", source) or env_value(
        "PAPERTRADER_SIGNATURE_TYPE", source
    )
    live_sig = int(sig) if sig else signature_type
    return ResolvedMode(
        mode=LIVE,
        data_dir=live_dir,
        private_key=key,
        funder=live_funder,
        signature_type=live_sig,
        clob_host=clob_host,
        chain_id=chain_id,
    )
