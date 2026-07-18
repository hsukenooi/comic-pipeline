# Wish-List Cover-Year Backfill — executed run (2026-07-18)

The reviewed, executed record of the one-time BUI-387 backfill described in
[`wishlist-year-backfill.md`](./wishlist-year-backfill.md). This file **is** the
BUI-129 decision: each stamped year is the issue's own Metron `cover_date` year,
cross-checked against the owned volume's era (a decoy exists precisely because
its intended Cover Year is older than the owned volume). Kept in-repo so the
one-time write is auditable and reproducible.

- **Run on:** the Mac Mini, against the server store (`~/.gixen-server/collection-store/wish-list.json`), after `uv tool install --force ./packages/locg-cli`.
- **Backup taken first:** `wish-list.json.bak.bui387-backfill-20260718-093220`.
- **Result:** conflicts audit **33 → 7** (`printing_conflicts` unchanged at 2). 26 stamped names cleared (27 entries — "The Avengers #1" had two duplicate entries, both stamped in one `matched=2` call).
- **Grounding:** every cover year came from Metron (`scripts/metron-curl.sh`), never memory. Series IDs: The Amazing Spider-Man (1963)=835, The Avengers (1963)=1583, Fantastic Four (1961)=26, The Incredible Hulk (1962)=1608, The X-Men (1963)=1581.

## Stamped (27 entries / 26 names)

| # | Wish (stamped) | Metron cover_date | Year | Owned-as (era cross-check) | Cleared? |
|---|----------------|-------------------|------|-----------------------------|----------|
| 1 | The Amazing Spider-Man #1 | 1963-03-01 | 1963 | The Amazing Spider-Man (Vol. 5) (2018 - 2022) | yes |
| 2 | The Amazing Spider-Man #3 | 1963-07-01 | 1963 | The Amazing Spider-Man (Vol. 2) (1998 - 2013) | yes |
| 3 | The Avengers #1 | 1963-09-01 | 1963 | Avengers (Vol. 5) (2012 - 2015) | yes |
| 4 | Fantastic Four #4 | 1962-05-01 | 1962 | Fantastic Four (Vol. 7) (2022 - 2025) | no — owned row has no release_date (see finding) |
| 5 | Fantastic Four #18 | 1963-09-01 | 1963 | Fantastic Four (Vol. 7) (2022 - 2025) | yes |
| 6 | The X-Men #1 | 1963-09-01 | 1963 | X-Men (Vol. 2) (1991 - 2001) | yes |
| 7 | The X-Men #2 | 1963-11-01 | 1963 | X-Men (Vol. 2) (1991 - 2001) | yes |
| 8 | The X-Men #3 | 1964-01-01 | 1964 | X-Men (Vol. 2) (1991 - 2001) | yes |
| 9 | The X-Men #4 | 1964-03-01 | 1964 | X-Men (Vol. 2) (1991 - 2001) | yes |
| 10 | The X-Men #5 | 1964-05-01 | 1964 | X-Men (Vol. 2) (1991 - 2001) | yes |
| 11 | The X-Men #6 | 1964-07-01 | 1964 | X-Men (Vol. 2) (1991 - 2001) | yes |
| 12 | The X-Men #7 | 1964-09-01 | 1964 | X-Men (Vol. 2) (1991 - 2001) | yes |
| 13 | The X-Men #8 | 1964-11-01 | 1964 | X-Men (Vol. 2) (1991 - 2001) | yes |
| 14 | The X-Men #9 | 1965-01-01 | 1965 | X-Men (Vol. 2) (1991 - 2001) | yes |
| 15 | The X-Men #10 | 1965-03-01 | 1965 | X-Men (Vol. 2) (1991 - 2001) | yes |
| 16 | The X-Men #12 | 1965-07-01 | 1965 | X-Men (Vol. 6) (2019 - 2021) | yes |
| 17 | The X-Men #13 | 1965-09-01 | 1965 | X-Men (Vol. 6) (2019 - 2021) | yes |
| 18 | The X-Men #14 | 1965-11-01 | 1965 | X-Men (Vol. 2) (1991 - 2001) | yes |
| 19 | The X-Men #15 | 1965-12-01 | 1965 | X-Men (Vol. 2) (1991 - 2001) | yes |
| 20 | The X-Men #16 | 1966-01-01 | 1966 | X-Men (Vol. 2) (1991 - 2001) | yes |
| 21 | The X-Men #17 | 1966-02-01 | 1966 | X-Men (Vol. 2) (1991 - 2001) | yes |
| 22 | The X-Men #41 | 1968-02-01 | 1968 | X-Men (Vol. 2) (1991 - 2001) | yes |
| 23 | The Incredible Hulk #1 | 1962-05-01 | 1962 | Hulk (Vol. 5) (2021 - 2023) | yes |
| 24 | The Amazing Spider-Man #28 | 1965-09-01 | 1965 | The Amazing Spider-Man (Vol. 7) (2025 - Present) | yes |
| 25 | The Amazing Spider-Man #29 | 1965-10-01 | 1965 | The Amazing Spider-Man (Vol. 7) (2025 - Present) | yes |
| 26 | The Amazing Spider-Man #30 | 1965-11-01 | 1965 | The Amazing Spider-Man (Vol. 7) (2025 - Present) | yes |

## Left unstamped (6) — reported for manual decision

Per the BUI-129 "do not guess" rule: an issue whose cover year can't be
confidently resolved, or whose intended year is the **same era** as the owned
volume (a genuine conflict, not a decoy), is left unstamped — it keeps today's
safe year-blind behavior (over-flag, never miss an owned book).

| Wish (left unstamped) | Owned-as | Reason |
|-----------------------|----------|--------|
| The Mighty Thor #1 | Thor (Vol. 4) (2014 - 2015) | Ambiguous — no clear vintage vol.1; owned Thor Vol.4 (2014); modern "Mighty Thor" #1s exist. Not guessed (BUI-129). |
| X-Men #19 1:50 Leinil Francis Yu Marvel Comics 50th Anniversary Variant | X-Men (Vol. 2) (1991 - 2001) | Variant-cover wish; volume/era unresolved from the name. |
| X-Men #1 Two Per Store Leinil Francis Yu Premiere Variant | X-Men (Vol. 2) (1991 - 2001) | Modern Leinil Yu variant; volume/year unresolved from the name. |
| X-Men #1 Walmart Leinil Francis Yu Variant | X-Men (Vol. 2) (1991 - 2001) | Modern Leinil Yu variant; volume/year unresolved from the name. |
| X-Men #1 Cover D Jim Lee Magneto Connecting Variant | X-Men (Vol. 2) (1991 - 2001) | Same-era (1991 Jim Lee X-Men #1 cover) — genuine, not a year-decoy; stamping would dodge a real owned book. |
| X-Men #1 Cover E Jim Lee Collectors Wraparound Gatefold Variant | X-Men (Vol. 2) (1991 - 2001) | Same-era (1991 Jim Lee X-Men #1 cover) — genuine, not a year-decoy; stamping would dodge a real owned book. |

## Not-yet-cleared, explained

- **Fantastic Four #4** — stamped `1962` correctly, but the owned `Fantastic Four (Vol. 7) #4` row has **no `release_date`**, so the year-gate has nothing to compare and conservatively keeps flagging (BUI-122-safe). Tracked as **BUI-412**; it clears automatically once that owned row gets a date. No wish-list change needed — the stamp is already right.

## Reproduce / extend

The stamps are import-durable (BUI-208); the only way to lose them is a manual
re-seed of `wish-list.json` from a LOCG-derived export. To re-run or extend
(e.g. after resolving a skipped entry's volume/year), on the Mac Mini:

```bash
export LOCG_DATA_DIR=~/.gixen-server/collection-store
locg wish-list set-year "<exact wish name>" <metron_cover_year>
curl -sf "$COMICS_SERVER_URL/api/comics/wish-list/conflicts" | jq '.conflicts | length'
```
