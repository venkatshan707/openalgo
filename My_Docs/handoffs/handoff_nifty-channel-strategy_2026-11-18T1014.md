# Handoff: Nifty Channel Strategy

**Date:** 2026-11-18 10:14 IST
**Project:** OpenAlgo — Nifty Channel Options Strategy (anchored 4-swing 15m channel)

## What we were working on

Investigating and fixing "dead patterns" — broadening wedge formations that widen to unrealistic widths (4,000+ pts) and never end, polluting the chart and backtest. Decided on `MAX_WIDTH_GROWTH = 4` as the single parameter to kill them, based on a systematic backtest across all candidate rules. The oscript and research defaults have not been updated yet.

## Decisions made

| Decision | Why |
|---|---|
| Use `MAX_WIDTH_GROWTH = 4` as the single dead-pattern killer | Kills the Aug 28 artifact (falling broadening wedge, 4,000+ pts wide); confirmed by growth_test and single_param_analysis. Width growth addresses structural widening; no other rule does. |
| Do NOT use `TOUCH_TIMEOUT_MULT` alongside width growth | Adding touch timeout on top of 4x width growth produces identical pattern count (239 vs 239) but worse P4 (-6,681 vs -921) — it fires prematurely on Oct 21 broadening wedge before natural break. Net negative. |
| Do NOT use `TOUCH_EXPIRE` (old skeleton) | One-shot check from lock, not rolling — does not track last touch correctly. Superseded by TOUCH_TIMEOUT_MULT. |
| `MAX_PATTERN_SPAN = 30` stays | Filters patterns at build time (pivots too far apart). Separate job from widening. Already in ANCHORED defaults. |
| Nested patterns: keep one-at-a-time rule | On 15m series, inner pattern pivots are subset of outer pivots — correlated signals, double entries. Industry standard (Lo, Mamaysky, Wang 2000) recommends most recently confirmed pattern only. |
| `TOUCH_TIMEOUT_FRAC = 0.05` added to CONFIG | Separate from TOUCH_FRAC (10%, used for entry detection). Needed to avoid changing entry logic if touch timeout is ever re-enabled. |
| Aug 28 falling broadening wedge is the main outlier | 1 pattern, 13 trades, Rs 49,000 of Rs 73,139 total (2-year baseline). Without it: Rs 24,132 on 74 trades. P6 goes from +43,073 to -5,934. All in-sample. |

## Key findings

- **Baseline (2 years, ANCHORED defaults):** 211 patterns, 87 trades, 46.0% win, Rs 73,139. P6 = +43,073 driven by single Aug 28 outlier.
- **Honest baseline (outlier excluded):** 74 trades, Rs 24,132. P2 and P4 lose money in every configuration tested.
- **MAX_WIDTH_GROWTH=4x:** 234 patterns, 77 trades, 48.1% win, Rs 34,857. Longest life: 151 bars (down from 1069). P6 = -220. The best single setting.
- **Touch timeout (3x, 5% zone):** 239 patterns, 72 trades, 45.8% win, Rs 27,025. P4 = -6,681 (worse than 4x growth alone). Aug 28 outlier still alive (it touched 5% zone regularly while growing wide).
- **Both rules together:** Identical to touch timeout alone — touch timeout fires first on every pattern. No benefit from adding width growth on top.
- **Industry research:** Width-at-lock multiplier (3-5x) is the most common approach in custom algo implementations (QuantConnect, Pine Script community). Bulkowski defines broadening formations as 5 reversal points; a 6th decides.
- **Nested patterns research:** No platform enforces one-at-a-time at detection layer. Execution layer typically does. Pivot reuse is the main risk for same-timeframe nesting.
- **TOUCH_TIMEOUT_MULT and TOUCH_TIMEOUT_FRAC code** is in channel_research.py but not set in ANCHORED defaults (both = 0, off).

## Current state

**Done:**
- `MAX_WIDTH_GROWTH` config key added to `channel_research.py` (default 0, off)
- `TOUCH_TIMEOUT_MULT` config key added (default 0, off)
- `TOUCH_TIMEOUT_FRAC = 0.05` config key added (default 0.05)
- Rolling `last_touch_j` tracker implemented in active loop (uses TOUCH_TIMEOUT_FRAC)
- `MAX_PATTERN_SPAN = 30` already set in ANCHORED defaults
- Backtest scripts in `c:\tmp\oscheck\`: growth_test.py, touch_timeout_test.py, single_param_analysis.py, combined_test.py
- Thorough analysis completed: 4x is the right value, touch timeout hurts more than it helps at 5% zone

**In progress / next steps (priority order):**
1. Set `ANCHORED["MAX_WIDTH_GROWTH"] = 4` in `channel_research.py`
2. Add `maxGrowth` input (default 4) + `lockW` widening break to `nifty-channel-anchored.oscript`
3. Compile and install the oscript via `node .claude/skills/openscript/validate.mjs <file> --install`
4. Verify: compare_study.py MATCH, look-ahead test (0 of 80), confirm Nov 4 rising broadening wedge ends by Nov 9
5. Optionally: add MAX_WIDTH_GROWTH to param_search ANCHORED_SPACE and re-run walk-forward search
6. Update `My_Docs/nifty_channel_findings.md` with 2-year findings and the outlier analysis

## Files changed or created

| File | Status | Notes |
|---|---|---|
| `strategies/research/channel_research.py` | modified | Added MAX_WIDTH_GROWTH, TOUCH_TIMEOUT_MULT, TOUCH_TIMEOUT_FRAC to CONFIG; rolling last_touch_j tracker; TOUCH_TIMEOUT_MULT logic in active loop |
| `c:\tmp\oscheck\growth_test.py` | created | Tests MAX_WIDTH_GROWTH at 0/2/3/4/6x |
| `c:\tmp\oscheck\touch_timeout_test.py` | created | Tests TOUCH_TIMEOUT_MULT at 0/2/3/4/5x with 5% zone |
| `c:\tmp\oscheck\single_param_analysis.py` | created | Strips Aug 28 outlier, compares both parameters head-to-head |
| `c:\tmp\oscheck\combined_test.py` | created | Tests all 4 combinations: off/growth-only/timeout-only/both |
| `strategies/research/nifty-channel-anchored.oscript` | pending | Needs maxGrowth input + lockW widening break added, then recompile+install |
| `strategies/research/channel_research.py` ANCHORED dict | pending | Needs `MAX_WIDTH_GROWTH: 4` set |

## Context a new session needs to know

**The Aug 28 outlier is the number one distortion.** Any headline result showing Rs 70k+ over 2 years is that one pattern. The honest 2-year figure without it is Rs 24k on 74 trades. Always exclude it when comparing parameter settings.

**MAX_WIDTH_GROWTH = 4 is confirmed and approved.** The user agreed. Only the implementation in ANCHORED defaults and the oscript remains.

**Do NOT change `strategies/nifty_channel_options.py`** — the live script. Never. Any improvements go to sandbox first, only after positive out-of-sample result.

**Frozen parameters** (never tune, never change): MIN_ENTRY_TIME=09:45, ENTRY_END=14:30, PIVOT_RIGHT=3, LOT1_EXIT_PCT=0.80, candle-2 confirmation beyond candle 1's high/low, no HALF_EXIT.

**The oscript install process:** never write into `strategies/openscript/` by hand. Always: (1) edit the .oscript source in `strategies/research/`, (2) `node .claude/skills/openscript/validate.mjs <file> --install`. Both .oscript and .program.json are written together only on PASSED.

**x-coordinates in channel_at:** `x15 = (df5.index.searchsorted(df15.index) + 1).astype(float)`. Do not use np.arange — it gives wrong values.

**Walk-forward run 20261101_101457** is the stored rolling-mode baseline. The anchored search has not been run yet. It is planned (Part B of the main plan) but blocked until pattern tags (Part A: channel/wedge/triangle naming) are implemented.

**The user trades ITM1 strike** (one step in the money). ATM - 50 for CE, ATM + 50 for PE. `STRIKE_ITM_STEPS = 1` is planned but not yet implemented in channel_research.py.

**P2 loses in every configuration tested.** This is a structural fold issue, not a parameter problem. Do not try to fix P2 by adding parameters.
