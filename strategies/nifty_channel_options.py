#!/usr/bin/env python
"""Nifty 50 channel strategy: buy ATM options at the channel boundaries.

The channel is a linear regression line through the last LOOKBACK_15M closed
15-minute bars, widened by BAND_MULT population standard deviations. It is the
same arithmetic as the "Nifty Channel" OpenScript study, so the chart and this
script agree on where support and resistance are.

Each closed 5-minute bar is checked against that channel:
  close near the lower rail -> hold an ATM CE (expecting a bounce)
  close near the upper rail -> hold an ATM PE (expecting a reversal)

Options use the next weekly expiry, not the current one, to avoid the fast
time decay of an expiring contract. Product is MIS and every position is
squared off at EXIT_TIME.

Run it from /python with the platform in analyzer mode first. Orders go to
the sandbox or to the broker depending on the platform's mode, not this file.
"""

import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from openalgo import api

# ---------------------------------------------------------------- settings
STRATEGY_NAME = "NiftyChannel"
UNDERLYING = "NIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPTION_EXCHANGE = "NFO"
PRODUCT = "MIS"
LOTS = 1

LOOKBACK_15M = 50  # bars in the regression window
BAND_MULT = 1.5  # channel half-width in standard deviations
PROXIMITY_PCT = 0.3  # how close to a rail counts as touching it, in percent

ENTRY_START = "09:20"
ENTRY_END = "15:00"  # no new positions after this
EXIT_TIME = "15:15"  # square off whatever is held
POLL_SECONDS = 20

IST = ZoneInfo("Asia/Kolkata")

api_key = os.getenv("OPENALGO_API_KEY")
host = os.getenv("HOST_SERVER", "http://127.0.0.1:5000")
if not api_key:
    print("OPENALGO_API_KEY is not set. Start this script from the /python page.")
    raise SystemExit(1)

client = api(api_key=api_key, host=host)


def now_ist():
    return datetime.now(IST)


def at(hhmm, day):
    h, m = map(int, hhmm.split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def closed_bars(interval, minutes, days_back):
    """Fetch index bars and drop the one still forming."""
    today = now_ist()
    df = client.history(
        symbol=UNDERLYING,
        exchange=INDEX_EXCHANGE,
        interval=interval,
        start_date=(today - timedelta(days=days_back)).strftime("%Y-%m-%d"),
        end_date=today.strftime("%Y-%m-%d"),
    )
    if not isinstance(df, pd.DataFrame) or df.empty:
        print(f"No {interval} data returned: {df}")
        return None
    idx = df.index if df.index.tz is not None else df.index.tz_localize(IST)
    df = df.set_axis(idx.tz_convert(IST))
    return df[df.index + timedelta(minutes=minutes) <= today]


def channel(closes):
    """Upper and lower rail at the last bar, matching linreg +/- stdev * mult."""
    y = closes[-LOOKBACK_15M:].to_numpy(dtype=float)
    if len(y) < LOOKBACK_15M:
        return None
    x = np.arange(len(y))
    slope, intercept = np.polyfit(x, y, 1)
    centre = slope * x[-1] + intercept
    dev = y.std(ddof=0) * BAND_MULT
    return centre + dev, centre - dev


def next_week_expiry():
    """Second-nearest NIFTY option expiry, as DDMMMYY (e.g. 11NOV26)."""
    resp = client.expiry(symbol=UNDERLYING, exchange=OPTION_EXCHANGE, instrumenttype="options")
    if resp.get("status") != "success" or len(resp.get("data", [])) < 2:
        raise RuntimeError(f"Could not read expiries: {resp}")
    dates = sorted(datetime.strptime(e.upper(), "%d-%b-%y") for e in resp["data"])
    return dates[1].strftime("%d%b%y").upper()


def buy_atm(option_type):
    """Buy one ATM option; returns (symbol, quantity) or None on refusal."""
    expiry = next_week_expiry()
    info = client.optionsymbol(
        underlying=UNDERLYING,
        exchange=INDEX_EXCHANGE,
        expiry_date=expiry,
        offset="ATM",
        option_type=option_type,
    )
    if info.get("status") != "success":
        print(f"Could not resolve the ATM {option_type}: {info}")
        return None
    qty = int(info["lotsize"]) * LOTS
    resp = client.optionsorder(
        strategy=STRATEGY_NAME,
        underlying=UNDERLYING,
        exchange=INDEX_EXCHANGE,
        expiry_date=expiry,
        offset="ATM",
        option_type=option_type,
        action="BUY",
        quantity=qty,
        price_type="MARKET",
        product=PRODUCT,
    )
    print(f"BUY {option_type} {expiry} x{qty}: {resp}")
    if resp.get("status") != "success":
        return None
    return resp["symbol"], qty


def sell(symbol, qty):
    resp = client.placeorder(
        strategy=STRATEGY_NAME,
        symbol=symbol,
        exchange=OPTION_EXCHANGE,
        action="SELL",
        price_type="MARKET",
        product=PRODUCT,
        quantity=qty,
    )
    print(f"SELL {symbol} x{qty}: {resp}")
    return resp.get("status") == "success"


def run():
    held = None  # {"type": "CE"/"PE", "symbol": str, "qty": int}
    last_bar = None

    while True:
        try:
            now = now_ist()

            if held and now >= at(EXIT_TIME, now):
                if sell(held["symbol"], held["qty"]):
                    held = None
            if now >= at(EXIT_TIME, now) or now < at(ENTRY_START, now):
                time.sleep(POLL_SECONDS)
                continue

            bars5 = closed_bars("5m", 5, 2)
            if bars5 is None or bars5.empty or bars5.index[-1] == last_bar:
                time.sleep(POLL_SECONDS)
                continue
            last_bar = bars5.index[-1]

            bars15 = closed_bars("15m", 15, 7)
            rails = channel(bars15["close"]) if bars15 is not None else None
            if rails is None:
                print("Not enough 15-minute bars for the channel yet.")
                time.sleep(POLL_SECONDS)
                continue
            upper, lower = rails
            close = float(bars5["close"].iloc[-1])
            band = PROXIMITY_PCT / 100.0

            want = None
            if close <= lower * (1 + band):
                want = "CE"
            elif close >= upper * (1 - band):
                want = "PE"

            print(
                f"{last_bar:%H:%M} close {close:.2f}  support {lower:.2f}  "
                f"resistance {upper:.2f}  signal {want or '-'}  holding {held['symbol'] if held else '-'}"
            )

            can_enter = now < at(ENTRY_END, now)
            if want and can_enter and (held is None or held["type"] != want):
                if held and not sell(held["symbol"], held["qty"]):
                    print(
                        "Exit was refused; keeping the current position and skipping the new entry."
                    )
                else:
                    held = None
                    bought = buy_atm(want)
                    if bought:
                        held = {"type": want, "symbol": bought[0], "qty": bought[1]}

        except Exception as e:
            print(f"Error in strategy loop: {e}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    print(f"Starting {STRATEGY_NAME}: {UNDERLYING} channel, ATM options, next weekly expiry")
    run()
