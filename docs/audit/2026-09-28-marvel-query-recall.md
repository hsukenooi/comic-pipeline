# Marvel sold-comps query recall (BUI-1004)

**Date:** 2026-09-28. **Source:** `uv run --project apps/ebay python docs/audit/2026-09-28-marvel-query-recall.py run {A,B,C,D} OUTDIR`, then `... analyze OUTDIR`, on main at 53c78a8 (includes the BUI-1003 `parse_grade` fix, so every variant uses the same parser). Input: `docs/audit/2026-09-28-marvel-query-recall.batch.json`. Provider: sold-comps.com (primary) through the real `fetch_book_comps` tier ladder. No comic-fmv run, no ledger or fmv write.

**Decision: ship B, cancel C.** Dropping "comics" from the Marvel qualifier raised graded comps from 90 to 215 on 26 vintage books and from 91 to 223 on 10 modern books, with 5 wrong-book graded comps among the 267 it added. Gating the broader tier on graded-comp count added 41 graded comps, 32 of them wrong books (reprints of keys and the 2024 *X-Men '97* series), so it stays gated on the total count.

## Method

- **Variants.** A: current (`"marvel comics"`, broader tier when `len(comps) < 5`). B: qualifier `"marvel"`. C: broader tier when graded comps < 5. D: B + C. The graded threshold for C is `THIN_RESULTS_THRESHOLD` (5), which equals `fmv_math.MIN_NARROW_POOL`, the pool size `build_pool` widens toward. The script swaps `_MARVEL_QUALIFIER` and the `_should_broaden` seam; everything else is production code.
- **Sample.** 26 vintage (pre-1985) books: the 21 vintage unpriced books from the 2026-09-26 batch, plus Hulk #181, Giant-Size X-Men #1, ASM #129, X-Men #97 (1976), and Wolverine #1 (1982). 10 modern books: the 3 modern unpriced batch books, plus ASM #300, New Mutants #98, Uncanny X-Men #207/#239/#266, Spider-Man #11, and Thor: God of Thunder #6. One collision case: X-Men #97 with no year, as it sits in the DB. Target grades come from the batch's fmv rows, or a typical grade for the added keys.
- **Graded comp.** A comp whose title parses to a grade. Only these enter `build_pool`, so they are the ones that move a price.
- **Pollution.** A comp for the wrong book: other issue, reprint or facsimile, lot, other series. Judged by reading every added graded comp's title for every book, plus a regex pass for facsimile, reprint, lot, and year or issue mismatch.
- **Priceable.** P: at least 3 graded comps in `build_pool`'s window, bracketing the target. c: `_classify_pool` returns no flag (guard-clean). Both approximate comic-fmv without running it; neither includes ledger comps.

## Results

Cells are total comps / graded comps, then the priceable marks. The last two columns count wrong-book graded comps among the comps that variant added over A.

| Book | Cohort | Unpriced 09-26 | Target | A | B | C | D | B pollution | C pollution |
|---|---|---|---|---|---|---|---|---|---|
| Thor #129 (1966) | vintage | yes | 5.0 | 13/4 P | 15/6 Pc | 14/4 P | 15/6 Pc | 0 | 0 |
| Thor #140 (1967) | vintage | yes | 6.0 | 4/2 c | 13/6 c | 4/2 c | 13/6 c | 0 | 0 |
| Thor #143 (1967) | vintage | yes | 7.0 | 7/4 | 17/9 | 10/5 | 17/9 | 0 | 0 |
| Thor #145 (1967) | vintage | yes | 4.0 | 11/3 | 17/4 | 12/3 | 19/4 | 0 | 0 |
| Thor #148 (1968) | vintage | yes | 4.5 | 6/1 | 19/10 Pc | 9/2 | 19/10 Pc | 0 | 0 |
| Thor #149 (1968) | vintage | yes | 7.5 | 6/4 | 21/12 P | 8/6 | 21/12 P | 0 | 0 |
| Thor #151 (1968) | vintage | yes | 5.5 | 5/1 | 14/10 Pc | 5/1 | 14/10 Pc | 0 | 0 |
| Thor #157 (1968) | vintage | yes | 5.5 | 13/5 P | 24/11 Pc | 13/5 P | 24/11 Pc | 0 | 0 |
| Thor #159 (1968) | vintage | yes | 8.0 | 8/4 | 16/7 Pc | 8/4 | 16/7 Pc | 0 | 0 |
| Thor #163 (1969) | vintage | yes | 5.5 | 7/0 | 17/5 | 9/2 | 17/5 | 1 | 0 |
| Thor #167 (1969) | vintage | yes | 7.5 | 7/2 | 13/5 Pc | 8/2 | 13/5 Pc | 0 | 0 |
| Thor #175 (1970) | vintage | yes | 8.0 | 8/5 | 14/6 P | 8/5 | 14/6 P | 0 | 0 |
| ASM #63 (1968) | vintage | yes | 4.0 | 36/7 P | 52/14 Pc | 36/7 P | 52/14 Pc | 0 | 0 |
| ASM #74 (1969) | vintage | yes | 4.0 | 22/7 P | 31/14 Pc | 22/7 P | 31/14 Pc | 0 | 0 |
| ASM #94 (1971) | vintage | yes | 6.0 | 16/7 P | 26/11 P | 16/7 P | 26/11 P | 0 | 0 |
| Avengers #87 (1971) | vintage | yes | 4.0 | 12/3 | 19/7 | 16/3 | 19/7 | 0 | 0 |
| Thor Annual #3 (1971) | vintage | yes | 7.0 | 1/1 | 6/4 | 1/1 | 6/4 | 0 | 0 |
| Thor Annual #5 (1976) | vintage | yes | 6.5 | 3/2 | 6/1 | 3/2 | 7/2 | 0 | 0 |
| Daredevil #163 NS (1980) | vintage | yes | 7.0 | 2/1 | 5/4 c | 2/1 | 6/5 Pc | 0 | 0 |
| Avengers #195 NS (1980) | vintage | yes | 9.0 | 1/1 | 3/3 | 1/1 | 3/3 | 0 | 0 |
| Daredevil #176 NS (1981) | vintage | yes | 7.5 | 2/1 | 9/2 | 2/1 | 9/2 | 0 | 0 |
| Incredible Hulk #181 (1974) | vintage | | 6.0 | 17/3 P | 37/8 P | 30/9 P | 37/8 P | 0 | 6 |
| Giant-Size X-Men #1 (1975) | vintage | | 8.0 | 23/4 | 32/5 | 48/10 Pc | 32/5 | 0 | 6 |
| ASM #129 (1974) | vintage | | 6.0 | 18/3 P | 40/8 P | 35/7 P | 40/8 P | 0 | 4 |
| X-Men #97 (1976) | vintage | | 8.5 | 15/2 | 32/9 Pc | 177/18 Pc | 32/9 Pc | 0 | 15 |
| Wolverine #1 (1982) | vintage | | 9.0 | 103/13 Pc | 146/34 Pc | 103/13 Pc | 146/34 Pc | 1 | 0 |
| X-Men #97 (no year) | collision | | 8.5 | 166/22 Pc | 168/30 Pc | 166/22 Pc | 168/30 Pc | 21 | 0 |
| Captain America Annual #8 (1986) | modern | yes | 5.5 | 23/7 P | 52/17 P | 23/7 P | 52/17 P | 0 | 0 |
| Ultimate Fallout #4 (2011) | modern | yes | 8.5 | 7/1 | 12/5 | 16/4 | 12/5 | 0 | 1 |
| Thor: God of Thunder #8 (2013) | modern | yes | 9.2 | 3/1 | 5/1 | 3/1 | 5/1 | 0 | 0 |
| ASM #300 (1988) | modern | | 9.0 | 54/14 Pc | 115/33 Pc | 54/14 Pc | 115/33 Pc | 1 | 0 |
| New Mutants #98 (1991) | modern | | 9.0 | 67/6 Pc | 80/14 Pc | 67/6 Pc | 80/14 Pc | 0 | 0 |
| Uncanny X-Men #207 (1986) | modern | | 9.0 | 31/14 Pc | 54/22 Pc | 31/14 Pc | 54/22 Pc | 0 | 0 |
| Uncanny X-Men #239 (1988) | modern | | 9.2 | 39/16 Pc | 141/65 Pc | 39/16 Pc | 141/65 Pc | 2 | 0 |
| Spider-Man #11 (1991) | modern | | 9.2 | 14/8 Pc | 32/16 Pc | 14/8 Pc | 32/16 Pc | 0 | 0 |
| Thor: God of Thunder #6 (2013) | modern | | 9.4 | 4/3 Pc | 5/5 Pc | 4/3 Pc | 5/5 Pc | 0 | 0 |
| Uncanny X-Men #266 (1990) | modern | | 9.0 | 74/21 Pc | 145/45 Pc | 74/21 Pc | 145/45 Pc | 0 | 0 |

| Aggregate | A graded | B graded | C graded | D graded |
|---|---|---|---|---|
| Vintage (26) | 90 | 215 | 128 | 217 |
| Modern (10) | 91 | 223 | 94 | 223 |
| 24 unpriced batch books, P | 6 | 12 | 6 | 13 |
| 24 unpriced batch books, guard-clean | 1 | 10 | 1 | 10 |

A's P count includes ASM #63/#74/#94, which did not price on 09-26; the guard-clean row is the fairer before/after. The ticket's own probe priced 7.

### B: qualifier "marvel"

- **Gain.** Vintage graded comps rise 2.4x (131 added, 6 lost), modern 2.5x (136 added, 4 lost). The ticket's Thor probe reproduces: #148 1 → 10, #143 4 → 9, #159 4 → 7, #129 4 → 6, ASM #94 7 → 11.
- **Pollution among the 267 added graded comps (excluding the collision case): 5.** A misspelled "FACSIMILIE" 2023 Wolverine #1 (evades `_FACSIMILE_RE`); an ASM #300 9.6 at $10.89 whose title ends "2023...", most likely a facsimile; a two-issue Thor #163/#166 listing; two "Uncanny X-Men #239 X2" two-copy lots. All are classes already present in the baseline pool (A's own graded comps carry at least 5 of 181: three X2 lots, a Thor #229 with a Hulk #181 ad, and a Spectacular Spider-Man #11). One Hulk #181 comp reads "VG Qualified"; it is the right book and is not counted.
- **Lost comps.** Ten graded comps A found are absent from B's response, although their titles contain "Marvel"; the provider's results for the looser query are not a strict superset. Thor Annual #5 drops 2 → 1.
- **Modern.** ASM #300 14 → 33; no modern book loses graded depth.
- **Spend.** B's base query text is new for every book: 62 provider calls for the sample, once. After that, B spends less than A, because the deeper base pool fires the broader tier on 2 books instead of 9.

### C: broader tier on graded count

C broadens 23 books. On non-keys it adds almost nothing (Thor: 6 graded comps across 12 books). On keys the year-less query reaches the reprints: Hulk #181 (Marvel Milestone, Wizard Ace, Hulk vs Wolverine TB, 6 of 6), Giant-Size X-Men #1 (Special Edition X-Men #1, Milestone, a 2025 Giant-Size X-Men #1, 6 of 6), ASM #129 (Lion's Gate, Wizard Ace, lenticular, 4 of 4). X-Men #97 (1976) broadens into the 2024 series, 15 of 16. These are graded, priced at $1–$25, and would enter the comps ledger for good. D (B + C) adds only 2 graded comps over B, both correct, because B lifts most books to 5 or more graded; Giant-Size X-Men #1 sits at exactly 5, so D's safety there is luck, not design.

### X-Men #97 collision

The "marvel comics" token never blocked the collision. Under A, the year-less "X-Men 97" query returns 22 graded comps and 21 are wrong books (the 2024 and 2026 *X-Men '97* series, a 2000 Vol. 2 #97, show-tie-in backlist titles), because those comics are published by Marvel Comics too. Under B, 26 of 30 are wrong, and correct graded comps rise from 1 to 4. With the year, B is clean: 2 → 9 graded, all 1976 copies. Only `year` separates the books; neither qualifier does.

### Certified (slab) targets

The qualifier also sits in the BUI-929 certified base query, so B changes it too. A spot check on 4 CGC targets (ASM #129, Hulk #181, Thor #148, ASM #300) raised slab comps from 115 to 237. None of the 122 added was a wrong book. Ten carry a Signature Series, signed, or Qualified label; graded mode keeps those on purpose and routes them by label (`_GRADED_MODE_EXCLUDE_RE`), so they are not pollution.

## Spend

104 live provider calls in total, all on sold-comps.com, with no errors or breaker trips: A 13, B 62, C 14, D 6, and 9 for the slab spot check. The budget was 200.

## Follow-up

- `_FACSIMILE_RE` and the `-facsimile` query exclusion miss the "facsimilie" misspelling.
- "X2" multi-copy listings pass the multibook-lot filter.
- Year-less vintage X-Men rows (such as comic 662, X-Men #97) price from a pool that is about 95% wrong books. Backfilling `year` is the fix.
