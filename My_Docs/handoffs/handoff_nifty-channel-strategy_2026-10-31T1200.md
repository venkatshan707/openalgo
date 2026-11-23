# Handoff: Nifty Channel Strategy

**Date:** 2026-10-31 12:00 IST
**Project:** OpenAlgo — Nifty 50 Channel-Based Intraday Options Strategy

## What we were working on

Building a fully automated intraday Nifty 50 options trading system on OpenAlgo/Kite that trades ATM options based on price-action channel bounces: BUY ATM CE when Nifty bounces from channel support, BUY ATM PE when it reverses from resistance, using next-week weekly expiry. The session ended after completing quantitative research and parameter sweep, with the live strategy still needing its full feature set (trailing stop, bounce confirmation, state persistence, scale-out).

## Decisions made

| Decision | Why |
|---|---|
| Next-week weekly expiry (not current) | Monday/Tuesday theta decay on same-week options is severe; next-week is more stable |
| Swing-point channel (not regression) | Matches how the user actually draws channels — pivotHigh/pivotLow trendlines extended forward, not a regression band |
| OpenScript for visual + Python for orders | OpenScript `leg.*` is entirely unimplemented (planned only); orders must go through the Python strategy host |
| 15-min channel + 5-min entry trigger | 15-min sets structure; 5-min times the entry — standard multi-timeframe price action |
| MIS product, exit at 15:15 | Intraday only; MIS auto-squareoff at 15:20 is the safety net |
| Stop-loss based on channel line crossing + buffer | A touch is not a reversal; buffer = max(15 pts, 0.3× avg recent candle range) |
| No trend filter | Sweep across 72 combinations showed trend filter hurt or was neutral in all cases; drop it |
| 2 lots, scale out half at channel centre | Locks in profit; trail remainder with low of last 2 green candles (CE) or high of last 2 red (PE) |
| Research database: DuckDB at `strategies/research/nifty_research.duckdb` | Persistent, queryable; historify.duckdb was empty so wrote download script |
| PROX_PCT=0.4% optimal (sweep) | Original 0.3% underperformed; 0.4% ranked best on profit factor across sweep |
| BOUNCE_BODY_PCT=0.50 optimal | Entry confirmation candle body must be ≥50% of candle range |
| TARGET_RATIO=2.5 optimal | Risk:reward 2.5× outperformed 2.0× default |

## Key findings

- **Parameter sweep (72 combinations):** Best config: PROX_PCT=0.4, BOUNCE_BODY=0.50, TARGET_RATIO=2.5
- **EOD exits = biggest P&L contributor:** +1,516 pts from end-of-day vs +662 from target exits — the 80% target zone exits too early; trades held to day end were more profitable
- **-411.5 max loss outlier:** Appears in 100% of sweep combinations — structural intrabar gap, not fixable by parameter tuning; solution is hard stop cap (max 2× buffer) checked every 20 seconds in live strategy
- **Research data:** 9,600 rows 5m + 3,200 rows 15m from 2026-04-28 to 2026-10-30 in DuckDB
- **OpenScript compiler errors fixed:**
  - `pivotHigh(leftBars, rightBars)` → needs source series: `pivotHigh(high, leftBars, rightBars)`
  - `draw.setExtend(line, "right")` → takes booleans: `draw.setExtend(line, false, true)`
- **OPENALGO_API_KEY:** Must be in `.env` file (not Windows `set`); `uv run` doesn't inherit Windows shell variables
- **Kite 60-day per-request limit:** OpenAlgo chunks automatically when downloading history

## Current state

**Done:**
- OpenScript chart study: `strategies/openscript/nifty-channel.oscript` — draws channel, marks BUY CE/PE signals; compiles and installs cleanly
- Basic Python live strategy: `strategies/nifty_channel_options.py` — polls every 20s, detects channel proximity, places ATM CE/PE orders, exits at 15:15
- Research script: `strategies/research/channel_research.py` — downloads Kite data, simulates trades, runs parameter sweep
- Research database populated: `strategies/research/nifty_research.duckdb` with 6 months of data
- Handoff skill and command created

**In progress / next steps (in priority order):**
1. Update `nifty_channel_options.py` with optimised parameters and full feature set:
   - Bounce confirmation: strong candle (body ≥ 50% of range) OR two rising closes
   - Trailing stop: track low of last 2 green candles (CE) / high of last 2 red candles (PE) on Nifty spot
   - State persistence: save/load JSON with position state; on restart verify with broker positions before trusting saved state
   - Scale-out: 2 lots, exit 1 lot at channel centre, trail remaining 1 lot to 80% target zone
   - Hard intrabar stop cap: max 2× buffer, checked every 20 seconds
2. Sandbox test: run updated strategy in analyzer mode for 1-2 weeks before going live

## Files changed or created

| File | Status | Notes |
|---|---|---|
| `strategies/openscript/nifty-channel.oscript` | Created | Swing-point channel study; compiles clean |
| `strategies/openscript/nifty-channel.oscript.program.json` | Created | Compiled program — never edit by hand |
| `strategies/nifty_channel_options.py` | Created | Basic live strategy; needs features listed above |
| `strategies/research/channel_research.py` | Created | Download + backtest + sweep script |
| `strategies/research/nifty_research.duckdb` | Created | 6 months of 5m + 15m Nifty data |
| `My_Docs/conversation_export.md` | Pre-existing | User's original context from Google AI; read-only |
| `.claude/skills/handoff/SKILL.md` | Created | This handoff skill |
| `.claude/commands/handoff.md` | Created | /handoff slash command |

## Context a new session needs to know

**Strategy logic (what the live script must implement):**
- Channel = swing-point trendlines through last 2 pivot highs (resistance) and last 2 pivot lows (support), extended forward
- Entry only if close is within PROX_PCT (0.4%) of a rail AND a bounce candle confirms: body ≥ 50% of range, or two consecutive rising closes from the rail
- Stop = channel line crossing + buffer (max(15, 0.3 × avg_range_last_5_bars))
- Hard stop cap = 2× buffer (prevents the -411pt outlier)
- 2 lots: exit lot 1 at channel centre; trail lot 2 with low of last 2 green candles (CE) or high of last 2 red (PE) — measured on Nifty spot, not options price
- Target zone = 80% of distance to opposite wall; if momentum fades there, exit
- EOD exit at 15:15; no new entries after 15:00

**Environment:**
- API key: in `.env` as `OPENALGO_API_KEY`; `uv run` picks it up automatically
- Platform must be in **Sandbox mode** (not analyzer) for sandbox testing before live
- Run from `/python` page in OpenAlgo; `OPENALGO_API_KEY` is injected automatically when launched that way

**OpenScript vocabulary gotchas:**
- `pivotHigh(high, left, right)` — source series is the FIRST argument
- `draw.setExtend(line, leftBool, rightBool)` — two booleans, not a string
- All `leg.*` functions are planned/unimplemented — do not use in strategy scripts

**Research script usage:**
```bash
cd strategies/research
uv run channel_research.py --download   # fetch fresh data
uv run channel_research.py --backtest   # run with current CONFIG
uv run channel_research.py --sweep      # 72-combination grid search
```
