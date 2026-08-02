# BUI-600 — the adjudication

This file occupies BUI-596's `rule.md` slot. It is named differently because the
honest finding is that **there is no rule here.** Measured read-only against
`~/.comics-server/db.sqlite` on 2026-08-02. **No write was made to the live DB.**

## Why this is a hand-curated id list and not a classifier

BUI-596's rule was **generative**. `series_prefix(title, issue)` split the title
on the `#<issue>` token, so one expression both *selected* the malformed row and
*derived* its repair. That is where its confidence came from, and it is why a
rule was the right shape there.

BUI-600's rows have no `#<issue>` token. There is nothing to split on. Every
proposed title below was recovered by joining the row to the eBay listing that
created it and then picking the spelling this table already uses. Two of the four
rewrites cannot be derived from the stored string by any rule at all:

- **328** stores `'Hulk Annual Silver age Steranko Inhumans'`. The book is
  **`Incredible Hulk Annual`** — a token the row does not contain. It comes from
  the source listing (`Incredible Hulk Annual #1 Silver age Steranko Key
  Inhumans VF- Beauty Wow z`) and matches twin 312's spelling.
- **315** stores `'Avengers Silver age Black Panther Joins'`. Knowing that
  `Black Panther Joins` is a cover blurb and not part of a series name is not a
  lexical property of the string.

So even a perfect detector would leave **100% of the repair as hand work**. A
general classifier here would be machinery that does none of the job. 12 rows do
not need one.

## The vocabulary scan, and where its confidence really comes from

A vocabulary rule was still derived and measured, because the ticket asks for the
false-positive number and because it is useful as a tripwire.

```python
AGE_MARKER = re.compile(r'\b(?:bronze|silver|golden|gold|copper|modern|atom)\s+age\b', re.I)
```

Measured over all 674 live rows:

| candidate rule | flagged | false positives | missed |
| -- | --: | --: | --: |
| **age marker only** | **12** | **0** | **0** |
| age \| grade token | 12 | 0 | 0 |
| age \| grade \| hype (`wow`/`beauty`/`gem`/`key`) | 12 | 0 | 0 |
| age \| grade \| hype \| ordinal (`1st`/`2nd`) | 14 | 2 — ids 330, 335 | 0 |
| **+ publisher (`Marvel Comics`/`DC Comics`)** | 16 | **4 — ids 55, 56, 330, 335** | 0 |

**`DC Comics Presents` (ids 55, 49-issue and 85-issue) is flagged only by the
publisher-token variant**, exactly as the ticket predicted. Dropping the
publisher token removes that false positive outright — the risk the ticket named
is avoidable, not inherent.

**But the precision-1.00 result does not license acting on the rule.** It is a
property of *this table today*, not a proof. BUI-596 could prove its rule had no
false positives because the rule and the independent `title LIKE '%#%'` heuristic
selected an identical set — two different tests agreeing. Nothing analogous is
available here. Real series names contain these tokens: DC's 2000 event one-shots
are literally titled `Silver Age: Showcase`, `Silver Age: Dial H for Hero`, and so
on. If one entered the collection, the scan would flag it.

So the scan is wired into `plan.py` as **`vocabulary_scan()` — a drift tripwire
that selects nothing.** `remediate.py` aborts if it returns any id outside the
adjudicated 12 (a new listing-titled row appeared and needs adjudication) or
fails to return one of them (the table moved under the plan).

**Stated plainly: this plan is safe because a human read all 12 rows and their
source listings, not because a rule scored well.**

## The 12 rows, adjudicated individually

Provenance for every row was recovered from `bids.ebay_title`. For four rows the
`bid_fmvs` junction still points at the malformed row; for the other eight,
`bids.fmv_id` had already been re-pointed to a clean twin, which is itself the
evidence that the malformed row is an abandoned placeholder.

### Rewrite (4)

| id | stored title | issue | new title | why not delete |
| --: | -- | --: | -- | -- |
| 201 | `Avengers Bronze age 1st Squadron Supreme` | 85 | `Avengers` | Holds **real FMV** (fmv 212: 7.5 / 40–45 / comps=2), the `bid_fmvs` link for bid 152, and `bids.fmv_id=212`. |
| 204 | `Avengers Bronze age Vision Declares his love for Wanda` | 81 | `Avengers` | fmv 213 has `comps=0` but **populated low/high** (15–25, *"using prior DB value"*), plus the link for bid 155 and `bids.fmv_id=213`. |
| 315 | `Avengers Silver age Black Panther Joins` | 52 | `Avengers` | Empty shell, but holds the link for bid 168 — **status WON** — and `bids.fmv_id=341`. Deleting would sever a won auction from its FMV. |
| 328 | `Hulk Annual Silver age Steranko Inhumans` | 1 | `Incredible Hulk Annual` | Empty shell, but holds the link for bid 244 and `bids.fmv_id=350`. |

### Delete (8)

Every one is an abandoned placeholder: the listing-titled write created the row,
the sold-comps lookup under that title found nothing, and the real computation
then wrote under the normalized title, taking `bids.fmv_id` with it.

| id | stored title | issue | clean twin now holding the book |
| --: | -- | --: | -- |
| 202 | `Avengers Bronze age Daredevil Fine+` | 82 | 173 `Avengers` #82/1970 — bid 153 → fmv 185 |
| 203 | `Avengers Bronze age 1st Lethal Legion Story FVF` | 79 | 205 `The Avengers` #79/1970 — bid 154 → fmv 214 |
| 314 | `Avengers Silver age` | 10 | 187 `Avengers` #10/1964 (bid 166 has `fmv_id` NULL) |
| 316 | `Avengers Silver age` | 54 | 233 `Avengers` #54/1968 (bid 169 has `fmv_id` NULL) |
| 339 | `Iron Man Bronze age Layton Sub-mariner Fine` | 120 | 266 `Iron Man` #120/1979 — bid 270 → fmv 334 |
| 340 | `Iron Man Bronze age` | 122 | 309 `Iron Man` #122/1979 — bid 271 → fmv 335 |
| 341 | `Iron Man Bronze age Layton` | 125 | 310 `Iron Man` #125/1979 — bid 272 → fmv 336 |
| 342 | `Iron Man Bronze age Layton` | 126 | 311 `Iron Man` #126/1979 — bid 273 → fmv 337 |

Rows 340 and 341 each carry one `fmv` row (363, 364) — both pure shells with
`low`/`high`/`comps` all NULL, zero `bid_fmvs` links and zero `bids.fmv_id`
references. They cascade away, losing nothing. The other six carry no `fmv` at all.

## The premise this ticket inherited, and where it breaks

BUI-596 established that its 173 rows had **`bid_links = 0` across the board**, and
built its preflight gate on that. **That does not hold here.** Four of these 12
rows are load-bearing:

| | BUI-596's 173 | BUI-600's 12 |
| -- | --: | --: |
| rows with a `bid_fmvs` link | 0 | **4** (201, 204, 315, 328) |
| rows referenced by `bids.fmv_id` | 0 | **4** (same) |
| rows carrying FMV price data | 2 (both rewritten) | **2** (201, 204 — both rewritten) |

This inverts BUI-596's conclusion. That plan was **delete-dominant** (134 delete /
39 rewrite) because its rows were inert. Here a third of the rows cannot be
deleted at all, and the split is **8 delete / 4 rewrite**.

### A second gate BUI-596 never needed: `bids.fmv_id`

`bids.fmv_id` is declared as a bare `INTEGER` with **no foreign key**:

```sql
CREATE TABLE bids ( ... , fmv_id INTEGER, ... )   -- added by ALTER, no REFERENCES
```

`fmv.comic_id` and `bid_fmvs.fmv_id` both carry `ON DELETE CASCADE`, so deleting a
`comics` row tidies those automatically. **`bids.fmv_id` is not cleaned up** — it
would be left pointing at a deleted `fmv` id, silently. `PRAGMA foreign_key_check`
would not catch it, because there is no constraint to violate.

BUI-596 got away without this gate because all 173 of its rows had zero links, so
nothing referenced their FMV. Four of these 12 are referenced. `plan.py` counts
`bid_fmv_id_refs` per row and `remediate.py` aborts if any delete target has a
non-zero count; `verify()` additionally proves zero dangling `bids.fmv_id` and
`bids.comic_id` after the write. (`bids.comic_id` is set on **0** of 641 rows
today, so it is inert — checked, not assumed.)

## The rewrite lands on the existing merge path, and it is lossless

The two partial unique indexes are unchanged from BUI-596:

```
idx_comics_tiyv         ON comics(LOWER(title), issue, year, COALESCE(variant,'')) WHERE year IS NOT NULL
idx_comics_tiv_nullyear ON comics(LOWER(title), issue, COALESCE(variant,''))       WHERE year IS NULL
```

All 12 rows have `year IS NULL`, so the rewrites land in the yearless index.
**All four target keys are free — 0 collisions**, and the end-state simulation
reports **0 unique-index violations**.

Each rewrite leaves the row beside its yeared twin as a *yearless orphan* — the
exact shape `_merge_yearless_into_yeared()` in
`plugins/gixen-overlay/src/gixen_overlay/db.py` exists to collapse. That function
is lossless: it reparents `fmv` children, reparents `bid_fmvs` rows, and
reparents `bids.fmv_id`, then removes the redundant shell.

This was **not** taken on faith. The real plugin function was run against a
scratchpad copy after applying the plan:

```
201 -> 172 : bid 152 links=1 now-> fmv 184 on comic 172 (7.5, 40.0, 45.0, comps=2)
204 -> 175 : bid 155 links=1 now-> fmv 187 on comic 175 (7.5, 15.0, 25.0, comps=1)
315 -> 232 : bid 168 links=1 now-> fmv 250 on comic 232 (4.5, 40.0, 60.0, comps=4)
328 -> 312 : bid 244 links=1 now-> fmv 338 on comic 312 (7.5, None, None, comps=0)
```

Every bid link survives, and three of the four land on **strictly better** FMV
data than they had. Bid 168 in particular — the WON auction — moves from an empty
shell to `40–60 / comps=4 / medium`. `bid_fmvs` stays at 613 rows, `integrity_check`
ok, `foreign_key_check` clean, 0 dangling references.

The plan **does not run that merge.** Doing so would mean re-implementing plugin
logic inside a remediation script and touching `bids`/`bid_fmvs`, which would
destroy the strongest property of the BUI-596 verifier — *"`bid_fmvs` and `bids`
untouched"*. The convergence step is left to the sanctioned endpoint; see the
README.

## Two rows of the same root cause, deliberately NOT in this plan

The wider vocabulary scan (adding ordinal tokens) surfaced two more rows with the
same origin, a third distinct signature — **partially** normalized, and yeared:

```
330  'Thor FA/ .5 1st issue Hercules'                    issue=126  year=1966
       source: bid 254 'Thor #126 FA/GD 1.5 1st issue Hercules Cover! Jack Kirby Cover! Marvel 1966'
335  'Thor .5 2nd Wrecker! Origin Black Bolt! Inhumans'  issue=149  year=1968
       source: bid 261 'Thor #149 FN+ 6.5 2nd Wrecker! Origin Black Bolt! Inhumans!  Marvel 1968'
```

Something stripped `#126` and the `GD 1` / `FN+ 6` grade tokens out of the middle
of the title, leaving mangled fragments. Both have a clean twin (295 `Thor`
#126/1966 and 301 `Thor` #149/1968), both carry only an empty `fmv` shell with 0
links and 0 `bids.fmv_id` references, and both would be clean deletes.

**They are not in this plan.** They carry no age marker, they are yeared rather
than yearless (so a rewrite would hard-collide with the twin, unlike all 12 here),
and no human adjudicated them as part of this ticket. Widening a data migration
past its reviewed set is precisely what this ritual exists to prevent. They are
recorded in `plan.py` as `OUT_OF_SCOPE` and printed by every dry run.
**Recommend a follow-up ticket.**

## Residual risk, stated honestly

1. **The selection is human judgement, not a proof.** If the hand-reading of any
   row is wrong, no gate catches it — the gates verify *safety* (nothing
   load-bearing is destroyed), not *correctness of the adjudication*. The
   fingerprint check narrows this to "wrong about a row I actually read", which
   is the best available guarantee for a curated list.
2. **The rewrites preserve source casing and masthead**, the same conservative
   choice BUI-596 made. `Avengers` is used rather than `The Avengers`, matching
   the 25 existing `Avengers` rows and the source listings; note 205 is
   `The Avengers` #79/1970, so a leading-article duplicate pair persists in the
   table. Deciding those are the same series is a masthead-alias judgement
   (the BUI-581 class) and is not made here.
3. **Four yearless/yeared orphan pairs are created deliberately** (17 total, up
   from 13). They are inert and not index violations, and they are the documented
   input to the existing sweep. If the sweep is never run they simply persist,
   the same residual BUI-596 accepted.
4. **Two known malformed rows are knowingly left in the table** (330, 335). The
   table will not be "clean" after this write, only clean of the adjudicated class.
5. **The write boundary is still open.** BUI-599 covers the writer that produces
   these shapes. Cleaning now without closing it means the class recurs — the same
   caveat BUI-596 recorded.
