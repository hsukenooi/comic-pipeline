---
title: "PER-132: seller-scan matcher false positives (timemachinecomics reference set)"
type: bug
status: active
date: 2026-05-24
linear: PER-132
---

# PER-132: Matcher False Positives

Reference set from `timemachinecomics` scan on 2026-05-24. 53 results total.
Goal: reduce to ≤15, all genuine series+issue matches.

## Already implemented (2026-05-24 session)

These fixes are live in `apps/ebay/src/seller_scan.py`:

1. **Word-set matching** — changed from substring (`t in title_norm`) to exact word set (`t in title_words` where `title_words = set(title_norm.split())`). Prevented single-char tokens from matching inside longer words.
2. **Min token length 2** — `_series_tokens` filters `len(t) >= 2`, dropping single-char noise tokens like `s`, `k`, `o` from series names with apostrophes or abbreviations.
3. **"comics" added to `_STOPWORDS`** — prevents "Detective Comics", "Futurama Comics" etc. from matching every listing that contains "(Marvel Comics)" or "(DC Comics)".
4. **Score threshold raised 0.5 → 0.65** — requires more token overlap; eliminates 50% matches on 2-token series like "Fantastic Four" where only one token appeared.
5. **CGC filter** — listings with "cgc" in title are skipped entirely in `main()`.
6. **Auction-only filter** — eBay fetch uses `buyingOptions:{AUCTION}` filter; BIN listings are excluded at the API level.

These reduced the result set from 229 → 53.

## Root cause patterns identified

| # | Pattern | Example |
|---|---------|---------|
| A | **Year-as-issue**: 2-digit year in title matches a 2-digit issue number | "Fantastic Four #135 (Jun 73)" matches wish "Fantastic Four #73" |
| B | **Annual/Giant-Size matching regular issue**: listing says "Annual #N" or "Giant-Size #N" but matches wish for plain "#N" | "Avengers Annual #1" matches "The Avengers #1" |
| C | **Spider-Man spinoff matching ASM**: "Noir", "Superior", "Spectacular", "Ultimate", "Giant-Size" Spider-Man titles match ASM wish items | "Spider-Man Noir #1" matches "The Amazing Spider-Man #1" |
| D | **Wrong series, right number**: different series with shared tokens matches | "X-Factor #1" matches "The X-Men #1" (token: "men"→no, actually "x"? needs investigation); "Avengers #53" matches "X-Men #53" |
| E | **Lot title containing number**: issue number appears in lot description | "Huge Lot 210+ Comics" matches "Batman #210" |
| F | **Giant-Size crossing series**: Giant-Size edition of series A matches Giant-Size edition of series B | "Giant-Size Man-Thing #1" matches "Giant-Size X-Men #1"; "Giant-Size Spider-Man #4" matches "ASM #4" |
| G | **Venom/variant title with series mention**: "#9 (174) Fantastic Four Villains" matches "Fantastic Four #9" | "Venom #9 (174) Variant-Fantastic Four Villains" matches "Fantastic Four #9" |

## Full annotated results

Legend: ✅ genuine | ❌ false positive | ⚠️ uncertain (variant/reprint, needs manual check)

| Status | Listing Title | Matched Wish | Score | Pattern | Notes |
|--------|---------------|--------------|-------|---------|-------|
| ❌ | Huge Lot 210+ Comics W/ Batman, Green Lantern, +More! Avg FN/VF | Batman #210 | 1.0 | E | "210" in lot description |
| ❌ | Spider-Man Noir #1 (Marvel Comics February 2009) Beautiful NM | The Amazing Spider-Man #1 | 0.67 | C | Noir ≠ Amazing |
| ⚠️ | The Amazing Spider-Man #1 Trick or Read "Birth of Tombstone" VF-NM | The Amazing Spider-Man #1 | 1.0 | — | Promo/reprint; probably not the desired issue |
| ❌ | Fantastic Four #71 (Marvel Comics 1968) GD/VG 1 in spine split | Fantastic Four #1 | 1.0 | A | "1" matches from "1 in spine split" |
| ✅ | The Mighty Thor #1 (Marvel Comics December 2014) NM- | The Mighty Thor #1 | 1.0 | — | |
| ❌ | Wolverine: Old Man Logan Giant-Size #1 Mcniven Cover VF/NM | Giant-Size X-Men #1 | 0.67 | F | Wrong Giant-Size series |
| ❌ | The Avengers #53 (Jun 68) GVG "Avengers vs The X-Men!" | X-Men #53 | 1.0 | D | "53" + "men" from subtitle — completely different series |
| ✅ | World's Finest Comics #210 (DC Comics March 1972) FN/VF | World's Finest Comics #210 | 1.0 | — | |
| ❌ | Venom #9 (174) Variant Edition-Fantastic Four Villains-Bill Sienkiewicz Cover NM | Fantastic Four #9 | 1.0 | G | "Fantastic Four" is subtitle, not series |
| ✅ | World's Finest Comics #204 (DC Comics August 1971) VF | World's Finest Comics #204 | 1.0 | — | |
| ✅ | The Amazing Spider-Man #309 (Marvel Comics Late November 1988) VF+ | The Amazing Spider-Man #309 | 1.0 | — | |
| ❌ | Fantastic Four #70 (Marvel Comics January 1968) FN- | Fantastic Four #70 | 1.0 | A | Wait — wish IS #70. ✅ Actually genuine. |
| ✅ | World's Finest Comics #199 (DC Comics December 1970) VF- | World's Finest Comics #199 | 1.0 | — | |
| ❌ | Giant-Size Spider-Man #4 (Marvel Comics April 1975) FN- | The Amazing Spider-Man #4 | 0.67 | F | Giant-Size spinoff ≠ main series |
| ❌ | Spider-Man Noir #3 (Apr 09) Fine | The Amazing Spider-Man #3 | 0.67 | C | Noir ≠ Amazing |
| ✅ | Green Lantern #86 (Nov 71) "Drug Issue" VG/Fine | Green Lantern #86 | 1.0 | — | |
| ✅ | Green Lantern #85 (Sep 71) "Drug Issue" VG | Green Lantern #85 | 1.0 | — | |
| ✅ | Fantastic Four #87 (Marvel Comics June 1969) FN- | Fantastic Four #87 | 1.0 | — | |
| ✅ | The Mighty Thor #167 (Marvel Comics August 1969) see condition | Thor #167 | 1.0 | — | |
| ❌ | The Spectacular Spider-Man Annual #1 (Marvel Comics 1979) FN- | The Amazing Spider-Man #1 | 0.67 | B+C | Annual + wrong series |
| ✅ | Fantastic Four #54 (Marvel Comics September 1966) VG+ | Fantastic Four #54 | 1.0 | — | |
| ✅ | Amazing Spider-Man #10 (811) Variant NM | The Amazing Spider-Man #10 | 1.0 | — | LGY issue; genuine |
| ❌ | Spider-Man Noir #2 (Mar 09) Sharp VF+ | The Amazing Spider-Man #2 | 0.67 | C | Noir ≠ Amazing |
| ✅ | Fantastic Four #63 (Marvel Comics June 1967) FN/VF | Fantastic Four #63 | 1.0 | — | |
| ✅ | Fantastic Four #54 (Marvel Comics September 1966) FN [duplicate listing] | Fantastic Four #54 | 1.0 | — | Duplicate item_id |
| ✅ | Fantastic Four #53 (1966) VG/FN 2nd Black Panther! | Fantastic Four #53 | 1.0 | — | |
| ❌ | Giant-Size Fantastic Four #5 (1975) VG/FN 1/2 in spine split | Fantastic Four #1 | 1.0 | A+B | "1" from "1/2"; Giant-Size ≠ main series |
| ✅ | Detective Comics #576 (DC Comics July 1987) FN+ | Detective Comics #576 | 1.0 | — | |
| ✅ | Fantastic Four #96 (Marvel Comics March 1970) see condition | Fantastic Four #96 | 1.0 | — | |
| ⚠️ | Invincible #1 Amazon Animated Series Promo #1 Beautiful NM | Invincible #1 | 1.0 | — | Promo; probably not the desired issue |
| ❌ | The Avengers Annual #1 (Marvel Comics September 1967) GD | The Avengers #1 | 1.0 | B | Annual ≠ regular issue |
| ❌ | Giant-Size Fantastic Four #5 [duplicate] | Fantastic Four #1 | 1.0 | A+B | Same as above |
| ✅ | Fantastic Four #92 (Marvel Comics November 1969) VF+ | Fantastic Four #92 | 1.0 | — | |
| ✅ | Fantastic Four #54 [3rd duplicate] | Fantastic Four #54 | 1.0 | — | |
| ✅ | The Mighty Thor #152 (Marvel Comics May 1968) see condition | Thor #152 | 1.0 | — | |
| ✅ | Fantastic Four #92 [duplicate] | Fantastic Four #92 | 1.0 | — | |
| ❌ | Ultimate Spider-Man #1 Solid VG Moisture Wrinkle | The Amazing Spider-Man #1 | 0.67 | C | Different series |
| ⚠️ | The Amazing Spider-Man #1 "Rise of Queen Goblin" Trick or Read LGY889 NM- | The Amazing Spider-Man #1 | 1.0 | — | Promo/modern reprint; check if wanted |
| ✅ | Fantastic Four #72 (Marvel Comics March 1968) VG/FN | Fantastic Four #72 | 1.0 | — | |
| ❌ | The Avengers Annual #2 (1968) GD/VG 1 1/2 in cumulative spine split | The Avengers #1 | 1.0 | A+B | Annual + "1" from "1 1/2 in spine split" |
| ❌ | Amazing Spider-Man Annual #9 (Marvel 1973) VG+ 1" cumulative spine split | The Amazing Spider-Man #1 | 1.0 | A+B | Annual + "1" from '1"' measurement |
| ✅ | The Transformers #1 (Marvel Comics Sep 84) VG+ | Transformers #1 | 1.0 | — | |
| ✅ | Fantastic Four #62 VG+ | Fantastic Four #62 | 1.0 | — | |
| ❌ | Giant-Size The Avengers #1 (Marvel Comics August 1974) VG+ | The Avengers #1 | 1.0 | B | Giant-Size ≠ regular series |
| ❌ | Fantastic Four #135 (Jun 73) vs Dragon-man! GVG | Fantastic Four #73 | 1.0 | A | "73" matches year "(Jun 73)" |
| ✅ | The Mighty Thor #163 (Marvel Comics April 1969) VG | Thor #163 | 1.0 | — | |
| ⚠️ | Invincible #1 Local Comic Shop Day #1 (Image Comics Nov 20) NM | Invincible #1 | 1.0 | — | LCSD variant; check if wanted |
| ✅ | The Mighty Thor #171 (Marvel Comics December 1969) VG+ | Thor #171 | 1.0 | — | |
| ✅ | The Mighty Thor #172 (Marvel Comics January 1970) see condition | Thor #172 | 1.0 | — | |
| ❌ | Superior Spider-Man #1 (Marvel Comics March 2013) NM- | The Amazing Spider-Man #1 | 0.67 | C | Different series |
| ❌ | Green Lantern #195 Newsstand Variant (Dec 85) Fine+ | Green Lantern #85 | 1.0 | A | "85" matches year "(Dec 85)" |
| ⚠️ | Invincible Undeluxe #1 JAN 2023 Image Comics Beautiful NM | Invincible #1 | 1.0 | — | "Undeluxe" variant; check if wanted |
| ❌ | X-Factor #1 Newsstand Variant (Feb 86) Original X-Men Return! VF- | The X-Men #1 | 1.0 | D | X-Factor ≠ X-Men; "men" from subtitle "X-Men Return" |
| ❌ | Giant-Size Man-Thing #1 (Marvel Comics August 1974) VG/FN | Giant-Size X-Men #1 | 0.67 | F | Wrong Giant-Size series |

## Summary

- ✅ Genuine: ~25
- ❌ False positive: ~23
- ⚠️ Uncertain: 5

## Fixes needed (priority order)

### Fix 1: Year-in-title as issue number (Pattern A)
Most impactful. 2-digit years like `(Jun 73)` or `(Dec 85)` match 2-digit issue numbers.
- Reject issue number match if the number only appears inside a year context: `\((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{2}\)` or `\b19\d{2}\b`
- Or: require issue match only near `#` (not as isolated `\bN\b`)

### Fix 2: Annual/Giant-Size matching regular issue (Pattern B)
Title has "Annual" or "Giant-Size" but wish item is a plain issue number.
- Detect "Annual" in listing title → require wish item to also have "Annual" in its series name
- Detect "Giant-Size" in listing title → require wish to be a Giant-Size item

### Fix 3: Spider-Man spinoffs (Pattern C)
"Noir", "Superior", "Spectacular", "Ultimate", "Giant-Size" Spider-Man titles matching ASM.
- Add "amazing" to required tokens when matching ASM wish items?
- Or: require that if wish series is "The Amazing Spider-Man", listing title must contain "amazing"

### Fix 4: Wrong-series-right-number via subtitle (Patterns D, G)
"Avengers #53" matches "X-Men #53" because subtitle says "vs The X-Men". "Venom #9" matches "FF #9" because subtitle says "Fantastic Four Villains".
- Hard: the series tokens match because they appear in the subtitle/description, not the actual series name
- Possible fix: weight tokens that appear near the `#` higher than tokens in the rest of the title

### Fix 5: Lot titles (Pattern E)
"Huge Lot 210+ Comics" matches "Batman #210".
- Detect "Lot" in listing title → skip matching entirely

### Fix 6: Giant-Size crossing series (Pattern F)  
"Giant-Size Spider-Man #4" matches "ASM #4" — tokens ["spider", "man"] overlap.
- If listing says "Giant-Size X" and wish doesn't say "Giant-Size", reject.
- If listing says "Giant-Size X" and wish says "Giant-Size Y", require full series token overlap including the Giant-Size qualifier.
