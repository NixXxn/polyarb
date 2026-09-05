#!/usr/bin/env python3
"""Taker two-leg arb backtest on Downloads/historic_data parquet prints.

Uses 15s buckets: conservative buy = max print on each leg in the same bucket.
This is last-trade, not a CLOB book — maker resting fills cannot be tested.
"""
from __future__ import annotations

import ast
import heapq
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

DATA = Path("/Users/nixxxon/Downloads/historic_data")
OUT = Path(__file__).resolve().parents[1] / "backtest_out"
STARTING_CASH = 1000.0
SYNC_SEC = 15
# 2026-01-01 UTC through end of file
T_START = 1767225600
PAIR_KEEP = 1.05

PREFERRED_MARKERS = (
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
SKIP_MARKERS = (
    "player-props",
    "more-markets",
    "-spread-",
    "-total-",
    "-handicap-",
    "-o-u-",
    "first-blood",
    "correct-score",
)


def _is_preferred(blob: str) -> bool:
    return any(m in blob for m in PREFERRED_MARKERS)


def _should_skip(blob: str) -> bool:
    return any(m in blob for m in SKIP_MARKERS)


def _parse_prices(raw: object) -> tuple[float, float] | None:
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        try:
            return float(raw[0]), float(raw[1])
        except (TypeError, ValueError):
            return None
    if isinstance(raw, str):
        try:
            parsed = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return None
        return _parse_prices(parsed)
    return None


def load_universe() -> pd.DataFrame:
    m = pq.read_table(DATA / "markets.parquet").to_pandas()
    a1 = m["answer1"].str.lower()
    a2 = m["answer2"].str.lower()
    m = m[a1.isin(["yes", "up"]) & a2.isin(["no", "down"])].copy()
    blob = (
        m["slug"].fillna("")
        + " "
        + m["question"].fillna("")
        + " "
        + m["event_slug"].fillna("")
    ).str.lower()
    m["blob"] = blob
    m = m[~blob.map(_should_skip)].copy()
    m["preferred"] = blob.map(_is_preferred)
    # lifetime volume is a stand-in for the bot's 24h volume gate
    m = m[(m["volume"] >= 1000) | (m["preferred"] & (m["volume"] >= 150))].copy()
    settle = m["outcome_prices"].map(_parse_prices)
    m["settle1"] = settle.map(lambda x: x[0] if x else np.nan)
    m["settle2"] = settle.map(lambda x: x[1] if x else np.nan)
    m["settle_sum"] = m["settle1"] + m["settle2"]
    m["end_ts"] = pd.to_datetime(m["end_date"], utc=True).map(
        lambda x: int(x.timestamp()) if pd.notna(x) else 0
    )
    m["cid"] = m["condition_id"].astype(str)
    m = m.drop_duplicates("cid")
    m["idx"] = np.arange(len(m), dtype=np.int32)
    return m.reset_index(drop=True)


def extract_snapshots(uni: pd.DataFrame) -> pd.DataFrame:
    cid_to_i = dict(zip(uni["cid"].to_numpy(), uni["idx"].to_numpy()))
    id_set = pa.array(uni["cid"].to_numpy())
    tf = pq.ParquetFile(DATA / "trades.parquet")
    ts_idx = tf.schema_arrow.get_field_index("timestamp")
    rgs = []
    for i in range(tf.num_row_groups):
        st = tf.metadata.row_group(i).column(ts_idx).statistics
        if st is None or not st.has_min_max:
            continue
        if int(st.max) < T_START:
            continue
        rgs.append(i)

    cols = ["timestamp", "condition_id", "price", "usd_amount", "nonusdc_side"]
    pair_chunks: list[pd.DataFrame] = []
    t0 = time.perf_counter()
    kept_rows = 0
    n_snaps = 0
    for n, i in enumerate(rgs, 1):
        table = tf.read_row_group(i, columns=cols)
        table = table.filter(pc.is_in(table["condition_id"], value_set=id_set))
        if table.num_rows == 0:
            continue
        df = table.to_pandas()
        df = df[(df["price"] > 0.0) & (df["price"] < 1.0) & (df["usd_amount"] > 0)]
        df = df[df["timestamp"] >= T_START]
        if df.empty:
            continue
        df["cid_i"] = df["condition_id"].map(cid_to_i)
        df = df.dropna(subset=["cid_i"])
        df["cid_i"] = df["cid_i"].astype(np.int32)
        df["leg"] = (df["nonusdc_side"].to_numpy() == "token2").astype(np.int8)
        df["bkt"] = (df["timestamp"].to_numpy() // SYNC_SEC).astype(np.int64)
        g = (
            df.groupby(["cid_i", "bkt", "leg"], sort=False)
            .agg(ask=("price", "max"), bid=("price", "min"), usd=("usd_amount", "sum"), ts=("timestamp", "max"))
            .reset_index()
        )
        pairs = _pivot_pairs(g)
        kept_rows += len(df)
        if not pairs.empty:
            pair_chunks.append(pairs)
            n_snaps += len(pairs)
        if n % 20 == 0 or n == len(rgs):
            dt = time.perf_counter() - t0
            print(
                f"  rg {n}/{len(rgs)} elapsed {dt:.0f}s prints {kept_rows:,} "
                f"pair_rows {n_snaps:,} chunks {len(pair_chunks)}",
                flush=True,
            )
        if len(pair_chunks) >= 40:
            pair_chunks = [_merge_pair_chunks(pair_chunks)]

    if not pair_chunks:
        return pd.DataFrame()
    snaps = _merge_pair_chunks(pair_chunks).sort_values("ts").reset_index(drop=True)
    print(f"both-leg snapshots {len(snaps):,}", flush=True)
    return snaps


def _merge_pair_chunks(chunks: list[pd.DataFrame]) -> pd.DataFrame:
    d = pd.concat(chunks, ignore_index=True)
    g = (
        d.groupby(["cid_i", "bkt"], sort=False)
        .agg(
            ask0=("ask0", "max"),
            ask1=("ask1", "max"),
            bid0=("bid0", "min"),
            bid1=("bid1", "min"),
            usd0=("usd0", "sum"),
            usd1=("usd1", "sum"),
            ts=("ts", "max"),
        )
        .reset_index()
    )
    g["pair_ask"] = g["ask0"] + g["ask1"]
    return g[g["pair_ask"] <= PAIR_KEEP].copy()


def _pivot_pairs(legs: pd.DataFrame) -> pd.DataFrame:
    a = legs.loc[legs["leg"] == 0, ["cid_i", "bkt", "ask", "bid", "usd", "ts"]]
    b = legs.loc[legs["leg"] == 1, ["cid_i", "bkt", "ask", "bid", "usd", "ts"]]
    p = a.merge(b, on=["cid_i", "bkt"], suffixes=("0", "1"))
    p["pair_ask"] = p["ask0"] + p["ask1"]
    p = p[p["pair_ask"] <= PAIR_KEEP].copy()
    p["ts"] = np.fmax(p["ts0"].to_numpy(), p["ts1"].to_numpy()).astype(np.int64)
    return p


@dataclass(frozen=True)
class Params:
    name: str
    max_pair_cost: float
    min_edge: float
    fee_buffer: float
    min_ask: float
    max_ask: float
    min_usd: float
    position_usd: float
    max_open_pairs: int
    prefer_only: bool
    max_horizon_hours: int


def grid() -> list[Params]:
    """Coarse grid, then a small size/slots refinement around the leader."""
    rows: list[Params] = [
        Params("current", 0.97, 0.025, 0.01, 0.02, 0.95, 3.0, 40.0, 8, True, 0)
    ]
    n = 0
    for max_pair, fee, min_ask, min_usd, pref, horizon in product(
        (0.95, 0.97, 0.99),
        (0.0, 0.01),
        (0.02, 0.05),
        (5.0, 20.0),
        (True, False),
        (0, 24, 168),
    ):
        n += 1
        rows.append(
            Params(
                name=f"g{n}",
                max_pair_cost=max_pair,
                min_edge=round(1.0 - max_pair, 4),
                fee_buffer=fee,
                min_ask=min_ask,
                max_ask=0.95,
                min_usd=min_usd,
                position_usd=40.0,
                max_open_pairs=8,
                prefer_only=pref,
                max_horizon_hours=horizon,
            )
        )
    return rows


def refine_grid(best: Params) -> list[Params]:
    rows = []
    n = 0
    for pos, slots in product((20.0, 40.0, 80.0), (4, 8, 16)):
        n += 1
        rows.append(
            Params(
                name=f"r{n}",
                max_pair_cost=best.max_pair_cost,
                min_edge=best.min_edge,
                fee_buffer=best.fee_buffer,
                min_ask=best.min_ask,
                max_ask=best.max_ask,
                min_usd=best.min_usd,
                position_usd=pos,
                max_open_pairs=slots,
                prefer_only=best.prefer_only,
                max_horizon_hours=best.max_horizon_hours,
            )
        )
    return rows


def replay(snaps: pd.DataFrame, uni: pd.DataFrame, p: Params) -> dict:
    preferred = uni["preferred"].to_numpy()
    settle_sum = uni["settle_sum"].to_numpy()
    end_ts_a = uni["end_ts"].to_numpy()

    cid = snaps["cid_i"].to_numpy(np.int32)
    ts = snaps["ts"].to_numpy(np.int64)
    ask0 = snaps["ask0"].to_numpy(np.float64)
    ask1 = snaps["ask1"].to_numpy(np.float64)
    pair = snaps["pair_ask"].to_numpy(np.float64)
    usd0 = snaps["usd0"].to_numpy(np.float64)
    usd1 = snaps["usd1"].to_numpy(np.float64)

    cash = STARTING_CASH
    realized = 0.0
    open_pos: dict[int, tuple[float, float]] = {}
    heap: list[tuple[int, int, float, float]] = []
    trades = 0
    skipped_open = 0
    wins = 0
    losses = 0
    peak = STARTING_CASH
    max_dd = 0.0
    pnl_list: list[float] = []
    horizon = int(p.max_horizon_hours) * 3600
    cap = p.max_pair_cost - p.fee_buffer
    min_edge = p.min_edge
    min_ask = p.min_ask
    max_ask = p.max_ask
    min_usd = p.min_usd
    prefer_only = p.prefer_only
    budget = p.position_usd
    max_open = p.max_open_pairs

    def _settle_one(c: int, sh: float, cost: float) -> None:
        nonlocal cash, realized, wins, losses
        ss = settle_sum[c]
        payoff = sh * (ss if ss == ss else 1.0)
        pnl = payoff - cost
        cash += payoff
        realized += pnl
        pnl_list.append(pnl)
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        open_pos.pop(c, None)

    def _settle_due(now: int) -> None:
        while heap and heap[0][0] <= now:
            _et, c, sh, cost = heapq.heappop(heap)
            if c not in open_pos:
                continue
            _settle_one(c, sh, cost)

    n = len(snaps)
    for i in range(n):
        now = int(ts[i])
        if heap and heap[0][0] <= now:
            _settle_due(now)
        pc = pair[i]
        if pc > cap:
            continue
        if (1.0 - pc) < min_edge:
            continue
        a0 = ask0[i]
        a1 = ask1[i]
        if a0 < min_ask or a1 < min_ask or a0 > max_ask or a1 > max_ask:
            continue
        if usd0[i] < min_usd or usd1[i] < min_usd:
            continue
        c = int(cid[i])
        if prefer_only and not preferred[c]:
            continue
        et = int(end_ts_a[c])
        if et <= now:
            continue
        if horizon and (et - now) > horizon:
            continue
        if c in open_pos:
            skipped_open += 1
            continue
        if len(open_pos) >= max_open or cash < budget:
            continue
        shares = budget / pc
        cost = budget
        cash -= cost
        open_pos[c] = (shares, cost)
        heapq.heappush(heap, (et, c, shares, cost))
        trades += 1
        locked = cash + len(open_pos) * budget
        if locked > peak:
            peak = locked
        dd = (peak - locked) / peak if peak else 0.0
        if dd > max_dd:
            max_dd = dd

    _settle_due(2_000_000_000)
    for c, (sh, cost) in list(open_pos.items()):
        _settle_one(c, sh, cost)

    arr = np.asarray(pnl_list, dtype=np.float64) if pnl_list else np.zeros(1)
    return {
        **asdict(p),
        "trades": trades,
        "skipped_already_open": skipped_open,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / (wins + losses)) if (wins + losses) else 0.0,
        "realized_pnl": round(float(realized), 2),
        "ending_cash": round(float(cash), 2),
        "roi_pct": round(100.0 * float(realized) / STARTING_CASH, 2),
        "max_drawdown_pct": round(100.0 * float(max_dd), 2),
        "avg_trade_pnl": round(float(arr.mean()), 4),
        "median_trade_pnl": round(float(np.median(arr)), 4),
        "p5_trade_pnl": round(float(np.quantile(arr, 0.05)), 4),
    }


def opportunity_stats(snaps: pd.DataFrame, uni: pd.DataFrame) -> dict:
    pref = uni["preferred"].to_numpy()
    s = snaps.copy()
    s["preferred"] = pref[s["cid_i"].to_numpy()]
    def bucket(max_pair: float, prefer_only: bool) -> dict:
        d = s[s["pair_ask"] <= max_pair]
        if prefer_only:
            d = d[d["preferred"]]
        if d.empty:
            return {"n": 0, "markets": 0, "median_pair": None, "p10_pair": None}
        return {
            "n": int(len(d)),
            "markets": int(d["cid_i"].nunique()),
            "median_pair": round(float(d["pair_ask"].median()), 4),
            "p10_pair": round(float(d["pair_ask"].quantile(0.10)), 4),
            "mean_usd": round(float(np.minimum(d["usd0"], d["usd1"]).mean()), 2),
        }
    return {
        "snapshots": int(len(s)),
        "markets_in_snaps": int(s["cid_i"].nunique()),
        "ts_min": int(s["ts"].min()) if len(s) else None,
        "ts_max": int(s["ts"].max()) if len(s) else None,
        "pair_le_0.95": bucket(0.95, False),
        "pair_le_0.97": bucket(0.97, False),
        "pair_le_0.99": bucket(0.99, False),
        "pref_pair_le_0.97": bucket(0.97, True),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    print("loading universe...", flush=True)
    uni = load_universe()
    print(
        f"universe {len(uni):,} preferred {int(uni['preferred'].sum()):,} "
        f"closed {int((uni['closed']==1).sum()):,}",
        flush=True,
    )
    snap_path = OUT / "snapshots_2026.parquet"
    if snap_path.exists():
        print(f"loading cached {snap_path}", flush=True)
        snaps = pd.read_parquet(snap_path)
    else:
        print("extracting 15s pair snapshots from 2026 trades...", flush=True)
        snaps = extract_snapshots(uni)
        snaps.to_parquet(snap_path, index=False)
        print(f"wrote {snap_path} rows={len(snaps):,}", flush=True)

    opp = opportunity_stats(snaps, uni)
    print("opportunities", json.dumps(opp, indent=2), flush=True)

    core = snaps[
        (snaps["pair_ask"] <= 0.99)
        & (snaps["ask0"] >= 0.02)
        & (snaps["ask1"] >= 0.02)
        & (snaps["ask0"] <= 0.95)
        & (snaps["ask1"] <= 0.95)
    ].sort_values("ts")
    print(f"replay snapshots {len(core):,} (pair<=0.99, asks in 0.02-0.95)", flush=True)

    params = grid()
    print(f"grid size {len(params)}", flush=True)
    results = []
    t0 = time.perf_counter()
    for i, p in enumerate(params, 1):
        results.append(replay(core, uni, p))
        if i % 25 == 0 or i == len(params):
            print(f"  replayed {i}/{len(params)} {time.perf_counter()-t0:.1f}s", flush=True)
    df = pd.DataFrame(results).sort_values(["realized_pnl", "win_rate"], ascending=False)
    best_row = df.iloc[0]
    seed = Params(
        name="seed",
        max_pair_cost=float(best_row["max_pair_cost"]),
        min_edge=float(best_row["min_edge"]),
        fee_buffer=float(best_row["fee_buffer"]),
        min_ask=float(best_row["min_ask"]),
        max_ask=float(best_row["max_ask"]),
        min_usd=float(best_row["min_usd"]),
        position_usd=float(best_row["position_usd"]),
        max_open_pairs=int(best_row["max_open_pairs"]),
        prefer_only=bool(best_row["prefer_only"]),
        max_horizon_hours=int(best_row["max_horizon_hours"]),
    )
    extra = []
    for rp in refine_grid(seed):
        extra.append(replay(core, uni, rp))
    df = pd.concat([df, pd.DataFrame(extra)], ignore_index=True)
    df = df.sort_values(["realized_pnl", "win_rate"], ascending=False)
    df.to_csv(OUT / "grid_results.csv", index=False)
    top = df.head(25)
    current = df[df["name"] == "current"].iloc[0].to_dict()
    best = df.iloc[0].to_dict()
    like = df[(df["prefer_only"] == True) & (df["fee_buffer"] >= 0.01) & (df["max_horizon_hours"] > 0)]
    best_conservative = like.iloc[0].to_dict() if len(like) else best

    keys = (
        "name",
        "realized_pnl",
        "roi_pct",
        "trades",
        "win_rate",
        "max_pair_cost",
        "min_edge",
        "fee_buffer",
        "position_usd",
        "max_open_pairs",
        "prefer_only",
        "min_ask",
        "min_usd",
        "max_horizon_hours",
        "max_drawdown_pct",
    )

    payload = {
        "data": {
            "source": str(DATA),
            "files": ["markets.parquet", "trades.parquet"],
            "window": "2026-01-01 to last print (~2026-07-19)",
            "sync_seconds": SYNC_SEC,
            "method": (
                "15s buckets; buy both legs at max print in-bucket (conservative last-trade ask); "
                "equal shares; hold to market end_date; payoff = shares * (settle1+settle2) "
                "from markets.outcome_prices (fallback $1 combined)."
            ),
            "not_tested": (
                "maker/resting fills, L2 books, lose-leg/ladder exits, "
                "live taker fees beyond fee_buffer; last-trade max-in-bucket is a conservative ask proxy"
            ),
        },
        "universe": {
            "binary_markets": int(len(uni)),
            "preferred": int(uni["preferred"].sum()),
        },
        "opportunities": opp,
        "current": current,
        "best": best,
        "best_conservative": best_conservative,
        "top25": top.to_dict(orient="records"),
        "grid_n": int(len(df)),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }
    (OUT / "summary.json").write_text(json.dumps(payload, indent=2))
    print("BEST", {k: best[k] for k in keys})
    print("CURRENT", {k: current[k] for k in keys})
    print("CONSERVATIVE", {k: best_conservative[k] for k in keys})


if __name__ == "__main__":
    main()
