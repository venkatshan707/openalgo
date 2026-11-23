#!/usr/bin/env python
"""Nifty channel-strategy research script.

What it does
------------
1. Downloads 5-min and 15-min Nifty bars via OpenAlgo → Kite.
   Kite serves 60 days per intraday request; OpenAlgo chunks automatically.
2. Stores them in a local DuckDB (research/nifty_research.duckdb).
3. Backtests the swing-point channel strategy rules:
   - Channel built from 15-min pivot highs/lows (last two of each).
   - Entry only after a confirmed bounce (one strong candle or two rising closes).
   - Room-to-run filter: target distance >= 2x stop distance.
   - Stop = channel line +/- max(15 pts, 0.3 * recent avg-range).
   - Breakeven when price crosses the channel centre.
   - Trailing stop on the low of the last two green candles (PE: high of two red).
   - Half exit (lot 1) at the centre line, remainder trails to the 80% zone.
4. Prints a trade-by-trade summary and key statistics.

How to run
----------
Set the OpenAlgo server running and logged into Kite, then:

    uv run python strategies/research/channel_research.py

To pull fresh data only:        --download
To backtest only (no download): --backtest
To tune a parameter:            --prox 0.4 --stop-mult 0.4 --bounce-body 0.55

Configuration lives in the CONFIG block below.
"""

import argparse
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from openalgo import api

load_dotenv()

# ------------------------------------------------------------------ config
CONFIG = {
    "UNDERLYING": "NIFTY",
    "EXCHANGE": "NSE_INDEX",
    "MONTHS_BACK": 12,  # how far to download
    "PIVOT_LEFT": 5,  # bars to the left of a swing high/low
    "PROX_PCT": 0.4,  # % proximity to line = "touch" (optimised from sweep)
    "STOP_FIXED": 15,  # minimum stop buffer in Nifty points
    "STOP_RANGE_MULT": 0.3,  # alternative: fraction of recent avg candle range
    "BOUNCE_BODY_PCT": 0.50,  # min body/range for a "strong" candle (optimised)
    "TARGET_RATIO": 2.5,  # reward/risk needed before entry (optimised)
    "PARALLEL_TOL": 0.30,  # max channel-width change allowed before "not parallel"
    "SIDE_FILTER": False,    # trade only the Bulkowski-preferred side per pattern
    "LOT1_RISK_MULT": 0.0,   # lot 1 at this many x the initial risk; 0 = use LOT1_EXIT_PCT (channel-width target)
    "BREAKOUT_ENTRY": False, # enter in the break direction when a channel breaks
    "CHANNEL_MODE": "rolling",  # rolling (last two swings, rebuilt) | anchored (4 swings, held until broken)
    "PAR_TOL": 0.30,  # anchored: width change over the swing span below which lines are made parallel
    "MIN_WIDTH_RANGES": 2.0,  # anchored: minimum channel width, in average 5m candle ranges
    "MAX_WIDTH_GROWTH": 0,  # anchored: end a pattern once it is this many times its width at lock (0 = off)
    "MAX_PATTERN_SPAN": 0,   # anchored: max 15m bars from earliest to latest pivot (0 = no cap)
    "TOUCH_TIMEOUT_MULT": 0,  # anchored: expire if no touch in N * pivot_span bars since last touch (0 = off)
    "TOUCH_TIMEOUT_FRAC": 0.05,  # anchored: zone size for touch-timeout check (fraction of channel width)
    "BREAK_BODY": 0.5,  # anchored: body fraction of the 15m candle that closes beyond a line
    "TRIANGLES": False,  # anchored: build patterns whose lines slope opposite ways (triangles)
    "FLAT_TOL": 0.15,  # anchored: a line is flat if it moves less than this share of the width over the swings
    "ENTRY_RULE": "old",  # old | two_candle (rejection candle at the line, then a close beyond it)
    "TOUCH_FRAC": 0.10,  # two_candle: touch band as a fraction of channel width
    "CONFIRM_WINDOW": 1,  # two_candle: candle 2 (1) or candle 2 or 3 (2) must close beyond candle 1's high/low
    "FAST_SWING": False,  # anchored: the 4th swing may be confirmed by the 5m entry rule instead of 3 15m candles
    "TRAIL_N": 2,  # trailing stop from the lows (CE) / highs (PE) of the last N with-trend candles
    "EXIT_RULE": "zone_red",  # zone_red (any opposite candle in the zone) | reversal (shape exit + zone trail)
    "HAMMER_WICK": 0.60,  # reversal: rejection wick at least this share of the range
    "HAMMER_BODY": 0.30,  # reversal: hammer body at most this share of the range
    "DOJI_BODY": 0.10,  # reversal: doji body at most this share of the range
    "REV_BODY": 0.50,  # reversal: strong opposite candle body at least this share of the range
    "MAX_TRADES_DAY": 3,
    "COOLOFF_MINS": 30,
    "TARGET_ZONE_PCT": 0.80,  # fraction of channel width where the runner may exit
    "SIZE_RULE": "fixed",  # fixed | rsi_div | pdhl | morning_1lot | combined
    "RUNNER_RULE": "target",  # target | trail_only | rsi_hold
    "ENTRY_GATE": None,  # None | "rsi_div" (information only; removes trades)
    "RSI_LEN": 14,
    "PDHL_TOL_PCT": 0.15,  # rail within this % of prior-day high/low = confluence
    "LOT_SIZE": 65,  # Nifty lot size; confirm with client.optionsymbol
    "STRIKE_ITM_STEPS": 1,  # strikes in the money from ATM: 1 = ITM1 (Nifty 23466 -> 23400 CE / 23500 PE); 0 = ATM
    "COST_PER_ORDER": 40,  # rupees: brokerage plus statutory charges, rough
    "SLIPPAGE_PREMIUM": 1.0,  # option premium points lost per side
    "RISK_FREE": 0.065,
    "DEFAULT_IV": 0.13,  # used when India VIX is missing for a date
    "SPLIT_DATE": "2026-04-28",  # H1 = before (unseen by the sweep), H2 = from (tuned on)
    "DB_PATH": Path(__file__).parent / "nifty_research.duckdb",
}

# Frozen by the trader on 2026-11-02. Not tuned, searched or overridden unless the trader asks;
# a config that tries another value is refused (see _check_frozen).
FROZEN = {
    "PIVOT_RIGHT": 3,  # a 15m swing is confirmed by 3 later 15m candles
    "MIN_ENTRY_TIME": "09:45",  # first entry
    "ENTRY_END": "14:30",  # last entry
    "LOT1_EXIT_PCT": 0.80,  # lot 1 is sold on a close 80% of the way across the channel
}
CONFIG.update(FROZEN)


def _check_frozen(cfg):
    for k, v in FROZEN.items():
        if cfg.get(k, v) != v:
            raise ValueError(f"{k} is frozen at {v!r} by the trader; got {cfg[k]!r}")

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"
ENTRY_END = FROZEN["ENTRY_END"]
EXIT_TIME = "15:15"
FINAL_REASONS = ("STOP", "TRAIL", "TARGET", "REVERSAL", "EOD")  # the leg that closes a trade


# ------------------------------------------------------------------ data
def _client():
    key = os.getenv("OPENALGO_API_KEY")
    host = os.getenv("HOST_SERVER", "http://127.0.0.1:5000")
    if not key:
        sys.exit("OPENALGO_API_KEY not set. Start the OpenAlgo server and log in to Kite.")
    return api(api_key=key, host=host)


def download(months=6):
    """Fetch 5-min and 15-min bars, store in DuckDB."""
    client = _client()
    db = duckdb.connect(str(CONFIG["DB_PATH"]))
    db.execute("""
        CREATE TABLE IF NOT EXISTS bars (
            interval  VARCHAR,
            timestamp TIMESTAMPTZ,
            open      DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT,
            PRIMARY KEY (interval, timestamp)
        )
    """)

    end = datetime.now(IST)
    start = end - timedelta(days=months * 31)

    # India VIX daily is stored as interval "VIX_D"; it is the IV for the rupee estimate.
    for interval, symbol, label in (
        ("5m", CONFIG["UNDERLYING"], "5m"),
        ("15m", CONFIG["UNDERLYING"], "15m"),
        ("D", "INDIAVIX", "VIX_D"),
    ):
        print(
            f"Downloading {symbol} {interval} bars from {start:%Y-%m-%d} to {end:%Y-%m-%d} ...",
            flush=True,
        )
        df = client.history(
            symbol=symbol,
            exchange=CONFIG["EXCHANGE"],
            interval=interval,
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
        )
        if not isinstance(df, pd.DataFrame) or df.empty:
            print(f"  No data returned for {interval}: {df}")
            continue

        df = df.reset_index()
        # normalise column names: history() returns 'datetime' or 'timestamp'
        df.columns = [c.lower() for c in df.columns]
        ts_col = next((c for c in df.columns if c in ("datetime", "timestamp", "date")), None)
        if ts_col is None:
            print(f"  Cannot find timestamp column. Got: {df.columns.tolist()}")
            continue
        df = df.rename(columns={ts_col: "timestamp"})
        if df["timestamp"].dt.tz is None:
            df["timestamp"] = df["timestamp"].dt.tz_localize(IST)
        else:
            df["timestamp"] = df["timestamp"].dt.tz_convert(IST)
        df["interval"] = label
        if "volume" not in df.columns:
            df["volume"] = 0

        before = db.execute("SELECT COUNT(*) FROM bars WHERE interval = ?", [label]).fetchone()[0]
        db.execute("""
            INSERT OR REPLACE INTO bars (interval, timestamp, open, high, low, close, volume)
            SELECT interval, timestamp, open, high, low, close, volume FROM df
        """)
        after = db.execute("SELECT COUNT(*) FROM bars WHERE interval = ?", [label]).fetchone()[0]
        print(f"  {label}: {after - before} new rows ({after} total)")

    db.close()
    print("Download complete.")


def load_bars(interval):
    db = duckdb.connect(str(CONFIG["DB_PATH"]), read_only=True)
    df = db.execute(
        "SELECT * FROM bars WHERE interval = ? ORDER BY timestamp",
        [interval],
    ).df()
    db.close()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(IST)
    df = df.set_index("timestamp")
    return df.drop(columns=["interval"])


def _columns(db, table):
    return {
        r[0]
        for r in db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [table]
        ).fetchall()
    }


def _init_result_tables(db):
    # Research output only, rewritten by every run: an old layout is dropped, not migrated.
    # The old trades key had no `reason`, so a half exit and its runner overwrote each other.
    old_trades = _columns(db, "trades")
    if old_trades and "lots" not in old_trades:
        db.execute("DROP TABLE trades")
    old_sweep = _columns(db, "sweep_results")
    if old_sweep and "min_entry" not in old_sweep:
        db.execute("DROP TABLE sweep_results")

    db.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            run_ts     TIMESTAMPTZ,
            entry_ts   TIMESTAMPTZ,
            exit_ts    TIMESTAMPTZ,
            side       VARCHAR,
            entry_px   DOUBLE,
            exit_px    DOUBLE,
            stop_px    DOUBLE,
            lots       INTEGER,
            pnl_pts    DOUBLE,
            reason     VARCHAR,
            prox_pct   DOUBLE,
            bounce_body DOUBLE,
            target_ratio DOUBLE,
            PRIMARY KEY (run_ts, entry_ts, side, reason)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS experiments (
            run_ts      TIMESTAMPTZ,
            exp_id      VARCHAR,
            name        VARCHAR,
            period      VARCHAR,
            entries     INTEGER,
            win_rate    DOUBLE,
            profit_factor DOUBLE,
            total_pts   DOUBLE,
            pts_per_lot DOUBLE,
            max_dd      DOUBLE,
            worst_trade DOUBLE,
            est_rupees  DOUBLE,
            PRIMARY KEY (run_ts, exp_id, period)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS backtest_runs (
            run_ts        TIMESTAMPTZ PRIMARY KEY,
            data_from     DATE,
            data_to       DATE,
            prox_pct      DOUBLE,
            bounce_body   DOUBLE,
            target_ratio  DOUBLE,
            target_zone   DOUBLE,
            stop_mult     DOUBLE,
            trend_filter  BOOLEAN,
            pivot_left    INTEGER,
            pivot_right   INTEGER,
            n_trades      INTEGER,
            n_wins        INTEGER,
            win_rate      DOUBLE,
            profit_factor DOUBLE,
            total_pts     DOUBLE,
            avg_win       DOUBLE,
            avg_loss      DOUBLE,
            max_loss      DOUBLE,
            half_exits    INTEGER
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS sweep_results (
            run_ts        TIMESTAMPTZ,
            prox_pct      DOUBLE,
            bounce_body   DOUBLE,
            target_ratio  DOUBLE,
            target_zone   DOUBLE,
            trend_filter  BOOLEAN,
            min_entry     VARCHAR,
            n_trades      INTEGER,
            win_rate      DOUBLE,
            profit_factor DOUBLE,
            total_pts     DOUBLE,
            max_loss      DOUBLE,
            PRIMARY KEY (run_ts, prox_pct, bounce_body, target_ratio, target_zone,
                         trend_filter, min_entry)
        )
    """)


def save_trades(run_ts, cfg, df_trades):
    """Persist individual trade rows for later analysis (e.g. finding outliers)."""
    df = df_trades.copy().reset_index(drop=True)
    df["run_ts"] = run_ts
    df["prox_pct"] = cfg["PROX_PCT"]
    df["bounce_body"] = cfg["BOUNCE_BODY_PCT"]
    df["target_ratio"] = cfg["TARGET_RATIO"]
    # keep only the columns the table expects
    cols = ["run_ts", "entry_ts", "exit_ts", "side", "entry_px", "exit_px",
            "stop_px", "lots", "pnl_pts", "reason", "prox_pct", "bounce_body", "target_ratio"]
    missing = [c for c in cols if c not in df.columns]
    for c in missing:
        df[c] = None
    df = df[cols]
    db = duckdb.connect(str(CONFIG["DB_PATH"]))
    _init_result_tables(db)
    db.execute("INSERT OR REPLACE INTO trades SELECT * FROM df")
    db.close()


def save_backtest(cfg, df_trades, data_from, data_to):
    """Persist one backtest run to DuckDB."""
    full = df_trades[df_trades["reason"].isin(FINAL_REASONS)]
    wins = full[full["pnl_pts"] > 0]
    losses = full[full["pnl_pts"] <= 0]
    gross_win = wins["pnl_pts"].sum() if len(wins) else 0
    gross_loss = abs(losses["pnl_pts"].sum()) if len(losses) else 1e-9

    row = {
        "run_ts": datetime.now(IST),
        "data_from": data_from,
        "data_to": data_to,
        "prox_pct": cfg["PROX_PCT"],
        "bounce_body": cfg["BOUNCE_BODY_PCT"],
        "target_ratio": cfg["TARGET_RATIO"],
        "target_zone": cfg.get("TARGET_ZONE_PCT", 0.80),
        "stop_mult": cfg["STOP_RANGE_MULT"],
        "trend_filter": cfg.get("TREND_FILTER", False),
        "pivot_left": cfg["PIVOT_LEFT"],
        "pivot_right": cfg["PIVOT_RIGHT"],
        "n_trades": len(full),
        "n_wins": len(wins),
        "win_rate": round(100 * len(wins) / max(len(full), 1), 1),
        "profit_factor": round(gross_win / gross_loss, 3),
        "total_pts": round(df_trades["pnl_pts"].sum(), 1),
        "avg_win": round(wins["pnl_pts"].mean(), 1) if len(wins) else 0.0,
        "avg_loss": round(losses["pnl_pts"].mean(), 1) if len(losses) else 0.0,
        "max_loss": round(losses["pnl_pts"].min(), 1) if len(losses) else 0.0,
        "half_exits": int((df_trades["reason"] == "LOT1").sum()),  # column kept; now lot 1 at 80%
    }
    df_row = pd.DataFrame([row])
    db = duckdb.connect(str(CONFIG["DB_PATH"]))
    _init_result_tables(db)
    db.execute("""
        INSERT OR REPLACE INTO backtest_runs
        SELECT * FROM df_row
    """)
    db.close()
    print(f"  Saved to backtest_runs (run_ts={row['run_ts']:%Y-%m-%d %H:%M})")


def save_sweep(run_ts, rows_list):
    """Persist all sweep rows for one run to DuckDB."""
    df_rows = pd.DataFrame(rows_list)
    df_rows["run_ts"] = run_ts
    df_rows = df_rows.rename(columns={
        "prox": "prox_pct", "body": "bounce_body", "rr": "target_ratio",
        "zone": "target_zone", "pf": "profit_factor", "trades": "n_trades",
    })
    df_rows["trend_filter"] = df_rows["trend"] == "Y"
    df_rows = df_rows.drop(columns=["trend"])
    db = duckdb.connect(str(CONFIG["DB_PATH"]))
    _init_result_tables(db)
    db.execute("""
        INSERT OR REPLACE INTO sweep_results
        SELECT run_ts, prox_pct, bounce_body, target_ratio, target_zone, trend_filter,
               min_entry, n_trades, win_rate, profit_factor, total_pts, max_loss
        FROM df_rows
    """)
    db.close()
    print(f"  Saved {len(df_rows)} rows to sweep_results (run_ts={run_ts:%Y-%m-%d %H:%M})")


def show_results():
    """Print stored backtest and sweep run history."""
    db = duckdb.connect(str(CONFIG["DB_PATH"]), read_only=True)
    tables = {r[0] for r in db.execute("SHOW TABLES").fetchall()}

    if "backtest_runs" in tables:
        df = db.execute(
            "SELECT * FROM backtest_runs ORDER BY run_ts DESC"
        ).df()
        print(f"\n{'=' * 70}")
        print(f"STORED BACKTEST RUNS ({len(df)} total)")
        print(f"{'=' * 70}")
        cols = ["run_ts", "prox_pct", "bounce_body", "target_ratio",
                "n_trades", "win_rate", "profit_factor", "total_pts", "max_loss"]
        print(df[cols].to_string(index=False))
    else:
        print("No backtest_runs table yet. Run --backtest first.")

    if "trades" in tables:
        df = db.execute("""
            SELECT entry_ts, exit_ts, side, entry_px, exit_px, stop_px, pnl_pts, reason
            FROM trades
            ORDER BY pnl_pts ASC
            LIMIT 10
        """).df()
        print(f"\n{'=' * 70}")
        print("10 WORST TRADES (all runs)")
        print(f"{'=' * 70}")
        print(df.to_string(index=False))

        df_best = db.execute("""
            SELECT entry_ts, exit_ts, side, entry_px, exit_px, pnl_pts, reason
            FROM trades
            ORDER BY pnl_pts DESC
            LIMIT 10
        """).df()
        print(f"\n10 BEST TRADES (all runs)")
        print(df_best.to_string(index=False))

    if "sweep_results" in tables:
        df = db.execute("""
            SELECT run_ts, COUNT(*) AS combos,
                   MAX(profit_factor) AS best_pf,
                   MAX(total_pts)     AS best_pts,
                   MIN(max_loss)      AS worst_loss
            FROM sweep_results
            GROUP BY run_ts
            ORDER BY run_ts DESC
        """).df()
        print(f"\n{'=' * 70}")
        print(f"STORED SWEEP RUNS ({len(df)} total)")
        print(f"{'=' * 70}")
        print(df.to_string(index=False))

        # Latest sweep top-10
        latest_ts = db.execute(
            "SELECT MAX(run_ts) FROM sweep_results"
        ).fetchone()[0]
        df_top = db.execute("""
            SELECT prox_pct, bounce_body, target_ratio, target_zone, trend_filter,
                   n_trades, win_rate, profit_factor, total_pts, max_loss
            FROM sweep_results
            WHERE run_ts = ?
            ORDER BY profit_factor DESC
            LIMIT 10
        """, [latest_ts]).df()
        print(f"\nLatest sweep top-10 (run {latest_ts}):")
        print(df_top.to_string(index=False))
    else:
        print("No sweep_results table yet. Run --sweep first.")

    db.close()


# ------------------------------------------------------------------ channel
def find_pivots(df, left, right):
    """Return series of pivot high and pivot low values (NaN where no pivot)."""
    highs = df["high"]
    lows = df["low"]
    ph = pd.Series(np.nan, index=df.index)
    pl = pd.Series(np.nan, index=df.index)

    for i in range(left, len(df) - right):
        window_h = highs.iloc[i - left : i + right + 1]
        if highs.iloc[i] == window_h.max() and highs.iloc[i] > window_h.drop(highs.index[i]).max():
            ph.iloc[i] = highs.iloc[i]

        window_l = lows.iloc[i - left : i + right + 1]
        if lows.iloc[i] == window_l.min() and lows.iloc[i] < window_l.drop(lows.index[i]).min():
            pl.iloc[i] = lows.iloc[i]

    return ph, pl


def swing_channel_at(ph_series, pl_series, idx, right=None):
    """
    Return (support, resistance, centre) projected to bar `idx`, from the last two
    pivot lows and highs that were confirmed by the close of bar `idx`.
    Returns None if not enough pivots.
    """
    # A pivot at bar j needs `right` later bars to exist, so it is known only from j + right.
    right = CONFIG["PIVOT_RIGHT"] if right is None else right
    ph_before = ph_series.iloc[: max(idx - right + 1, 0)].dropna()
    pl_before = pl_series.iloc[: max(idx - right + 1, 0)].dropna()
    if len(ph_before) < 2 or len(pl_before) < 2:
        return None

    # Last two highs / lows
    h2, h1 = ph_before.iloc[-2], ph_before.iloc[-1]
    l2, l1 = pl_before.iloc[-2], pl_before.iloc[-1]
    ht2, ht1 = ph_before.index[-2], ph_before.index[-1]
    lt2, lt1 = pl_before.index[-2], pl_before.index[-1]

    # Treat bar indices as integers for slope
    ph_idx1 = ph_series.index.get_loc(ht2)
    ph_idx2 = ph_series.index.get_loc(ht1)
    pl_idx1 = pl_series.index.get_loc(lt2)
    pl_idx2 = pl_series.index.get_loc(lt1)
    cur_idx = idx

    r_slope = (h1 - h2) / max(ph_idx2 - ph_idx1, 1)
    r_val = h1 + r_slope * (cur_idx - ph_idx2)

    s_slope = (l1 - l2) / max(pl_idx2 - pl_idx1, 1)
    s_val = l1 + s_slope * (cur_idx - pl_idx2)

    width_at_l1 = abs(r_val - s_val)
    width_at_l2 = abs((h2 + r_slope * (ph_idx1 - ph_idx1)) - (l2 + s_slope * (pl_idx1 - pl_idx1)))
    # Parallelism check
    if width_at_l2 > 0 and abs(width_at_l1 - width_at_l2) / width_at_l2 > CONFIG["PARALLEL_TOL"]:
        return None

    centre = (r_val + s_val) / 2
    return s_val, r_val, centre


def stop_buffer(df5, idx):
    """max(STOP_FIXED, STOP_RANGE_MULT * avg_range_last_20)."""
    recent = df5.iloc[max(0, idx - 20) : idx + 1]
    avg_range = (recent["high"] - recent["low"]).mean()
    return max(CONFIG["STOP_FIXED"], CONFIG["STOP_RANGE_MULT"] * avg_range)


def is_bounce_candle(row):
    """True for a strong green candle (body >= BOUNCE_BODY_PCT of range)."""
    rng = row["high"] - row["low"]
    if rng < 0.01:
        return False
    body = row["close"] - row["open"]
    return body > 0 and (body / rng) >= CONFIG["BOUNCE_BODY_PCT"]


def is_rejection_candle(row):
    """Strong red candle for PE confirmation."""
    rng = row["high"] - row["low"]
    if rng < 0.01:
        return False
    body = row["open"] - row["close"]
    return body > 0 and (body / rng) >= CONFIG["BOUNCE_BODY_PCT"]


# ------------------------------------------------------------------ features
def rsi(close, n=14):
    """Wilder RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + gain / loss)


def prior_day_hl(df5):
    """Previous session's high and low, aligned to every 5m bar."""
    dates = pd.Series(df5.index.date, index=df5.index)
    daily = df5.groupby(dates.values).agg(high=("high", "max"), low=("low", "min"))
    prev = daily.shift(1)
    pdh = dates.map(prev["high"]).to_numpy(dtype=float)
    pdl = dates.map(prev["low"]).to_numpy(dtype=float)
    return pdh, pdl


def _line_at(line, x):
    """A line is (x0, y0, slope) in 5m-bar units; overnight gaps add no slope."""
    return line[1] + line[2] * (x - line[0])


def channel_at(rec, x):
    """(support, resistance, centre) of a locked channel record at 5m position x."""
    s, r = _line_at(rec["sup"], x), _line_at(rec["res"], x)
    return s, r, (s + r) / 2


def _try_build(highs, lows, df15, x15, cfg):
    """Channel record from the latest two swing highs and lows, or a reason it fails."""
    (kh1, ph1), (kh2, ph2) = highs[-2], highs[-1]
    (kl1, pl1), (kl2, pl2) = lows[-2], lows[-1]
    xh1, xh2, xl1, xl2 = x15[kh1], x15[kh2], x15[kl1], x15[kl2]
    max_span = cfg.get("MAX_PATTERN_SPAN", 0)
    if max_span > 0:
        pivot_span = max(kh1, kh2, kl1, kl2) - min(kh1, kh2, kl1, kl2)
        if pivot_span > max_span:
            return None, "span_too_long"
    s_h = (ph2 - ph1) / max(xh2 - xh1, 1)
    s_l = (pl2 - pl1) / max(xl2 - xl1, 1)
    opposite = s_h * s_l < 0
    if opposite and not cfg.get("TRIANGLES", False):
        return None, "triangle"
    res = (xh1, ph1, s_h)
    sup = (xl1, pl1, s_l)
    xa, xb = min(xh1, xl1), max(xh2, xl2)
    w_a = _line_at(res, xa) - _line_at(sup, xa)
    w_b = _line_at(res, xb) - _line_at(sup, xb)
    if w_a <= 0 or w_b <= 0:
        return None, "crossed"
    pattern = _pattern_of(s_h, s_l, w_a, w_b, xb - xa, cfg)
    if pattern == "broadening_triangle":
        # a megaphone widens until it is broken, which seldom happens, and blocks every later pattern
        return None, "broadening_triangle"
    if pattern.startswith("channel"):
        if (s_h + s_l) / 2 < 0:
            # Downtrend: resistance through H1-H2, support parallel through the lowest low between them
            k = kh1 + int(np.argmin(df15["low"].to_numpy()[kh1 : kh2 + 1]))
            res, sup = (xh1, ph1, s_h), (x15[k], df15["low"].iat[k], s_h)
        else:
            k = kl1 + int(np.argmax(df15["high"].to_numpy()[kl1 : kl2 + 1]))
            sup, res = (xl1, pl1, s_l), (x15[k], df15["high"].iat[k], s_l)
        kind = "parallel"
    else:
        kind = "two_line"
    return {"res": res, "sup": sup, "kind": kind, "trend": "down" if s_h + s_l < 0 else "up",
            "pattern": pattern, "family": _family_of(pattern), "pivots": (kh1, kh2, kl1, kl2)}, None


def _pattern_of(s_h, s_l, w_a, w_b, span, cfg):
    """Name the shape of two lines (slopes per 5m bar) whose gap goes from w_a to w_b over span bars."""
    avg_w = (w_a + w_b) / 2
    flat_h = abs(s_h) * span <= cfg.get("FLAT_TOL", 0.15) * avg_w
    flat_l = abs(s_l) * span <= cfg.get("FLAT_TOL", 0.15) * avg_w
    rising = s_h + s_l >= 0
    if abs(w_b - w_a) / max(w_a, w_b) <= cfg.get("PAR_TOL", 0.3):
        return "channel_flat" if flat_h and flat_l else "channel_up" if rising else "channel_down"
    narrowing = w_b < w_a
    if narrowing and flat_h and not flat_l and s_l > 0:
        return "ascending_triangle"
    if narrowing and flat_l and not flat_h and s_h < 0:
        return "descending_triangle"
    if s_h * s_l < 0:
        return "symmetric_triangle" if narrowing else "broadening_triangle"
    if narrowing:
        return "rising_wedge" if rising else "falling_wedge"
    return "rising_broadening_wedge" if rising else "falling_broadening_wedge"


def _family_of(pattern):
    if pattern.startswith("channel"):
        return "channel"
    if pattern.endswith("triangle"):
        return "triangle"
    return "broadening_wedge" if pattern.endswith("broadening_wedge") else "wedge"


def anchored_channel_series(df5, df15, ph15, pl15, avg_range, cfg, idx15=None):
    """
    Channels built from 4 swings and held until broken, one value per 5m bar.

    Runs over closed 15m bars. A swing at bar k is used only from bar k + right. A
    channel is broken when a 15m candle closes beyond a line with a body of at least
    BREAK_BODY of its range and the next 15m candle opens and closes beyond it too;
    the next channel is then built from swings formed after the break. Returns
    (active per 5m bar, record list, active record index per 5m bar).
    """
    right = cfg["PIVOT_RIGHT"]
    min_w_mult = cfg.get("MIN_WIDTH_RANGES", 2.0)
    brk_body = cfg.get("BREAK_BODY", 0.5)
    eod_expire = cfg.get("EOD_EXPIRE", False)
    touch_expire = cfg.get("TOUCH_EXPIRE", False)
    touch_frac = cfg.get("TOUCH_FRAC", 0.10)
    max_growth = cfg.get("MAX_WIDTH_GROWTH", 0)
    touch_timeout_mult = cfg.get("TOUCH_TIMEOUT_MULT", 0)
    touch_timeout_frac = cfg.get("TOUCH_TIMEOUT_FRAC", 0.05)
    n15 = len(df15)
    start5 = df5.index.searchsorted(df15.index)
    x15 = (start5 + 1).astype(float)  # a 15m bar sits at its middle 5m bar
    o15, h15, l15, c15 = (df15[k].to_numpy() for k in ("open", "high", "low", "close"))
    ph, pl = ph15.to_numpy(), pl15.to_numpy()

    fast = cfg.get("FAST_SWING", False) and idx15 is not None
    if fast:
        o5, h5, l5, c5 = (df5[k].to_numpy() for k in ("open", "high", "low", "close"))
        first5 = np.searchsorted(idx15, np.arange(n15), side="left")
        last5 = np.searchsorted(idx15, np.arange(n15), side="right")
        fast_args = (df15, x15, start5, avg_range, cfg, o5, h5, l5, c5, l15, h15)

    records = []
    active_at = np.full(n15, -1)
    cur = -1
    reset = -1
    pending = -1
    highs, lows = [], []
    for j in range(n15):
        if pending >= 0:
            cur, pending = pending, -1
        k = j - right
        if k >= 0 and k > reset:
            if not np.isnan(ph[k]):
                highs = (highs + [(k, ph[k])])[-2:]
            if not np.isnan(pl[k]):
                lows = (lows + [(k, pl[k])])[-2:]

        if cur >= 0:
            rec = records[cur]
            pos = min(start5[j] + 2, len(avg_range) - 1)
            s_j, r_j, _ = channel_at(rec, x15[j])
            ended = None
            if "lock_w" not in rec:
                s_l, r_l, _ = channel_at(rec, x15[rec["lock_j"]])
                rec["lock_w"] = r_l - s_l
                rec["last_touch_j"] = rec["lock_j"]
                kh1, kh2, kl1, kl2 = rec["pivots"]
                rec["pivot_span"] = max(kh1, kh2, kl1, kl2) - min(kh1, kh2, kl1, kl2)
            # update last_touch_j if this bar is within touch_timeout_frac of either line
            tt_band = touch_timeout_frac * (r_j - s_j)
            if h15[j] >= r_j - tt_band or l15[j] <= s_j + tt_band:
                rec["last_touch_j"] = j
            if r_j - s_j < min_w_mult * avg_range[pos]:
                ended = "narrowed"
            elif max_growth > 0 and r_j - s_j > max_growth * rec["lock_w"]:
                ended = "widened"
            elif j - 1 > rec["lock_j"]:
                s_a, r_a, _ = channel_at(rec, x15[j - 1])
                rng = h15[j - 1] - l15[j - 1]
                body = (c15[j - 1] - o15[j - 1]) / rng if rng > 0.01 else 0.0
                up = c15[j - 1] > r_a and body >= brk_body and o15[j] > r_j and c15[j] > r_j
                dn = c15[j - 1] < s_a and -body >= brk_body and o15[j] < s_j and c15[j] < s_j
                if up or dn:
                    ended = "break_up" if up else "break_down"
            # Approach 1: expire at end of each trading day
            if not ended and eod_expire and j > 0:
                if df15.index[j].date() != df15.index[j - 1].date():
                    ended = "eod_expire"
            # Approach 2: expire if span-many bars pass from lock without a touch
            if not ended and touch_expire:
                kh1, kh2, kl1, kl2 = rec["pivots"]
                pivot_span = max(kh1, kh2, kl1, kl2) - min(kh1, kh2, kl1, kl2)
                bars_since_lock = j - rec["lock_j"]
                if bars_since_lock >= max(pivot_span, 1):
                    # Check if any bar from lock to now touched either band
                    touched = False
                    for jj in range(rec["lock_j"], j + 1):
                        s_jj, r_jj, _ = channel_at(rec, x15[jj])
                        band = touch_frac * (r_jj - s_jj)
                        if h15[jj] >= r_jj - band or l15[jj] <= s_jj + band:
                            touched = True
                            break
                    if not touched:
                        ended = "touch_expire"
            # Approach 3: expire if no touch in N * pivot_span bars since last touch
            if not ended and touch_timeout_mult > 0:
                pivot_span = max(rec.get("pivot_span", 1), 1)
                timeout = touch_timeout_mult * pivot_span
                if j - rec.get("last_touch_j", rec["lock_j"]) > timeout:
                    ended = "touch_timeout"
            if ended:
                rec["end_j"], rec["end_reason"] = j, ended
                cur, reset, highs, lows = -1, j, [], []

        if cur < 0 and len(highs) >= 2 and len(lows) >= 2:
            rec, why = _try_build(highs, lows, df15, x15, cfg)
            pos = min(start5[j] + 2, len(avg_range) - 1)
            if rec is not None:
                s_j, r_j, _ = channel_at(rec, x15[j])
                if r_j - s_j < min_w_mult * avg_range[pos]:
                    rec = None
            if rec is None:
                # drop the oldest of the four swings and wait for the next one
                if highs[0][0] < lows[0][0]:
                    highs = highs[1:]
                else:
                    lows = lows[1:]
            else:
                rec.update(lock_j=j, end_j=None, end_reason=None, id=len(records))
                records.append(rec)
                cur = rec["id"]
        if fast and cur < 0 and j + 1 < n15:
            rec = _fast_swing(highs, lows, range(first5[j], last5[j]), *fast_args)
            if rec is not None:
                # counted by the 15m machine from the next 15m bar; live on 5m from rec["lock5"]
                rec.update(lock_j=j + 1, end_j=None, end_reason=None, id=len(records))
                records.append(rec)
                pending = rec["id"]
        active_at[j] = cur
    return active_at, records


def _fast_swing(highs, lows, bars5, df15, x15, start5, avg_range, cfg, o5, h5, l5, c5, l15, h15):
    """Confirm the 4th swing by the 5m entry rule: a strong rejection candle at a new low (high),
    then candle 2 (or 3) closing beyond its high (low). Uses closed 5m bars only."""
    if len(highs) == 2 and len(lows) == 1:
        side = "CE"
    elif len(highs) == 1 and len(lows) == 2:
        side = "PE"
    else:
        return None
    left = cfg["PIVOT_LEFT"]
    body_min = cfg["BOUNCE_BODY_PCT"]
    touch_frac = cfg.get("TOUCH_FRAC", 0.10)
    min_w = cfg.get("MIN_WIDTH_RANGES", 2.0)
    window = cfg.get("CONFIRM_WINDOW", 1)
    last_k = max(k for k, _ in highs + lows)
    if last_k + 1 >= len(start5):
        return None
    s5 = start5[last_k + 1]  # first 5m bar after the latest confirmed swing's 15m bar
    for i in bars5:
        for m in range(i - 1, i - 1 - window, -1):
            if m < s5:
                break
            seg = l5[s5:i + 1] if side == "CE" else h5[s5:i + 1]
            arg = s5 + int(np.argmin(seg) if side == "CE" else np.argmax(seg))
            if arg > m:  # a new extreme after candle 1: candle 1 is not the turn
                continue
            ext = seg[arg - s5]
            k = int(np.searchsorted(start5, arg, side="right") - 1)  # 15m bar holding the extreme
            if k <= last_k or k - left < 0:
                continue
            prior = l15[k - left:k] if side == "CE" else h15[k - left:k]
            if (side == "CE" and not ext < prior.min()) or (side == "PE" and not ext > prior.max()):
                continue
            rng1 = h5[m] - l5[m]
            b1 = (c5[m] - o5[m]) / rng1 if rng1 > 0.01 else 0.0
            if side == "CE":
                ok = b1 >= body_min and c5[i] > h5[m] and (m == i - 1 or c5[i - 1] <= h5[m])
            else:
                ok = -b1 >= body_min and c5[i] < l5[m] and (m == i - 1 or c5[i - 1] >= l5[m])
            if not ok:
                continue
            # the swing's own 15m bar may still be forming: give it only the 5m bars seen so far
            view = df15.iloc[:k + 1].copy()
            seen = slice(start5[k], i + 1)
            view.iloc[k, view.columns.get_loc("high")] = h5[seen].max()
            view.iloc[k, view.columns.get_loc("low")] = l5[seen].min()
            new = (k, float(ext))
            rec, _ = _try_build(highs, lows + [new], view, x15, cfg) if side == "CE" else \
                _try_build(highs + [new], lows, view, x15, cfg)
            if rec is None:
                continue
            sup_i, res_i, _ = channel_at(rec, i)
            if res_i - sup_i < min_w * avg_range[i]:
                continue
            sup_m, res_m, _ = channel_at(rec, m)
            band = touch_frac * (res_m - sup_m)
            inside = [channel_at(rec, b) for b in range(m, i)]
            if side == "CE":
                ok = l5[m] <= sup_m + band and all(c5[b] >= sp for b, (sp, _, _) in zip(range(m, i), inside))
            else:
                ok = h5[m] >= res_m - band and all(c5[b] <= rp for b, (_, rp, _) in zip(range(m, i), inside))
            if ok:
                rec.update(lock5=int(i), fast_side=side, fast_c1=int(m))
                return rec
    return None


def build_features(df5, df15, ph15, pl15, cfg):
    """Everything the simulator reads that does not depend on the swept parameters."""
    _check_frozen(cfg)
    # Last 15m bar already closed when this 5m bar closes; the forming one is not known yet.
    idx15 = df15.index.searchsorted(df5.index - timedelta(minutes=10), side="right") - 1
    rng = (df5["high"] - df5["low"]).rolling(21, min_periods=1).mean().to_numpy()
    pdh, pdl = prior_day_hl(df5)
    feats = {
        "idx15": idx15,
        "avg_range": rng,
        "rsi": rsi(df5["close"], cfg.get("RSI_LEN", 14)).to_numpy(),
        "pdh": pdh,
        "pdl": pdl,
        "tstr": df5.index.strftime("%H:%M").to_numpy(),
        "date": np.array(df5.index.date),
    }
    if cfg.get("CHANNEL_MODE", "rolling") == "anchored":
        active_at, records = anchored_channel_series(df5, df15, ph15, pl15, rng, cfg, idx15)
        rec5 = np.where(idx15 >= 0, active_at[np.maximum(idx15, 0)], -1)
        for r in records:
            # a pattern locked by the 5m entry rule is live from that 5m bar, before its 15m bar closes
            if r.get("lock5") is not None:
                upto = int(np.searchsorted(idx15, r["lock_j"], side="left"))
                rec5[r["lock5"]:upto] = r["id"]
        feats["records"] = records
        feats["rec5"] = rec5
        feats["chan5"] = [channel_at(records[r], i) if r >= 0 else None for i, r in enumerate(rec5)]
        feats["chan"] = None
    else:
        chan = [swing_channel_at(ph15, pl15, j, cfg["PIVOT_RIGHT"]) for j in range(len(df15))]
        feats["chan"] = chan
        feats["chan5"] = [chan[j] if j >= 0 else None for j in idx15]
        feats["rec5"] = None
        feats["records"] = None
    return feats


def _lots_for(size_rule, div, confl, tstr):
    morning = tstr < "09:45"
    if size_rule == "rsi_div":
        return 3 if div else 2
    if size_rule == "pdhl":
        return 3 if confl else 2
    if size_rule == "morning_1lot":
        return 1 if morning else 2
    if size_rule == "combined":
        n = 3 if (div or confl) else 2
        return n - 1 if morning else n
    return 2


def _touch(clusters, i, px, rsi_val, lower):
    """Group consecutive rail touches into one swing; keep the extreme of each."""
    if clusters and i - clusters[-1][0] <= 2:
        last = clusters[-1]
        if (lower and px < last[1]) or (not lower and px > last[1]):
            last[1], last[2] = px, rsi_val
        last[0] = i
    else:
        clusters.append([i, px, rsi_val])
        del clusters[:-2]


def _divergence(clusters, i, index, side):
    """CE: lower (or equal) low with higher RSI. PE: higher high with lower RSI."""
    if len(clusters) < 2 or clusters[-1][0] < i - 2:
        return False
    prev, cur = clusters[-2], clusters[-1]
    if np.isnan(prev[2]) or np.isnan(cur[2]):
        return False
    if index[cur[0]] - index[prev[0]] > timedelta(days=4):
        return False
    if side == "CE":
        return cur[1] <= prev[1] and cur[2] > prev[2]
    return cur[1] >= prev[1] and cur[2] < prev[2]


# Bulkowski's preferred side per pattern. SKIP = no bounce trade at all.
_PREFERRED_SIDE = {
    "channel_up": "CE", "channel_down": "PE",   # trade with the slope
    "falling_broadening_wedge": "PE",            # short at the top of a widening down pattern
    "rising_broadening_wedge": "CE",             # long at the bottom of a widening up pattern
}
_SKIP_SIDE = frozenset({"falling_wedge", "rising_wedge",
                         "ascending_triangle", "descending_triangle", "symmetric_triangle"})


# ------------------------------------------------------------------ simulation core
def _simulate(df5, df15, ph15, pl15, cfg, feats=None):
    """Pure simulation: returns a DataFrame of trade legs. No I/O."""
    _check_frozen(cfg)
    if feats is None:
        feats = build_features(df5, df15, ph15, pl15, cfg)
    idx15_arr, chan, avg_range = feats["idx15"], feats["chan"], feats["avg_range"]
    chan5, rec5, records = feats["chan5"], feats["rec5"], feats["records"]
    rsi_arr, pdh, pdl = feats["rsi"], feats["pdh"], feats["pdl"]
    tstrs, dates = feats["tstr"], feats["date"]
    index = df5.index
    o = df5["open"].to_numpy()
    h = df5["high"].to_numpy()
    lo = df5["low"].to_numpy()
    c = df5["close"].to_numpy()

    prox = cfg["PROX_PCT"] / 100.0
    zone = cfg.get("TARGET_ZONE_PCT", 0.80)
    min_entry = FROZEN["MIN_ENTRY_TIME"]
    lot1_pct = FROZEN["LOT1_EXIT_PCT"]
    size_rule = cfg.get("SIZE_RULE", "fixed")
    runner_rule = cfg.get("RUNNER_RULE", "target")
    gate = cfg.get("ENTRY_GATE")
    trend_filter = cfg.get("TREND_FILTER", False)
    body_min = cfg["BOUNCE_BODY_PCT"]
    pdhl_tol = cfg.get("PDHL_TOL_PCT", 0.15) / 100.0
    be_at = cfg.get("BE_AT", 0.5)  # channel fraction for breakeven; None = never
    trail_mode = cfg.get("TRAIL", "always")  # always | after_be | off
    stop_anchor = cfg.get("STOP_ANCHOR", "line")  # line | swing (beyond the touch candles)
    entry_end = FROZEN["ENTRY_END"]
    two_candle = cfg.get("ENTRY_RULE", "old") == "two_candle"
    confirm_window = cfg.get("CONFIRM_WINDOW", 1)
    touch_frac = cfg.get("TOUCH_FRAC", 0.10)
    trail_n = cfg.get("TRAIL_N", 2)
    reversal = cfg.get("EXIT_RULE", "zone_red") == "reversal"
    ham_wick, ham_body = cfg.get("HAMMER_WICK", 0.60), cfg.get("HAMMER_BODY", 0.30)
    doji_body, rev_body = cfg.get("DOJI_BODY", 0.10), cfg.get("REV_BODY", 0.50)
    side_filter = cfg.get("SIDE_FILTER", False)
    lot1_risk_mult = cfg.get("LOT1_RISK_MULT", 0.0)
    breakout_entry_on = cfg.get("BREAKOUT_ENTRY", False)
    # open of the most recent green / red candle strictly before bar i
    last_green_open = pd.Series(np.where(c > o, o, np.nan)).ffill().shift(1).to_numpy()
    last_red_open = pd.Series(np.where(c < o, o, np.nan)).ffill().shift(1).to_numpy()

    trades = []
    held = None
    st = {}
    day_trades = 0
    last_date = None
    cooloff_until = None
    sup_touches = []
    res_touches = []
    _last_ended_rec = -1  # most recent channel record that just ended (for breakout entries)
    _prev_rec5 = -1       # rec5 value on the prior bar

    def add(reason, i, exit_px, lots):
        direction = 1 if held == "CE" else -1
        trades.append(
            {
                "entry_ts": st["entry_ts"],
                "exit_ts": index[i],
                "side": held,
                "entry_px": st["entry_px"],
                "exit_px": exit_px,
                "stop_px": st["stop"],
                "lots": lots,
                "pnl_pts": direction * (exit_px - st["entry_px"]) * lots,
                "reason": reason,
                "div": st["div"],
                "confl": st["confl"],
                "pattern": None if st["rec"] is None else records[st["rec"]]["pattern"],
                "family": None if st["rec"] is None else records[st["rec"]]["family"],
                "fast": st.get("fast", False),
            }
        )

    def body_ratio(i):
        rng = h[i] - lo[i]
        if rng < 0.01:
            return None
        return (c[i] - o[i]) / rng

    def _reversal_candle(i, turn):
        """turn -1: a top (CE exit), +1: a bottom (PE exit). Inverted hammer or hammer, doji, strong opposite candle."""
        rng = h[i] - lo[i]
        if rng < 0.01:
            return False
        body = abs(c[i] - o[i]) / rng
        if body <= doji_body:
            return True
        wick = (h[i] - max(o[i], c[i])) if turn < 0 else (min(o[i], c[i]) - lo[i])
        if wick / rng >= ham_wick and body <= ham_body:
            return True
        if turn < 0:
            return c[i] < o[i] and body >= rev_body and c[i] < last_green_open[i]
        return c[i] > o[i] and body >= rev_body and c[i] > last_red_open[i]

    def runner_may_exit(side, r):
        if runner_rule == "trail_only":
            return False
        if runner_rule == "rsi_hold" and not np.isnan(r):
            return r <= 60 if side == "CE" else r >= 40
        return True

    def near_pdhl(level, i):
        ds = [abs(level - x) for x in (pdh[i], pdl[i]) if not np.isnan(x)]
        return bool(ds) and min(ds) / level <= pdhl_tol

    for i in range(len(df5)):
        if i < 10:
            continue
        tstr = tstrs[i]
        date = dates[i]
        if date != last_date:
            day_trades = 0
            last_date = date
            held = None

        # ---------- forced EOD exit ----------
        if tstr >= EXIT_TIME and held:
            add("EOD", i, c[i], st["lots"])
            held = None
            continue

        entry_open = min_entry <= tstr < entry_end
        if not held and not entry_open:
            continue

        close = c[i]
        buf = max(cfg["STOP_FIXED"], cfg["STOP_RANGE_MULT"] * avg_range[i])

        # ---------- stop check and trailing stop (no channel needed) ----------
        # Must run on every bar: skipping it when the channel is invalid once left a
        # position unmanaged all day (the -411-point loss on 2026-06-12).
        trailing = held and (trail_mode == "always" or (trail_mode == "after_be" and st["be_done"]))
        if held == "CE":
            if close < st["stop"]:
                add("TRAIL" if st["stop"] != st["init_stop"] else "STOP", i, close, st["lots"])
                held = None
                cooloff_until = index[i] + timedelta(minutes=cfg["COOLOFF_MINS"])
                continue
            if c[i] > o[i]:
                st["swing"] = (st["swing"] + [lo[i]])[-trail_n:]
            if trailing and len(st["swing"]) == trail_n:
                trail = min(st["swing"]) - buf
                st["trail"] = max(st["trail"] or trail, trail)
                st["stop"] = max(st["stop"], st["trail"])
        elif held == "PE":
            if close > st["stop"]:
                add("TRAIL" if st["stop"] != st["init_stop"] else "STOP", i, close, st["lots"])
                held = None
                cooloff_until = index[i] + timedelta(minutes=cfg["COOLOFF_MINS"])
                continue
            if c[i] < o[i]:
                st["swing"] = (st["swing"] + [h[i]])[-trail_n:]
            if trailing and len(st["swing"]) == trail_n:
                trail = max(st["swing"]) + buf
                st["trail"] = min(st["trail"] or trail, trail)
                st["stop"] = min(st["stop"], st["trail"])

        # ---------- 15m channel ----------
        j = idx15_arr[i]
        if j < 0:
            continue
        ch = chan5[i]
        if held and st.get("rec") is not None and (rec5[i] != st["rec"]):
            # a held trade keeps the channel it was entered on, even after a break
            ch = channel_at(records[st["rec"]], i)
        # track the most recent ended channel record (for breakout entries)
        if rec5 is not None:
            cur_r = int(rec5[i]) if rec5[i] >= 0 else -1
            if cur_r != _prev_rec5 and _prev_rec5 >= 0:
                _last_ended_rec = _prev_rec5  # the channel that just ended or was replaced
            _prev_rec5 = cur_r
        if ch is None:
            # --- breakout entry when no active channel ---
            if (breakout_entry_on and held is None and entry_open
                    and day_trades < cfg["MAX_TRADES_DAY"]
                    and not (cooloff_until and index[i] < cooloff_until)
                    and rec5 is not None and _last_ended_rec >= 0):
                r_brk = _last_ended_rec
                end_reason = records[r_brk].get("end_reason", "")
                end_j = records[r_brk].get("end_j", -1)
                if end_j >= 0 and 0 < j - end_j <= 8 and end_reason in ("break_up", "break_down"):
                    brk_ch = channel_at(records[r_brk], end_j)
                    if brk_ch is not None:
                        s_brk, r_brk_level, _ = brk_ch
                        brk_w = r_brk_level - s_brk
                        rng_i = h[i] - lo[i]
                        brat = (c[i] - o[i]) / rng_i if rng_i > 0.01 else 0.0
                        if end_reason == "break_up" and brat >= body_min and c[i] > r_brk_level:
                            brk_stop = r_brk_level - buf
                            brk_risk = c[i] - brk_stop
                            brk_tgt = r_brk_level + brk_w
                            if brk_risk > 0 and (brk_tgt - c[i]) >= cfg["TARGET_RATIO"] * brk_risk:
                                held = "CE"
                                st = {
                                    "entry_px": c[i], "entry_ts": index[i],
                                    "stop": brk_stop, "init_stop": brk_stop,
                                    "zone_hit": False,
                                    "lots": _lots_for(size_rule, False, False, tstr),
                                    "lot1_done": False, "be_done": False,
                                    "trail": None, "swing": [], "div": False, "confl": False,
                                    "rec": None, "fast": False,
                                    "lot1_target": c[i] + 1.5 * brk_risk if lot1_risk_mult > 0 else None,
                                    "brk_target": brk_tgt,
                                }
                                day_trades += 1
                        elif end_reason == "break_down" and (-brat) >= body_min and c[i] < s_brk:
                            brk_stop = s_brk + buf
                            brk_risk = brk_stop - c[i]
                            brk_tgt = s_brk - brk_w
                            if brk_risk > 0 and (c[i] - brk_tgt) >= cfg["TARGET_RATIO"] * brk_risk:
                                held = "PE"
                                st = {
                                    "entry_px": c[i], "entry_ts": index[i],
                                    "stop": brk_stop, "init_stop": brk_stop,
                                    "zone_hit": False,
                                    "lots": _lots_for(size_rule, False, False, tstr),
                                    "lot1_done": False, "be_done": False,
                                    "trail": None, "swing": [], "div": False, "confl": False,
                                    "rec": None, "fast": False,
                                    "lot1_target": c[i] - 1.5 * brk_risk if lot1_risk_mult > 0 else None,
                                    "brk_target": brk_tgt,
                                }
                                day_trades += 1
            continue
        support, resistance, centre = ch
        r_i = rsi_arr[i]

        if lo[i] <= support * (1 + prox):
            _touch(sup_touches, i, lo[i], r_i, lower=True)
        if h[i] >= resistance * (1 - prox):
            _touch(res_touches, i, h[i], r_i, lower=False)

        # ---------- channel-dependent position management ----------
        width = resistance - support
        if held == "CE":
            if be_at is not None and close >= support + be_at * width:
                st["be_done"] = True
                if st["stop"] < st["entry_px"]:
                    st["stop"] = st["entry_px"]
            _lot1_hit = (st["lot1_target"] is not None and close >= st["lot1_target"]) or \
                        (st["lot1_target"] is None and close >= support + lot1_pct * width)
            if not st["lot1_done"] and st["lots"] >= 2 and _lot1_hit:
                add("LOT1", i, close, 1)
                st["lots"] -= 1
                st["lot1_done"] = True
            tgt = support + zone * (resistance - support)
            if st.get("brk_target") is not None and close >= st["brk_target"] and c[i] < o[i]:
                add("TARGET", i, close, st["lots"])
                held = None
                continue
            if reversal:
                if h[i] >= tgt and _reversal_candle(i, -1):
                    add("REVERSAL", i, close, st["lots"])
                    held = None
                else:
                    st["zone_hit"] = st["zone_hit"] or h[i] >= tgt
                    if st["zone_hit"] and c[i] > o[i]:
                        st["stop"] = max(st["stop"], (o[i] + c[i]) / 2)
            elif close >= tgt and c[i] < o[i] and runner_may_exit("CE", r_i):
                add("TARGET", i, close, st["lots"])
                held = None
        elif held == "PE":
            if be_at is not None and close <= resistance - be_at * width:
                st["be_done"] = True
                if st["stop"] > st["entry_px"]:
                    st["stop"] = st["entry_px"]
            _lot1_hit = (st["lot1_target"] is not None and close <= st["lot1_target"]) or \
                        (st["lot1_target"] is None and close <= resistance - lot1_pct * width)
            if not st["lot1_done"] and st["lots"] >= 2 and _lot1_hit:
                add("LOT1", i, close, 1)
                st["lots"] -= 1
                st["lot1_done"] = True
            tgt = resistance - zone * (resistance - support)
            if st.get("brk_target") is not None and close <= st["brk_target"] and c[i] > o[i]:
                add("TARGET", i, close, st["lots"])
                held = None
                continue
            if reversal:
                if lo[i] <= tgt and _reversal_candle(i, 1):
                    add("REVERSAL", i, close, st["lots"])
                    held = None
                else:
                    st["zone_hit"] = st["zone_hit"] or lo[i] <= tgt
                    if st["zone_hit"] and c[i] < o[i]:
                        st["stop"] = min(st["stop"], (o[i] + c[i]) / 2)
            elif close <= tgt and c[i] > o[i] and runner_may_exit("PE", r_i):
                add("TARGET", i, close, st["lots"])
                held = None

        # ---------- new entry ----------
        if held is not None or not entry_open:
            continue
        if day_trades >= cfg["MAX_TRADES_DAY"]:
            continue
        if cooloff_until and index[i] < cooloff_until:
            continue

        if trend_filter and chan is not None:
            ch_prev = chan[max(0, j - 1)]
            sup_rising = ch_prev is not None and support > ch_prev[0]
            res_falling = ch_prev is not None and resistance < ch_prev[1]
        else:
            sup_rising = res_falling = True

        side = None
        r_now = -1 if rec5 is None else int(rec5[i])
        if two_candle and r_now >= 0 and records[r_now].get("lock5") == i:
            # the pattern was just completed by the entry rule at its 4th swing: trade that bounce
            m = records[r_now]["fast_c1"]
            if records[r_now]["fast_side"] == "CE":
                stop_px = min(support, lo[m:i + 1].min()) - buf
                risk = close - stop_px
                if risk > 0 and resistance - close >= cfg["TARGET_RATIO"] * risk:
                    side, div, confl = "CE", False, near_pdhl(support, i)
            else:
                stop_px = max(resistance, h[m:i + 1].max()) + buf
                risk = stop_px - close
                if risk > 0 and close - support >= cfg["TARGET_RATIO"] * risk:
                    side, div, confl = "PE", False, near_pdhl(resistance, i)
        elif two_candle:
            # candle 1 rejects at the line; candle 2 (or, with CONFIRM_WINDOW 2, candle 3) closes beyond it
            for m in range(i - 1, i - 1 - confirm_window, -1):
                if m < 1 or side is not None:
                    break
                same = rec5 is None or all(rec5[b] == rec5[i] for b in range(m, i))
                prev = chan5[m] if same else None
                if prev is None:
                    continue
                s1, r1, _ = prev
                band = touch_frac * (r1 - s1)
                rng1 = h[m] - lo[m]
                b1 = (c[m] - o[m]) / rng1 if rng1 > 0.01 else 0.0
                ce_ok = lo[m] <= s1 + band and c[m] >= s1 and b1 >= body_min and c[i] > h[m]
                pe_ok = h[m] >= r1 - band and c[m] <= r1 and -b1 >= body_min and c[i] < lo[m]
                if m == i - 2:
                    # candle 2 neither confirmed nor broke the line
                    s2, r2, _ = chan5[i - 1]
                    ce_ok = ce_ok and s2 <= c[i - 1] <= h[m]
                    pe_ok = pe_ok and lo[m] <= c[i - 1] <= r2
                if ce_ok:
                    anchor = min(support, lo[m:i + 1].min()) if stop_anchor == "swing" else support
                    stop_px = anchor - buf
                    risk = close - stop_px
                    if risk > 0 and resistance - close >= cfg["TARGET_RATIO"] * risk:
                        side = "CE"
                        div = _divergence(sup_touches, i, index, "CE")
                        confl = near_pdhl(support, i)
                elif pe_ok:
                    anchor = max(resistance, h[m:i + 1].max()) if stop_anchor == "swing" else resistance
                    stop_px = anchor + buf
                    risk = stop_px - close
                    if risk > 0 and close - support >= cfg["TARGET_RATIO"] * risk:
                        side = "PE"
                        div = _divergence(res_touches, i, index, "PE")
                        confl = near_pdhl(resistance, i)
        elif sup_rising and close >= support * (1 - prox) and lo[i - 1] <= support * (1 + prox):
            br = body_ratio(i)
            one_strong = br is not None and br >= body_min
            two_rising = c[i - 1] > c[i - 2] and c[i] > c[i - 1] and c[i] > o[i]
            if one_strong or two_rising:
                anchor = min(support, lo[i], lo[i - 1]) if stop_anchor == "swing" else support
                stop_px = anchor - buf
                risk = close - stop_px
                if risk > 0 and resistance - close >= cfg["TARGET_RATIO"] * risk:
                    side = "CE"
                    div = _divergence(sup_touches, i, index, "CE")
                    confl = near_pdhl(support, i)
        elif (
            res_falling
            and close <= resistance * (1 + prox)
            and h[i - 1] >= resistance * (1 - prox)
        ):
            br = body_ratio(i)
            one_strong = br is not None and -br >= body_min
            two_falling = c[i - 1] < c[i - 2] and c[i] < c[i - 1] and c[i] < o[i]
            if one_strong or two_falling:
                anchor = max(resistance, h[i], h[i - 1]) if stop_anchor == "swing" else resistance
                stop_px = anchor + buf
                risk = stop_px - close
                if risk > 0 and close - support >= cfg["TARGET_RATIO"] * risk:
                    side = "PE"
                    div = _divergence(res_touches, i, index, "PE")
                    confl = near_pdhl(resistance, i)

        # side filter: skip if the pattern doesn't support this direction
        if side is not None and side_filter and rec5 is not None and r_now >= 0:
            pat = records[r_now].get("pattern", "")
            if pat in _SKIP_SIDE:
                side = None
            elif pat in _PREFERRED_SIDE and _PREFERRED_SIDE[pat] != side:
                side = None

        if side is None or (gate == "rsi_div" and not div):
            continue
        held = side
        _init_risk = abs(close - stop_px)
        st = {
            "entry_px": close,
            "entry_ts": index[i],
            "stop": stop_px,
            "init_stop": stop_px,
            "zone_hit": False,
            "lots": _lots_for(size_rule, div, confl, tstr),
            "lot1_done": False,
            "be_done": False,
            "trail": None,
            "swing": [],
            "div": div,
            "confl": confl,
            "rec": None if rec5 is None else int(rec5[i]),
            "fast": rec5 is not None and records[int(rec5[i])].get("lock5") == i,
            "lot1_target": (close + lot1_risk_mult * _init_risk if held == "CE"
                            else close - lot1_risk_mult * _init_risk) if lot1_risk_mult > 0 else None,
            "brk_target": None,
        }
        day_trades += 1

    return pd.DataFrame(trades) if trades else pd.DataFrame()


# ------------------------------------------------------------------ backtest (single run with full report)
def backtest(cfg=None):
    cfg = {**CONFIG, **(cfg or {})}
    print("Loading bars from DuckDB ...", flush=True)
    df5 = load_bars("5m")
    df15 = load_bars("15m")

    if df5.empty or df15.empty:
        sys.exit("No bars in the database. Run with --download first.")

    print(f"  5m  bars: {len(df5):,}  ({df5.index[0]:%Y-%m-%d} – {df5.index[-1]:%Y-%m-%d})")
    print(f"  15m bars: {len(df15):,}  ({df15.index[0]:%Y-%m-%d} – {df15.index[-1]:%Y-%m-%d})")

    ph15, pl15 = find_pivots(df15, cfg["PIVOT_LEFT"], cfg["PIVOT_RIGHT"])
    df_trades = _simulate(df5, df15, ph15, pl15, cfg)

    if df_trades.empty:
        print("\nNo trades generated. Channel may not be forming on the downloaded data.")
        return

    df_trades["duration"] = df_trades["exit_ts"] - df_trades["entry_ts"]

    print(f"\n{'=' * 60}")
    print(f"BACKTEST RESULTS  ({df5.index[0]:%Y-%m-%d} to {df5.index[-1]:%Y-%m-%d})")
    print(f"{'=' * 60}")

    full = df_trades[df_trades["reason"].isin(FINAL_REASONS)]
    wins = full[full["pnl_pts"] > 0]
    losses = full[full["pnl_pts"] <= 0]

    print(f"\nTotal Nifty points (all legs): {df_trades['pnl_pts'].sum():+.1f}")
    print(
        f"Full-trade exits: {len(full)} | wins: {len(wins)} "
        f"({100 * len(wins) / max(len(full), 1):.0f}%) | losses: {len(losses)}"
    )
    print(f"Lot 1 exits at {FROZEN['LOT1_EXIT_PCT']:.0%} of the channel: {len(df_trades[df_trades['reason'] == 'LOT1'])}")
    if len(wins):
        print(f"  Avg win  : {wins['pnl_pts'].mean():+.1f}  max: {wins['pnl_pts'].max():+.1f}")
    if len(losses):
        print(f"  Avg loss : {losses['pnl_pts'].mean():+.1f}  max: {losses['pnl_pts'].min():+.1f}")

    print(f"\nP&L by exit reason:\n{df_trades.groupby('reason')['pnl_pts'].sum().to_string()}")
    print(f"\nP&L by side:\n{df_trades.groupby('side')['pnl_pts'].sum().to_string()}")

    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df_trades["month"] = df_trades["entry_ts"].dt.to_period("M")
    print(f"\nMonthly P&L:\n{df_trades.groupby('month')['pnl_pts'].sum().to_string()}")

    print("\nTrade log (last 20):")
    cols = ["entry_ts", "side", "entry_px", "exit_px", "pnl_pts", "reason", "duration"]
    print(df_trades[cols].tail(20).to_string(index=False))

    run_ts = datetime.now(IST)
    save_trades(run_ts, cfg, df_trades)
    save_backtest(cfg, df_trades, df5.index[0].date(), df5.index[-1].date())


# ------------------------------------------------------------------ parameter sweep
def sweep():
    """Grid search over the parameters most likely to improve results."""
    print("Loading bars for sweep ...", flush=True)
    df5 = load_bars("5m")
    df15 = load_bars("15m")
    if df5.empty or df15.empty:
        sys.exit("No bars in the database. Run with --download first.")

    # Pre-compute pivots at default settings (pivot geometry is stable)
    ph15, pl15 = find_pivots(df15, CONFIG["PIVOT_LEFT"], CONFIG["PIVOT_RIGHT"])
    feats = build_features(df5, df15, ph15, pl15, CONFIG)

    grid = {
        "PROX_PCT": [0.2, 0.3, 0.4],
        "BOUNCE_BODY_PCT": [0.50, 0.65],
        "TARGET_RATIO": [1.5, 2.0, 2.5],
        "TARGET_ZONE_PCT": [0.80, 0.92],  # how far into channel before exit
        "TREND_FILTER": [False, True],  # CE only on rising support, PE on falling resistance
    }

    # Build all combinations
    from itertools import product as iproduct

    keys = list(grid.keys())
    combos = list(iproduct(*grid.values()))
    total = len(combos)
    print(f"Running {total} combinations ...", flush=True)

    rows = []
    for n, vals in enumerate(combos, 1):
        cfg = {**CONFIG, **dict(zip(keys, vals, strict=True))}
        df_t = _simulate(df5, df15, ph15, pl15, cfg, feats)
        if df_t.empty:
            continue

        full = df_t[df_t["reason"].isin(FINAL_REASONS)]
        wins = full[full["pnl_pts"] > 0]
        losses = full[full["pnl_pts"] <= 0]
        gross_win = wins["pnl_pts"].sum() if len(wins) else 0
        gross_loss = abs(losses["pnl_pts"].sum()) if len(losses) else 1e-9
        pf = gross_win / gross_loss
        win_rate = len(wins) / max(len(full), 1) * 100
        total_pts = df_t["pnl_pts"].sum()
        max_loss = losses["pnl_pts"].min() if len(losses) else 0
        n_trades = len(full)

        rows.append(
            {
                "prox": vals[keys.index("PROX_PCT")],
                "body": vals[keys.index("BOUNCE_BODY_PCT")],
                "rr": vals[keys.index("TARGET_RATIO")],
                "zone": vals[keys.index("TARGET_ZONE_PCT")],
                "trend": "Y" if vals[keys.index("TREND_FILTER")] else "N",
                "min_entry": FROZEN["MIN_ENTRY_TIME"],
                "trades": n_trades,
                "win_rate": round(win_rate, 1),
                "pf": round(pf, 2),
                "total_pts": round(total_pts, 1),
                "max_loss": round(max_loss, 1),
            }
        )

        if n % 10 == 0:
            print(f"  {n}/{total} done ...", flush=True)

    if not rows:
        print("No trades in any combination.")
        return

    df_results = pd.DataFrame(rows)

    # Rank by profit factor (risk-adjusted), with a minimum trade count filter
    min_trades = max(10, df_results["trades"].median() * 0.5)
    ranked = (
        df_results[df_results["trades"] >= min_trades]
        .sort_values("pf", ascending=False)
        .reset_index(drop=True)
    )

    print(f"\n{'=' * 75}")
    print(f"SWEEP RESULTS — top 20 by profit factor  (min {min_trades:.0f} trades)")
    print(f"{'=' * 75}")
    print(ranked.head(20).to_string(index=False))

    print(f"\n{'=' * 75}")
    print("SWEEP RESULTS — top 20 by total points")
    print(f"{'=' * 75}")
    print(
        df_results[df_results["trades"] >= min_trades]
        .sort_values("total_pts", ascending=False)
        .head(20)
        .to_string(index=False)
    )

    # Highlight the single best row
    best = ranked.iloc[0]
    print("\n>>> Best by profit factor:")
    print(
        f"    prox={best['prox']}%  body={best['body']}  rr={best['rr']}  "
        f"zone={best['zone']}  trend_filter={best['trend']}"
    )
    print(
        f"    {best['trades']} trades  win={best['win_rate']}%  "
        f"PF={best['pf']}  total={best['total_pts']:+.1f} pts  max_loss={best['max_loss']:.1f}"
    )

    print("\nTo run the best config in detail:")
    print(
        f"  uv run python strategies/research/channel_research.py --backtest "
        f"--prox {best['prox']} --bounce-body {best['body']} "
        f"--target-ratio {best['rr']} --target-zone {best['zone']}"
        + (" --trend-filter" if best["trend"] == "Y" else "")
    )

    run_ts = datetime.now(IST)
    save_sweep(run_ts, rows)


# ------------------------------------------------------------------ CLI
# ------------------------------------------------------------------ option P&L estimate
def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot, strike, t_years, sigma, r, kind):
    """Black-Scholes price of a European CE or PE."""
    if t_years <= 0 or sigma <= 0:
        return max(spot - strike, 0.0) if kind == "CE" else max(strike - spot, 0.0)
    sq = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / sq
    d2 = d1 - sq
    disc = strike * math.exp(-r * t_years)
    if kind == "CE":
        return spot * _norm_cdf(d1) - disc * _norm_cdf(d2)
    return disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def next_week_expiry(ts):
    """Tuesday of next week at 15:30: the week after the current weekly expiry."""
    days = (1 - ts.weekday()) % 7
    current = (ts + timedelta(days=days)).replace(hour=15, minute=30, second=0, microsecond=0)
    return current + timedelta(days=7)


def load_vix():
    """Map date -> India VIX close as a fraction; empty dict when not downloaded."""
    try:
        df = load_bars("VIX_D")
    except Exception:
        return {}
    return {ts.date(): close / 100.0 for ts, close in df["close"].items()}


def estimate_option_pnl(df_t, vix, cfg):
    """Rupee P&L of buying the option (ATM moved STRIKE_ITM_STEPS strikes in the money). Adds columns."""
    year = 365.0 * 24 * 3600
    steps = cfg.get("STRIKE_ITM_STEPS", 1)
    rows = []
    for t in df_t.itertuples():
        atm = round(t.entry_px / 50.0) * 50
        strike = atm - 50 * steps if t.side == "CE" else atm + 50 * steps
        expiry = next_week_expiry(t.entry_ts)
        sigma = vix.get(t.entry_ts.date(), cfg["DEFAULT_IV"])
        t0 = (expiry - t.entry_ts).total_seconds() / year
        t1 = (expiry - t.exit_ts).total_seconds() / year
        p0 = bs_price(t.entry_px, strike, t0, sigma, cfg["RISK_FREE"], t.side)
        p1 = bs_price(t.exit_px, strike, t1, sigma, cfg["RISK_FREE"], t.side)
        gross = (p1 - p0 - 2 * cfg["SLIPPAGE_PREMIUM"]) * cfg["LOT_SIZE"] * t.lots
        rows.append((p0, p1, gross))
    out = df_t.copy()
    out["prem_entry"] = [r[0] for r in rows]
    out["prem_exit"] = [r[1] for r in rows]
    out["rupees"] = [r[2] for r in rows]
    # one entry order per trade plus one exit order per leg
    legs = out.groupby("entry_ts")["reason"].transform("size")
    out["rupees"] -= cfg["COST_PER_ORDER"] * (1 + legs) / legs
    return out


# ------------------------------------------------------------------ experiments
EXPERIMENTS = [
    ("E0", "Baseline", {}),
    ("E1", "Target zone 0.92", {"TARGET_ZONE_PCT": 0.92}),
    ("E2", "Target zone 0.95", {"TARGET_ZONE_PCT": 0.95}),
    ("E3", "No distance target, trail or EOD only", {"RUNNER_RULE": "trail_only"}),
    ("E4", "RSI hold on the runner", {"RUNNER_RULE": "rsi_hold"}),
    ("E6", "RSI divergence -> 3 lots", {"SIZE_RULE": "rsi_div"}),
    ("E7", "Prior-day H/L confluence -> 3 lots", {"SIZE_RULE": "pdhl"}),
    ("E8", "Before 09:45 -> 1 lot", {"SIZE_RULE": "morning_1lot"}),
    ("G1", "Info: RSI divergence entry gate", {"ENTRY_GATE": "rsi_div"}),
]
SIZE_EXPS = {"E6", "E7", "E8", "E9"}
EXIT_EXPS = ["E1", "E2", "E3", "E4"]


def summarize(df_t):
    """Per-entry metrics: an entry's legs are added together before win/loss is judged."""
    if df_t.empty:
        return {"entries": 0, "win_rate": 0.0, "profit_factor": 0.0, "total_pts": 0.0,
                "pts_per_lot": 0.0, "max_dd": 0.0, "worst_trade": 0.0, "est_rupees": 0.0}
    per = df_t.groupby("entry_ts").agg(pnl=("pnl_pts", "sum"), lots=("lots", "sum"))
    gw = per.loc[per.pnl > 0, "pnl"].sum()
    gl = -per.loc[per.pnl <= 0, "pnl"].sum()
    legs = df_t.sort_values("exit_ts")["pnl_pts"].cumsum().to_numpy()
    curve = np.r_[0.0, legs]
    dd = float((np.maximum.accumulate(curve) - curve).max())
    return {
        "entries": len(per),
        "win_rate": round(100.0 * (per.pnl > 0).mean(), 1),
        "profit_factor": round(gw / gl, 2) if gl > 0 else float("inf"),
        "total_pts": round(df_t["pnl_pts"].sum(), 1),
        "pts_per_lot": round(df_t["pnl_pts"].sum() / per.lots.sum(), 1),
        "max_dd": round(dd, 1),
        "worst_trade": round(per.pnl.min(), 1),
        "est_rupees": round(df_t["rupees"].sum(), 0) if "rupees" in df_t else 0.0,
    }


def _split(df_t, split):
    if df_t.empty:
        return {"FULL": df_t, "H1": df_t, "H2": df_t}
    h1 = df_t["entry_ts"] < split
    return {"FULL": df_t, "H1": df_t[h1], "H2": df_t[~h1]}


def _beats(m, base):
    return m["total_pts"] > base["total_pts"] and m["profit_factor"] > base["profit_factor"]


def run_experiments(cfg=None):
    cfg = {**CONFIG, **(cfg or {})}
    df5 = load_bars("5m")
    df15 = load_bars("15m")
    if df5.empty or df15.empty:
        sys.exit("No bars in the database. Run with --download first.")
    split = pd.Timestamp(cfg["SPLIT_DATE"], tz=IST)
    vix = load_vix()
    print(f"Data {df5.index[0]:%Y-%m-%d} to {df5.index[-1]:%Y-%m-%d}; H1 before {split:%Y-%m-%d}, "
          f"H2 from it. VIX days: {len(vix)}.")
    print(f"Settings: PROX {cfg['PROX_PCT']}  BODY {cfg['BOUNCE_BODY_PCT']}  "
          f"RR {cfg['TARGET_RATIO']}  ZONE {cfg['TARGET_ZONE_PCT']}  from {cfg['MIN_ENTRY_TIME']}")

    ph15, pl15 = find_pivots(df15, cfg["PIVOT_LEFT"], cfg["PIVOT_RIGHT"])
    feats = build_features(df5, df15, ph15, pl15, cfg)

    def run(over):
        df_t = _simulate(df5, df15, ph15, pl15, {**cfg, **over}, feats)
        return estimate_option_pnl(df_t, vix, cfg) if not df_t.empty else df_t

    results = {}
    for exp_id, name, over in EXPERIMENTS:
        df_t = run(over)
        results[exp_id] = (name, over, df_t, {k: summarize(v) for k, v in _split(df_t, split).items()})

    base = results["E0"][3]
    best_exit = max(
        (e for e in EXIT_EXPS if all(_beats(results[e][3][p], base[p]) for p in ("H1", "H2"))),
        key=lambda e: results[e][3]["FULL"]["total_pts"],
        default=None,
    )
    e9_over = {"SIZE_RULE": "combined", **(results[best_exit][1] if best_exit else {})}
    e9_name = f"E6+E7+E8 combined, exit {best_exit or 'as E0'}"
    df_t = run(e9_over)
    results["E9"] = (e9_name, e9_over, df_t, {k: summarize(v) for k, v in _split(df_t, split).items()})

    for exp_id in SIZE_EXPS:
        n = results[exp_id][3]["FULL"]["entries"]
        if exp_id != "E9" or best_exit is None:
            assert n == base["FULL"]["entries"], f"{exp_id} changed the entry count: {n}"

    order = [e for e, *_ in EXPERIMENTS] + ["E9"]
    cols = ["entries", "win_rate", "profit_factor", "total_pts", "pts_per_lot", "max_dd",
            "worst_trade", "est_rupees"]
    for period in ("FULL", "H1", "H2"):
        tbl = pd.DataFrame(
            [{"id": e, "name": results[e][0][:38], **results[e][3][period]} for e in order]
        )
        print(f"\n{'=' * 118}\n{period}\n{'=' * 118}")
        print(tbl[["id", "name", *cols]].to_string(index=False))

    print("\nVerdict (adopt only if total points and profit factor beat E0 in both H1 and H2):")
    for e in order[1:]:
        if e.startswith("G"):
            continue
        both = all(_beats(results[e][3][p], base[p]) for p in ("H1", "H2"))
        print(f"  {e}: {'ADOPT' if both else 'no'}  "
              f"H1 {results[e][3]['H1']['total_pts']:+.0f} vs {base['H1']['total_pts']:+.0f}, "
              f"H2 {results[e][3]['H2']['total_pts']:+.0f} vs {base['H2']['total_pts']:+.0f}")

    # G3: baseline by VIX regime
    e0 = results["E0"][2]
    if not e0.empty:
        per = e0.groupby("entry_ts").agg(pnl=("pnl_pts", "sum"), rupees=("rupees", "sum"))
        per["vix"] = [vix.get(ts.date(), np.nan) * 100 for ts in per.index]
        per["bucket"] = pd.cut(per["vix"], [0, 12, 16, 100], labels=["<12", "12-16", ">16"])
        g = per.groupby("bucket", observed=False).agg(
            entries=("pnl", "size"), pts=("pnl", "sum"), rupees=("rupees", "sum"),
            win_rate=("pnl", lambda s: round(100 * (s > 0).mean(), 1) if len(s) else 0),
        )
        print(f"\nG3: baseline by India VIX on the entry day\n{g.round(0).to_string()}")
        by_reason = e0.groupby("reason").agg(pts=("pnl_pts", "sum"), rupees=("rupees", "sum"))
        print(f"\nBaseline by exit reason (points vs estimated rupees)\n{by_reason.round(0).to_string()}")

    run_ts = datetime.now(IST)
    recs = [
        {"run_ts": run_ts, "exp_id": e, "name": results[e][0], "period": p, **results[e][3][p]}
        for e in order for p in ("FULL", "H1", "H2")
    ]
    df_rec = pd.DataFrame(recs).replace([np.inf], 999.0)
    db = duckdb.connect(str(CONFIG["DB_PATH"]))
    _init_result_tables(db)
    db.execute("""
        INSERT OR REPLACE INTO experiments
        SELECT run_ts, exp_id, name, period, entries, win_rate, profit_factor, total_pts,
               pts_per_lot, max_dd, worst_trade, est_rupees
        FROM df_rec
    """)
    db.close()
    print(f"\nSaved {len(df_rec)} rows to experiments (run_ts={run_ts:%Y-%m-%d %H:%M})")
    return results


ANCHORED = {
    "CHANNEL_MODE": "anchored",
    "ENTRY_RULE": "two_candle",
    "STOP_ANCHOR": "swing",
    "EXIT_RULE": "reversal",
    "BOUNCE_BODY_PCT": 0.30,
    "TARGET_RATIO": 1.5,
    "TRIANGLES": True,
    "MAX_PATTERN_SPAN": 30,  # reject channels whose 4 swings span more than 30 15m bars (~7.5 hours)
    "MAX_WIDTH_GROWTH": 4,   # kill a pattern once it is 4x its width at lock
    "TOUCH_TIMEOUT_MULT": 3, # kill a pattern if no 5% touch for 3 * pivot_span bars since last touch
}


def channel_stats(cfg=None, examples=3):
    """How many anchored channels form, how long they hold, how often price touches them."""
    cfg = {**CONFIG, **ANCHORED, **(cfg or {})}
    df5, df15 = load_bars("5m"), load_bars("15m")
    ph15, pl15 = find_pivots(df15, cfg["PIVOT_LEFT"], cfg["PIVOT_RIGHT"])
    f = build_features(df5, df15, ph15, pl15, cfg)
    recs, rec5 = f["records"], f["rec5"]
    c, h, lo = (df5[k].to_numpy() for k in ("close", "high", "low"))
    n = len(df5)
    print(f"Data {df5.index[0]:%Y-%m-%d} to {df5.index[-1]:%Y-%m-%d}: {len(recs)} channels "
          f"({len(recs) / 12:.1f} a month)")
    if not recs:
        return
    kinds = pd.Series([r["kind"] for r in recs]).value_counts().to_dict()
    trades = _simulate(df5, df15, ph15, pl15, cfg, f)
    fin = trades[trades.reason.isin(FINAL_REASONS)] if not trades.empty else trades
    trends = pd.Series([r["trend"] for r in recs]).value_counts().to_dict()
    ends = pd.Series([r["end_reason"] or "still open" for r in recs]).value_counts().to_dict()
    life = [((r["end_j"] if r["end_j"] is not None else len(df15) - 1) - r["lock_j"]) * 15 / 60 for r in recs]
    on = rec5 >= 0
    inside = np.array([on[i] and f["chan5"][i][0] <= c[i] <= f["chan5"][i][1] for i in range(n)])
    touches = []
    for r in recs:
        idx = np.where(rec5 == r["id"])[0]
        cnt, last = 0, -10
        for i in idx:
            s_, r_, _ = f["chan5"][i]
            band = cfg["TOUCH_FRAC"] * (r_ - s_)
            if (h[i] >= r_ - band or lo[i] <= s_ + band) and i - last > 3:
                cnt += 1
            if h[i] >= r_ - band or lo[i] <= s_ + band:
                last = i
        touches.append(cnt)
    print(f"  kinds {kinds}; trend {trends}; ended by {ends}")
    print(f"  life in trading hours: median {np.median(life):.1f}, 25-75% {np.percentile(life, 25):.1f}"
          f"-{np.percentile(life, 75):.1f}")
    print(f"  5m candles with a channel {100 * on.mean():.0f}%; price inside it {100 * inside[on].mean():.0f}% of those")
    print(f"  touches of either line per channel: median {np.median(touches):.0f}, total {sum(touches)}")
    tab = pd.DataFrame({"family": [r["family"] for r in recs], "pattern": [r["pattern"] for r in recs],
                        "life_h": life, "touches": touches})
    n_tr = fin.groupby("pattern").size() if not fin.empty else pd.Series(dtype=int)
    by = tab.groupby(["family", "pattern"]).agg(patterns=("life_h", "size"), median_life_h=("life_h", "median"),
                                                median_touches=("touches", "median"))
    by["trades"] = [int(n_tr.get(p, 0)) for _, p in by.index]
    print(f"\n  by pattern ({len(fin)} trades):\n{by.round(1).to_string()}")
    for r in recs[:examples]:
        kh1, kh2, kl1, kl2 = r["pivots"]
        t = df15.index
        end = f"{t[r['end_j']]:%Y-%m-%d %H:%M} ({r['end_reason']})" if r["end_j"] is not None else "open"
        print(f"\n  channel {r['id']}: {r['kind']}, {r['trend']}trend, locked at close of {t[r['lock_j']]:%Y-%m-%d %H:%M}, ended {end}")
        print(f"    H1 {t[kh1]:%m-%d %H:%M} {df15['high'].iat[kh1]:.2f}  H2 {t[kh2]:%m-%d %H:%M} {df15['high'].iat[kh2]:.2f}  "
              f"L1 {t[kl1]:%m-%d %H:%M} {df15['low'].iat[kl1]:.2f}  L2 {t[kl2]:%m-%d %H:%M} {df15['low'].iat[kl2]:.2f}")
        print(f"    resistance through x={r['res'][0]:.0f} y={r['res'][1]:.2f} slope {r['res'][2]:+.3f}/5m; "
              f"support through x={r['sup'][0]:.0f} y={r['sup'][1]:.2f} slope {r['sup'][2]:+.3f}/5m")
    return recs


def main():
    ap = argparse.ArgumentParser(description="Nifty channel strategy research")
    ap.add_argument("--download", action="store_true", help="Download fresh data")
    ap.add_argument("--backtest", action="store_true", help="Run backtest only")
    ap.add_argument("--sweep", action="store_true", help="Grid search over parameters")
    ap.add_argument("--results", action="store_true", help="Show stored backtest and sweep history")
    ap.add_argument("--experiments", action="store_true", help="Run the E0-E9 experiment set")
    ap.add_argument("--months", type=int, default=CONFIG["MONTHS_BACK"])
    ap.add_argument("--prox", type=float, default=CONFIG["PROX_PCT"], help="Proximity band pct")
    ap.add_argument("--stop-mult", type=float, default=CONFIG["STOP_RANGE_MULT"])
    ap.add_argument("--bounce-body", type=float, default=CONFIG["BOUNCE_BODY_PCT"])
    ap.add_argument("--target-ratio", type=float, default=CONFIG["TARGET_RATIO"])
    ap.add_argument(
        "--target-zone",
        type=float,
        default=0.80,
        help="Fraction of channel width for second-lot exit (0.80-0.99)",
    )
    ap.add_argument(
        "--trend-filter",
        action="store_true",
        help="CE only when support rising, PE only when resistance falling",
    )
    ap.add_argument("--pivot-left", type=int, default=CONFIG["PIVOT_LEFT"])
    ap.add_argument(
        "--channel",
        choices=["rolling", "anchored"],
        default=CONFIG["CHANNEL_MODE"],
        help="anchored = 4-swing channel held until broken, two-candle entry, stop beyond the candles",
    )
    ap.add_argument("--channel-stats", action="store_true", help="Describe the channels built")
    args = ap.parse_args()

    cfg_override = {
        "MONTHS_BACK": args.months,
        "PROX_PCT": args.prox,
        "STOP_RANGE_MULT": args.stop_mult,
        "BOUNCE_BODY_PCT": args.bounce_body,
        "TARGET_RATIO": args.target_ratio,
        "TARGET_ZONE_PCT": args.target_zone,
        "TREND_FILTER": args.trend_filter,
        "PIVOT_LEFT": args.pivot_left,
    }
    if args.channel == "anchored":
        cfg_override.update(ANCHORED)
    CONFIG.update(cfg_override)

    if args.channel_stats:
        channel_stats()
        return

    if args.results:
        show_results()
        return

    if args.experiments:
        run_experiments()
        return

    run_download = args.download or (not args.backtest and not args.sweep)
    run_bt = args.backtest
    run_sweep = args.sweep

    if run_download:
        download(months=CONFIG["MONTHS_BACK"])
    if run_bt:
        backtest()
    if run_sweep:
        sweep()
    if not run_bt and not run_sweep and not args.download:
        # default: download + backtest
        backtest()


if __name__ == "__main__":
    main()
