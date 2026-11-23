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
    "PIVOT_RIGHT": 3,  # bars to the right (confirmation)
    "PROX_PCT": 0.4,  # % proximity to line = "touch" (optimised from sweep)
    "STOP_FIXED": 15,  # minimum stop buffer in Nifty points
    "STOP_RANGE_MULT": 0.3,  # alternative: fraction of recent avg candle range
    "BOUNCE_BODY_PCT": 0.50,  # min body/range for a "strong" candle (optimised)
    "TARGET_RATIO": 2.5,  # reward/risk needed before entry (optimised)
    "PARALLEL_TOL": 0.30,  # max channel-width change allowed before "not parallel"
    "MAX_TRADES_DAY": 3,
    "COOLOFF_MINS": 30,
    "MIN_ENTRY_TIME": "09:45",  # skip the first two 5-min bars; morning volatility trap
    "DB_PATH": Path(__file__).parent / "nifty_research.duckdb",
}

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"
ENTRY_END = "15:00"
EXIT_TIME = "15:15"


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

    for interval in ("5m", "15m"):
        print(
            f"Downloading {interval} bars from {start:%Y-%m-%d} to {end:%Y-%m-%d} ...", flush=True
        )
        df = client.history(
            symbol=CONFIG["UNDERLYING"],
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
        df["interval"] = interval

        before = db.execute("SELECT COUNT(*) FROM bars WHERE interval = ?", [interval]).fetchone()[
            0
        ]
        db.execute("""
            INSERT OR REPLACE INTO bars (interval, timestamp, open, high, low, close, volume)
            SELECT interval, timestamp, open, high, low, close, volume FROM df
        """)
        after = db.execute("SELECT COUNT(*) FROM bars WHERE interval = ?", [interval]).fetchone()[0]
        print(f"  {interval}: {after - before} new rows ({after} total)")

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


def _init_result_tables(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            run_ts     TIMESTAMPTZ,
            entry_ts   TIMESTAMPTZ,
            exit_ts    TIMESTAMPTZ,
            side       VARCHAR,
            entry_px   DOUBLE,
            exit_px    DOUBLE,
            stop_px    DOUBLE,
            pnl_pts    DOUBLE,
            reason     VARCHAR,
            prox_pct   DOUBLE,
            bounce_body DOUBLE,
            target_ratio DOUBLE,
            PRIMARY KEY (run_ts, entry_ts, side)
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
            n_trades      INTEGER,
            win_rate      DOUBLE,
            profit_factor DOUBLE,
            total_pts     DOUBLE,
            max_loss      DOUBLE,
            PRIMARY KEY (run_ts, prox_pct, bounce_body, target_ratio, target_zone, trend_filter)
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
            "stop_px", "pnl_pts", "reason", "prox_pct", "bounce_body", "target_ratio"]
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
    full = df_trades[df_trades["reason"].isin(["STOP", "TARGET", "EOD"])]
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
        "half_exits": int((df_trades["reason"] == "HALF_CTR").sum()),
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
               n_trades, win_rate, profit_factor, total_pts, max_loss
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


def swing_channel_at(ph_series, pl_series, idx):
    """
    Return (support_price, resistance_price, support_slope, resistance_slope)
    at bar `idx` using the last two confirmed pivot lows and highs.
    Returns None if not enough pivots.
    """
    ph_before = ph_series.iloc[: idx + 1].dropna()
    pl_before = pl_series.iloc[: idx + 1].dropna()
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


# ------------------------------------------------------------------ simulation core
def _simulate(df5, df15, ph15, pl15, cfg):
    """Pure simulation: returns a DataFrame of trades. No I/O."""

    trades = []
    state = {
        "held": None,
        "entry_price": None,
        "entry_ts": None,
        "touch_extreme": None,
        "stop": None,
        "lots": 0,
        "half_done": False,
        "trail_stop": None,
        "last_two_green": [],
        "last_two_red": [],
    }
    day_trades = 0
    last_trade_date = None
    cooloff_until = None
    prox_frac = cfg["PROX_PCT"] / 100.0
    target_zone = cfg.get("TARGET_ZONE_PCT", 0.80)  # fraction of channel width

    def _buf(i):
        recent = df5.iloc[max(0, i - 20) : i + 1]
        avg_r = (recent["high"] - recent["low"]).mean()
        return max(cfg["STOP_FIXED"], cfg["STOP_RANGE_MULT"] * avg_r)

    def _bounce(row):
        rng = row["high"] - row["low"]
        if rng < 0.01:
            return False
        return (row["close"] - row["open"]) / rng >= cfg["BOUNCE_BODY_PCT"]

    def _reject(row):
        rng = row["high"] - row["low"]
        if rng < 0.01:
            return False
        return (row["open"] - row["close"]) / rng >= cfg["BOUNCE_BODY_PCT"]

    def _add(reason, side, exit_px, entry_ts, entry_px, lots):
        direction = 1 if side == "CE" else -1
        trades.append(
            {
                "entry_ts": entry_ts,
                "exit_ts": ts,
                "side": side,
                "entry_px": entry_px,
                "exit_px": exit_px,
                "pnl_pts": direction * (exit_px - entry_px) * lots,
                "reason": reason,
            }
        )

    for i, (ts, row5) in enumerate(df5.iterrows()):
        if i < 10:
            continue

        tstr = ts.strftime("%H:%M")
        date = ts.date()

        if date != last_trade_date:
            day_trades = 0
            last_trade_date = date
            state["held"] = None

        min_entry = cfg.get("MIN_ENTRY_TIME", MARKET_OPEN)
        in_entry_window = min_entry <= tstr < ENTRY_END
        at_exit = tstr >= EXIT_TIME

        # ---------- forced EOD exit ----------
        if at_exit and state["held"]:
            _add(
                "EOD",
                state["held"],
                row5["close"],
                state["entry_ts"],
                state["entry_price"],
                state["lots"],
            )
            state["held"] = None
            continue

        if not in_entry_window:
            continue

        buf = _buf(i)
        close = row5["close"]

        # ---------- stop check and trailing-stop update (no channel needed) ----------
        # These must run on every bar regardless of channel validity.
        # Bug previously: `if ch is None: continue` skipped this block, leaving open
        # positions unmanaged for hours on days where the parallel-tolerance check
        # failed after entry, producing the -411-pt outlier.
        if state["held"] == "CE":
            if close < state["stop"]:
                _add("STOP", "CE", close, state["entry_ts"], state["entry_price"], state["lots"])
                state["held"] = None
                cooloff_until = ts + timedelta(minutes=cfg["COOLOFF_MINS"])
                continue

            if row5["close"] > row5["open"]:
                state["last_two_green"].append(row5["low"])
                state["last_two_green"] = state["last_two_green"][-2:]
            if len(state["last_two_green"]) == 2:
                trail = min(state["last_two_green"]) - buf
                new_trail = max(state["trail_stop"] or trail, trail)
                state["trail_stop"] = new_trail
                state["stop"] = max(state["stop"], new_trail)

        elif state["held"] == "PE":
            if close > state["stop"]:
                _add("STOP", "PE", close, state["entry_ts"], state["entry_price"], state["lots"])
                state["held"] = None
                cooloff_until = ts + timedelta(minutes=cfg["COOLOFF_MINS"])
                continue

            if row5["close"] < row5["open"]:
                state["last_two_red"].append(row5["high"])
                state["last_two_red"] = state["last_two_red"][-2:]
            if len(state["last_two_red"]) == 2:
                trail = max(state["last_two_red"]) + buf
                new_trail = min(state["trail_stop"] or trail, trail)
                state["trail_stop"] = new_trail
                state["stop"] = min(state["stop"], new_trail)

        # ---------- 15m channel (needed for half-exit, target, break-even, entries) ----------
        idx15 = df15.index.searchsorted(ts, side="right") - 1
        if idx15 < 0:
            continue
        ch = swing_channel_at(ph15, pl15, idx15)
        if ch is None:
            continue
        support, resistance, centre = ch

        # Optional trend filter: CE only when support is rising, PE only when falling
        trend_filter = cfg.get("TREND_FILTER", False)

        # ---------- channel-dependent position management ----------
        if state["held"] == "CE":
            if close >= centre and state["stop"] < state["entry_price"]:
                state["stop"] = state["entry_price"]

            if not state["half_done"] and close >= centre:
                _add("HALF_CTR", "CE", close, state["entry_ts"], state["entry_price"], 1)
                state["lots"] -= 1
                state["half_done"] = True

            tgt = support + target_zone * (resistance - support)
            if state["lots"] > 0 and close >= tgt and row5["close"] < row5["open"]:
                _add("TARGET", "CE", close, state["entry_ts"], state["entry_price"], state["lots"])
                state["held"] = None

        elif state["held"] == "PE":
            if close <= centre and state["stop"] > state["entry_price"]:
                state["stop"] = state["entry_price"]

            if not state["half_done"] and close <= centre:
                _add("HALF_CTR", "PE", close, state["entry_ts"], state["entry_price"], 1)
                state["lots"] -= 1
                state["half_done"] = True

            tgt = resistance - target_zone * (resistance - support)
            if state["lots"] > 0 and close <= tgt and row5["close"] > row5["open"]:
                _add("TARGET", "PE", close, state["entry_ts"], state["entry_price"], state["lots"])
                state["held"] = None

        # ---------- new entry ----------
        if state["held"] is not None:
            continue
        if day_trades >= cfg["MAX_TRADES_DAY"]:
            continue
        if cooloff_until and ts < cooloff_until:
            continue

        prev1 = df5.iloc[i - 1]
        prev2 = df5.iloc[i - 2]

        # Trend filter uses the slope of the support/resistance line
        # swing_channel_at returns extrapolated values; compare current vs one bar ago
        if trend_filter:
            ch_prev = swing_channel_at(ph15, pl15, max(0, idx15 - 1))
            sup_rising = ch_prev is not None and support > ch_prev[0]
            res_falling = ch_prev is not None and resistance < ch_prev[1]
        else:
            sup_rising = res_falling = True  # no filter

        # CE entry
        if (
            sup_rising
            and close >= support * (1 - prox_frac)
            and prev1["low"] <= support * (1 + prox_frac)
        ):
            one_strong = _bounce(row5)
            two_rising = (
                prev1["close"] > prev2["close"]
                and row5["close"] > prev1["close"]
                and row5["close"] > row5["open"]
            )
            if one_strong or two_rising:
                stop_px = support - buf
                risk = close - stop_px
                reward = resistance - close
                if risk > 0 and reward >= cfg["TARGET_RATIO"] * risk:
                    state.update(
                        {
                            "held": "CE",
                            "entry_price": close,
                            "entry_ts": ts,
                            "touch_extreme": prev1["low"],
                            "stop": stop_px,
                            "lots": 2,
                            "half_done": False,
                            "trail_stop": None,
                            "last_two_green": [],
                            "last_two_red": [],
                        }
                    )
                    day_trades += 1

        # PE entry
        elif (
            res_falling
            and close <= resistance * (1 + prox_frac)
            and prev1["high"] >= resistance * (1 - prox_frac)
        ):
            one_strong = _reject(row5)
            two_falling = (
                prev1["close"] < prev2["close"]
                and row5["close"] < prev1["close"]
                and row5["close"] < row5["open"]
            )
            if one_strong or two_falling:
                stop_px = resistance + buf
                risk = stop_px - close
                reward = close - support
                if risk > 0 and reward >= cfg["TARGET_RATIO"] * risk:
                    state.update(
                        {
                            "held": "PE",
                            "entry_price": close,
                            "entry_ts": ts,
                            "touch_extreme": prev1["high"],
                            "stop": stop_px,
                            "lots": 2,
                            "half_done": False,
                            "trail_stop": None,
                            "last_two_green": [],
                            "last_two_red": [],
                        }
                    )
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

    full = df_trades[df_trades["reason"].isin(["STOP", "TARGET", "EOD"])]
    wins = full[full["pnl_pts"] > 0]
    losses = full[full["pnl_pts"] <= 0]

    print(f"\nTotal Nifty points (all legs): {df_trades['pnl_pts'].sum():+.1f}")
    print(
        f"Full-trade exits: {len(full)} | wins: {len(wins)} "
        f"({100 * len(wins) / max(len(full), 1):.0f}%) | losses: {len(losses)}"
    )
    print(f"Half exits at centre: {len(df_trades[df_trades['reason'] == 'HALF_CTR'])}")
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

    grid = {
        "PROX_PCT": [0.2, 0.3, 0.4],
        "BOUNCE_BODY_PCT": [0.50, 0.65],
        "TARGET_RATIO": [1.5, 2.0, 2.5],
        "TARGET_ZONE_PCT": [0.80, 0.92],  # how far into channel before exit
        "TREND_FILTER": [False, True],  # CE only on rising support, PE on falling resistance
        "MIN_ENTRY_TIME": ["09:15", "09:45"],  # skip early-morning bars vs full session
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
        df_t = _simulate(df5, df15, ph15, pl15, cfg)
        if df_t.empty:
            continue

        full = df_t[df_t["reason"].isin(["STOP", "TARGET", "EOD"])]
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
                "min_entry": vals[keys.index("MIN_ENTRY_TIME")],
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
def main():
    ap = argparse.ArgumentParser(description="Nifty channel strategy research")
    ap.add_argument("--download", action="store_true", help="Download fresh data")
    ap.add_argument("--backtest", action="store_true", help="Run backtest only")
    ap.add_argument("--sweep", action="store_true", help="Grid search over parameters")
    ap.add_argument("--results", action="store_true", help="Show stored backtest and sweep history")
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
    ap.add_argument("--pivot-right", type=int, default=CONFIG["PIVOT_RIGHT"])
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
        "PIVOT_RIGHT": args.pivot_right,
    }
    CONFIG.update(cfg_override)

    if args.results:
        show_results()
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
