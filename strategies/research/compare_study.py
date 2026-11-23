#!/usr/bin/env python
"""Check that a chart study trades exactly like the Python backtest.

Runs the study over the same 5m bars with the chart's engine (run_study.mjs), then
compares, candle by candle, the channel lines, the entries and the exits.

    uv run python strategies/research/compare_study.py --mode anchored
    (the rolling study, nifty-channel-study.oscript, is historical and no longer matches)
"""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

import numpy as np

import channel_research as cr

HERE = Path(__file__).parent
STUDIES = {
    "anchored": (HERE / "nifty-channel-anchored.oscript", {**cr.ANCHORED},
                 ["CE", "PE", "Lot1", "Sell", "Stop", "Trail", "15:15", "Break", "Pattern"],
                 {"STOP": "Stop", "TRAIL": "Trail", "REVERSAL": "Sell", "TARGET": "Sell",
                  "EOD": "15:15", "LOT1": "Lot1"}),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=list(STUDIES), default="anchored")
    args = ap.parse_args()
    study, over, names, REASON = STUDIES[args.mode]
    cfg = {**cr.CONFIG, **over}

    df5, df15 = cr.load_bars("5m"), cr.load_bars("15m")
    ph, pl = cr.find_pivots(df15, cfg["PIVOT_LEFT"], cfg["PIVOT_RIGHT"])
    feats = cr.build_features(df5, df15, ph, pl, cfg)
    trades = cr._simulate(df5, df15, ph, pl, cfg, feats)

    with tempfile.TemporaryDirectory() as tmp:
        bars_path, out_path = Path(tmp) / "bars.json", Path(tmp) / "cols.json"
        bars = [{"time": int(ts.timestamp() * 1000), "open": float(r.open), "high": float(r.high),
                 "low": float(r.low), "close": float(r.close), "volume": None, "oi": None}
                for ts, r in df5.iterrows()]
        bars_path.write_text(json.dumps(bars))
        subprocess.run(["node", str(HERE / "run_study.mjs"), str(study), str(bars_path), str(out_path)],
                       check=True)
        cols = json.loads(out_path.read_text())

    idx = df5.index
    sig0 = len(cols[0]) - len(names)
    marks = {(idx[i], names[k]) for i, c in enumerate(cols) for k in range(len(names)) if c[sig0 + k] is not None}

    # channel lines on every 5m bar (the anchored study plots resistance first, support second)
    if args.mode == "anchored":
        py = feats["chan5"]
        res_os = np.array([np.nan if c[0] is None else c[0] for c in cols])
        sup_os = np.array([np.nan if c[1] is None else c[1] for c in cols])
        res_py = np.array([np.nan if ch is None else ch[1] for ch in py])
        sup_py = np.array([np.nan if ch is None else ch[0] for ch in py])
        both = ~np.isnan(res_os) & ~np.isnan(res_py)
        only_py = int((np.isnan(res_os) & ~np.isnan(res_py)).sum())
        only_os = int((~np.isnan(res_os) & np.isnan(res_py)).sum())
        dmax = float(np.nanmax(np.abs(np.r_[res_os[both] - res_py[both], sup_os[both] - sup_py[both]]))) if both.any() else 0.0
        print(f"channel on {int(both.sum())} candles in both, only backtest {only_py}, only study {only_os}, "
              f"largest line difference {dmax:.4f} pts")
        if only_py or only_os:
            first = np.where(np.isnan(res_os) != np.isnan(res_py))[0][:5]
            print("  first disagreeing candles:", [f"{idx[i]:%Y-%m-%d %H:%M}" for i in first])

    bt_e = {(e, s) for e, s in zip(trades.entry_ts, trades.side)} if not trades.empty else set()
    st_e = {m for m in marks if m[1] in ("CE", "PE")}
    bt_x = {(e, REASON[r]) for e, r in zip(trades.exit_ts, trades.reason)} if not trades.empty else set()
    st_x = {m for m in marks if m[1] in set(REASON.values())}
    print(f"entries: backtest {len(bt_e)}, study {len(st_e)}, same candle and side {len(bt_e & st_e)}")
    print(f"exits:   backtest {len(bt_x)}, study {len(st_x)}, same candle and type {len(bt_x & st_x)}")
    for label, a, b in (("only backtest", bt_e - st_e, bt_x - st_x), ("only study", st_e - bt_e, st_x - bt_x)):
        if a or b:
            print(f"  {label}: entries {sorted(a)[:4]} exits {sorted(b)[:4]}")
    if not trades.empty:
        per = trades.groupby("entry_ts")["pnl_pts"].sum()
        print(f"backtest: {len(per)} trades, win {100 * (per > 0).mean():.1f}%, {trades.pnl_pts.sum():.1f} pts")
    ok = bt_e == st_e and bt_x == st_x
    if args.mode == "anchored":
        # the pattern name on the first candle of each pattern
        rec5 = feats["rec5"]
        first = {}
        for i, r in enumerate(rec5):
            if r >= 0 and r not in first:
                first[r] = i
        k = names.index("Pattern")
        py_tags = {(idx[i], feats["records"][r]["pattern"].replace("_", " ").capitalize()) for r, i in first.items()}
        os_tags = {(idx[i], c[sig0 + k]) for i, c in enumerate(cols) if c[sig0 + k] is not None}
        print(f"pattern names: backtest {len(py_tags)}, study {len(os_tags)}, same candle and name {len(py_tags & os_tags)}")
        for label, a in (("only backtest", py_tags - os_tags), ("only study", os_tags - py_tags)):
            if a:
                print(f"  {label}: {sorted(a)[:4]}")
        ok = ok and py_tags == os_tags
    print("MATCH" if ok else "MISMATCH")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
