# Handoff: Nifty Channel Strategy

**Date:** 2026-11-18 10:24 IST
**Project:** OpenAlgo — Nifty Channel Options Strategy (anchored 4-swing 15m channel)

## What we were working on

Fixing "dead patterns" — broadening wedge formations that widen to unrealistic widths and never end, polluting the chart and backtest. Implemented and installed two dead-pattern rules: `MAX_WIDTH_GROWTH=4` (structural widening killer) and `TOUCH_TIMEOUT_MULT=3` (price-abandonment killer). Both rules are now live in the Python backtest defaults and in the installed oscript indicator, fully in sync.

## Decisions made

| Decision | Why |
|---|---|
| `MAX_WIDTH_GROWTH = 4` added to ANCHORED defaults | Kills patterns that grow to 4x their width at lock. The Aug 28 falling broadening wedge grew to 4,000+ pts and produced 13 artificial trades (Rs 49k of Rs 73k total). 4x is industry standard (3-5x range). |
| `TOUCH_TIMEOUT_MULT = 3` added to ANCHORED defaults | Kills patterns where price stops visiting within 5% of either line for 3 × pivot_span bars since last touch. Addresses price abandonment — different problem from structural widening. |
| `TOUCH_TIMEOUT_FRAC = 0.05` as a separate config key | Kept separate from `TOUCH_FRAC` (10%, used for entry detection) so touch-timeout band does not affect entry logic. |
| When both rules are on, touch timeout fires first on most patterns | Width-growth rarely triggers because touch timeout expires the pattern first. Combined result is identical to touch-timeout alone (239 patterns, 72 trades). This was confirmed by backtest before implementation. |
| Nested patterns: keep one-at-a-time rule | On 15m series, inner pattern pivots are subset of outer pivots — correlated signals, double entries. Industry research (Lo, Mamaysky, Wang 2000) recommends most recently confirmed pattern. Same-timeframe nesting is unreliable. |
| `MAX_PATTERN_SPAN = 30` stays | Filters patterns at build time (pivots too far apart). Different job from widening rules. Already in ANCHORED defaults. |
| Oscript: `pivSpan` at lock computed as `pivSpan` local var, stored as `lockedPivSpan` | `pivSpan` is computed inside `if not active` block at build time; needed a persistent copy. `lockW` computed from `nrX/nrY/nrS/nsX/nsY/nsS` (not `wNow` which is inner-block scoped). |

## Key findings

- **Aug 28 outlier confirmed:** 1 falling broadening wedge pattern, 13 trades, Rs 49,000 of Rs 73,139 2-year total. Without it: Rs 24,132 on 74 trades. P6 goes from +43,073 to -5,934. This is the main distortion in any headline result.
- **With both rules (final ANCHORED defaults):** 239 patterns, 72 trades, 45.8% win, Rs 27,025. Longest life: 139 bars (down from 1069 without any rules). P6 = -1,988.
- **Nov 4 rising broadening wedge** ends Nov 8 by touch_timeout (3 × 20 bars = 60 bar timeout). No longer runs to Nov 17.
- **Touch timeout fires first** on every pattern that width-growth would have caught. The two rules are complementary in design but in practice touch_timeout dominates.
- **Industry research confirmed:** width-at-lock multiplier (3-5x) is standard in custom algo implementations. No universal platform standard exists. Bulkowski: broadening formations need 5 reversal points.
- **P2 loses in every configuration tested** — structural fold issue, not a parameter problem.
- **Compile confirmed PASSED, 23 inputs** (was 21; 2 new: `maxGrowth`, `touchTOmult`).

## Current state

**Done:**
- `channel_research.py` CONFIG: `MAX_WIDTH_GROWTH`, `TOUCH_TIMEOUT_MULT`, `TOUCH_TIMEOUT_FRAC` added
- `channel_research.py` ANCHORED: `MAX_WIDTH_GROWTH=4`, `TOUCH_TIMEOUT_MULT=3` set as defaults
- Rolling `last_touch_j` tracker implemented in active loop (uses `TOUCH_TIMEOUT_FRAC=0.05`)
- `nifty-channel-anchored.oscript`: `maxGrowth=4`, `touchTOmult=3` inputs added; `lockW`, `lastTouchI15`, `lockedPivSpan` state vars; widening break; touch timeout check; teardown reset — compiled PASSED, installed
- Both rules in sync between Python backtest and oscript chart indicator
- Backtest scripts in `c:\tmp\oscheck\`: growth_test.py, touch_timeout_test.py, single_param_analysis.py, combined_test.py

**In progress / next steps (priority order):**
1. Visual verification on chart: restart the study at /trading, confirm Nov 4 wedge ends by Nov 9 and Aug 28 wedge is not visible in Oct-Nov
2. Look-ahead test: 0 of 80 future-data violations on two seeds (not yet run)
3. compare_study.py MATCH check (not yet run for the new rules)
4. Continue main plan: Part 0 (ITM1 strike), Part A (pattern tags: channel/wedge/triangle naming), Part B (walk-forward parameter search)

## Files changed or created

| File | Status | Notes |
|---|---|---|
| `strategies/research/channel_research.py` | modified | CONFIG: added MAX_WIDTH_GROWTH, TOUCH_TIMEOUT_MULT, TOUCH_TIMEOUT_FRAC; ANCHORED: set both to 4 and 3; active loop: lockW storage, last_touch_j tracker, touch_timeout_mult check |
| `strategies/research/nifty-channel-anchored.oscript` | modified | Added maxGrowth + touchTOmult inputs; lockW/lastTouchI15/lockedPivSpan state; widening break; touch timeout; teardown reset |
| `strategies/openscript/nifty-channel-anchored.oscript` | installed | Compiled and installed via validate.mjs --install |
| `strategies/openscript/nifty-channel-anchored.oscript.program.json` | installed | Compiled program, written alongside source |
| `c:\tmp\oscheck\growth_test.py` | created | Tests MAX_WIDTH_GROWTH at 0/2/3/4/6x |
| `c:\tmp\oscheck\touch_timeout_test.py` | created | Tests TOUCH_TIMEOUT_MULT at 0/2/3/4/5x with 5% zone |
| `c:\tmp\oscheck\single_param_analysis.py` | created | Strips Aug 28 outlier, compares both parameters head-to-head |
| `c:\tmp\oscheck\combined_test.py` | created | Tests all 4 combinations: off/growth-only/timeout-only/both |

## Context a new session needs to know

**The Aug 28 outlier is the number one distortion.** Any headline result showing Rs 70k+ over 2 years is that one pattern. The honest 2-year figure without it is Rs 24k on 74 trades. Always exclude it when comparing parameter settings.

**Both dead-pattern rules are now ON by default.** The combined result (239 patterns, 72 trades, Rs 27,025) is what a new session will see as the ANCHORED baseline — not the old 211 patterns / 87 trades / Rs 73,139.

**Do NOT change `strategies/nifty_channel_options.py`** — the live script. Never. Any improvements go to sandbox first, only after positive out-of-sample result.

**Frozen parameters** (never tune, never change): MIN_ENTRY_TIME=09:45, ENTRY_END=14:30, PIVOT_RIGHT=3, LOT1_EXIT_PCT=0.80, candle-2 confirmation beyond candle 1's high/low, no HALF_EXIT.

**The oscript install process:** never write into `strategies/openscript/` by hand. Always: (1) edit the .oscript source in `strategies/research/`, (2) `node .claude/skills/openscript/validate.mjs <file> --install`. Both .oscript and .program.json are written together only on PASSED.

**x-coordinates in channel_at:** `x15 = (df5.index.searchsorted(df15.index) + 1).astype(float)`. Do not use np.arange — it gives wrong values.

**oscript scoping:** variables assigned inside an `if` block are not in scope outside it. `wNow` (assigned inside `if ok`) cannot be used at the lock point. Use `(nrY + nrS * (x15 - nrX)) - (nsY + nsS * (x15 - nsX))` instead — the same calculation, using variables that are in scope.

**Walk-forward run 20261101_101457** is the stored rolling-mode baseline. The anchored search (Part B of main plan) has not been run yet — blocked until pattern tags (Part A) are implemented.

**The user trades ITM1 strike** (one step in the money). ATM - 50 for CE, ATM + 50 for PE. `STRIKE_ITM_STEPS = 1` is planned (Part 0 of main plan) but not yet implemented.

**P2 loses in every configuration tested.** Structural fold issue, not a parameter problem.

**Main plan remaining work (from plan file):** Part 0 (ITM1 strike in backtest), Part A (pattern tags: channel/wedge/triangle/broadening naming in `_try_build`), Part B (anchored walk-forward search in param_search.py).
