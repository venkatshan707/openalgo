#!/usr/bin/env python
"""Random parameter search with walk-forward validation for the channel strategy.

Every sampled setting is simulated once over all the data. Its trades are split
into equal time folds (default 6 folds of about 2 months). Then:

  IN-SAMPLE   best setting over all folds. Optimistic: with thousands of tries,
              something always looks good by luck.
  WALK-FORWARD for each fold k from 3 on, pick the best setting using only the
              folds before k, and record what it did in fold k. Joined together,
              these out-of-sample folds are the honest estimate of what the
              search would have earned live.

Ranking is on estimated option rupees after costs (Black-Scholes premium of the
strike STRIKE_ITM_STEPS in the money, India VIX as IV, slippage and charges), not
Nifty points.

    uv run python strategies/research/param_search.py --n 40000
    uv run python strategies/research/param_search.py --space anchored --n 6000
    uv run python strategies/research/param_search.py --report        # latest run
"""

import argparse
import json
import os
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

import duckdb
import numpy as np
import pandas as pd

import channel_research as cr

SPACE = {
    # channel construction (needs features rebuilt)
    "PIVOT_LEFT": [3, 4, 5, 6, 8],
    "PARALLEL_TOL": [0.2, 0.3, 0.5, 1.0],
    # entry
    "PROX_PCT": [0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.6],
    "BOUNCE_BODY_PCT": [0.3, 0.4, 0.5, 0.6, 0.7],
    "TARGET_RATIO": [1.0, 1.5, 2.0, 2.5, 3.0],
    "TREND_FILTER": [False, True],
    "MAX_TRADES_DAY": [1, 2, 3, 5],
    "COOLOFF_MINS": [0, 15, 30, 60],
    # stop
    "STOP_FIXED": [5, 10, 15, 25, 40],
    "STOP_RANGE_MULT": [0.2, 0.3, 0.5, 0.8, 1.2],
    "STOP_ANCHOR": ["line", "swing"],
    "BE_AT": [None, 0.3, 0.5, 0.7],
    "TRAIL": ["always", "after_be", "off"],
    # exit
    "TARGET_ZONE_PCT": [0.6, 0.7, 0.8, 0.9],
    "RUNNER_RULE": ["target", "trail_only", "rsi_hold"],
}
FEATURE_KEYS = ("PIVOT_LEFT", "PARALLEL_TOL")
BASELINE = {k: cr.CONFIG.get(k, v[0]) for k, v in SPACE.items()}
BASELINE.update({"BE_AT": 0.5, "TRAIL": "always", "STOP_ANCHOR": "line", "TREND_FILTER": False})
BASE_OVERRIDES = {}

# anchored 4-swing patterns (channels, wedges, triangles); the first six keys rebuild the patterns
ANCHORED_SPACE = {
    "PIVOT_LEFT": [3, 4, 5, 6],
    "PAR_TOL": [0.2, 0.3, 0.4],
    "FLAT_TOL": [0.10, 0.15, 0.25],
    "TRIANGLES": [True, False],
    "MIN_WIDTH_RANGES": [1.5, 2.0, 3.0],
    "MAX_PATTERN_SPAN": [0, 15, 20, 25, 30],  # 0 = no cap; others in 15m bars (~25 bars/day)
    # entry
    "TOUCH_FRAC": [0.05, 0.10, 0.15, 0.20],
    "BOUNCE_BODY_PCT": [0.2, 0.3, 0.4, 0.5],
    "TARGET_RATIO": [1.0, 1.5, 2.0, 2.5],
    "MAX_TRADES_DAY": [2, 3, 5],
    "COOLOFF_MINS": [0, 15, 30],
    # stop and exit
    "STOP_FIXED": [10, 15, 25],
    "STOP_RANGE_MULT": [0.2, 0.3, 0.6],
    "BE_AT": [None, 0.35, 0.5],
    "TRAIL": ["always", "after_be"],
    "TRAIL_N": [1, 2],
    "TARGET_ZONE_PCT": [0.6, 0.7, 0.8],
    "EXIT_RULE": ["reversal", "zone_red"],
}
_DEFAULTS = {"BE_AT": 0.5, "TRAIL": "always", "TRAIL_N": 2}
SPACES = {
    "rolling": (dict(SPACE), FEATURE_KEYS, dict(BASELINE), {}),
    "anchored": (
        ANCHORED_SPACE,
        ("PIVOT_LEFT", "PAR_TOL", "FLAT_TOL", "TRIANGLES", "MIN_WIDTH_RANGES", "MAX_PATTERN_SPAN"),
        {k: {**_DEFAULTS, **cr.CONFIG, **cr.ANCHORED}.get(k) for k in ANCHORED_SPACE},
        dict(cr.ANCHORED),
    ),
}


# values the trader froze are never searched
assert not any(set(sp) & set(cr.FROZEN) for sp, *_ in SPACES.values()), "a frozen setting is in a search space"


def use_space(name):
    """Point the module-level search space at one of SPACES (also run in every worker)."""
    global SPACE, FEATURE_KEYS, BASELINE, BASE_OVERRIDES
    SPACE, FEATURE_KEYS, BASELINE, BASE_OVERRIDES = SPACES[name]
N_FOLDS = 6
FIRST_TEST_FOLD = 2  # 0-based: folds 3..6 are tested out of sample
MIN_ENTRIES_PER_FOLD = 4  # a setting must average this many trades per fold to be picked

_DATA = {}


# ------------------------------------------------------------------ worker
def _init(df5, df15, vix, fold_of_date, space="rolling"):
    use_space(space)
    _DATA.update(df5=df5, df15=df15, vix=vix, fold=fold_of_date)


def _metrics(df_t, fold_of_date):
    out = {"entries": 0, "wins": 0, "pts": 0.0, "rupees": 0.0, "pf": 0.0, "max_dd": 0.0,
           "worst": 0.0, "stop_rupees": 0.0}
    for k in range(N_FOLDS):
        out[f"f{k + 1}_n"] = 0
        out[f"f{k + 1}_w"] = 0
        out[f"f{k + 1}_rs"] = 0.0
        out[f"f{k + 1}_gw"] = 0.0
        out[f"f{k + 1}_gl"] = 0.0
    if df_t.empty:
        return out
    per = df_t.groupby("entry_ts").agg(pts=("pnl_pts", "sum"), rs=("rupees", "sum"))
    gw, gl = per.rs[per.rs > 0].sum(), -per.rs[per.rs <= 0].sum()
    curve = np.r_[0.0, per.rs.cumsum().to_numpy()]
    out.update(
        entries=len(per),
        wins=int((per.rs > 0).sum()),
        pts=float(per.pts.sum()),
        rupees=float(per.rs.sum()),
        pf=float(gw / gl) if gl > 0 else 99.0,
        max_dd=float((np.maximum.accumulate(curve) - curve).max()),
        worst=float(per.rs.min()),
        stop_rupees=float(df_t.loc[df_t.reason.isin(("STOP", "TRAIL")), "rupees"].sum()),
    )
    folds = np.array([fold_of_date[ts.date()] for ts in per.index])
    for k in range(N_FOLDS):
        m = folds == k
        out[f"f{k + 1}_n"] = int(m.sum())
        out[f"f{k + 1}_w"] = int((per.rs.to_numpy()[m] > 0).sum())
        rs_k = per.rs.to_numpy()[m]
        out[f"f{k + 1}_rs"] = float(rs_k.sum())
        out[f"f{k + 1}_gw"] = float(rs_k[rs_k > 0].sum())
        out[f"f{k + 1}_gl"] = float(-rs_k[rs_k <= 0].sum())
    return out


def _run_group(task):
    fkey, configs = task
    feat = dict(zip(FEATURE_KEYS, fkey))
    df5, df15, vix = _DATA["df5"], _DATA["df15"], _DATA["vix"]
    cr.CONFIG.update(feat)  # a few legacy helpers read the module CONFIG
    base = {**cr.CONFIG, **BASE_OVERRIDES, **feat}
    ph, pl = cr.find_pivots(df15, base["PIVOT_LEFT"], base["PIVOT_RIGHT"])
    feats = cr.build_features(df5, df15, ph, pl, base)
    rows = []
    for cid, params in configs:
        cfg = {**base, **params}
        df_t = cr._simulate(df5, df15, ph, pl, cfg, feats)
        if not df_t.empty:
            df_t = cr.estimate_option_pnl(df_t, vix, cfg)
        rows.append({"config_id": cid, **params, **_metrics(df_t, _DATA["fold"])})
    return rows


# ------------------------------------------------------------------ search
def sample(n, seed):
    rng = np.random.default_rng(seed)
    out = [dict(BASELINE)]
    seen = {json.dumps(out[0], sort_keys=True)}
    while len(out) < n:
        p = {k: v[rng.integers(len(v))] for k, v in SPACE.items()}
        p = {k: (v.item() if hasattr(v, "item") else v) for k, v in p.items()}
        key = json.dumps(p, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def fold_map(df5):
    days = sorted(set(df5.index.date))
    edges = np.array_split(np.arange(len(days)), N_FOLDS)
    fmap = {}
    bounds = []
    for k, idx in enumerate(edges):
        for d in idx:
            fmap[days[d]] = k
        bounds.append((days[idx[0]], days[idx[-1]]))
    return fmap, bounds


def evaluate(configs, data, workers, space="rolling"):
    groups = defaultdict(list)
    for cid, p in configs:
        groups[tuple(p[k] for k in FEATURE_KEYS)].append((cid, p))
    tasks = sorted(groups.items(), key=lambda t: -len(t[1]))
    rows = []
    with ProcessPoolExecutor(workers, initializer=_init, initargs=(*data, space)) as ex:
        for done, part in enumerate(ex.map(_run_group, tasks), 1):
            rows.extend(part)
            if done % 10 == 0 or done == len(tasks):
                print(f"  {done}/{len(tasks)} channel groups, {len(rows)} settings done", flush=True)
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ selection
SELECTORS = {
    "total": "highest total rupees on the training folds",
    "worst_fold": "highest rupees in the weakest training fold",
    "total_60trades": "highest total rupees, at least 10 trades per fold (about 60 a year)",
    "quality_60trades": "highest profit factor, at least 10 trades per fold and training rupees above 0",
}


def _pick(df, train_folds, selector):
    n = sum(df[f"f{k + 1}_n"] for k in train_folds)
    rs = [df[f"f{k + 1}_rs"] for k in train_folds]
    min_n = (10 if selector.endswith("60trades") else MIN_ENTRIES_PER_FOLD) * len(train_folds)
    ok = n >= min_n
    if selector == "quality_60trades":
        if f"f{train_folds[0] + 1}_gw" not in df:
            return None
        ok &= sum(rs) > 0
        gw = sum(df[f"f{k + 1}_gw"] for k in train_folds)
        gl = sum(df[f"f{k + 1}_gl"] for k in train_folds)
        score = gw / gl.where(gl > 0, 1.0)
    elif selector == "worst_fold":
        score = pd.concat(rs, axis=1).min(axis=1)
    else:
        score = sum(rs)
    if not ok.any():
        return None
    return score[ok].idxmax()


def walk_forward(df):
    out = []
    for sel in SELECTORS:
        for k in range(FIRST_TEST_FOLD, N_FOLDS):
            i = _pick(df, list(range(k)), sel)
            if i is None:
                continue
            r = df.loc[i]
            out.append({
                "selector": sel, "test_fold": k + 1, "config_id": int(r.config_id),
                "train_rupees": float(sum(r[f"f{j + 1}_rs"] for j in range(k))),
                "test_rupees": float(r[f"f{k + 1}_rs"]),
                "test_entries": int(r[f"f{k + 1}_n"]),
                "test_wins": int(r[f"f{k + 1}_w"]),
            })
    return pd.DataFrame(out)


# ------------------------------------------------------------------ storage
def save(run_id, df, wf, meta):
    db = duckdb.connect(str(cr.CONFIG["DB_PATH"]))
    db.execute("""CREATE TABLE IF NOT EXISTS search_runs
                  (run_id VARCHAR PRIMARY KEY, run_ts TIMESTAMP, meta VARCHAR)""")
    db.execute("INSERT INTO search_runs VALUES (?, ?, ?)", [run_id, datetime.now(), json.dumps(meta)])
    for name, frame in (("search_results", df), ("search_walkforward", wf)):
        frame = frame.assign(run_id=run_id)
        if name == "search_results":
            frame["BE_AT"] = frame["BE_AT"].astype(float)
        db.register("frame", frame)
        exists = db.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]
        ).fetchone()[0]
        if exists:
            have = {r[0] for r in db.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [name]).fetchall()}
            for col, dt in frame.dtypes.items():
                if col not in have:
                    sql = ("BOOLEAN" if dt == bool else "BIGINT" if dt.kind in "iu"
                           else "DOUBLE" if dt.kind == "f" else "VARCHAR")
                    db.execute(f'ALTER TABLE {name} ADD COLUMN "{col}" {sql}')
            db.execute(f"INSERT INTO {name} BY NAME SELECT * FROM frame")
        else:
            db.execute(f"CREATE TABLE {name} AS SELECT * FROM frame")
        db.unregister("frame")
    db.close()


def load(run_id=None):
    db = duckdb.connect(str(cr.CONFIG["DB_PATH"]), read_only=True)
    if run_id is None:
        run_id = db.execute("SELECT run_id FROM search_runs ORDER BY run_ts DESC LIMIT 1").fetchone()[0]
    meta = json.loads(db.execute("SELECT meta FROM search_runs WHERE run_id = ?", [run_id]).fetchone()[0])
    df = db.execute("SELECT * FROM search_results WHERE run_id = ?", [run_id]).df()
    wf = db.execute("SELECT * FROM search_walkforward WHERE run_id = ?", [run_id]).df()
    db.close()
    return run_id, meta, df, wf


# ------------------------------------------------------------------ report
def _params(r):
    def fmt(k):
        v = r[k]
        if k == "BE_AT":
            return "off" if pd.isna(v) else f"{v:g}"
        if isinstance(v, (bool, np.bool_)):
            return str(bool(v))
        return f"{v:g}" if isinstance(v, float) else str(v)
    return ", ".join(f"{k}={fmt(k)}" for k in SPACE)


def _row(r):
    return (f"{int(r.entries):4d} trades  win {100 * r.wins / max(r.entries, 1):5.1f}%  "
            f"PF {r.pf:5.2f}  Rs {r.rupees:>10,.0f}  pts {r.pts:>8,.0f}  "
            f"max DD Rs {r.max_dd:>9,.0f}  folds Rs "
            + " ".join(f"{r[f'f{k + 1}_rs']:>8,.0f}" for k in range(N_FOLDS)))


def report(run_id, meta, df, wf):
    use_space(meta.get("space_name", "rolling"))
    fold_rs = df[[f"f{k + 1}_rs" for k in range(N_FOLDS)]]
    print(f"\nRun {run_id}: {len(df)} settings, data {meta['data_from']} to {meta['data_to']}")
    for k, (a, b) in enumerate(meta["folds"]):
        print(f"  fold {k + 1}: {a} to {b}")

    print("\nHow many settings make money (estimated option rupees after costs)")
    enough = df.entries >= MIN_ENTRIES_PER_FOLD * N_FOLDS
    print(f"  with at least {MIN_ENTRIES_PER_FOLD * N_FOLDS} trades:       {enough.sum()}")
    print(f"  profitable over 12 months:         {(enough & (df.rupees > 0)).sum()}")
    print(f"  profitable in every one of 6 folds: {(enough & (fold_rs > 0).all(axis=1)).sum()}")
    print(f"  median 12-month result:            Rs {df.loc[enough, 'rupees'].median():,.0f}")

    base = df[df.config_id == 0].iloc[0]
    print(f"\nBaseline (current settings)\n  {_row(base)}")

    print("\nIN-SAMPLE best 5 by 12-month rupees (optimistic, chosen with hindsight)")
    for _, r in df[enough].nlargest(5, "rupees").iterrows():
        print(f"  #{int(r.config_id):6d} {_row(r)}\n          {_params(r)}")

    hi = df.entries >= 75
    if hi.any():
        print("\nIN-SAMPLE best 3 with at least 75 trades (baseline trade count or more)")
        for _, r in df[hi].nlargest(3, "rupees").iterrows():
            print(f"  #{int(r.config_id):6d} {_row(r)}\n          {_params(r)}")

    print("\nWALK-FORWARD (honest): settings picked on earlier folds, scored on the next unseen fold")
    for sel, desc in SELECTORS.items():
        w = wf[wf.selector == sel]
        if w.empty:
            continue
        n, wins, rs = w.test_entries.sum(), w.test_wins.sum(), w.test_rupees.sum()
        folds = "  ".join(f"f{int(t.test_fold)} #{int(t.config_id)} Rs {t.test_rupees:,.0f}" for t in w.itertuples())
        print(f"  {sel:15s} ({desc})")
        print(f"      out of sample: {n} trades, win {100 * wins / max(n, 1):.1f}%, Rs {rs:,.0f}   [{folds}]")

    print("\nWhich levers help in general: median rupees of all settings with each value "
          "(12 months | last 6 months, folds 4-6)")
    pool = df[enough].assign(h2=df[[f"f{k + 1}_rs" for k in range(3, N_FOLDS)]].sum(axis=1))
    for k in SPACE:
        g = pool.groupby(pool[k].astype(str))
        cells = "   ".join(f"{v}: {a:>8,.0f} | {b:>8,.0f}" for v, a, b in
                           zip(g.groups, g.rupees.median(), g.h2.median()))
        print(f"  {k:16s} {cells}")

    print("\nWhat the winners share (top 1% by 12-month rupees vs all, share of settings with each value)")
    top = df[enough].nlargest(max(len(df) // 100, 20), "rupees")
    for k in SPACE:
        a = top[k].astype(str).value_counts(normalize=True)
        b = df[enough][k].astype(str).value_counts(normalize=True)
        lift = (a / b).dropna().sort_values(ascending=False)
        best = ", ".join(f"{v} {100 * a[v]:.0f}% (vs {100 * b[v]:.0f}%)" for v in lift.index[:2])
        print(f"  {k:16s} {best}")


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20000, help="number of random settings")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--workers", type=int, default=max(os.cpu_count() - 1, 1))
    ap.add_argument("--report", action="store_true", help="report the latest stored run")
    ap.add_argument("--run-id")
    ap.add_argument("--space", choices=list(SPACES), default="rolling")
    args = ap.parse_args()
    use_space(args.space)

    if args.report:
        report(*load(args.run_id))
        return

    df5, df15 = cr.load_bars("5m"), cr.load_bars("15m")
    vix = cr.load_vix()
    fmap, bounds = fold_map(df5)
    configs = list(enumerate(sample(args.n, args.seed)))
    print(f"Simulating {len(configs)} settings on {args.workers} workers...")
    t0 = datetime.now()
    df = evaluate(configs, (df5, df15, vix, fmap), args.workers, args.space)
    df = df.sort_values("config_id").reset_index(drop=True)
    wf = walk_forward(df)
    run_id = datetime.now(cr.IST).strftime("%Y%m%d_%H%M%S")
    meta = {"n": len(df), "seed": args.seed, "space_name": args.space,
            "strike_itm_steps": cr.CONFIG.get("STRIKE_ITM_STEPS", 1), "data_from": str(df5.index[0].date()),
            "data_to": str(df5.index[-1].date()), "folds": [[str(a), str(b)] for a, b in bounds],
            "space": {k: [str(x) for x in v] for k, v in SPACE.items()},
            "seconds": (datetime.now() - t0).total_seconds()}
    save(run_id, df, wf, meta)
    print(f"Done in {meta['seconds']:.0f}s; stored as run {run_id}")
    report(run_id, meta, df, wf)


if __name__ == "__main__":
    main()
