# Grader turn cut: batched crops and silence rule (BUI-1083)

**Date:** 2026-10-01. **Result: gate FAILED on both counts.** Grades drift on 2 of 8 books (the ticket allows 1), and the usage targets are out of reach in this harness. The procedure itself held: every new run made 5 or 6 API calls, as designed.

**Harness:** `~/comic-grader-fixtures/bui-51-baseline/rebaseline-2026-09-07/`, `jobs/run-bui-1083.sh` (16 runs: 8 books, 2 runs each, `--max-turns 8`, Fable 5.1). `parse_bui1083.py` builds the tables below. "Old" is the September `patched` single run, the procedure on `main` today.

## Per book

| Book | June | Sept patched | new run 1 | new run 2 | within 0.5 (both) | turns old / new | output old / new | cache-read old / new | cache-create old / new |
|---|---|---|---|---|---|---|---|---|---|
| Uncanny X-Men #210 | 8.0 | 6.5 | 8.0 | 7.5 | NO | 33 / 14 / 16 | 10.7k / 4.1k / 4.7k | 333.8k / 217.6k / 237.1k | 71.4k / 67.3k / 54.8k |
| Uncanny X-Men #277 | 8.5 | 8.0 | 9.0 | 9.0 | NO | 32 / 21 / 17 | 9.6k / 5.6k / 4.7k | 315.6k / 218.7k / 237.0k | 74.3k / 81.0k / 56.8k |
| Strange Tales #162 | 4.5 | 4.0 | 4.0 | 4.0 | yes | 25 / 17 / 22 | 8.2k / 4.7k / 5.7k | 410.6k / 217.3k / 297.6k | 63.3k / 76.0k / 57.4k |
| X-Men #24 | 8.5 | 9.2 | 9.2 | 9.2 | yes | 44 / 24 / 20 | 9.1k / 5.6k / 5.5k | 354.3k / 218.4k / 237.2k | 69.6k / 76.9k / 62.9k |
| Thor #173 | 6.5 | 6.0 | 6.0 | 6.0 | yes | 49 / 18 / 19 | 13.1k / 4.6k / 5.4k | 549.3k / 237.0k / 237.0k | 87.6k / 58.6k / 61.0k |
| Detective Comics #578 | 8.0 | 7.5 | 8.0 | 7.5 | yes | 26 / 17 / 16 | 8.7k / 5.7k / 5.5k | 411.4k / 237.5k / 237.2k | 63.5k / 55.6k / 54.9k |
| Fantastic Four Annual #4 | 4.5 | 1.8 | 2.0 | 1.8 | yes | 26 / 18 / 15 | 11.7k / 5.0k / 4.6k | 352.0k / 236.9k / 236.8k | 59.6k / 49.3k / 52.5k |
| Incredible Hulk King-Size Special #1 | 6.5 | 7.5 | 7.5 | 7.0 | yes | 31 / 18 / 21 | 8.5k / 5.7k / 6.4k | 415.5k / 236.9k / 297.9k | 64.4k / 58.7k / 55.7k |

## Medians

| Set | turns | output | cache-read | cache-create |
|---|---|---|---|---|
| Sept patched singles (old procedure) | 31.5 | 9.3k | 382.5k | 67.0k |
| Sept baseline + patched singles (the ticket's figure) | 14.5 | 8.3k | 188.5k | 54.4k |
| New body, 16 runs | 18.0 | 5.4k | 237.0k | 58.0k |
| Target | 8 or fewer | | 80k or fewer | |

Against the old procedure, the new body cuts turns 43%, output 42%, and cache-read 38%. The ticket's 14.5 / 201k reference mixes in the September `baseline` runs, which made no crops at all (3 turns each), so it is not the procedure being replaced.

## Why the usage targets are out of reach

- **`num_turns` is not API calls.** It counts every tool-use message, so one response with six parallel Reads counts as six. The new runs made 5 API calls each (6 on two runs): `grade-crops`, one parallel Read turn, one extra crop, one parallel Read turn, and the final block. That minimum is already 8 by this count for a 2-photo book (1 + 6 reads + 1).
- **Cache-read has a fixed floor of about 49k per API call.** The `claude -p` harness loads its own tool definitions with the system prompt, and the first call caches 48.7k before any image is read. Five calls cost about 237k. Even the bare minimum (crop, read, answer) would be about 150k, so 80k is not reachable by any procedure that looks at a crop.

## Grade drift and likely cause

- **Uncanny X-Men #210 (+1.5, +1.0).** Both new runs saw the back-cover tide-line stain the September patched runs found, but graded it as a VF- defect (8.0, 7.5), not FN+ (6.5). The September runs also counted 4 to 5 color-breaking spine ticks and a 1/2" crease at the bottom spine corner of the black back cover, found over about 30 sequential crops.
- **Uncanny X-Men #277 (+1.0 on both runs).** The September runs counted several color-breaking spine stress lines on the black back cover (8.0); the new runs saw only two tip ticks (9.0). The September audit called this book run-to-run noise (8.0 to 9.0 over four runs), but the gate counts it.
- **Likely cause: the fixed tiles miss the back cover's spine.** On a back-cover photo the spine is the right edge, and sheet 2 covers only the right edge's middle. Its top and bottom tiles are on the left (open) edge. Both drifted books have black back covers, where spine ticks drive the grade. The fix to test is adding right-edge top and bottom tiles (a 2x4 sheet 2), then re-running the gate. A second, weaker suspect is the silence rule cutting written deliberation (output fell 42%).
