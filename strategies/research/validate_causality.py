#!/usr/bin/env python
"""Truncation test: does a backtest use information it could not have had live?

For a cut bar i, the backtest is run twice on the same window start:
  REF   on data running a few days past bar i
  TRUNC on data ending at bar i, with the 15m bars rebuilt from the 5m bars
        (so the last 15m bar is the one still forming, as it would be live)
An honest backtest makes identical entries and exits up to bar i in both runs.
Any difference means a decision at or before bar i depended on later data.

    uv run python strategies/research/validate_causality.py --cuts 60
"""

import argparse
import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
PARAMS = {
    "PROX_PCT": 0.4,
    "BOUNCE_BODY_PCT": 0.50,
    "TARGET_RATIO": 2.5,
    "TARGET_ZONE_PCT": 0.80,
}
LOOKBACK_DAYS = 40
LOOKAHEAD_BARS = 300


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make15(df5):
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    return df5.resample("15min").agg(agg).dropna()


def run(mod, df5):
    cfg = {**mod.CONFIG, **PARAMS}
    df15 = make15(df5)
    ph, pl = mod.find_pivots(df15, cfg["PIVOT_LEFT"], cfg["PIVOT_RIGHT"])
    return mod._simulate(df5, df15, ph, pl, cfg)


def decisions(df_t, t_cut):
    """Legs closed by the cut bar. A position still open at the cut has no row in either run."""
    if df_t.empty:
        return set()
    return {
        (r.entry_ts, r.side, round(r.entry_px, 2), r.exit_ts, r.reason, round(r.exit_px, 2))
        for r in df_t.itertuples()
        if r.exit_ts <= t_cut
    }


def check(mod, df5, cut_positions):
    bad = 0
    examples = []
    for i in cut_positions:
        t_cut = df5.index[i]
        start = df5.index.searchsorted(t_cut - timedelta(days=LOOKBACK_DAYS))
        ref = run(mod, df5.iloc[start : i + LOOKAHEAD_BARS])
        trunc = run(mod, df5.iloc[start : i + 1])
        d_ref, d_tr = decisions(ref, t_cut), decisions(trunc, t_cut)
        if d_ref != d_tr:
            bad += 1
            if len(examples) < 3:
                examples.append((t_cut, sorted(d_ref - d_tr)[:2], sorted(d_tr - d_ref)[:2]))
    return bad, examples


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cuts", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--params", help="JSON of settings to test, e.g. a search winner")
    ap.add_argument("--new-only", action="store_true", help="skip the old backup simulator")
    args = ap.parse_args()
    if args.params:
        PARAMS.update(json.loads(args.params))

    new = _load("cr_new", HERE / "channel_research.py")
    old = _load("cr_old", HERE / "_backup" / "channel_research_2026-11-01.py")
    df5 = new.load_bars("5m")
    df15_kite = new.load_bars("15m")

    rebuilt = make15(df5)
    common = rebuilt.index.intersection(df15_kite.index)
    diff = (rebuilt.loc[common, ["open", "high", "low", "close"]] - df15_kite.loc[common, ["open", "high", "low", "close"]]).abs()
    print(f"15m rebuilt from 5m vs downloaded 15m: {len(common)} bars, max OHLC difference {diff.to_numpy().max():.2f} pts")

    rng = np.random.default_rng(args.seed)
    tstr = df5.index.strftime("%H:%M")
    eligible = np.where(tstr >= "09:30")[0]
    eligible = eligible[eligible > df5.index.searchsorted(df5.index[0] + timedelta(days=LOOKBACK_DAYS + 5))]
    eligible = eligible[eligible < len(df5) - LOOKAHEAD_BARS]

    versions = [("NEW (current)", new)] if args.new_only else [("OLD (pre-fix backup)", old), ("NEW (current)", new)]
    for label, mod in versions:
        mod.CONFIG.update(PARAMS)
        full = run(mod, df5)
        exit_pos = df5.index.get_indexer(pd.DatetimeIndex(full["exit_ts"].unique()))
        exit_pos = exit_pos[np.isin(exit_pos, eligible)]
        n_half = min(args.cuts // 2, len(exit_pos))
        cuts = np.r_[
            rng.choice(exit_pos, n_half, replace=False),
            rng.choice(eligible, args.cuts - n_half, replace=False),
        ]
        bad, ex = check(mod, df5, sorted(cuts))
        print(f"\n{label}: {bad} of {len(cuts)} cut points changed a past decision "
              f"({n_half} cuts at exit bars, {len(cuts) - n_half} random)")
        for t_cut, only_ref, only_tr in ex:
            print(f"  cut {t_cut:%Y-%m-%d %H:%M}: only with future data {only_ref}  only without {only_tr}")


if __name__ == "__main__":
    main()
