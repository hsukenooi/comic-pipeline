# Slab Grader Loop Ledger

One row per loop round (BUI-1444). Each round screens a candidate change on dev out of fold, then grades a fresh slab batch that no method was tuned on. Scripts compute every score (`~/comic-grader-fixtures/slab-loop/tools/loop.py`). The full rows are in `slab-loop/loop/ledger.jsonl`.

**Stop Rules** – Success means the champion scores 80%+ within 1.0 over 150+ fresh books, every band 70%+, and bias within ±0.3. Failure means 4 rounds in a row with no promotion.

**Scores** – "Within 1.0" hits. Dev is 150 books out of fold (folds A to C). Fresh batches are 60 books, 12 per band. Bands run B1 (0.5 to 3.5) through B5 (9.2+).

## Rounds

| Round | Batch | Candidate | Dev Screen (Champion vs Candidate) | Fresh (Champion) | Fresh Bands B1–B5 | Fresh Bias | Range Coverage | Promoted | Tokens |
|---|---|---|---|---|---|---|---|---|---|
| R1 | F01 | c1446: no prompt shade-down, cross-fitted isotonic calibration (BUI-1446) | r3 112/150 (75%) vs c1446 104/150 (69%), fail | r3 39/60 (65%) | 7, 9, 6, 10, 7 of 12 | −0.36 | 65% | No | Fable 1.91M, Opus 0 |
| R9 | F02 | None (fresh step only; c-crop scored separately under S3, below) | – | r3 44/60 (73%) | 12, 5, 8, 8, 11 of 12 | −0.19 | 72% | No | Fable 1.26M, Opus 0 |

## Dev Screens Without a Fresh Batch (R2 to R8)

Each screen grades all 210 dev books out of fold (folds A to C plus F01). The champion is r3 run 1 at 151/210 (71.9%). A candidate passes with +3 hits or more.

| Round | Candidate | Dev Result | Pass | Tokens |
|---|---|---|---|---|
| R2 | c1445: per-book reference sheets (BUI-1445) | 152/210 (+1) | No | Fable 1.57M |
| R3 | cpair: paired comparison against references | 141/210 (−10). B3 bias +1.26 | No | Fable 0.86M |
| R4 | cpair2: paired comparison, references on both sides | 148/210 (−3). B4 +5 and B5 +6, B2 −7 and B3 −9 | No | Fable 2.32M |
| R5 | c-median: median of 3 r3 runs | 151/210 (+0). Errors correlate 0.78 to 0.84 across runs | No | Fable 2.48M |
| R6 | c-crop: located-box native-resolution zooms, 2 replicates | 153 and 154 (+2, +3). Needs both at +3 | No | Fable 3.22M, plus locator 1.48M |
| R7 | Selective coverage rules S1 to S3 on existing runs | No rule reaches 85% within 1.0 at 40%+ coverage with over-read at most 10% | No | 0 (scoring only) |
| R8 | Defect-list rule grader (BUI-1453), 37 always-missed plus 37 always-hit books, 2 replicates | Rep 1 fixed 17, broke 3. Rep 2 fixed 12, broke 5 (limit 3) | No | Fable 2.09M |

## Round Notes

**R1** – The candidate's calibration cost hits. Its raw ranges under the old PRIMARY rule would have scored 110/150, so isotonic calibration lost 6. Dropping the shade-down raised B1 and B2 bias (B2 +0.62). The champion fell from 75% on dev to 65% on fresh F01. Its fresh misses cluster in B3 (6/12) and B5 (7/12, bias −0.88). F01's issue-universe books and series top-up books score about the same (20/29 and 19/31). Champion tally toward the success rule: 39/60.

**R9** – The fresh step on F02 (sales 11-23 to 11-25), r3 only. r3 scored 44/60 with misses in B2 (5/12) and B3 to B4 (8/12 each). Champion tally toward the success rule: 83/120 (69.2%), bias −0.28. No promotion, so the streak is 2. The pre-registered c-crop selective test (S3: range width at most 1.5) failed all three marks: coverage 36.7%, within 1.0 77.3% (17/22), over-read 27.3%. The target was 40%+, 85%+, and 10% or less. At full coverage c-crop also scored 44/60. F02 moved into dev as fold F02.
