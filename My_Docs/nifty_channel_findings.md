# Nifty channel strategy: research findings

Last updated: 2026-11-01 18:29 IST

**Data:** NIFTY 5m and 15m candles, 2025-10-27 to 2026-10-30.

**Rupee figures:**
- Estimated option P&L after costs: Black-Scholes premium, India VIX as the implied volatility, 1 point slippage a side, Rs 40 an order.
- Strike: ITM1 (one 50-point strike in the money).
- The old 40,000-setting run used the ATM strike.

**Periods:** the year is split into six two-month periods:

| Period | From | To |
|---|---|---|
| P1 | 2025-10-27 | 2025-12-24 |
| P2 | 2025-12-26 | 2026-02-24 |
| P3 | 2026-02-25 | 2026-04-30 |
| P4 | 2026-05-04 | 2026-07-02 |
| P5 | 2026-07-03 | 2026-08-31 |
| P6 | 2026-09-01 | 2026-10-30 |

## 0. Frozen settings (set by the trader on 2026-11-02)

These are fixed. They are not tuned or searched, and they change only when the trader explicitly asks. In code they live in `FROZEN` in `strategies/research/channel_research.py`:
- the backtest refuses any other value;
- the search spaces in `param_search.py` cannot contain them;
- the chart study has them as constants, not inputs.

| Setting | Frozen value | Meaning |
|---|---|---|
| `MIN_ENTRY_TIME` | 09:45 | No entry before 09:45 (09:30 was tried on 2026-11-02 and reverted by the trader: it added 2 trades that lost about Rs 10k) |
| `ENTRY_END` | 14:30 | No entry after 14:30 |
| `PIVOT_RIGHT` | 3 | A 15m swing is confirmed by 3 later 15m candles |
| `LOT1_EXIT_PCT` | 0.80 | Lot 1 is sold on a close 80% of the way across the channel |
| Candle confirmation | beyond candle 1's high or low | CE: candle 2 closes above candle 1's high. PE: candle 2 closes below candle 1's low. The looser "beyond candle 1's close" option was removed |

Also on 2026-11-02:
- **The "book 1 lot at the middle line" rule was removed** (`HALF_EXIT` no longer exists).
- **Lot 1 now exits at 80% of the channel.**
- **Lot 2 keeps the existing rules:**
  - stop to entry at the middle line;
  - reversal-candle sell in the zone;
  - zone trail and trailing stop;
  - 15:15 exit.

The old rolling-channel study (`nifty-channel-study.oscript`) predates these changes and no longer matches the backtest. It is historical.

**Result with the frozen settings** (anchored, triangles on, 12 months, ITM1, after costs):
- Tried and reverted, first entry 09:30: 48 trades, 41.7% win, +Rs 37,952, max drawdown Rs 25,466.
  - By period: +5.8k, -10.5k, -7.3k, -6.4k, +6.0k, +50.3k.
- Frozen, first entry 09:45: 46 trades, 43.5% win, +Rs 48,327, max drawdown Rs 15,042.
  - The 2 extra trades between 09:30 and 09:45 cost about Rs 10k, mostly in P3.
  - Under the 09:45 setting, trades ended 30 at the trailing stop, 10 at 15:15 and 6 at the stop.
- Lot 1 reaches 80% only rarely (2 trades).

This is in-sample: the frozen values were chosen on this same year.

## 0b. Tested and rejected on 2026-11-02: two looser entry ideas

Both are in the backtest as options, off by default (`CONFIRM_WINDOW`, `FAST_SWING`). Same 12 months, frozen settings, ITM1, after costs.

| Run | Trades | Win | Rupees | Max drawdown | By period |
|---|---|---|---|---|---|
| Today's rules | 46 | 43.5% | +48,327 | 15,042 | +2.7k, -10.5k, +7.5k, -6.4k, +6.0k, +49.0k |
| Idea 1: candle 2 or candle 3 may close beyond candle 1 | 53 | 37.7% | +25,430 | 27,304 | -4.6k, -10.7k, +2.2k, -10.6k, +0.1k, +49.0k |
| Idea 2: the 4th swing confirmed by the 5m entry rule (no 45-minute wait) | 61 | 42.6% | +22,598 | 39,729 | +4.4k, -13.2k, -1.6k, -15.9k, -0.1k, +49.0k |
| Both | 68 | 41.2% | +20,792 | 39,321 | +0.2k, -12.7k, -0.9k, -9.7k, -5.1k, +49.0k |

- **Idea 1:** adds 9 entries and drops 2. The 9 new trades won 1 of 9, -Rs 15,842. A bounce that needs a third candle is usually a weak one.
- **Idea 2:** draws 62 of 104 patterns early.
  - Its 30 trades at the 4th-swing bounce won 9 and lost 21, -Rs 44,115. A brand-new low is often not the low yet.
  - Its other 31 trades made +Rs 66,713. Drawing the channel earlier but not trading the 4th swing itself may be worth a separate test. That observation was picked out after the fact, so it is not a result.
- **Verdict:** both raise the trade count but lower quality in 4 to 5 of the 6 periods. Today's rules stay.

## 0d. Experiment results on 2 years of data (2026-11-18)

Backtest: anchored channels, ITM1, frozen settings, 2024-11-04 to 2026-11-17 (37,751 5m bars).
Periods P1–P12 are two-month blocks; none of this is out of sample.

| Experiment | Trades | Win% | Rs | MaxDD | Notes |
|---|---|---|---|---|---|
| Base (frozen settings) | 72 | 45.8 | +8,980 | 29,460 | 2-year baseline |
| E1: Side filter | 47 | 48.9 | **+35,931** | **14,617** | Drops 25 trades, almost 4x the Rs, half the drawdown |
| E2: Lot 1 at 1.5x risk | 72 | 45.8 | -24,572 | 41,876 | Worse: lot 1 often exits too early at stop-distance, before the move pays |
| E2: Lot 1 at 2.0x risk | 72 | 45.8 | -13,661 | 37,234 | Still worse than base |
| E3: Breakout entries | 77 | 45.5 | +11,237 | 27,203 | 5 extra trades, +Rs 2,257; 40% win |
| E1+E2 (1.5x risk) | 47 | 48.9 | -1,519 | 23,607 | Side filter + risk target: risk target kills the gain |
| E1+E2+E3 | 52 | 48.1 | +737 | 21,350 | All three: near zero |

By period (Rs), P1 to P12:

| Experiment | P1 | P2 | P3 | P4 | P5 | P6 | P7 | P8 | P9 | P10 | P11 | P12 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Base | +19k | +1.9k | -2.2k | -7.7k | -2.9k | +1.6k | +5.6k | -11k | +10.9k | -14.5k | +7.8k | +2.8k |
| E1 side filter | +14.9k | +1.9k | +0.3k | -3.2k | -2.9k | +5.5k | +4.7k | -12.2k | +12.4k | -1.4k | +7.8k | +10.4k |
| E3 breakout | +19k | -3k | +4k | -7.7k | -2.9k | +2.5k | +5.6k | -11k | +10.9k | -14.5k | +7.8k | +2.8k |

Breakout trades (E3): 5 trades, 2 won, +Rs 2,257. 1 EOD exit (+Rs 2,919), 4 trail stops (-Rs 662).

**What this means for each experiment:**

1. **E1 side filter: strong result.** 25 fewer trades but Rs 27k more profit and the drawdown halved. The dropped trades are the wrong-side ones (rising broadening wedge CE — 8 trades, +3 won, -Rs 3.3k; channel_flat, wedges, triangles — net losing). The two big winning patterns (falling broadening wedge PE, channel_up CE) are exactly what Bulkowski recommends. Worth adopting once confirmed on out-of-sample periods.

2. **E2 risk-multiple lot 1: clearly worse.** Replacing the 80% channel target with a 1.5x or 2x risk target loses Rs 33k to 24k versus base. The initial risk (stop distance) is too small relative to where the trade actually travels. The frozen 80%-of-channel target is better calibrated for these patterns than a risk multiple.

3. **E3 breakout entries: too few to judge.** Only 5 trades in 2 years. The two-hour window and the strong-body requirement are very tight. The concept is not disproved but not confirmed either. Needs a looser entry filter or a longer window to generate enough trades for a verdict.

## 0c. Gaps compared with common trader practice (review of 2026-11-02, nothing changed)

Based on today's 46 trades under the frozen settings, plus Bulkowski (thepatternsite.com) and Zerodha Varsity.

1. **The profit target does not fit an intraday trade.**
   - The median channel at entry is 305 pts wide; the median NIFTY day range is 204 pts.
   - Lot 1 at 80% of the channel was reached in 2 of 46 trades.
   - The median best move after entry was 42 pts, against a median initial risk of 23 pts (about 1.8x); 36 of 46 trades reached 1x risk.
   - The reward/risk entry check measures to the far line, so it almost never filters.
   - Intraday practice is to target multiples of risk, or the next level.
   - `LOT1_EXIT_PCT` is frozen, so testing a risk-multiple target needs the trader's approval.
2. **We trade both sides of every shape; Bulkowski trades one side.**
   - **His tactics:**
     - channels: with the slope only;
     - descending (falling) broadening wedge: short at the top;
     - ascending broadening wedge: long at the bottom;
     - narrowing wedges: he avoids them.
   - **Our trades by pattern and side:**

     | Pattern | Side | Trades | Won | Rupees |
     |---|---|---|---|---|
     | Falling broadening wedge | PE at the top | 23 | 12 | +51,972 |
     | Channel up | CE at support | 6 | 4 | +23,949 |
     | Rising broadening wedge | CE at the bottom | 8 | 3 | -3,300 |
     | Everything else | | 9 | 1 | -24,294 |

   - Against-slope trades: 5 trades, 1 won, -Rs 7,023.
   - Small samples, but the two big winners match his tactics.
3. **Missing setup: breakouts and retests.**
   - After our 86 breaks, price ran a median 55 pts in the break direction within 2 hours, against 42 pts the other way. 15 of the breaks ran a full channel width.
   - Bulkowski: 58% of breakouts throw back to the line, and 65% of those resume. Wedges and triangles are breakout patterns.
   - This is the literature-backed way to add trades.
4. **Sizing:** fixed 2 lots. Varsity recommends lots sized from a fixed rupee risk divided by the stop distance, plus a daily loss limit.
5. **No event-day rules** (RBI policy, Budget, Fed). IV rises before events and drops after, which hurts an option buyer. Expiry Tuesdays were not worse in our data (10 trades, +Rs 9.5k).
6. **Rupee results are estimates.** They come from Black-Scholes with India VIX, not real ITM1 option prices, spreads or skew. Real historical option prices would make them trustworthy.
7. **Not supported by our data:**
   - Entries within 30 pts of the prior-day high or low did worse: 13 trades, 23% win, -Rs 8.8k.
   - Fridays were weak (14 trades, -Rs 25k), but that is likely noise.
8. **Still only 12 months of data.**

## 1. Results that changed the verdict

| Finding | Effect |
|---|---|
| The old backtest used swing points before the candles that confirm them had closed (look-ahead) | Its profits were not real. Fixed; the cut-off test now shows 0 of 80 changed decisions |
| The old rolling channel (last two swings, rebuilt at every swing) was not the trader's method | -457 pts and -Rs 37,458 a year. 0 of 40,000 settings held up on unseen months (about -Rs 1.3 lakh) |
| Anchored method: 4 swings, held until a two-candle 15m break | Pattern on 40% of candles, price inside it 90% of that time. The chart study matches the backtest candle for candle |
| Every pattern is touched, but most touches fail candle 1's body rule or candle 2's follow-through | 24 to 30 of about 90 patterns get a trade |
| Trades rarely get far across the pattern | The median trade gets 17% of the way; about 1 in 10 reach the middle line. Most end at the trailing stop or at 15:15 |

## 2. Pattern tags (Bulkowski names)

- **Families:** channel, wedge (narrowing), broadening wedge (widening), and triangle (symmetric, ascending, descending).
- **Megaphones (broadening triangles) are not built.** When they were built they lasted for hours, blocked every later pattern, and cut the count from 88 to 36.
- **Today's settings, triangles on:** 97 patterns and 52 trades.
  - Broadening wedges get most trades and most of the profit: +Rs 36.5k on 39 trades.
  - Channels are flat: about +Rs 1.7k on 12 trades.
  - Narrowing wedges and triangles barely trade. The far line comes too close for the reward/risk rule.
- **No library is used.** The wedge detectors found online (tradingpattern, PatternPy, trendln, stock-pattern) either find swing points with future candles or are toy rules.

## 3. Anchored search, run 20261101_231926 (6,000 settings)

- **Today's settings:** 52 trades, 38.5% win, +Rs 21,268, worst drawdown Rs 25,753.
  - By period: +3.7k, -10.5k, -7.3k, -3.2k, -3.4k, +41.9k.
  - Without P6 it loses.
- **The median setting:** -Rs 874 a year. Only 2 of 6,000 settings were profitable in all 6 periods.
- **Unseen-month test.** Each period's setting is picked on the earlier periods, then scored on that period; P3 to P6 are tested.

  | Pick rule | Trades | Win | Rupees |
  |---|---|---|---|
  | Most rupees | 40 | 57.5% | +2,345 |
  | Best weakest period | 31 | 48.4% | +31,445 |
  | Most rupees, 60+ trades | 52 | 51.9% | +4,833 |
  | Best profit factor, 60+ trades | 44 | 47.7% | +19,689 |

  - Every rule lost about Rs 25k in P3 and made it back in P5 and P6.
- **The best setting over the whole year earned Rs 1.55 lakh.** It was chosen with hindsight, so it is not a result.
- **Verdict:** better than the rolling channel, but no reliable edge yet. 12 months and about 50 trades cannot separate an edge from one good stretch.

## 4. Settings that helped consistently

**Test used:** in each search, compare the median of all settings that use a value with the median of all settings, period by period. Only a value ahead in at least 5 of 6 periods, with the same direction in both searches (anchored 6,000 and rolling 40,000), counts as consistent.

| Setting | Consistent value | Today's default | Evidence |
|---|---|---|---|
| Last entry time (`ENTRY_END`) | 14:30 (anchored); 13:30 (rolling) | 15:00 | Anchored: 14:30 best in 5 of 6 periods. Rolling: 13:30 ahead in 5 of 6. 15:00 was the worst value in both. Late entries have too little time before the 15:15 exit |
| Book 1 lot at the middle line (`HALF_EXIT`) | off | on | Off was the better value in 5 of 6 periods in both searches. Holding both lots lets the winners pay for the losers. It conflicts with the trader's own rule, so it is not adopted |
| Stop to entry price at the middle line (`BE_AT`) | on (0.5 anchored, 0.3 rolling) | 0.5 | Anchored: on is ahead in 5 of 6 periods, off in only 1. Rolling: 0.3 best in 5 of 6. Today's default already does this |
| Candle 2 closes beyond candle 1's high or low (`ENTRY_CONFIRM`) | extreme | extreme | Best in 5 of 6 periods (anchored only). The trader's rule. Keep it |
| Swing confirmation bars (`PIVOT_RIGHT`) | 3 | 3 | Ahead in 4 of 6 (anchored) and 5 of 6 (rolling). Keep it |
| First entry time (`MIN_ENTRY_TIME`) | later than 09:15 (09:30 anchored, 10:15 rolling) | 09:15 | Anchored 09:30 ahead in 4 of 6, rolling 10:15 in 5 of 6. Moved alone from today's settings, it was the same or better in every period (+Rs 37.6k vs +21.3k) |

## 5. Settings that only looked good (not consistent)

These raised the 12-month total, but did not win period by period. That means the gain came from one good stretch.

| Setting | What it looked like | Why it is not consistent |
|---|---|---|
| Trail only after the middle line (`TRAIL=after_be`) | Best 12-month median, about +Rs 5k vs -Rs 5k | Ahead in only 2 of 6 periods. Worse in the rolling search. Moved alone, it hurt P1 and P2 and raised the drawdown |
| Wider stop buffer (25 pts, 0.6 x range) | Best single change from today's settings: +Rs 41k | Search medians are ahead in only 3 of 6 periods. Worth re-testing on more history |
| Touch band 10% of the width | Best 12-month median | Not ahead in 5 of 6 periods |
| Reward/risk, triangles, trades a day, wait after a stop, target zone, candle 1 body, 1-candle trail, reversal-candle exit | Mixed | No direction holds across periods or across the two searches |

**Combining the individually good levers made results worse:** Rs 5k to 7k, with drawdowns up to Rs 81k.

## 6. What to do next

1. Download 3 or more years of 5m NIFTY data, then re-run the search and this consistency check. About 150 trades and more test periods are needed before any default changes.
2. **Candidates to adopt once confirmed on the longer data:**
   - last entry at 14:30;
   - first entry at 09:30;
   - keep breakeven at the middle line;
   - consider holding both lots (needs the trader's approval).
3. The live script `strategies/nifty_channel_options.py` stays on its old method, on ATM. When the anchored method is ported, it should use offset ITM1. Sandbox mode comes only after a positive result on unseen months over the longer data.

## Where things are

- **Backtest:** `strategies/research/channel_research.py`
  - Anchored mode is the `ANCHORED` dict.
  - `--channel anchored --channel-stats` prints per-pattern stats.
- **Chart study:** `strategies/research/nifty-channel-anchored.oscript`, installed as "Nifty Anchored Channel".
- **Check that the chart equals the backtest:** `uv run python strategies/research/compare_study.py --mode anchored`
- **Look-ahead test:** `strategies/research/validate_causality.py`
- **Search:** `strategies/research/param_search.py --space anchored --report --run-id 20261101_231926`
  - Results are in the research DuckDB, tables `search_runs`, `search_results` and `search_walkforward`.
  - The older rolling run is `20261101_101457`.
