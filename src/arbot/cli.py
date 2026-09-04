from __future__ import annotations

import logging
import os
from pathlib import Path

import click

from arbot.accounts import STRATEGY, data_dir_from_env, make_engine, reset_all_strategies
from arbot.config import ROOT, load_settings
from arbot.execution import get_shared_live_client
from arbot.live import LiveTrader, PyClobLiveClient
from arbot.loop import run_loop
from arbot.mode import ModeError, load_dotenv_file, resolve_mode
from arbot.report import account_stats, mark_positions

log = logging.getLogger("arbot")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _mode_options(fn):
    fn = click.option(
        "--mode",
        "cli_mode",
        type=click.Choice(["paper", "test", "live"], case_sensitive=False),
        default=None,
        help="paper (default) or live. Live also needs --confirm-live + private key.",
    )(fn)
    fn = click.option(
        "--confirm-live",
        is_flag=True,
        help="Required safety gate for live CLOB orders (or set ARBOT_LIVE=1).",
    )(fn)
    fn = click.option(
        "--data-dir",
        type=click.Path(path_type=Path),
        default=None,
        help="Ledger root (default ~/.pm-arb or ~/.pm-arb-live).",
    )(fn)
    return fn


def _start(
    *,
    dry_run: bool,
    once: bool,
    reset: bool,
    data_dir: Path | None,
    cli_mode: str | None,
    confirm_live: bool,
) -> None:
    load_dotenv_file(ROOT / ".env")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("py_clob_client_v2").setLevel(logging.WARNING)
    settings = load_settings()
    try:
        resolved = resolve_mode(
            settings_mode=settings.mode,
            cli_mode=cli_mode,
            confirm_live=confirm_live,
            data_dir=data_dir_from_env(data_dir),
            clob_host=settings.live.clob_host,
            chain_id=settings.live.chain_id,
            signature_type=settings.live.signature_type,
            funder=settings.live.funder,
        )
    except ModeError as e:
        raise click.ClickException(str(e)) from e

    balance = float(settings.arbitrage.starting_balance or settings.starting_balance)
    live = None
    if resolved.is_live:
        log.warning(
            "LIVE MODE — real CLOB orders. data=%s funder=%s",
            resolved.data_dir,
            resolved.funder or "(EOA)",
        )
        live = LiveTrader(get_shared_live_client(resolved))
    else:
        log.info("Paper mode. Ledger: %s", resolved.data_dir)

    engine = make_engine(STRATEGY, resolved.data_dir, balance, reset=reset)
    if live is not None:
        try:
            live.sync_cash(engine)
        except Exception as e:
            log.warning("live cash sync skipped: %s", e)

    run_loop(
        settings=settings,
        engine=engine,
        dry_run=dry_run,
        once=once,
        live=live,
        data_dir=resolved.data_dir,
    )


@click.group()
def main() -> None:
    """Polymarket arbitrage bot (paper by default; live is opt-in)."""
    load_dotenv_file(ROOT / ".env")
    _setup_logging()
    from arbot.api_patch import patch_polymarket_client

    patch_polymarket_client()


@main.command("run")
@click.option("--dry-run", is_flag=True, help="Log would-be trades without filling.")
@click.option("--once", is_flag=True, help="Run a single scan then exit.")
@click.option("--reset", is_flag=True, help="Wipe paper ledger and start from configured balance.")
@_mode_options
def run_cmd(
    dry_run: bool,
    once: bool,
    reset: bool,
    data_dir: Path | None,
    cli_mode: str | None,
    confirm_live: bool,
) -> None:
    _start(
        dry_run=dry_run,
        once=once,
        reset=reset,
        data_dir=data_dir,
        cli_mode=cli_mode,
        confirm_live=confirm_live,
    )


@main.command("scan")
@click.option("--dry-run/--no-dry-run", default=True)
@_mode_options
def scan_cmd(
    dry_run: bool,
    data_dir: Path | None,
    cli_mode: str | None,
    confirm_live: bool,
) -> None:
    """One discovery pass (dry-run by default)."""
    _start(
        dry_run=dry_run,
        once=True,
        reset=False,
        data_dir=data_dir,
        cli_mode=cli_mode,
        confirm_live=confirm_live,
    )


@main.command("status")
@click.option("--mode", "cli_mode", type=click.Choice(["paper", "test", "live"], case_sensitive=False), default=None)
@click.option("--data-dir", type=click.Path(path_type=Path), default=None)
def status_cmd(cli_mode: str | None, data_dir: Path | None) -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings()
    try:
        resolved = resolve_mode(
            settings_mode=settings.mode,
            cli_mode=cli_mode,
            confirm_live=False,
            data_dir=data_dir_from_env(data_dir),
            clob_host=settings.live.clob_host,
            chain_id=settings.live.chain_id,
            signature_type=settings.live.signature_type,
            funder=settings.live.funder,
            require_credentials=False,
        )
    except ModeError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"mode: {resolved.mode}  data: {resolved.data_dir}")
    balance = float(settings.arbitrage.starting_balance or settings.starting_balance)
    engine = make_engine(STRATEGY, resolved.data_dir, balance)
    try:
        if resolved.is_live and resolved.private_key:
            try:
                client = PyClobLiveClient(resolved)
                bal = client.get_balance()
                click.echo(f"wallet funder: {resolved.funder or '(EOA)'}")
                click.echo(f"CLOB balance: {'unavailable' if bal is None else f'${bal:.2f}'}")
                LiveTrader(client).sync_cash(engine)
            except Exception as e:
                click.echo(f"wallet: unavailable ({e})")
        stats = account_stats(engine)
        positions = engine.db.get_open_positions()
        click.echo(f"cash: ${stats['cash']:.2f}  starting: ${stats['starting_balance']:.2f}")
        click.echo(
            f"realized P&L: ${stats.get('realized_pnl', 0):.2f}  "
            f"unrealized: ${stats.get('unrealized_pnl', 0):.2f}"
        )
        click.echo(f"open legs: {len(positions)}")
        for p in positions:
            click.echo(
                f"  {p.market_slug} {p.outcome} shares={p.shares:.2f} "
                f"avg={p.avg_entry_price:.3f} cost=${p.total_cost:.2f}"
            )
    finally:
        engine.close()


@main.command("reset-balances")
@click.option("--balance", type=float, default=None)
@click.option("--data-dir", type=click.Path(path_type=Path), default=None)
@click.option("--mode", "cli_mode", type=click.Choice(["paper", "test", "live"], case_sensitive=False), default=None)
def reset_balances_cmd(balance: float | None, data_dir: Path | None, cli_mode: str | None) -> None:
    settings = load_settings()
    try:
        resolved = resolve_mode(
            settings_mode=settings.mode,
            cli_mode=cli_mode,
            confirm_live=False,
            data_dir=data_dir_from_env(data_dir),
            clob_host=settings.live.clob_host,
            chain_id=settings.live.chain_id,
            signature_type=settings.live.signature_type,
            funder=settings.live.funder,
            require_credentials=False,
        )
    except ModeError as e:
        raise click.ClickException(str(e)) from e
    amount = balance if balance is not None else float(
        settings.arbitrage.starting_balance or settings.starting_balance
    )
    if resolved.is_live:
        click.echo(click.style("Warning: ", fg="yellow") + "local ledger only — no on-chain moves.")
    for name, cash, starting in reset_all_strategies(resolved.data_dir, amount):
        click.echo(f"{name}: cash=${cash:.2f} starting=${starting:.2f}")


@main.command("dashboard")
@click.option("--host", default=None, help="Bind address (default 127.0.0.1, or HOST env).")
@click.option("--port", default=None, type=int, help="Port (default 8788, or PORT env).")
def dashboard_cmd(host: str | None, port: int | None) -> None:
    try:
        from arbot.dashboard.app import run_dashboard
    except ImportError as e:
        raise click.ClickException(
            "Dashboard needs Flask. Install with: pip install -e '.[dashboard]'"
        ) from e
    bind_host = host or os.environ.get("HOST") or "127.0.0.1"
    bind_port = port if port is not None else int(os.environ.get("PORT") or "8788")
    click.echo(f"Dashboard http://{bind_host}:{bind_port}")
    run_dashboard(host=bind_host, port=bind_port)


if __name__ == "__main__":
    main()
