# Handoff: Nifty Channel Strategy

**Date:** 2026-11-01 00:46 IST
**Project:** OpenAlgo — Nifty 50 Channel-Based Intraday Options Strategy

## What we were working on

Testing the channel strategy on 12 months of data (2025-10-27 to 2026-10-30), with RSI
and other add-ons that change only size or exits, never entries. While doing it we found
that the earlier backtest looked into the future. With that removed, the strategy loses
money, and none of the add-ons or parameter settings fix it.

## Decisions made

| Decision | Why |
|---|---|
| Backtest uses only swing points confirmed by 3 later 15-min candles | A pivot is only known 45 min after it forms; using it earlier was the main source of the old profit |
| Backtest uses only closed 15-min candles | The forming candle's high/low is not known yet when a 5-min bar closes |
| Stops checked until 15:15, not only until 15:00 | Positions were unmanaged in the last 15 minutes |
| Stop check runs before the channel lookup | Fix for the -411.5 loss on 2026-06-12 (channel invalid all day, stop never checked) |
| New indicators change size or exits only, never entries | User does not want fewer trades; filters that block entries are reported for information only |
| Adopt a change only if it beats baseline in both halves (H1 before 2026-04-28, H2 from it) | H2 is the period the old settings were tuned on; winning only there is overfitting |
| Morning (before 09:45) entries kept | Skipping them costs trades; on equal settings it cost 144 pts, not 436 as first reported |
| Live strategy not changed and must not go live | No tested version has an edge after costs |

## Key findings

- **Old results were mostly look-ahead.** 6-month baseline: +3,066 pts with the leaks,
  +228 pts honest. Unconfirmed pivots alone account for about +2,500 pts.
- **Honest 12-month baseline (0.4 / 0.50 / 2.5):** 75 trades, 31% win rate, profit factor
  0.86, -457 Nifty pts, estimated -Rs 37,458 on ATM options. H1 -759 pts, H2 +301 pts.
- **Stops are the problem:** STOP exits lose 2,486 pts (-Rs 1.03 lakh); every other exit
  type is positive. Entries get stopped out far too often.
- **Experiments E1-E9 (size/exit only):** none makes money. E5 (no half exit) and E8
  (1 lot before 09:45) lose less in both halves, but only because they risk less.
- **RSI divergence:** fires on 11 of 75 trades; as a 3-lot rule it made results worse in
  H1. On 5-min bars an overnight gap inflates RSI at 09:15, so many "divergences" are noise.
- **Prior-day high/low confluence:** worse in H1, better in H2. Not consistent.
- **India VIX:** 12-16 was the only profitable bucket in H1 (+668) but flat in H2 (+28). No rule.
- **Honest sweep, 144 combinations:** 0 positive in rupees over 12 months, 0 positive in
  points in both halves. Best: 0.3 / 0.6 / 2.5 / stop 0.6 / no half exit, +141 pts, -Rs 13,573.
- **Rupee estimate:** Black-Scholes on the ATM strike, next-week Tuesday expiry, IV = India
  VIX that day, lot 65, Rs 40 per order, 1 premium point slippage per side. Holidays not modelled.
- Earlier statement corrected: Rs 2.19 lakh was wrong; pnl_pts already counts both lots.

## Current state

**Done:**
- 12 months of 5m, 15m and India VIX daily in `strategies/research/nifty_research.duckdb`
- Look-ahead and stop-window fixes in the research simulator (verified: the refactor
  reproduced the old numbers exactly before the fixes were applied)
- `--experiments` runner (E0-E9, G1-G3), results stored in the `experiments` table
- Trades table key fixed (half exit and runner rows no longer overwrite each other)

**Next steps (priority order):**
1. Study why stops fire: compare stopped trades with winners (distance from rail at entry,
   channel age, channel width, slope, time of day). STOP exits are where all the loss is.
2. Rethink the entry, since the confirmed-pivot channel arrives late: consider entering on
   a rail that is already confirmed by a third touch, or a wider stop beyond the swing low.
3. Only after an honest backtest is positive in both halves in rupees: update the live
   script and run it in Sandbox mode.

## Files changed or created

| File | Status | Notes |
|---|---|---|
| `strategies/research/channel_research.py` | modified | Honest simulator, features, size/exit rules, experiments, option P&L, VIX download |
| `strategies/research/nifty_research.duckdb` | modified | 12 months of bars, VIX, experiments table |
| `strategies/research/_backup/` | created | Script and database as they were before this session's changes |
| `strategies/nifty_channel_options.py` | unchanged | Do not run live |

## Context a new session needs to know

- `uv run channel_research.py --experiments` runs the whole set in about 6 seconds.
- `--backtest` now runs the honest simulator; old numbers (+3,358 etc.) cannot be reproduced
  and should not be quoted.
- The chart study (`nifty-channel.oscript`) shows historical BUY CE/PE labels; judge it
  by honest backtest results, not by how good past labels look.
- Config defaults: PROX 0.4, BODY 0.50, RR 2.5, ZONE 0.80, from 09:15, lot 65 (confirm
  with `client.optionsymbol`), split date 2026-04-28.
