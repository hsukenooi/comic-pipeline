# Slab Grader Loop Ledger

One row per loop round (BUI-1444). Each round screens a candidate change on dev out of fold, then grades a fresh slab batch that no method was tuned on. Scripts compute every score (`~/comic-grader-fixtures/slab-loop/tools/loop.py`). The full rows are in `slab-loop/loop/ledger.jsonl`.

**Stop Rules** – Success means the champion scores 80%+ within 1.0 over 150+ fresh books, every band 70%+, and bias within ±0.3. Failure means 4 rounds in a row with no promotion.

**Scores** – "Within 1.0" hits. Dev is 150 books out of fold (folds A to C). Fresh batches are 60 books, 12 per band. Bands run B1 (0.5 to 3.5) through B5 (9.2+).

## Rounds

| Round | Batch | Candidate | Dev Screen (Champion vs Candidate) | Fresh (Champion) | Fresh Bands B1–B5 | Fresh Bias | Range Coverage | Promoted | Tokens |
|---|---|---|---|---|---|---|---|---|---|
| R1 | F01 | c1446: no prompt shade-down, cross-fitted isotonic calibration (BUI-1446) | r3 112/150 (75%) vs c1446 104/150 (69%), fail | r3 39/60 (65%) | 7, 9, 6, 10, 7 of 12 | −0.36 | 65% | No | Fable 1.91M, Opus 0 |

## Round Notes

**R1** – The candidate's calibration cost hits. Its raw ranges under the old PRIMARY rule would have scored 110/150, so isotonic calibration lost 6. Dropping the shade-down raised B1 and B2 bias (B2 +0.62). The champion fell from 75% on dev to 65% on fresh F01. Its fresh misses cluster in B3 (6/12) and B5 (7/12, bias −0.88). F01's issue-universe books and series top-up books score about the same (20/29 and 19/31). Champion tally toward the success rule: 39/60.
