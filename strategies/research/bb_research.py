#!/usr/bin/env python
"""Bollinger Band mean-reversion strategy for NIFTY index options.

Research basis
--------------
John Bollinger (bollingerbands.com) and StockCharts chartschool document
three canonical setups:

  1. Band-Touch Mean Reversion (W-Bottom / M-Top family)
     Price touches or closes outside a band while RSI diverges (or is at an
     extreme). The statistical expectation is that 95 % of closes lie within
     ±2σ, so a band touch at an RSI extreme is an overextension and tends to
     snap back to the midline (20-period SMA). Bulkowski (thepatternsite.com)
     confirms the same edge on liquid-index charts: a touch of the lower band
     with RSI < 40 produces a profitable reversal more than half the time.
     Weakness: will lose on strongly trending days when price walks along the
     band.

  2. Band Walk (Continuation)
     Two or more consecutive 15 m candles close OUTSIDE the band + RSI is
     already extreme = the trend is strong enough to continue.  Buy CEs when
     walking above the upper band; buy PEs when walking below the lower band.
     Weakness: enters late; gamma drag on options bought into extended moves.

  3. BB Squeeze Breakout
     BandWidth at a percentile-low relative to recent history → expansion →
     trade the direction.  Hard to determine direction in advance and no clean
     mechanical stop/target, so it is left for a future study.

This script implements Setup 1 as the primary (CONFIG "SIGNAL_MODE":
"mean_reversion"), with Setup 2 as an option ("band_walk" or "both").

Multi-timeframe rules
---------------------
  15 m chart: determines the signal (BB touch + RSI threshold)
  5 m chart:  entry and exit precision

Setup (15 m, closed bar):
  CE: low <= lower_band  AND  RSI(14) < RSI_OVERSOLD
  PE: high >= upper_band AND  RSI(14) > RSI_OVERBOUGHT

  Band Walk CE: 2 consecutive 15 m closes > upper_band AND RSI > 55
  Band Walk PE: 2 consecutive 15 m closes < lower_band AND RSI < 45

Signal is valid for SIGNAL_EXPIRY_BARS subsequent 15 m bars (default 4 = 1 h).

Entry (5 m):
  First 5 m candle within 09:45–14:30 that closes beyond the prior 5 m
  candle's high (CE) or low (PE) with body >= BODY_MIN of the range.

Stop:
  CE: low of entry candle − max(STOP_FIXED, STOP_RANGE_MULT × avg_range)
  PE: high of entry candle + same

Targets:
  Lot 1 (1 lot): 15 m BB midline (SMA 20) → "LOT1" exit
  Runner (1 lot): trailing stop = low of last TRAIL_N green 5 m candles (CE)
                                   high of last TRAIL_N red  5 m candles (PE)
  Hard exit: 15:15 IST → "EOD"

P&L: Black-Scholes, India VIX as σ, ITM1 strike, 1 pt slippage/side, Rs 40/order.

Usage
-----
  uv run python strategies/research/bb_research.py              # default backtest
  uv run python strategies/research/bb_research.py --backtest
  uv run python strategies/research/bb_research.py --mode band_walk
  uv run python strategies/research/bb_research.py --mode both --rsi-oversold 35
"""

import argparse
import math
import sys
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import numpy as np
import pandas as pd

DB_PATH = Path(__file__).parent / "nifty_research.duckdb"
IST = ZoneInfo("Asia/Kolkata")

# ------------------------------------------------------------------ config
CONFIG = {
    # Bollinger Band
    "BB_PERIOD": 20,
    "BB_STD": 2.0,
    # RSI
    "RSI_PERIOD": 14,
    # Signal filters
    "RSI_OVERSOLD": 35,       # CE touch: RSI below this (stricter = 30, looser = 40)
    "RSI_OVERBOUGHT": 65,     # PE touch: RSI above this (stricter = 70, looser = 60)
    "WALK_RSI_CE": 55,        # Band-walk CE: RSI above this (trending up)
    "WALK_RSI_PE": 45,        # Band-walk PE: RSI below this (trending down)
    # 'mean_reversion' | 'band_walk' | 'both'
    "SIGNAL_MODE": "mean_reversion",
    # Recovery confirmation: require the 15m bar AFTER the band touch to close
    # back inside the band before the signal is valid.
    # True = filters out "catching a falling knife" (fewer but higher-quality signals)
    # False = signal fires immediately on the touch bar (original behaviour)
    "RECOVERY_CONFIRM": True,
    # How many 15m bars after the signal to look for 5m entry (0 = unlimited until EOD)
    "SIGNAL_EXPIRY_BARS": 3,
    # 5m entry candle
    "BODY_MIN": 0.50,
    # Stop buffer
    "STOP_FIXED": 15,
    "STOP_RANGE_MULT": 0.3,
    # Lot 1 target ('midline' = 15m SMA20 | 'none' = hold full size)
    "LOT1_TARGET": "midline",
    # When True, the LOT1 exit closes ALL remaining lots (no runner).
    # Isolates the pure mean-reversion edge to the midline.
    "MIDLINE_EXIT_ALL": False,
    # Trend filter: only take CE at the lower band when the 15m SMA is trending UP
    # (SMA[j] > SMA[j - TREND_LOOKBACK_BARS]); only take PE when trending DOWN.
    # Prevents catching a falling knife during a strong downtrend.
    # Set to False to disable.
    "TREND_FILTER": True,
    "TREND_LOOKBACK_BARS": 5,   # 5 × 15m = 75 minutes of slope
    "TREND_TOLERANCE": 0.0010,  # fraction of price: slope flatter than this = "flat" (both sides allowed)
    # Minimum R:R before entry: distance to midline must be >= TARGET_RATIO × stop distance.
    # 0 = disabled (same as the original BB setup which has no R:R gate).
    "TARGET_RATIO": 1.5,
    # Trailing stop
    "TRAIL_N": 2,
    # Timing
    "MIN_ENTRY_TIME": "09:45",
    "ENTRY_END": "14:30",
    "EXIT_TIME": "15:15",
    # Limits
    "MAX_TRADES_DAY": 2,
    "COOLOFF_MINS": 30,
    # Option P&L
    "LOT_SIZE": 65,
    "LOTS": 2,
    "STRIKE_ITM_STEPS": 1,
    "SLIPPAGE_PREMIUM": 1.0,
    "COST_PER_ORDER": 40,
    "RISK_FREE": 0.065,
    "DEFAULT_IV": 0.13,
}

# Bi-monthly periods matching the channel-strategy findings doc
PERIODS = [
    ("P1", "2025-10-27", "2025-12-24"),
    ("P2", "2025-12-26", "2026-02-24"),
    ("P3", "2026-02-25", "2026-04-30"),
    ("P4", "2026-05-04", "2026-07-02"),
    ("P5", "2026-07-03", "2026-08-31"),
    ("P6", "2026-09-01", "2026-10-30"),
]


# ------------------------------------------------------------------ data
def load_bars(interval):
    db = duckdb.connect(str(DB_PATH), read_only=True)
    df = db.execute(
        "SELECT * FROM bars WHERE interval = ? ORDER BY timestamp",
        [interval],
    ).df()
    db.close()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(IST)
    df = df.set_index("timestamp")
    return df.drop(columns=["interval"])


def load_vix():
    try:
        df = load_bars("VIX_D")
    except Exception:
        return {}
    return {ts.date(): close / 100.0 for ts, close in df["close"].items()}


# ------------------------------------------------------------------ indicators
def bollinger_bands(close, period, std):
    """Returns (sma, upper, lower) as Series aligned to close."""
    sma = close.rolling(period, min_periods=period).mean()
    sd = close.rolling(period, min_periods=period).std(ddof=0)
    return sma, sma + std * sd, sma - std * sd


def rsi(close, period):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


# ------------------------------------------------------------------ Black-Scholes P&L
def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot, strike, t_years, sigma, r, kind):
    if t_years <= 0 or sigma <= 0:
        return max(spot - strike, 0.0) if kind == "CE" else max(strike - spot, 0.0)
    sq = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma**2) * t_years) / sq
    d2 = d1 - sq
    disc = strike * math.exp(-r * t_years)
    if kind == "CE":
        return spot * _norm_cdf(d1) - disc * _norm_cdf(d2)
    return disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def next_week_expiry(ts):
    days = (1 - ts.weekday()) % 7
    cur = (ts + timedelta(days=days)).replace(hour=15, minute=30, second=0, microsecond=0)
    return cur + timedelta(days=7)


def option_rupees(entry_ts, exit_ts, entry_px, exit_px, side, lots, vix, cfg):
    """Rupee P&L for buying one option position (entry → exit)."""
    year = 365.0 * 24 * 3600
    atm = round(entry_px / 50.0) * 50
    steps = cfg["STRIKE_ITM_STEPS"]
    strike = atm - 50 * steps if side == "CE" else atm + 50 * steps
    expiry = next_week_expiry(entry_ts)
    sigma = vix.get(entry_ts.date(), cfg["DEFAULT_IV"])
    t0 = (expiry - entry_ts).total_seconds() / year
    t1 = (expiry - exit_ts).total_seconds() / year
    p0 = bs_price(entry_px, strike, t0, sigma, cfg["RISK_FREE"], side)
    p1 = bs_price(exit_px, strike, t1, sigma, cfg["RISK_FREE"], side)
    gross = (p1 - p0 - 2 * cfg["SLIPPAGE_PREMIUM"]) * cfg["LOT_SIZE"] * lots
    return gross - cfg["COST_PER_ORDER"] * 2  # entry + exit order


# ------------------------------------------------------------------ 15m signal computation
def compute_signals(df15, cfg):
    """Return a dict of arrays (one value per 15m bar) for use in simulation."""
    close = df15["close"]
    high = df15["high"]
    low = df15["low"]
    period = cfg["BB_PERIOD"]
    std = cfg["BB_STD"]

    sma, upper, lower = bollinger_bands(close, period, std)
    rsi_val = rsi(close, cfg["RSI_PERIOD"])

    mode = cfg["SIGNAL_MODE"]
    n = len(df15)
    # signal[i] is set on the bar whose candle just closed
    sig = np.full(n, "", dtype=object)

    sma_arr = sma.to_numpy()
    upper_arr = upper.to_numpy()
    lower_arr = lower.to_numpy()
    rsi_arr = rsi_val.to_numpy()
    high_arr = high.to_numpy()
    low_arr = low.to_numpy()
    close_arr = close.to_numpy()

    recovery = cfg.get("RECOVERY_CONFIRM", True)

    for i in range(period + 1, n):
        if np.isnan(sma_arr[i]):
            continue
        if mode in ("mean_reversion", "both"):
            if recovery:
                # Signal fires on bar i when bar i-1 was the band touch + RSI extreme
                # AND bar i's close recovers back inside the band.
                # This is John Bollinger's "W-bottom / M-top" confirmation:
                # the first close outside the band + RSI extreme is the setup;
                # the first close back inside is the trigger.
                prev_ce = low_arr[i - 1] <= lower_arr[i - 1] and rsi_arr[i - 1] < cfg["RSI_OVERSOLD"]
                if prev_ce and close_arr[i] > lower_arr[i]:
                    sig[i] = "CE_MR"
                    continue
                prev_pe = high_arr[i - 1] >= upper_arr[i - 1] and rsi_arr[i - 1] > cfg["RSI_OVERBOUGHT"]
                if prev_pe and close_arr[i] < upper_arr[i]:
                    sig[i] = "PE_MR"
                    continue
            else:
                # Original: signal on the touch bar itself
                if low_arr[i] <= lower_arr[i] and rsi_arr[i] < cfg["RSI_OVERSOLD"]:
                    sig[i] = "CE_MR"
                    continue
                if high_arr[i] >= upper_arr[i] and rsi_arr[i] > cfg["RSI_OVERBOUGHT"]:
                    sig[i] = "PE_MR"
                    continue
        if mode in ("band_walk", "both") and i >= period + 1:
            if (close_arr[i] > upper_arr[i] and close_arr[i - 1] > upper_arr[i - 1]
                    and rsi_arr[i] > cfg["WALK_RSI_CE"]):
                sig[i] = "CE_WALK"
            elif (close_arr[i] < lower_arr[i] and close_arr[i - 1] < lower_arr[i - 1]
                    and rsi_arr[i] < cfg["WALK_RSI_PE"]):
                sig[i] = "PE_WALK"

    # Trend direction: compare current SMA to SMA N bars ago
    lb = cfg.get("TREND_LOOKBACK_BARS", 5)
    tol = cfg.get("TREND_TOLERANCE", 0.001)
    trend = np.full(n, 0, dtype=int)  # +1 = rising, -1 = falling, 0 = flat
    for i in range(lb, n):
        if np.isnan(sma_arr[i]) or np.isnan(sma_arr[i - lb]) or sma_arr[i - lb] == 0:
            continue
        slope = (sma_arr[i] - sma_arr[i - lb]) / sma_arr[i - lb]
        if slope > tol:
            trend[i] = 1
        elif slope < -tol:
            trend[i] = -1

    return {
        "sig": sig,
        "sma": sma_arr,
        "upper": upper_arr,
        "lower": lower_arr,
        "rsi": rsi_arr,
        "trend": trend,
    }


# ------------------------------------------------------------------ simulation
def simulate(df5, df15, cfg):
    """Pure simulation: returns list of trade-leg dicts."""
    s15 = compute_signals(df15, cfg)
    sig15 = s15["sig"]
    sma15 = s15["sma"]
    trend15 = s15["trend"]
    use_trend_filter = cfg.get("TREND_FILTER", True)

    # Map each 5m bar to the last **closed** 15m bar
    # subtract 10 min to ensure the 15m bar's close time is strictly before this 5m bar's open
    idx15 = df15.index.searchsorted(df5.index - timedelta(minutes=10), side="right") - 1

    avg_range5 = (df5["high"] - df5["low"]).rolling(21, min_periods=1).mean().to_numpy()

    o5 = df5["open"].to_numpy()
    h5 = df5["high"].to_numpy()
    lo5 = df5["low"].to_numpy()
    c5 = df5["close"].to_numpy()
    n = len(df5)

    tstrs = df5.index.strftime("%H:%M").to_numpy()
    dates = np.array(df5.index.date)

    min_entry = cfg["MIN_ENTRY_TIME"]
    entry_end = cfg["ENTRY_END"]
    exit_time = cfg["EXIT_TIME"]
    body_min = cfg["BODY_MIN"]
    max_trades_day = cfg["MAX_TRADES_DAY"]
    cooloff_mins = cfg["COOLOFF_MINS"]
    trail_n = cfg["TRAIL_N"]
    expiry_bars = cfg["SIGNAL_EXPIRY_BARS"]
    total_lots = cfg["LOTS"]
    lot1_target = cfg["LOT1_TARGET"]

    held = None
    st = {}
    day_trades = 0
    last_date = None
    cooloff_until = None
    last_j_seen = -1   # tracks when a new 15m bar closes so we don't re-fire the same signal
    active_sig = None  # {"side": "CE"|"PE", "j": int, "midline": float, "type": str}

    legs = []

    def add_leg(reason, i, exit_px, lots):
        direction = 1 if held == "CE" else -1
        legs.append({
            "entry_ts": st["entry_ts"],
            "exit_ts": df5.index[i],
            "side": held,
            "entry_px": st["entry_px"],
            "exit_px": exit_px,
            "stop_px": st["init_stop"],
            "lots": lots,
            "pnl_pts": direction * (exit_px - st["entry_px"]) * lots,
            "reason": reason,
            "signal": st["signal"],
        })

    for i in range(21, n):
        tstr = tstrs[i]
        date = dates[i]

        # Day reset
        if date != last_date:
            day_trades = 0
            last_date = date
            held = None
            active_sig = None
            last_j_seen = -1

        # Hard EOD exit
        if held and tstr >= exit_time:
            add_leg("EOD", i, c5[i], st["runner_lots"])
            held = None
            continue

        j = int(idx15[i])
        if j < 0:
            continue

        # Check if the signal has expired
        if active_sig is not None and expiry_bars > 0:
            if j - active_sig["j"] > expiry_bars:
                active_sig = None

        # Pick up a new 15m signal when j advances (new 15m bar closed)
        if not held and active_sig is None and j > last_j_seen and j >= cfg["BB_PERIOD"]:
            last_j_seen = j
            sv = sig15[j]
            if sv:
                side_s = "CE" if sv.startswith("CE") else "PE"
                # Trend filter: CE only when midline is rising (or flat), PE only when falling (or flat)
                slope = trend15[j] if j < len(trend15) else 0
                trend_ok = (not use_trend_filter
                            or slope == 0
                            or (side_s == "CE" and slope >= 0)
                            or (side_s == "PE" and slope <= 0))
                if trend_ok:
                    active_sig = {"side": side_s, "j": j, "midline": sma15[j], "type": sv}

        # ---- entry search ----
        if not held:
            if active_sig is None:
                continue
            if not (min_entry <= tstr < entry_end):
                continue
            if day_trades >= max_trades_day:
                continue
            if cooloff_until is not None and df5.index[i] < cooloff_until:
                continue
            if i < 1:
                continue

            side = active_sig["side"]
            rng = h5[i] - lo5[i]
            body = (c5[i] - o5[i]) / rng if rng > 0.01 else 0.0
            buf = max(cfg["STOP_FIXED"], cfg["STOP_RANGE_MULT"] * avg_range5[i])

            target_ratio = cfg.get("TARGET_RATIO", 0)
            entered = False
            if side == "CE" and c5[i] > h5[i - 1] and body >= body_min:
                stop_px = lo5[i] - buf
                risk = c5[i] - stop_px
                target_dist = active_sig["midline"] - c5[i]
                if risk <= 0 or (target_ratio > 0 and target_dist < target_ratio * risk):
                    pass  # skip: not enough room to midline
                else:
                    entered = True
            elif side == "PE" and c5[i] < lo5[i - 1] and (-body) >= body_min:
                stop_px = h5[i] + buf
                risk = stop_px - c5[i]
                target_dist = c5[i] - active_sig["midline"]
                if risk <= 0 or (target_ratio > 0 and target_dist < target_ratio * risk):
                    pass  # skip: not enough room to midline
                else:
                    entered = True

            if entered:
                midline = active_sig["midline"]
                sig_type = active_sig["type"]
                active_sig = None   # signal consumed
                day_trades += 1
                held = side
                st = {
                    "entry_px": c5[i],
                    "entry_ts": df5.index[i],
                    "stop": stop_px,
                    "init_stop": stop_px,
                    "midline": midline,
                    "lot1_done": False,
                    "runner_lots": total_lots - 1 if lot1_target != "none" else total_lots,
                    "swing": [],
                    "trail": None,
                    "signal": sig_type,
                }
                # If LOT1 target is 'none', runner holds all lots
                if lot1_target == "none":
                    st["lot1_done"] = True

        # ---- trade management ----
        else:
            # Refresh midline from latest 15m bar
            if 0 <= j < len(sma15) and not np.isnan(sma15[j]):
                st["midline"] = sma15[j]

            close_px = c5[i]
            buf = max(cfg["STOP_FIXED"], cfg["STOP_RANGE_MULT"] * avg_range5[i])

            if held == "CE":
                # Stop check
                if close_px < st["stop"]:
                    reason = "TRAIL" if st["stop"] != st["init_stop"] else "STOP"
                    add_leg(reason, i, close_px, total_lots if not st["lot1_done"] else st["runner_lots"])
                    held = None
                    cooloff_until = df5.index[i] + timedelta(minutes=cooloff_mins)
                    continue
                # Trailing stop update (after green candles)
                if c5[i] > o5[i]:
                    st["swing"] = (st["swing"] + [lo5[i]])[-trail_n:]
                if len(st["swing"]) == trail_n:
                    trail = min(st["swing"]) - buf
                    st["trail"] = max(st["trail"] or trail, trail)
                    st["stop"] = max(st["stop"], st["trail"])
                # Lot 1 at midline
                if not st["lot1_done"] and close_px >= st["midline"]:
                    exit_lots = total_lots if cfg.get("MIDLINE_EXIT_ALL") else 1
                    add_leg("LOT1", i, close_px, exit_lots)
                    st["lot1_done"] = True
                    if cfg.get("MIDLINE_EXIT_ALL") or st["runner_lots"] <= 0:
                        held = None

            else:  # PE
                if close_px > st["stop"]:
                    reason = "TRAIL" if st["stop"] != st["init_stop"] else "STOP"
                    add_leg(reason, i, close_px, total_lots if not st["lot1_done"] else st["runner_lots"])
                    held = None
                    cooloff_until = df5.index[i] + timedelta(minutes=cooloff_mins)
                    continue
                # Trailing stop update (after red candles)
                if c5[i] < o5[i]:
                    st["swing"] = (st["swing"] + [h5[i]])[-trail_n:]
                if len(st["swing"]) == trail_n:
                    trail = max(st["swing"]) + buf
                    st["trail"] = min(st["trail"] or trail, trail)
                    st["stop"] = min(st["stop"], st["trail"])
                # Lot 1 at midline
                if not st["lot1_done"] and close_px <= st["midline"]:
                    exit_lots = total_lots if cfg.get("MIDLINE_EXIT_ALL") else 1
                    add_leg("LOT1", i, close_px, exit_lots)
                    st["lot1_done"] = True
                    if cfg.get("MIDLINE_EXIT_ALL") or st["runner_lots"] <= 0:
                        held = None

    return pd.DataFrame(legs) if legs else pd.DataFrame()


# ------------------------------------------------------------------ output
def print_results(df_t, vix, cfg):
    if df_t.empty:
        print("\nNo trades generated.")
        print("Check that the DuckDB has data: run channel_research.py --download first.")
        return

    # Add rupee P&L per leg
    rupees = []
    for row in df_t.itertuples():
        r = option_rupees(
            row.entry_ts, row.exit_ts,
            row.entry_px, row.exit_px,
            row.side, row.lots,
            vix, cfg,
        )
        rupees.append(r)
    df_t = df_t.copy()
    df_t["rupees"] = rupees

    total_rupees = df_t["rupees"].sum()

    # Count "full trade" exits for win rate (LOT1 partials are progress, not full trades)
    final = df_t[df_t["reason"].isin({"STOP", "TRAIL", "EOD"})]
    wins = final[final["pnl_pts"] > 0]
    losses = final[final["pnl_pts"] <= 0]

    # Equity curve (by entry_ts, sum all legs including LOT1 of same entry)
    eq = df_t.groupby("entry_ts")["rupees"].sum().sort_index().cumsum()
    running_max = eq.cummax()
    max_dd = (eq - running_max).min()

    date_lo = df_t["entry_ts"].min()
    date_hi = df_t["exit_ts"].max()

    print(f"\n{'=' * 65}")
    print(f"BB STRATEGY BACKTEST  ({date_lo:%Y-%m-%d} to {date_hi:%Y-%m-%d})")
    print(f"Mode: {cfg['SIGNAL_MODE']}  |  RSI thresholds: "
          f"oversold < {cfg['RSI_OVERSOLD']}  overbought > {cfg['RSI_OVERBOUGHT']}")
    print(f"{'=' * 65}")
    print(f"\nFull trade exits (stop/trail/EOD): {len(final)}")
    print(f"  Wins  : {len(wins):3d}  ({100*len(wins)/max(len(final),1):.1f}%)")
    print(f"  Losses: {len(losses):3d}")
    if len(wins):
        print(f"  Avg win  pts: {wins['pnl_pts'].mean():+.1f}  "
              f"max: {wins['pnl_pts'].max():+.1f}")
    if len(losses):
        print(f"  Avg loss pts: {losses['pnl_pts'].mean():+.1f}  "
              f"max: {losses['pnl_pts'].min():+.1f}")

    print(f"\nTotal P&L (all legs): {df_t['pnl_pts'].sum():+.1f} pts  "
          f"/ Rs {total_rupees:+,.0f}")
    print(f"Max drawdown:  Rs {max_dd:,.0f}")

    print(f"\nBy signal type:")
    for sig, grp in df_t.groupby("signal"):
        n = len(grp[grp["reason"].isin({"STOP","TRAIL","EOD"})])
        w = len(grp[(grp["reason"].isin({"STOP","TRAIL","EOD"})) & (grp["pnl_pts"] > 0)])
        print(f"  {sig:12s}: {n} trades, {100*w/max(n,1):.0f}% win, "
              f"Rs {grp['rupees'].sum():+,.0f}")

    print(f"\nBy side:")
    for side, grp in df_t.groupby("side"):
        print(f"  {side}: Rs {grp['rupees'].sum():+,.0f}")

    print(f"\nBy exit reason:")
    for reason, grp in df_t.groupby("reason"):
        print(f"  {reason:6s}: {len(grp):3d} legs, Rs {grp['rupees'].sum():+,.0f}")

    print(f"\nBy period (Rs after costs):")
    for label, start, end in PERIODS:
        mask = (
            (df_t["entry_ts"].dt.date >= pd.Timestamp(start).date())
            & (df_t["entry_ts"].dt.date <= pd.Timestamp(end).date())
        )
        sub = df_t[mask]
        n_final = len(sub[sub["reason"].isin({"STOP","TRAIL","EOD"})])
        pnl = sub["rupees"].sum()
        print(f"  {label} ({start} – {end}):  {n_final:3d} final trades,  "
              f"Rs {pnl:+,.0f}")

    print(f"\nFull trade log ({len(df_t)} legs total):")
    cols = ["entry_ts", "exit_ts", "side", "entry_px", "exit_px",
            "pnl_pts", "rupees", "reason", "signal"]
    print(df_t[cols].to_string(index=False))


# ------------------------------------------------------------------ entry point
def backtest(override=None):
    cfg = {**CONFIG, **(override or {})}

    print("Loading bars from DuckDB ...", flush=True)
    try:
        df5 = load_bars("5m")
        df15 = load_bars("15m")
    except Exception as exc:
        sys.exit(f"Cannot open DuckDB: {exc}\nRun channel_research.py --download first.")

    if df5.empty or df15.empty:
        sys.exit("No bars in the database. Run channel_research.py --download first.")

    print(f"  5m  bars: {len(df5):,}  ({df5.index[0]:%Y-%m-%d} – {df5.index[-1]:%Y-%m-%d})")
    print(f"  15m bars: {len(df15):,}  ({df15.index[0]:%Y-%m-%d} – {df15.index[-1]:%Y-%m-%d})")

    vix = load_vix()
    if not vix:
        print("  VIX data not found — using default IV of "
              f"{cfg['DEFAULT_IV']:.0%} for all days.")

    print("Running simulation ...", flush=True)
    df_t = simulate(df5, df15, cfg)
    print_results(df_t, vix, cfg)


def main():
    p = argparse.ArgumentParser(description="Bollinger Band Nifty options backtest")
    p.add_argument("--backtest", action="store_true", default=True,
                   help="Run backtest (default)")
    p.add_argument("--mode", default=None,
                   choices=["mean_reversion", "band_walk", "both"],
                   help="Signal mode (default: mean_reversion)")
    p.add_argument("--rsi-oversold", type=float, default=None,
                   help="RSI threshold for CE touch (default 40)")
    p.add_argument("--rsi-overbought", type=float, default=None,
                   help="RSI threshold for PE touch (default 60)")
    p.add_argument("--signal-expiry", type=int, default=None,
                   help="15m bars a signal stays valid (default 4)")
    p.add_argument("--body-min", type=float, default=None,
                   help="Min body/range for entry candle (default 0.50)")
    p.add_argument("--no-recovery", action="store_true",
                   help="Disable recovery-candle confirmation (original behaviour)")
    args = p.parse_args()

    override = {}
    if args.mode:
        override["SIGNAL_MODE"] = args.mode
    if args.rsi_oversold is not None:
        override["RSI_OVERSOLD"] = args.rsi_oversold
    if args.rsi_overbought is not None:
        override["RSI_OVERBOUGHT"] = args.rsi_overbought
    if args.signal_expiry is not None:
        override["SIGNAL_EXPIRY_BARS"] = args.signal_expiry
    if args.body_min is not None:
        override["BODY_MIN"] = args.body_min
    if args.no_recovery:
        override["RECOVERY_CONFIRM"] = False

    backtest(override)


if __name__ == "__main__":
    main()
