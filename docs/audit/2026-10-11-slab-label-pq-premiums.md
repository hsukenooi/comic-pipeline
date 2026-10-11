# Slab Label and Page-Quality Premiums (BUI-1233)

**Decision:** NO-GO on every class. Label classes are too thin to estimate. The page-quality premium shrinks to within +/-3% once venue is held fixed, and its CIs span 1.

Script: `docs/audit/2026-10-11-slab-label-pq-premiums.py` (read-only, about 10 minutes, pure Python). Data: the BUI-1229 test set, which keeps every graded row, so no re-parse was needed.

## Method

- **Rows** – CGC only, `exclude_code` empty, `price_outlier` not true. 9,468 rows, 47 books.
- **Matching** – ln(price) with one fixed effect per book, grade, and 90-day bin, so only within-cell variation identifies a premium. Grade and date mix cannot leak in.
- **Page quality** – universal-label rows, reference `ow` (off-white). `c_ow` is cream to off-white, `ow_w` is off-white to white.
- **Label** – all rows, reference universal, page-quality dummies as controls.
- **CIs** – 95% percentile bootstrap resampling books (2,000 reps, seed 1233).
- **Thin rule** – a cell needs at least 20 treated rows in cells where the treatment varies, and at least 5 books. Otherwise it is dropped.
- **Basis** – PC accepted price. A list-price run (PC `ask` on best-offer sales, pooled cells only) matches within 0.01 on every headline cell.

## Label classes

| Class | Treated rows (books) | Result |
| --- | --- | --- |
| Signature Series | 1 (1) in the whole set, 0 matched | Dropped |
| Restored / conserved | 4 (3), 3 matched | Dropped |
| Qualified (purple) | 21 (13), 18 matched | Dropped (below 20 rows) |

**Why:** PC history holds almost none of these labels for the 47 approved books. A multiplier from 1 to 18 rows is noise. No era or price-tier cell holds more than 11 qualified rows.

## Page quality (universal label, vs off-white)

| Contrast | Pooled | eBay only | Venue fixed effect |
| --- | --- | --- | --- |
| White | x1.056 [1.014, 1.098], n=1000, 47 books | x1.016 [0.970, 1.059], n=771 | x1.030 [0.987, 1.071], n=830 |
| OW/W | x0.977 [0.931, 1.017], n=618, 34 books | x0.975 [0.932, 1.015], n=326 | x0.979 [0.938, 1.016], n=404 |
| C/OW | x0.907 [0.864, 0.949], n=82, 25 books | x0.979 [0.902, 1.085], n=33 | x0.982 [0.912, 1.066], n=46 |
| Cream | n=1 | Dropped | Dropped |

**Pooled white premium is mostly venue mix:** Heritage and eBay differ in both price level and page-quality mix. Holding venue fixed, the white premium falls from +5.6% to +3.0% with a CI spanning 1. The C/OW discount (-9%) falls to -2%, also spanning 1. eBay-only gives the same picture.

### By era and price tier (pooled venues, so read as upper bounds)

| Cell | White | OW/W | C/OW |
| --- | --- | --- | --- |
| Silver (21 books) | x1.090 [1.038, 1.142] | x1.000 [0.941, 1.046] | x0.933 [0.890, 0.984] |
| Bronze (13 books) | x1.011 [0.955, 1.047] | x0.916 [0.857, 0.953] | dropped (n=10) |
| Copper, modern | dropped | dropped | dropped |
| Under $100 (24 books) | x0.858 [0.712, 1.062] | x0.773 [0.640, 0.939], n=22 | dropped |
| $100-400 | x1.054 [0.994, 1.107] | x0.958 [0.898, 1.018] | x0.880 [0.787, 0.981], n=23 |
| $400 and up | x1.070 [1.019, 1.127] | x0.997 [0.941, 1.057] | x0.911 [0.872, 0.951] |

Era and tier cells were not re-run with venue fixed effects, so they carry the same venue confound. They do not agree on a sign across eras (bronze OW/W is -8%, silver is 0%). Tier is the median price of the book at that grade, so it is not independent of the book. Copper has 2 books and modern has 11, mostly one venue.

## Go or no-go

| Class | Call | Reason |
| --- | --- | --- |
| Signature Series | NO-GO | 1 row. |
| Restored / conserved | NO-GO | 3 matched rows in 3 books. |
| Qualified | NO-GO | 18 matched rows; no CI at the thin rule. Keep excluding. |
| Page quality | NO-GO | Within venue the white and OW/W premiums are within +/-3% and their CIs span 1. A premium that size is probably well inside the FMV band (not measured here), so a build is not justified on this evidence. |

## Limits

- Page quality in a title is seller-reported and unverified.
- 60% of universal rows have no page quality (`unknown`). They sit at x1.03 vs `ow` in the pooled fit, so the reference is noisy too.
- Venue fixed effects absorb all between-venue price differences, including real ones. A true white premium of 3% cannot be ruled out. It is still small.
- Mostly Marvel Silver and Bronze books.

## Out of scope

- A real test of the label classes needs a harvest keyed on `signature series`, `restored`, and `qualified` titles across many books, not this 47-book set.
