# Handoff: Nifty Channel Strategy

**Date:** 2026-11-01 09:41 IST
**Project:** OpenAlgo — Nifty 50 Channel-Based Intraday Options Strategy

## What we were working on

The user asked for two things:

1. Properly prove whether the earlier profitable results were wrong.
2. A randomised or grid search over every parameter, with results stored, to find the best settings, their win rate and their profitability.

A truncation test was built for part 1 and run once. Its result is unexpected and must be explained before part 2 starts.

## Decisions made

| Decision | Why |
|---|---|
| Prove look-ahead with a truncation test, not by reasoning | An honest backtest cannot change any decision at or before bar i when data after bar i is removed. That is a direct, mechanical proof. |
| The truncated run rebuilds 15m bars from 5m bars, so its last 15m bar is the one still forming | This is what a live system sees. The rebuilt 15m bars match the downloaded ones exactly (0.00 pt difference over 6,275 bars), so this changes nothing else. |
| Both runs in each test start from the same window (40 days before the cut) | Only the future differs between them, so warmup cannot explain a difference |
| The parameter search must use walk-forward validation, not one in-sample best | 144 settings were already swept with none profitable; a larger search will find something profitable by luck. Only out-of-sample folds tell whether it is real. |
| Rank on rupees after costs, not Nifty points | Points ignore theta, slippage and charges |
| The earlier claim "entries are good, stops are the problem" is not a valid conclusion | Every strategy's losses sit in stop exits by construction. It does not show the entries have an edge. Correct this when reporting. |
| Live strategy not changed and must not go live | No tested version has an edge after costs |

## Key findings

- **Truncation test, 60 cut points each, PROX 0.4 / BODY 0.50 / RR 2.5 / zone 0.80 / from 09:15:**

  | Version | Cut points where a past decision changed |
  |---|---|
  | OLD, the pre-fix backup | 33 of 60 |
  | NEW, the current "honest" simulator | 32 of 60 |

  - The OLD failure was expected. That code reads unconfirmed pivots (`iloc[: idx + 1]`) and the forming 15m bar.
  - **The NEW failure was not expected. It means the current simulator may still use future data**, so the "honest" -457 pt / -Rs 37,458 result may also be wrong, in either direction.
  - Every difference shown so far is an entry at the cut bar itself. Exits matched in every example.
  - Not yet diagnosed. The cause may be a bug in the test rather than in the simulator.
- Previous honest results (12 months, 75 trades, 31% win rate, -457 pts, -Rs 37,458, 0 of 144 settings profitable in rupees) stand only if the NEW failure turns out to be a test artefact.

## Current state

**Done:**
- `validate_causality.py` written and run once (60 cuts per version, seed 7, about 3.7 minutes).

**Next steps (priority order):**
1. **Diagnose why the NEW simulator fails the truncation test.**
   - Take one failing cut, for example 2025-12-16 12:55 CE.
   - Print which run (REF or TRUNC) has the entry, and compare the channel (support, resistance, centre), `idx15`, `avg_range` and the pivot lists between the two runs at that bar.
   - Suspects:
     - `find_pivots` on truncated data: the pivot loop window reaches into the partial last 15m bar.
     - The partial last 15m bar entering `chan` or `idx15`.
     - A test bug: REF and TRUNC differ only in their last bars, so check `range(len(df15))` and the `i < 10` warmup.
   - Fix whichever is wrong, then rerun until NEW gives 0 of 60 (try more cuts and another seed).
2. Rerun the 12-month experiments (`--experiments`) on the fully causal simulator and record the true baseline.
3. Build the parameter search, `strategies/research/param_search.py`.
   - **Search:** random search over PIVOT_LEFT/RIGHT, PARALLEL_TOL, PROX, BODY, RR, STOP_FIXED, STOP_RANGE_MULT, TARGET_ZONE, COOLOFF, MAX_TRADES_DAY, MIN_ENTRY_TIME, HALF_EXIT, RUNNER_RULE and TREND_FILTER. Possibly add breakeven trigger level, trailing on/off and latest entry time as new config keys with today's defaults.
   - **Speed:** group configs by pivot settings so channel features are built once per group. Run them on a multiprocessing pool.
   - **Simulation:** simulate each config once over 12 months, then split its trades into 6 two-month folds.
   - **Selection:** walk-forward. Pick the best config on folds 1..k-1 and record its fold-k result for k = 3..6. The joined out-of-sample result is the honest number to report.
   - **Checks:** also report the in-sample best, clearly labelled as optimistic, and check stability by perturbing its parameters slightly.
   - **Storage:** store everything in DuckDB tables (`search_results`, `search_walkforward`).
4. Only if walk-forward out-of-sample is positive in rupees: update the live script and run it in Sandbox mode.

## Files changed or created

| File | Status | Notes |
|---|---|---|
| `strategies/research/validate_causality.py` | created | Truncation test of OLD (backup) and NEW simulators; `--cuts`, `--seed` |
| `strategies/research/channel_research.py` | unchanged this session | Simulator under test; may still contain a leak |
| `strategies/research/_backup/channel_research_2026-11-01.py` | unchanged | The pre-fix code, used as the known-leaky reference |
| `strategies/research/param_search.py` | pending | Not started; waits on step 1 |
| `strategies/nifty_channel_options.py` | unchanged | Do not run live |

## Context a new session needs to know

- **Do not quote any profit figure yet.**
  - The old +3,358 pts is look-ahead.
  - The "honest" -457 pts is now in doubt too, until the NEW simulator passes the truncation test.
- Run the test with `uv run python strategies/research/validate_causality.py --cuts 60`.
  - It loads the OLD module from `_backup/` and the NEW one from `channel_research.py`, then applies the same parameters to both.
  - It reads bars from `strategies/research/nifty_research.duckdb`: 12 months of 5m and 15m bars, plus India VIX daily.
- `swing_channel_at` reads `PARALLEL_TOL` from the module-level CONFIG, not from the cfg it is passed. A search script must set `CONFIG` in each worker before building features.
- The user wants to keep trade count and points. Report trade count beside every searched setting, and show the best setting at about the baseline trade count (around 75 a year) as well as the best overall.
- Write user-facing results in plain trader language. Use "Sandbox mode", never "paper trading". No emojis.
