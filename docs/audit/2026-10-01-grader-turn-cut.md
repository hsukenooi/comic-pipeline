# Grader turn cut: batched crops and silence rule (BUI-1083)

**Date:** 2026-10-01. **Result after the right-edge tile fix (run 2, below): the grade gate passes, 7 of 8 books within 0.5 on both runs; API calls are 5 per run (target 8 or fewer); cache-read stays at a 239k median, above the 80k target, which this harness cannot reach.** Run 1 (the first sheet layout) drifted on 2 books and is kept below as the record of why the fix was made.

**Harness:** `~/comic-grader-fixtures/bui-51-baseline/rebaseline-2026-09-07/`, `jobs/run-bui-1083.sh` (16 runs: 8 books, 2 runs each, `--max-turns 8`, Fable 5.1). `parse_bui1083.py` builds the tables below. "Old" is the September `patched` single run, the procedure on `main` today.

## Run 2: right-edge top and bottom tiles added

Sheet 2 gained right edge top and bottom tiles, so a back-cover photo's spine (the right edge) gets the same coverage as the front spine. Envelopes in `out/bui-1083-new2{,-rerun}/`.

| Book | June | Sept patched | new run 1 | new run 2 | within 0.5 (both) | turns old / new | output old / new | cache-read old / new | cache-create old / new |
|---|---|---|---|---|---|---|---|---|---|
| Uncanny X-Men #210 | 8.0 | 6.5 | 7.5 | 7.0 | NO | 33 / 19 / 17 | 10.7k / 4.7k / 4.3k | 333.8k / 230.0k / 239.4k | 71.4k / 62.5k / 53.8k |
| Uncanny X-Men #277 | 8.5 | 8.0 | 8.5 | 8.5 | yes | 32 / 16 / 17 | 9.6k / 3.7k / 4.7k | 315.6k / 231.2k / 239.5k | 74.3k / 60.1k / 53.9k |
| Strange Tales #162 | 4.5 | 4.0 | 4.0 | 3.5 | yes | 25 / 19 / 18 | 8.2k / 4.7k / 4.4k | 410.6k / 231.3k / 239.4k | 63.3k / 67.9k / 56.0k |
| X-Men #24 | 8.5 | 9.2 | 9.2 | 9.2 | yes | 44 / 20 / 16 | 9.1k / 4.5k / 4.5k | 354.3k / 230.2k / 239.5k | 69.6k / 69.7k / 48.0k |
| Thor #173 | 6.5 | 6.0 | 6.0 | 5.5 | yes | 49 / 20 / 19 | 13.1k / 5.3k / 4.3k | 549.3k / 239.5k / 239.4k | 87.6k / 59.8k / 57.9k |
| Detective Comics #578 | 8.0 | 7.5 | 8.0 | 7.0 | yes | 26 / 16 / 15 | 8.7k / 4.1k / 4.2k | 411.4k / 239.2k / 239.3k | 63.5k / 48.6k / 48.2k |
| Fantastic Four Annual #4 | 4.5 | 1.8 | 2.0 | 1.8 | yes | 26 / 18 / 15 | 11.7k / 4.9k / 4.8k | 352.0k / 239.2k / 239.6k | 59.6k / 55.7k / 46.6k |
| Incredible Hulk King-Size Special #1 | 6.5 | 7.5 | 7.0 | 7.0 | yes | 31 / 19 / 20 | 8.5k / 5.6k / 4.8k | 415.5k / 239.4k / 239.3k | 64.4k / 58.0k / 59.8k |

Medians, 16 runs: `num_turns` 18, API calls 5 (every run exactly 5), output 4.6k, cache-read 239.4k, cache-create 57.0k.

- **The fix moved both drifted books toward September.** Uncanny X-Men #277 went from 9.0 / 9.0 to 8.5 / 8.5 (now within 0.5). Uncanny X-Men #210 went from 8.0 / 7.5 to 7.5 / 7.0, still 0.5 to 1.0 above September's 6.5. It is the one allowed miss, and it sits between the September baseline prompt (8.0, 7.5) and the patched prompt (6.5, 6.5).
- **API calls are measured from the session transcripts** (distinct assistant message ids). `usage.iterations` in the envelope always holds 1 entry (the final call), so it does not count calls.

## Run 1: first sheet layout (6 tiles)


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

### Run 1 medians

| Set | turns | output | cache-read | cache-create |
|---|---|---|---|---|
| Sept patched singles (old procedure) | 31.5 | 9.3k | 382.5k | 67.0k |
| Sept baseline + patched singles (the ticket's figure) | 14.5 | 8.3k | 188.5k | 54.4k |
| New body, 16 runs | 18.0 | 5.4k | 237.0k | 58.0k |
| Target | 8 or fewer | | 80k or fewer | |

Against the old procedure, the new body cuts turns 43%, output 42%, and cache-read 38%. The ticket's 14.5 / 201k reference mixes in the September `baseline` runs, which made no crops at all (3 turns each), so it is not the procedure being replaced.

### Run 1 grade drift and likely cause

- **Uncanny X-Men #210 (+1.5, +1.0).** Both new runs saw the back-cover tide-line stain the September patched runs found, but graded it as a VF- defect (8.0, 7.5), not FN+ (6.5). The September runs also counted 4 to 5 color-breaking spine ticks and a 1/2" crease at the bottom spine corner of the black back cover, found over about 30 sequential crops.
- **Uncanny X-Men #277 (+1.0 on both runs).** The September runs counted several color-breaking spine stress lines on the black back cover (8.0); the new runs saw only two tip ticks (9.0). The September audit called this book run-to-run noise (8.0 to 9.0 over four runs), but the gate counts it.
- **Likely cause: the fixed tiles miss the back cover's spine.** On a back-cover photo the spine is the right edge, and sheet 2 covers only the right edge's middle. Its top and bottom tiles are on the left (open) edge. Both drifted books have black back covers, where spine ticks drive the grade. The fix to test is adding right-edge top and bottom tiles (a 2x4 sheet 2), then re-running the gate. A second, weaker suspect is the silence rule cutting written deliberation (output fell 42%).

## Why the cache-read target is out of reach

- **`num_turns` is not API calls.** It counts every tool-use message, so one response with six parallel Reads counts as six. The runs made 5 API calls each (6 on two run-1 runs): `grade-crops`, one parallel Read turn, one extra crop, one parallel Read turn, and the final block. Measured in API calls, the turn target is met.
- **Cache-read has a fixed floor of about 49k per API call.** The `claude -p` harness loads its own tool definitions with the system prompt, and the first call caches 48.7k before any image is read. Five calls cost about 237k to 239k. Even the bare minimum (crop, read, answer) would be about 150k, so 80k is not reachable by any procedure that looks at a crop.
