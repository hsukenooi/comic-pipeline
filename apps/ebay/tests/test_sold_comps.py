"""Tests for sold_comps.py — eBay sold-listings via SerpApi.

Pure-function tests only. Network calls are exercised through fetch() with
a mocked requests.get; no real SerpApi calls happen in CI.
"""

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import requests

import comic_identity
import sold_comps as sc


# ─── Grade parsing ────────────────────────────────────────────────────────────

class TestParseGrade:
    @pytest.mark.parametrize("title,expected", [
        # Numeric grades — including the previously-broken 9.2/9.4/9.6/9.9
        ("ASM #270 NM 9.2 Marvel 1985", 9.2),
        ("ASM #270 NM 9.4 Marvel 1985", 9.4),
        ("Wolverine #1 9.6 condition", 9.6),
        ("Hulk #181 9.8 OW pages", 9.8),
        ("X-Men 9.9 unicorn", 9.9),
        ("ASM #142 5.5 OW pages", 5.5),
        ("VG 4.0 Bronze Age", 4.0),
        ("(8.5) cover detached", None),  # cover-detached hard-excludes downstream, but grade still parses
        # Letter grades
        ("Uncanny X-Men #185 NM-", 9.2),
        ("Uncanny X-Men #185 NM", 9.4),
        ("Batman #224 FN+", 6.5),
        ("Batman #224 FN/VF", 7.0),
        ("VF/NM bright cover", 9.0),
        ("Spider-Man VG+", 4.5),
        # Numeric beats letter when both present
        ("ASM #270 NM 9.2", 9.2),
        # No grade
        ("Just a comic title 1985", None),
    ])
    def test_parse(self, title, expected):
        if expected is None and "(8.5)" in title:
            # `(8.5)` should still parse — boundaries on `(` are word-boundary safe
            assert sc.parse_grade(title) == 8.5
        else:
            assert sc.parse_grade(title) == expected

    def test_letter_priority(self):
        # 'NM-' should win over 'NM' (longer pattern matched first by ordering)
        assert sc.parse_grade("Some NM- copy") == 9.2

    def test_combined_letters(self):
        assert sc.parse_grade("VF/NM 9.0 bright") == 9.0
        # The numeric 9.0 wins, but if it weren't there, VF/NM would still give 9.0
        assert sc.parse_grade("VF/NM bright") == 9.0

    # ── BUI-183: price/measurement context must NOT be mis-read as a grade ──

    @pytest.mark.parametrize("title", [
        # Price context ($ prefix)
        "$9.5 shipping",
        "price $9.5",
        # Measurement units following the number
        "lot 3.5 inches",
        "lot 3.5 inch",
        "ships 9.5 oz",
        "size 2.5 cm",
        "3.5 mm wide",
        "2.5 lbs weight",
        "3.5 lb box",
        "9.5 in long",
        # Dimension separator (x)
        "ruler 2.5 x 3.5",
        "2.5 x 3.5 size",
        # X.X0 price-like forms: a trailing digit means it's a number, not a
        # one-decimal grade (the restored trailing boundary, BUI-183).
        "comic 5.50 dollars",
        "lot of 9.50 value",
    ])
    def test_price_measurement_context_excluded(self, title):
        """Numbers in price or measurement context must never be parsed as a grade."""
        assert sc.parse_grade(title) is None

    @pytest.mark.parametrize("title,expected", [
        # Bare numeric grades with trailing non-unit words must still parse
        ("ASM #300 9.8", 9.8),
        ("X-Men #1 6.0 nice", 6.0),
        # Titles starting with X- must not be blocked by the dimension-x lookbehind
        ("X-Men #1 9.8", 9.8),
        ("X-Factor #6 9.4", 9.4),
    ])
    def test_bare_numeric_grades_survive(self, title, expected):
        """Bare numeric grades with non-unit trailing words must still be detected."""
        assert sc.parse_grade(title) == expected


# ─── Hard excludes ────────────────────────────────────────────────────────────

class TestHardExclude:
    @pytest.mark.parametrize("title", [
        "Coverless ASM #5 1963",
        "ASM #142 missing pages",
        "Lot of 5 Spider-Man comics",
        "ASM #1-5 lot",
        "ASM #5 facsimile edition 2024",
        "ASM #5 Marvel Tales reprint",
        "Batman #608 UK pence variant",
        "X-Men vol 2 #1",
        "ASM #5 PSA 8 graded",
        "Spider-Man action figure 1:6 scale",
        "Spawn #1 trading card Upper Deck",
        "ASM #129 signed by Stan Lee",
        "WW Live Sale ASM result",
        "X-Men #1 restored copy",
        # BUI-269: previously sold_comps-only markers, now unioned into
        # comic_identity — pin that reconciling the two lexicons didn't drop
        # coverage on the sold_comps side.
        "ASM #300 9d variant UK",
        "Fantastic Four #1 rare Brazil edition",
        "Amazing Spider-Man #1 rare Mexico edition",
        "Batman #1 Norway edition",
        "Superman #1 Australia edition",
        "ASM #1 Italian edition",
        "ASM #1 Spain edition",
        "ASM #1 Ebal edition",
        "Spawn #1 Johnny Lightning promo",
        # BUI-269 (Opus PR#119 review): multi-issue lot shapes the reconciled
        # lexicon must still exclude from the comp pool — the shared _LOT_RE
        # misses these, so is_comp_excluded's comp-only _FMV_LOT_RE covers them.
        "Amazing Spider-Man #1 #2 #3 CGC",   # space-separated hash list (3)
        "Hulk #181 #182",                     # space-separated hash pair (2)
        "ASM #64, #65",                       # 2-member comma pair (both hashed)
        "ASM #64, 65",                        # 2-member comma pair (2nd unhashed)
        # BUI-598: a BARE (un-'#'-anchored) issue range paired with a run/set
        # word. Every title here is a real corpus listing that survived
        # hard_exclude before this rule; the first is the $6500 full-run lot
        # that manufactured a $3825 max bid on a single X-Men #94.
        "X-Men 94-300 FULL RUN NM Marvel 1975 Key 94 101 129 141 266",
        "THE SAVAGE SHE-HULK 1-25 NEAR FULL RUN!!! MARVEL COMICS 1980",
        "Watchmen 1-12 Run, Complete, Alan Moore 1986 VF 8.0+",
        "DETECTIVE COMICS 575-578 1987 BATMAN YEAR TWO 1-4 SET TODD MCFARLANE DC COMICS",
        "X-MEN 92 HOUSE OF XCII 1-5 MARVEL COMIC SET COMPLETE FOXE ESPIN SILVA 2022 NM",
        "Absolute Martian Manhunter 1-10 Set Cover 1 2 3 4 5 6 7 8 9 10 Random Printings",
        # keyword BEFORE the range — the rule is order-independent
        "(2026) ABSOLUTE MARTIAN MANHUNTER #1 2 3 4 5 6 CURRENT PRINT SET! 1-6",
        # en dash (U+2013) is a real range separator in listing titles
        "Venom #1–4 (2018) Shiver Set NM Donny Cates + Sam KIETH Art Marvel",
        # BUI-637: a SPACE-separated bare-number issue enumeration (>=4 members,
        # strictly ascending). Every title here is a real corpus listing that
        # survived hard_exclude before this rule. The Batman #655-658 set is the
        # one that mattered: four such comps priced a single #655 at fmv_high
        # $100 where the single-issue pool median was ~$16.
        "Batman 655 656 657 658 1st Damian Wayne NM Set Kubert DC Comics 2006",
        "X-Men 46 47 48 49 50 51 52 1995 VF/NM Rare Newsstand Variants Gambit Bishop",
        "CLASSIC X-MEN 41 42 43 44 45 NM 9.4 9.6 Dark Phoenix Saga WOLVERINE Marvel 1989",
        "Spawn 1 2 3 4 5 DIRECT Todd McFarlane Image Comics 1992",
        # '#' on the first member only — the shape _FMV_LOT_RE misses because
        # it requires a hash on at least the first TWO members.
        "Detective Comics #523 524 525 526 DC Comics 1983",
        # non-contiguous enumeration (skips issues) — still a lot
        "Uncanny X-Men #146 147 148 152 154 157 158 162 (1981-82) Claremont Cockrum",
        # the ascending slice may sit anywhere in the run, not just at its head
        "Ghost Rider #15 15 (2nd) 16 19 20 21 23 24 25 26 39! VF/NM! 1990! 1st Badalino!",
        # BUI-645: an ORDINAL later printing. _reprint_reject's lexicon carries
        # only the "-ing" spellings ("2nd printing"/"second printing"), so every
        # title here is a real corpus listing that survived hard_exclude before
        # this rule. The Ghost Rider #15 and Hulk #377 pools each had 14 and 12
        # such comps, holding fmv_high at $15 where $10 is right (-33%).
        "Ghost Rider #15 2nd Print Marvel 1991",
        "The Incredible Hulk #377 (Marvel Comics January 1991) - VF/NM - 2nd Print",
        "1986 Copper Age DC Comic Watchmen #1 2nd Print Newstand Edition NM-",
        "Absolute Flash #3 3rd Print",
        "Absolute Batman #6 5th Print Comic Book 2026",
        "ABSOLUTE FLASH #2 Third Printing",
        "Ultimate Spider-Man #1 Fourth Printing-Marco Checchetto (Marvel Comics 2024) NM",
        "Ultimate Spider-Man #1 Fifth Printing-Marco Checchetto NM 2024 Marvel Comics",
        "Saga #1 Fourth Print (Image Comics March 2012)",
        "X-MEN '97 #2 Second Print Gambit and Rogue Cover Marvel Comics NM HOT!",
        "Venom #1 Stegman 2nd Print 2018 Marvel Comics NM",
        # A lot bundling a 1st WITH a later print is not a single-issue comp
        # either — the ordinal token catches it regardless of the "1st Print".
        "GHOST RIDER #15 (1991) 1st Print & Gold 2nd Print Glow in the Dark Cover NM",
        "Marvel The Incredible Hulk #377 (1991) direct 1st & 2nd print",
    ])
    def test_excludes(self, title):
        assert sc.hard_exclude(title)

    @pytest.mark.parametrize("title", [
        "ASM #142 FN+ 1975",
        "Uncanny X-Men #185 VF Marvel",
        "Batman #226 1970 Neal Adams cover",
        "Aliens vs Predator #2 Dark Horse 1990",
        # BUI-269 (Opus PR#119 review): a single issue must NOT be caught by the
        # new comp-only lot regex — it has only one # token.
        "Amazing Spider-Man #300",
        "X-Men #266 CGC 9.8",
        # comma edge: "#1, 2018" is a hash then a YEAR, not a 2-issue lot —
        # _FMV_LOT_RE bounds the comma member to 1-3 digits to avoid this.
        "Detective Comics #1, 2018",
        # comma edge: "#300, 9.8" is a hash then a decimal GRADE, not a lot —
        # the (?!\.\d) lookahead keeps this single graded issue in the comp pool.
        "Amazing Spider-Man #300, 9.8 CGC",
        # ── BUI-598 negative controls ────────────────────────────────────────
        # The new bare-range rule fires only when a run/set word CO-OCCURS.
        # These are real corpus titles where a bare 1-3 digit range appears but
        # means something other than an issue range — a range-only rule would
        # have dropped them, so pin that they stay in the comp pool.
        "CAPTAIN AMERICA # 100, Vol. 1, Marvel 1967, Kirby, VG+/FN- (2-/2-3)",
        "Ultimate X-Men #21 Peach Momoko Connecting Covers 1-3 Trinity Comics LTD 850",
        "Marvel Comics the West Coast Avengers #45 1989. Excellent Condition. 2-5.",
        # The mirror case: a run/set word with NO range. "run" is how listings
        # name a CREATOR run on a single issue, so it can never exclude alone.
        "New Mutants #98 1st Deadpool Claremont run VF",
        "Daredevil #181 Frank Miller run Death of Elektra",
        "Marvel Comics: X-Men '97 Mixed Animated Comic Variant Set (2024-2025)",
        # Year spans must never read as an issue range — the \d{1,3} member
        # bound inherited from _LOT_MEMBER is what guarantees it. Both of these
        # carry a run/set word, so ONLY that bound keeps them.
        "Amazing Spider-Man #4 1962-1963 Ditko run VF",
        "Fantastic Four #48 (1966) Kirby 1961-1970 run Silver Age set",
        # A decimal grade pair straddling a dash is not an issue range either.
        "X-Men #94 CGC 9.8 - 9.6 Claremont run",
        # The ticket's stated control: no range, no keyword.
        "DC Comics Presents #26 1980 VF",
        # Span guard: adjacent integers are grade notation / box codes, never a
        # run. This is the real corpus title above with the one word added that
        # would otherwise have dropped a genuine Silver Age key from the pool.
        "CAPTAIN AMERICA # 100, Vol. 1, Marvel 1967, Kirby run, VG+/FN- (2-/2-3)",
        # Span guard: a DESCENDING pair is never an issue range.
        "NEW McFarlane Toys Marvel Captain America #100 – 1:10 Scale Figure set",
        # Em dash (U+2014) separates phrases in listing titles, so it is
        # deliberately NOT a range separator. Pins the accepted under-catch.
        "X-Men #94 — 300 Claremont run",
        # ── BUI-637 negative controls ────────────────────────────────────────
        # Real corpus titles that carry two adjacent bare numbers because that
        # is how ordinary SINGLE-issue notation reads. A 2-member gate would
        # have dropped every one of these, which is why the floor is 4.
        "Uncanny X-Men #19 1:100 J. Scott Campbell Virgin Variant (2025)",
        "The X-Men #99 25 Cent Variant (Marvel Comics June 1976) NM!!",
        "The Uncanny X-Men #20 720 Cover A 2025 Marvel Comics 1st Print",
        "King Size Hulk #1 VF/NM VHTF NEWSSTAND Variant Marvel 2008 Reprints #180 181",
        "Figpin X-Men ‘92 Rogue & Gambit Set  MARVEL 438 439",
        "Marvel Legends Bishop X-Men 97 6\" Figure MOSC New Sealed",
        # Three ascending members is deliberately below the gate — measured at
        # precision 1.000 on the corpus but declined for no measured money and
        # a real unmeasured failure shape (see the block comment).
        "Uncanny X-Men #286 287 288 - Newsstand / Bishop - Marvel Comics, 1992",
        # A 4-digit YEAR can never join a run: the \d{1,3} member bound means
        # "1995" contributes nothing, leaving a 1-member run here.
        "Amazing Spider-Man 252 1995 VF/NM black costume Marvel",
        # A decimal grade pair can never join a run either (the _LOT_MEMBER
        # lookarounds), so this stays a single graded issue.
        "New Mutants #98 NM 9.4 9.6 1st Deadpool Marvel 1991",
        # A leading non-issue number must not manufacture a run: [92, 2, 3, 4]
        # has a longest ascending slice of 3. Accepted miss, pinned so the
        # slice-not-whole-run semantics can't silently regress to whole-run.
        "X Men '92 2 3 4",
        # Descending numbers are never an issue enumeration.
        "Marvel Kotobukiya ArtFX+ X-Men 92 97 1/10 JUBILEE RARE LOOSE",
        # ── BUI-645 keep-list ────────────────────────────────────────────────
        # Original print-run distribution variants and explicit first prints.
        # 1,307 corpus titles carry one of these words; none may be filtered.
        "X-Men #94 newsstand VF",
        "Amazing Spider-Man #129 direct VF",
        "X-Men #1 first print NM",
        "Amazing Spider-Man #300 1st print",
        # Bare "printing" with no ordinal is a condition descriptor, not a
        # later pressing — left to Haiku, per the BUI-244 comment.
        "Fine printing condition Batman #1",
        # BUI-645 negative controls: the BARE "reprint"/"reprints" token is
        # deliberately NOT on the comp path. X-Men vol.1 #67-93 (1970-1975) were
        # published as all-reprint issues, so their listings honestly say
        # "reprints #28" — but each is a genuine first-print issue with a real
        # market, and these ARE the comps for those books. Excluding them killed
        # the #76 and #79 pools outright and raised #72's fmv_high +25%.
        "X-MEN 76 (VG+) reprints #28 new Gil Kane cover! BANSHEE! 1972 Marvel Comics k733",
        "X-Men #72 VG+ All Reprints 1971 Marvel Comics",
        "X-Men 79 1972 Marvel Comics F/VF 7.0 Reprint X-Men 31 Cyclops Cobalt Man Beast",
        "UNCANNY X-MEN #76 (1972) - GRADE 6.0 - REPRINT 1ST FULL APPEARANCE BANSHEE",
        # Same class: a genuine issue whose title describes what it CONTAINS.
        "Tales to Astonish 60 Giant Man Includes Reprint of Hulk 6 Silver Age 1964",
        "Nick Fury Agent of SHIELD #17 (1971)- Bronze Age, 52-Pg Giant, Reprints",
        "X-Men #66 Marvel 1970 Last Original Story Before Reprints Silver Age",
    ])
    def test_keeps(self, title):
        assert not sc.hard_exclude(title)

    @pytest.mark.parametrize("title", [
        # BUI-668: the gap that motivated the fix — a bare "Signed <name>" that
        # `signed\s+by` never matched, so a signed/COA copy entered a raw pool.
        # This exact listing is the BUI-665 IQR re-admit that surfaced the class.
        "Wolverine #75 (1993 Marvel Comics) NM Signed Adam Kubert w/ COA DF #2589/7500",
        # The same class at the money end: 18.9x its pool's median.
        "X-Force #11 Deadpool! NM- 1st Appearance Domino! Signed Stan Lee & Rob Liefeld!",
        # Leading-position and all-caps forms.
        "Signed X-Men '97 #1 (Marvel Comics May 2024) Whatnot Gold Foil. LTD. 50 NM",
        "SIGNED! Todd McFarlane Detective Comics #576 DC Comics 1987 VF Batman Year Two",
        # `autograph`, the other way a signature is advertised.
        "X-MEN #13 CGC 6.0 OW-W 1965 KIRBY, Goldberg autograph/signature 2nd JUGGERNAUT",
        # Must stay excluded: the pre-BUI-668 `signed by` form still fires.
        "1991 Incredible Hulk #377 Signed By DALE KEOWN & PETER DAVID Comic Book Marvel",
    ])
    def test_signed_copies_are_excluded(self, title):
        """BUI-668: a signed/COA copy is not comparable to a raw one.

        `signed\\s+by` matched only one phrasing. The class is systematically
        dearer than the pools it lands in (2.22x the pool median, 84 of 99
        members above it) and is the first in this ticket sequence to register
        on the sharp test, so excluding it is cap-LOWERING: measured over the
        offline corpus at max_bid DOWN 12 / UP 4 across the CGC ladder.
        """
        assert sc.hard_exclude(title)

    @pytest.mark.parametrize("title", [
        # BUI-668: the word boundary does the precision work. "DESIGN" and
        # "Designs" contain "sign"; an unbounded token would sweep them in.
        "X-Men #21 (DESIGN VARIANT) COMIC BOOK ~ Marvel Comics",
        "Ultimate X-Men #15 Peach Momoko Design Cover RI 1:10 RAT Marvel Comics July 2025",
        "WATCHMEN 1st Edition Hardcover (1987) | Graphitti Designs Slipcase | Alan Moore",
        # "unsigned" is the token's own opposite — \b already rejects it.
        "X-Men 100 unsigned raw copy",
        # A metal wall SIGN, not a signature.
        "The Uncanny X-Men #94 NEW METAL SIGN: Count Nefaria - The New X-Men",
        # The one residual false positive in the corpus, and the reason the
        # branch carries a negative lookbehind: the seller is advertising that
        # the book is NOT signed, which makes it a GOOD raw comp at $174.50.
        "Batman #251 - Neal Adams & Denny O'Neil - 1973 - NOT signed - KEY - FREE SHIP",
    ])
    def test_signed_lookalikes_are_kept(self, title):
        """BUI-668: precision guard on the signature branch.

        Measured at 0 disagreements against the hand-labelled class over all
        13,876 surviving corpus comps; dropping `signed\\s+by` for the bounded
        token re-admits 0 of the 16,825 comps the corpus holds.
        """
        assert not sc.hard_exclude(title)

    @pytest.mark.parametrize("title,excluded", [
        # BUI-668 measured-but-not-shipped: `restored` still matches inside
        # "unrestored", so these two genuine raw comps stay dropped. The fix
        # (`(?<!un)restored`) is precision 1.000 but moved only 1 of 483 pools,
        # UP +28.6% — cap-RAISING, which buys no downside protection. Pinned so
        # the known-wrong behavior is deliberate and a future change is a
        # visible decision rather than an accident.
        ("X-Men #54 in VF+ 8.5 COND from 1969! Marvel very fine unrestored C166", True),
        ("X-MEN 54 VF+ 8.5 UNRESTORED", True),
        # The genuinely restored books this branch exists for.
        ("Captain America #100 VG- 3.5 RESTORED 1968", True),
        ("Fantastic Four 50 - Marvel Silver Age Key Cover, F/F+, Restored", True),
    ])
    def test_restored_token_is_still_substring_matched(self, title, excluded):
        assert sc.hard_exclude(title) is excluded

    def test_spaced_run_rule_stays_comp_only(self):
        """BUI-637/BUI-239: the new rule must never reach the purchase path.

        Same contract as test_bare_range_rule_stays_comp_only above — _LOT_RE
        feeds should_reject/hard_reject, where a false lot-reject drops a book
        you actually want, so this shape is excluded as a COMP only.
        """
        title = "Batman 655 656 657 658 1st Damian Wayne NM Set Kubert DC Comics 2006"
        assert sc.hard_exclude(title)
        assert comic_identity._fmv_spaced_number_run_lot(title)
        assert not comic_identity._LOT_RE.search(title)
        assert not comic_identity.should_reject(title, "Batman", "655")

    @pytest.mark.parametrize("numbers,expected", [
        ([655, 656, 657, 658], 4),
        ([92, 2, 3, 4], 3),          # leading non-issue number breaks the slice
        ([92, 97, 1], 2),            # descending tail
        ([15, 15, 16], 2),           # equal adjacent members are not ascending
        ([1], 1),
        ([], 0),                     # total on the degenerate input
        ([9, 8, 7, 6], 1),           # strictly descending — no run at all
        ([10, 25, 50, 100], 4),      # ascending but not contiguous — still a run
    ])
    def test_longest_ascending_run(self, numbers, expected):
        """BUI-637: the ascending gate reads the longest CONSECUTIVE ascending
        slice, not the whole run — the property that keeps "X Men '92 2 3 4"
        out and "Ghost Rider #15 15 (2nd) 16 19 20 21 ..." in."""
        assert comic_identity._longest_ascending_run(numbers) == expected

    def test_bare_range_rule_stays_comp_only(self):
        """BUI-598/BUI-239: the new rule must never reach the purchase path.

        _LOT_RE feeds should_reject/hard_reject, where a false lot-reject drops
        a book you actually want. The bare-range+run/set rule lives behind
        is_comp_excluded only, so the same title must be excluded as a COMP
        while _LOT_RE itself stays unwidened.
        """
        title = "X-Men 94-300 FULL RUN NM Marvel 1975 Key 94 101 129 141 266"
        assert sc.hard_exclude(title)
        assert comic_identity._fmv_run_range_lot(title)
        assert not comic_identity._LOT_RE.search(title)

    def test_later_printing_rule_stays_comp_only(self):
        """BUI-645/BUI-239: the comp-path later-printing check adds no purchase-
        path behavior.

        _comp_later_printing_reject is reached only through is_comp_excluded.
        should_reject already rejected this title via its own step-7
        _second_print_reject call, so the purchase path is unchanged either way
        — what this pins is that the NEW function is not wired into it.
        """
        title = "Ghost Rider #15 2nd Print Marvel 1991"
        assert sc.hard_exclude(title)
        assert comic_identity._comp_later_printing_reject(title)
        assert not comic_identity._LOT_RE.search(title)
        assert not comic_identity.identify_comic(title).is_lot

    def test_bare_reprint_stays_off_the_comp_path(self):
        """BUI-645: the bare "reprint"/"reprints" half of _second_print_reject
        must NOT reach is_comp_excluded.

        This is the load-bearing half of the BUI-645 split. _second_print_reject
        still fires on the title (the purchase path is unchanged), but the comp
        path must keep it — it is a genuine first-print issue and its own pool's
        comp. Wiring the full _second_print_reject in would flip this.
        """
        title = "X-MEN 76 (VG+) reprints #28 new Gil Kane cover! BANSHEE! 1972 Marvel Comics k733"
        assert comic_identity._second_print_reject(title)      # purchase path: rejects
        assert not comic_identity._comp_later_printing_reject(title)
        assert not comic_identity.is_comp_excluded(title)      # comp path: KEEPS
        assert not sc.hard_exclude(title)

    def test_later_printing_regex_split_is_behavior_preserving(self):
        """BUI-645 recomposed _LATER_PRINTING_RE from two named halves so the
        comp path could adopt one. _second_print_reject must be unchanged.

        Verified across all 12,418 corpus titles at 0 drift; these pin the two
        alternations and their union so a future edit to one half cannot
        silently change what the purchase path rejects.
        """
        ordinal = "Amazing Spider-Man #300 2nd print NM"
        bare = "Amazing Fantasy #15 reprint VF"
        neither = "Amazing Spider-Man #4 VF"
        assert comic_identity._LATER_PRINTING_ORDINAL_RE.search(ordinal)
        assert not comic_identity._BARE_REPRINT_RE.search(ordinal)
        assert comic_identity._BARE_REPRINT_RE.search(bare)
        assert not comic_identity._LATER_PRINTING_ORDINAL_RE.search(bare)
        for title in (ordinal, bare):
            assert comic_identity._second_print_reject(title)
            assert comic_identity._LATER_PRINTING_RE.search(title)
        assert not comic_identity._second_print_reject(neither)
        assert not comic_identity._LATER_PRINTING_RE.search(neither)
        # Same empty/None tolerance as _second_print_reject — this sits on the
        # money path and must never raise on a missing title.
        assert not comic_identity._comp_later_printing_reject("")
        assert not comic_identity._comp_later_printing_reject(None)  # type: ignore[arg-type]


# ─── Comp parsing ─────────────────────────────────────────────────────────────

class TestParseComp:
    def _make(self, **overrides):
        base = {
            "product_id": "147295505028",
            "title": "Uncanny X-Men #185 VF Marvel 1984",
            "price": {"raw": "$11.99", "extracted": 11.99},
            "sold_date": "Sold Oct 12, 2026",
            "buying_format": "auction",
            "link": "https://example.com",
        }
        base.update(overrides)
        return base

    def test_full_parse(self):
        c = sc.parse_comp(self._make())
        assert c["product_id"] == "147295505028"
        assert c["price"] == 11.99
        assert c["grade"] == 8.0
        assert c["sold_date"].startswith("Sold")

    def test_falls_back_to_extracted_price(self):
        c = sc.parse_comp(self._make(price={"extracted": 5.99}))
        assert c["price"] == 5.99

    def test_falls_back_to_raw_price(self):
        c = sc.parse_comp(self._make(price={"raw": "$8.00"}))
        assert c["price"] == 8.0

    def test_drops_no_price(self):
        assert sc.parse_comp(self._make(price={})) is None

    def test_drops_no_title(self):
        assert sc.parse_comp(self._make(title="")) is None

    def test_uses_item_id_fallback(self):
        c = sc.parse_comp({
            "title": "X",
            "item_id": "123",
            "price": {"raw": "$5"},
        })
        assert c["product_id"] == "123"

    def test_price_out_of_range_drops(self):
        assert sc.parse_comp(self._make(price={"extracted": 0.10})) is None
        assert sc.parse_comp(self._make(price={"extracted": 100000})) is None


# ─── Query construction ─────────────────────────────────────────────────────

class TestBuildQuery:
    def test_minimal(self):
        q = sc.build_query("Amazing Spider-Man", "300")
        assert '"Amazing Spider-Man 300"' in q
        assert "-cgc" in q and "-slab" in q

    def test_with_year_and_publisher(self):
        q = sc.build_query("Invincible", "1", year=2003, publisher="image comics")
        assert "2003" in q
        assert "image comics" in q

    def test_with_grade_label(self):
        q = sc.build_query("Batman", "224", year=1970, grade_label="FN")
        assert " FN " in q or q.endswith(" FN") or " FN -" in q

    # ── BUI-348: exclude_graded toggle ──────────────────────────────────────
    def test_default_excludes_graded(self):
        q = sc.build_query("Amazing Spider-Man", "50", year=1967)
        for t in ("-cgc", "-cbcs", "-graded", "-slab"):
            assert t in q

    def test_include_graded_omits_exclusion_terms(self):
        q = sc.build_query("Amazing Spider-Man", "50", year=1967,
                           exclude_graded=False)
        for t in ("-cgc", "-cbcs", "-graded", "-slab"):
            assert t not in q

    def test_include_graded_only_removes_graded_terms(self):
        # Dropping graded exclusion must NOT disturb the rest of the query — the
        # base phrase, year, and BUI-347 vintage hardening are all still present.
        q = sc.build_query("Amazing Spider-Man", "50", year=1967,
                           exclude_graded=False)
        assert '"Amazing Spider-Man 50"' in q
        assert "1967" in q
        assert "-variant" in q  # vintage-masthead hardening independent of graded

    # ── BUI-304 (issue 1): variant appended as a query keyword ──────────────
    def test_variant_appended_when_present(self):
        q = sc.build_query("X-Men", "123", variant="Newsstand")
        assert "Newsstand" in q

    def test_base_unchanged_when_variant_absent(self):
        # Guard: absent/None/empty variant must leave the base query byte-for-byte
        # identical to the pre-BUI-304 output.
        base = sc.build_query("X-Men", "123")
        assert sc.build_query("X-Men", "123", variant=None) == base
        assert sc.build_query("X-Men", "123", variant="") == base
        assert sc.build_query("X-Men", "123", variant="   ") == base
        assert "Newsstand" not in base

    def test_variant_and_publisher_both_appended(self):
        q = sc.build_query("Invincible", "1", publisher="image comics",
                           variant="Direct")
        assert "Direct" in q
        assert "image comics" in q

    # ── BUI-304 (issue 2): Marvel publisher normalized to "marvel comics" ──
    def test_marvel_publisher_qualifier(self):
        for pub in ("Marvel", "marvel", "Marvel Comics"):
            q = sc.build_query("Amazing Spider-Man", "300", publisher=pub)
            assert "marvel comics" in q

    # ── BUI-315: DC is Marvel-only gated — DC gets NO qualifier appended ──
    def test_dc_publisher_gets_no_qualifier(self):
        # The DC "dc comics" two-token qualifier regressed recall in BUI-304's
        # live spot-check, so a DC publisher must leave the query untouched —
        # byte-for-byte identical to omitting the publisher entirely.
        base = sc.build_query("Detective Comics", "27")
        for pub in ("DC", "dc", "DC Comics"):
            q = sc.build_query("Detective Comics", "27", publisher=pub)
            assert q == base
            assert "dc comics" not in q.lower()

    def test_indie_publisher_passes_through_unchanged(self):
        # The pre-existing indie mechanism must keep working verbatim.
        for pub in ("image comics", "dark horse", "Valiant"):
            q = sc.build_query("Spawn", "1", publisher=pub)
            assert pub in q

    def test_publisher_qualifier_helper(self):
        assert sc._publisher_qualifier(None) is None
        assert sc._publisher_qualifier("") is None
        assert sc._publisher_qualifier("   ") is None
        assert sc._publisher_qualifier("Marvel") == "marvel comics"
        # BUI-315: DC recognized publishers get no qualifier (Marvel-only).
        assert sc._publisher_qualifier("DC") is None
        assert sc._publisher_qualifier("dc") is None
        assert sc._publisher_qualifier("DC Comics") is None
        assert sc._publisher_qualifier("Dark Horse") == "Dark Horse"

    # ── BUI-321: DC/Marvel imprints map to their parent's gate ──
    def test_marvel_imprints_map_to_marvel_gate(self):
        # Marvel imprints must get the Marvel qualifier, not fall through to the
        # indie branch and append the imprint name as a recall-noise keyword.
        for pub in ("Epic", "Epic Comics", "Icon", "MAX", "Marvel Knights",
                    "Star Comics", "Timely"):
            assert sc._publisher_qualifier(pub) == "marvel comics", pub
            q = sc.build_query("Moon Knight", "1", publisher=pub)
            assert "marvel comics" in q
            assert pub not in q  # imprint name is NOT appended as a keyword

    def test_malibu_is_not_gated_to_marvel(self):
        # BUI-321: Malibu published independently (1986–1994) before Marvel
        # acquired it, so it must NOT get the year-less "marvel comics"
        # qualifier — that over-narrows pre-acquisition titles. It passes
        # through as a genuine indie publisher instead.
        for pub in ("Malibu", "Malibu Comics"):
            assert sc._publisher_qualifier(pub) == pub, pub

    def test_every_imprint_table_entry_maps_to_its_declared_gate(self):
        # BUI-321: exhaustively lock every _IMPRINT_PARENT_GATE row so a
        # wrong-gate typo in any variant spelling fails loudly, not silently.
        for key, gate in sc._IMPRINT_PARENT_GATE.items():
            result = sc._publisher_qualifier(key)
            if gate == "marvel":
                assert result == "marvel comics", key
            elif gate == "dc":
                assert result is None, key
            else:  # pragma: no cover - guards against an unknown gate value
                raise AssertionError(f"unexpected gate {gate!r} for {key!r}")

    def test_dc_imprints_map_to_dc_gate_no_qualifier(self):
        # DC imprints must be gated to NO qualifier (Marvel-only, BUI-315) —
        # byte-for-byte identical to omitting the publisher, and the imprint
        # name is never appended as a keyword.
        base = sc.build_query("Sandman", "1")
        for pub in ("Vertigo", "Wildstorm", "Black Label", "DC Black Label",
                    "Milestone", "Paradox Press", "Minx", "Helix", "Homage",
                    "Zuda"):
            assert sc._publisher_qualifier(pub) is None, pub
            q = sc.build_query("Sandman", "1", publisher=pub)
            assert q == base
            assert pub.lower() not in q.lower()  # imprint not appended

    # ── BUI-321: "D.C." punctuation tolerated → gated, not appended ──
    def test_dc_with_periods_is_gated_not_appended(self):
        base = sc.build_query("Detective Comics", "27")
        for pub in ("D.C.", "D.C", "D.C. Comics"):
            assert sc._publisher_qualifier(pub) is None, pub
            q = sc.build_query("Detective Comics", "27", publisher=pub)
            assert q == base
            assert "d.c" not in q.lower()  # "D.C." not appended as a keyword

    def test_genuine_indie_still_passes_through_unchanged(self):
        # Regression guard: a non-imprint indie publisher must still pass
        # through verbatim (unaffected by the BUI-321 imprint table).
        for pub in ("Image Comics", "Dark Horse", "Valiant", "Boom Studios",
                    "IDW"):
            assert sc._publisher_qualifier(pub) == pub, pub
            assert pub in sc.build_query("Saga", "1", publisher=pub)


# ── BUI-346: title normalization (leading article + embedded issue dedup) ──

class TestTitleNormalization:
    def test_leading_article_stripped(self):
        assert sc._strip_leading_article("The Amazing Spider-Man") == "Amazing Spider-Man"
        assert sc._strip_leading_article("A Man Called X") == "Man Called X"
        assert sc._strip_leading_article("An X-Men Story") == "X-Men Story"
        # Case-insensitive, and a title with none of these leading words is untouched.
        assert sc._strip_leading_article("THE Amazing Spider-Man") == "Amazing Spider-Man"
        assert sc._strip_leading_article("Amazing Spider-Man") == "Amazing Spider-Man"

    def test_embedded_issue_stripped(self):
        assert sc._strip_embedded_issue("Amazing Spider-Man #50", "50") == "Amazing Spider-Man"
        # Bare trailing issue number (no '#') is also stripped.
        assert sc._strip_embedded_issue("Amazing Spider-Man 50", "50") == "Amazing Spider-Man"

    def test_embedded_issue_left_alone_when_it_does_not_match(self):
        # A DIFFERENT number in the title (not the separate issue field) must
        # survive — this isn't a generic "strip trailing digits" pass.
        assert sc._strip_embedded_issue("Spider-Man 2099", "50") == "Spider-Man 2099"

    def test_embedded_issue_guard_avoids_partial_digit_match(self):
        # issue="99" must not chew into the "20" of "2099" — the (?<!\d) guard.
        assert sc._strip_embedded_issue("X-Men 2099", "99") == "X-Men 2099"

    def test_embedded_issue_noop_without_issue_or_title(self):
        assert sc._strip_embedded_issue("Amazing Spider-Man #50", "") == "Amazing Spider-Man #50"
        assert sc._strip_embedded_issue("", "50") == ""

    # ── The BUI-346 acceptance criterion, verbatim ──
    def test_doubled_title_and_clean_title_build_identical_query(self):
        """A row title:"The Amazing Spider-Man #50", issue:"50" must build the
        same query as title:"Amazing Spider-Man", issue:"50" — the real ASM #50
        incident (2026-07-13): the un-normalized form doubled into
        `"The Amazing Spider-Man #50 50"`, 0 results on every tier."""
        doubled = sc.build_query("The Amazing Spider-Man #50", "50")
        clean = sc.build_query("Amazing Spider-Man", "50")
        assert doubled == clean
        assert '"Amazing Spider-Man 50"' in clean
        assert "50 50" not in doubled
        assert "#50" not in doubled

    def test_leading_article_alone_does_not_affect_issue_untouched(self):
        # A leading article with NO embedded issue: only the article is stripped.
        q = sc.build_query("The Amazing Spider-Man", "300")
        assert '"Amazing Spider-Man 300"' in q

    def test_embedded_issue_alone_no_leading_article(self):
        q = sc.build_query("Fantastic Four #1", "1")
        assert '"Fantastic Four 1"' in q
        assert "1 1" not in q


# ── BUI-347: vintage-key hardening on rebootable mastheads ─────────────────

class TestVintageKeyHardening:
    def test_vintage_rebootable_masthead_gets_exclusion_terms(self):
        # Real incident: ASM #50 (1967) — the phrase-quoted base query collided
        # with the 2018+ relaunch's own #50 (LGY #944), swamping genuine 1967
        # sales with cheap modern variant listings.
        q = sc.build_query("Amazing Spider-Man", "50", year=1967)
        for term in ("-variant", "-foil", "-virgin", "-reprint", "-facsimile",
                     "-homage", "-timeless"):
            assert term in q, f"{term!r} missing from vintage query: {q}"
        assert "1967" in q  # the year discriminator still applies

    def test_modern_book_byte_for_byte_unaffected(self):
        # Acceptance: modern books (recent year) must be COMPLETELY unaffected
        # by the vintage hardening — same masthead, same shape, only the year
        # differs, and the query must be identical to the pre-BUI-347 shape
        # (no exclusion terms at all).
        q = sc.build_query("Amazing Spider-Man", "50", year=2018)
        for term in ("-variant", "-foil", "-virgin", "-reprint", "-facsimile",
                     "-homage", "-timeless"):
            assert term not in q
        assert q == '"Amazing Spider-Man 50" 2018 -cgc -cbcs -graded -slab'

    def test_no_year_rebootable_masthead_unaffected(self):
        # No year at all → the hard year-gate can't fire (there's no year to
        # compare), so a year-agnostic rebootable-masthead query is untouched.
        q = sc.build_query("Amazing Spider-Man", "50")
        assert "-variant" not in q
        assert q == '"Amazing Spider-Man 50" -cgc -cbcs -graded -slab'

    def test_non_rebootable_masthead_unaffected_even_if_old(self):
        # Old year alone isn't enough — the masthead must ALSO be a known
        # rebootable one. A vintage indie/one-shot title is untouched.
        q = sc.build_query("Swamp Thing", "1", year=1972)
        assert "-variant" not in q
        assert q == '"Swamp Thing 1" 1972 -cgc -cbcs -graded -slab'

    def test_year_boundary_2000_is_not_vintage(self):
        # The cutoff is a hard pre-2000 gate — year=2000 itself is NOT vintage.
        q2000 = sc.build_query("Batman", "1", year=2000)
        q1999 = sc.build_query("Batman", "1", year=1999)
        assert "-variant" not in q2000
        assert "-variant" in q1999

    def test_is_rebootable_masthead_matches_known_titles(self):
        for title in ("Amazing Spider-Man", "The Amazing Spider-Man",
                      "Fantastic Four", "Uncanny X-Men", "X-Men", "Avengers",
                      "Thor", "Iron Man", "Incredible Hulk", "Captain America",
                      "Batman", "Superman", "Wonder Woman"):
            assert sc._is_rebootable_masthead(title), title

    def test_is_rebootable_masthead_does_not_match_others(self):
        for title in ("Swamp Thing", "Saga", "Invincible", "Spawn",
                      "Hellboy", "The Walking Dead"):
            assert not sc._is_rebootable_masthead(title), title

    def test_she_hulk_does_not_trip_the_hulk_masthead_gate(self):
        """BUI-351: a plain `\\bhulk\\b` matches INSIDE "She-Hulk" because the
        word-boundary lands on the hyphen ("-" is a non-word char, so the
        "-"→"h" transition already satisfies `\\b`). "She-Hulk" is a distinct
        title, not the Hulk masthead, and must not trip the vintage-hardening
        gate meant for genuine Hulk/Incredible Hulk keys."""
        for title in ("She-Hulk", "She-Hulk (2004)", "Sensational She-Hulk",
                      "The Savage She-Hulk"):
            assert not sc._is_rebootable_masthead(title), title
        # Acceptance at the build_query level too: a vintage "She-Hulk" query
        # must stay byte-for-byte untouched by the Hulk exclusion terms.
        q = sc.build_query("She-Hulk", "1", year=1980)
        assert "-variant" not in q
        assert q == '"She-Hulk 1" 1980 -cgc -cbcs -graded -slab'

    def test_hulk_and_incredible_hulk_still_trip_the_masthead_gate(self):
        """BUI-351 regression guard: tightening the boundary must not lose the
        genuine matches — bare "Hulk" and "Incredible Hulk" (both real
        rebootable mastheads) must still gate."""
        for title in ("Hulk", "The Hulk", "Incredible Hulk",
                      "The Incredible Hulk"):
            assert sc._is_rebootable_masthead(title), title
        q = sc.build_query("Incredible Hulk", "1", year=1962)
        assert "-variant" in q

    def test_masthead_boundary_edge_cases(self):
        """BUI-351: pin down the new `(?<![-\\w])...(?![-\\w])` boundary's
        behavior on shapes the hyphen fix didn't explicitly target, so a future
        loosening of the character class (e.g. back toward plain `\\w`) fails
        loudly instead of silently reintroducing a false match/non-match.
        Digit-adjacency (no separating space/punctuation) is the one shape
        where this boundary is intentionally as strict as `\\w`: a masthead
        glued directly to a number is treated as part of a different token."""
        # Digit-adjacent, no separator: correctly rejected (matches \w rules).
        assert not sc._is_rebootable_masthead("X-Men2099")
        assert not sc._is_rebootable_masthead("2099X-Men")
        # Punctuation-adjacent (not a word char): still correctly matched.
        assert sc._is_rebootable_masthead("X-Men's Legacy")
        assert sc._is_rebootable_masthead("X-Men: Legacy")
        assert sc._is_rebootable_masthead("Superman/Batman")
        assert sc._is_rebootable_masthead("Hulk (1962)")
        # A masthead directly abutting an unrelated hyphenated word is
        # correctly rejected — the documented tradeoff of this fix.
        assert not sc._is_rebootable_masthead("Hulk-Buster")

    def test_masthead_gate_sees_the_bui_346_normalized_title(self):
        # BUI-346 + BUI-347 interaction: the un-normalized "The Amazing
        # Spider-Man #50" must still trip the vintage gate — the masthead
        # check runs on the title AFTER the leading-article/embedded-issue
        # strip, not the raw input.
        q = sc.build_query("The Amazing Spider-Man #50", "50", year=1967)
        assert "-variant" in q
        assert q == sc.build_query("Amazing Spider-Man", "50", year=1967)

    # ── BUI-350 (issue 1): `vintage_year` gates hardening independent of the
    #    query-text `year` (the broaden tier drops `year` from the text but
    #    must not thereby drop the exclusion terms) ──
    def test_vintage_year_gates_hardening_even_when_year_dropped_from_query(self):
        q = sc.build_query("Amazing Spider-Man", "50", year=None, vintage_year=1967)
        for term in ("-variant", "-foil", "-virgin", "-reprint", "-facsimile",
                     "-homage", "-timeless"):
            assert term in q, f"{term!r} missing: {q}"
        assert "1967" not in q  # the year token itself is still absent

    def test_vintage_year_defaults_to_year_when_omitted(self):
        # Backward-compat: every pre-BUI-350 caller (no vintage_year arg) keeps
        # byte-for-byte behavior — the gate falls back to `year`.
        assert (sc.build_query("Amazing Spider-Man", "50", year=1967)
                == sc.build_query("Amazing Spider-Man", "50", year=1967, vintage_year=None))

    def test_vintage_year_modern_book_unaffected(self):
        # A modern vintage_year (>= cutoff) must not gate, even with year=None.
        q = sc.build_query("Amazing Spider-Man", "50", year=None, vintage_year=2018)
        assert "-variant" not in q

    # ── Money-safety: the genuine vintage comp pool must survive ──
    def test_vintage_comp_pool_survives_exclusion_terms(self):
        """CRITICAL money-safety acceptance (BUI-347): no genuine vintage sale
        may be excluded by the new terms. These are representative titles from
        the ASM #50 (1967) genuine $402-$700 raw sale cluster (the incident
        this ticket documents) — none of them may contain any of the new
        exclusion tokens, or a real sale would be silently dropped from the
        comp pool."""
        genuine_1967_listings = [
            "Amazing Spider-Man #50 1967 1st Appearance Kingpin Marvel VG+",
            "Amazing Spider-Man 50 (Marvel, 1967) Spider-Man No More! FN-",
            "AMAZING SPIDER-MAN #50 1st KINGPIN 1967 SILVER AGE KEY VG",
            "Amazing Spider-Man #50 Marvel 1967 Romita Kingpin key GD/VG raw",
            "Amazing Spider-Man 50 1967 1st app Kingpin ROMITA cover raw comic",
        ]
        excluded_tokens = ("variant", "foil", "virgin", "reprint",
                           "facsimile", "homage", "timeless")
        for listing in genuine_1967_listings:
            lowered = listing.lower()
            for token in excluded_tokens:
                assert token not in lowered, (
                    f"genuine vintage listing {listing!r} contains excluded "
                    f"token {token!r} — would be wrongly dropped from the "
                    "comp pool"
                )


# ── BUI-565: `year` arrives as a STRING from /comic:identify ───────────────

class TestStringYearCoercion:
    """BUI-565: `year` is a documented `--batch` field and /comic:identify
    emits it as a string ("1976"). Every year test in sold_comps compares
    against the int `_VINTAGE_YEAR_CUTOFF`, so an uncoerced string raised
    `TypeError: '<' not supported between instances of 'str' and 'int'` on the
    FIRST tier — before any query ran. `fetch_book_comps`' broad handler
    swallowed it into an empty `queries_used`, which comic-fmv then read as a
    genuine no-comps book: a silent n=0 on a money path (two live X-Men #101
    auctions, $291 and $127.50, came back unpriced in the 2026-07-31 buy run).
    """

    def test_string_year_builds_the_same_query_as_int_year(self):
        # The literal reproduction from the ticket: this raised TypeError.
        assert (sc.build_query("X-Men", "101", year="1976")
                == sc.build_query("X-Men", "101", year=1976))

    def test_string_year_is_not_only_a_vintage_problem(self):
        # EVERY string year raised, not just vintage ones — the comparison
        # itself is what blew up, and it runs before any cutoff logic.
        assert (sc.build_query("Wolverine", "50", year="1992")
                == sc.build_query("Wolverine", "50", year=1992))
        assert (sc.build_query("Saga", "1", year="2012")
                == sc.build_query("Saga", "1", year=2012))

    def test_string_year_activates_the_vintage_hardening(self):
        # Not merely "doesn't crash": the BUI-347 exclusion terms must fire,
        # exactly as they do for the int form.
        q = sc.build_query("X-Men", "101", year="1976")
        for term in ("-variant", "-foil", "-virgin", "-reprint", "-facsimile",
                     "-homage", "-timeless"):
            assert term in q, f"{term!r} missing from string-year query: {q}"
        assert "1976" in q

    def test_string_vintage_year_kwarg_also_coerces(self):
        # The broaden tier passes `year=None, vintage_year=<the book's year>`;
        # that kwarg takes the same string and must gate identically.
        assert (sc.build_query("X-Men", "101", year=None, vintage_year="1976")
                == sc.build_query("X-Men", "101", year=None, vintage_year=1976))
        assert "-variant" in sc.build_query("X-Men", "101", year=None,
                                            vintage_year="1976")

    def test_float_and_padded_string_years_coerce(self):
        assert sc._coerce_year(1976.0) == 1976
        assert sc._coerce_year("  1976  ") == 1976
        assert sc._coerce_year("1976.0") == 1976

    def test_absent_and_empty_years_stay_absent(self):
        # `""` never crashed (falsy short-circuits the gate) — preserve that,
        # and keep the query byte-for-byte equal to the no-year form.
        assert sc._coerce_year(None) is None
        assert sc._coerce_year("") is None
        assert sc._coerce_year("   ") is None
        assert (sc.build_query("X-Men", "101", year="")
                == sc.build_query("X-Men", "101"))

    def test_unparseable_year_raises_rather_than_being_dropped(self):
        # Money-safety: a year-less query still returns comps, so silently
        # dropping a garbage year would swap a loud failure for a quietly
        # DIFFERENT search the operator never learns about. Raise instead —
        # fetch_book_comps turns it into a per-book `error`.
        for bad in ("n/a", "c. 1976", "nineteen seventy-six", [], {}, True):
            with pytest.raises(ValueError):
                sc._coerce_year(bad)

    def test_int_year_output_is_byte_for_byte_unchanged(self):
        # Acceptance: every pre-BUI-565 int caller's query is untouched.
        assert (sc.build_query("Amazing Spider-Man", "50", year=1967)
                == '"Amazing Spider-Man 50" 1967 -variant -foil -virgin '
                   '-reprint -facsimile -homage -timeless '
                   '-cgc -cbcs -graded -slab')
        assert (sc.build_query("Amazing Spider-Man", "50", year=2018)
                == '"Amazing Spider-Man 50" 2018 -cgc -cbcs -graded -slab')


class TestCanonicalUrl:
    def test_excludes_api_key(self):
        url = sc.canonical_serpapi_url('"X-Men 1"')
        assert "secret-key" not in url
        assert "show_only=Sold" in url
        assert "engine=ebay" in url

    def test_deterministic(self):
        url1 = sc.canonical_serpapi_url('"X-Men 1"')
        url2 = sc.canonical_serpapi_url('"X-Men 1"')
        assert url1 == url2

    def test_page1_omits_pgn_param(self):
        """BUI-523: page 1 (the default) must NOT gain a `_pgn` param — the
        canonical URL (and therefore the cache key) stays byte-for-byte
        identical to pre-BUI-523 output so the existing 7-day cache isn't
        invalidated for the overwhelmingly common single-page case."""
        assert "_pgn" not in sc.canonical_serpapi_url('"X-Men 1"')
        assert "_pgn" not in sc.canonical_serpapi_url('"X-Men 1"', page=1)
        assert sc.canonical_serpapi_url('"X-Men 1"') == sc.canonical_serpapi_url('"X-Men 1"', page=1)

    def test_page2_includes_pgn_param(self):
        """BUI-523 AC: the page param must join the canonical cache key."""
        url = sc.canonical_serpapi_url('"X-Men 1"', page=2)
        assert "_pgn=2" in url

    def test_page1_and_page2_are_different_cache_keys(self):
        """BUI-523 AC: each page caches independently under the same TTL —
        this is what makes that true at the cache-path layer."""
        url1 = sc.canonical_serpapi_url('"X-Men 1"', page=1)
        url2 = sc.canonical_serpapi_url('"X-Men 1"', page=2)
        assert sc._cache_path(url1) != sc._cache_path(url2)


class TestCanonicalSoldCompsUrl:
    def test_pins_sold_and_include_complete_listing(self):
        """BUI-557: both `sold` and `includeCompleteListing` are pinned
        explicitly even though `true` is sold-comps.com's default for each —
        an unpinned vendor default is a silent-drift risk (BUI-552 showed
        includeCompleteListing alone flips OBO badge detection 73->0)."""
        url = sc.canonical_sold_comps_url('"X-Men 1"')
        assert "sold=true" in url
        assert "includeCompleteListing=true" in url

    def test_deterministic(self):
        url1 = sc.canonical_sold_comps_url('"X-Men 1"')
        url2 = sc.canonical_sold_comps_url('"X-Men 1"')
        assert url1 == url2


# ─── Cache layer ──────────────────────────────────────────────────────────────

class TestCache:
    def test_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        path = sc._cache_path("https://example.com/q?foo=bar")
        sc._cache_put(path, {"hello": "world"})
        assert sc._cache_get(path, ttl_sec=60) == {"hello": "world"}

    def test_expired(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        path = sc._cache_path("k")
        sc._cache_put(path, {"x": 1})
        # Backdate mtime past TTL
        old = time.time() - 100
        import os
        os.utime(path, (old, old))
        assert sc._cache_get(path, ttl_sec=10) is None

    def test_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        assert sc._cache_get(tmp_path / "nope.json", 60) is None

    def test_concurrent_put_to_same_key_under_thread_pool_executor(self, tmp_path, monkeypatch):
        """BUI-335 regression, at the real collision surface named in
        atomic_write_json()'s docstring: run_batch() fans out fetch_book_comps
        across a ThreadPoolExecutor, and two workers whose books resolve to the
        same canonical SerpApi query (duplicate cache keys in one batch) both
        call _cache_put() for the same path. Before the fix, a shared
        deterministic tmp filename meant one worker's failure-cleanup could
        unlink a different worker's still-in-flight tmp, raising
        FileNotFoundError instead of just losing a write silently. Drive
        _cache_put() from a real ThreadPoolExecutor (matching run_batch's own
        concurrency primitive) with every worker targeting the same path and
        assert none of them raise and the cache file ends up valid."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        path = sc._cache_path("https://example.com/q?_nkw=duplicate+key")

        def put(n):
            sc._cache_put(path, {"worker": n})

        with sc.ThreadPoolExecutor(max_workers=sc.DEFAULT_MAX_WORKERS) as pool:
            futures = [pool.submit(put, n) for n in range(sc.DEFAULT_MAX_WORKERS * 2)]
            # Propagates any exception raised inside a worker (e.g. the
            # BUI-335 FileNotFoundError) as a test failure instead of
            # silently swallowing it.
            for fut in futures:
                fut.result()

        cached = sc._cache_get(path, ttl_sec=60)
        assert cached is not None
        assert "worker" in cached
        assert not list(tmp_path.glob(f"{path.name}.*.tmp"))


# ─── Fetch with verification ──────────────────────────────────────────────────

class TestFetch:
    def _mock_response(self, organic_results=None, ebay_url=None, error=None):
        m = MagicMock()
        m.raise_for_status = MagicMock()
        m.status_code = 200  # BUI-333: retry_request() reads status_code directly
        body = {}
        if error:
            body["error"] = error
        else:
            body["organic_results"] = organic_results or []
            body["search_metadata"] = {"ebay_url": ebay_url or ""}
        m.json = MagicMock(return_value=body)
        return m

    def test_rejects_when_lh_sold_missing(self, tmp_path, monkeypatch):
        # If SerpApi silently dropped show_only=Sold, the returned eBay URL
        # won't contain LH_Sold=1 — we must fail loudly.
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        bad_url = "https://www.ebay.com/sch/i.html?_nkw=test"  # no LH_Sold=1
        with patch("sold_comps.requests.get",
                   return_value=self._mock_response(ebay_url=bad_url)):
            with pytest.raises(sc.SerpApiError, match="LH_Sold=1"):
                sc.fetch("test", "key")

    def test_accepts_when_lh_sold_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        good_url = "https://www.ebay.com/sch/i.html?_nkw=test&LH_Sold=1"
        with patch("sold_comps.requests.get",
                   return_value=self._mock_response(ebay_url=good_url,
                                                    organic_results=[{"product_id": "1"}])):
            data, cache_hit = sc.fetch("test", "key")
            assert cache_hit is False
            assert data["organic_results"] == [{"product_id": "1"}]

    def test_serves_from_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        good_url = "https://www.ebay.com/sch/i.html?_nkw=t&LH_Sold=1"
        # First call: real fetch
        with patch("sold_comps.requests.get",
                   return_value=self._mock_response(ebay_url=good_url)) as m:
            sc.fetch("t", "key")
            assert m.call_count == 1
            # Second call: should hit cache
            sc.fetch("t", "key")
            assert m.call_count == 1, "expected cache hit, but a second HTTP call happened"

    def test_force_bypasses_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        good_url = "https://www.ebay.com/sch/i.html?_nkw=t&LH_Sold=1"
        with patch("sold_comps.requests.get",
                   return_value=self._mock_response(ebay_url=good_url)) as m:
            sc.fetch("t", "key")
            sc.fetch("t", "key", force=True)
            assert m.call_count == 2

    def test_serpapi_error_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        with patch("sold_comps.requests.get",
                   return_value=self._mock_response(error="Invalid API key")):
            with pytest.raises(sc.SerpApiError, match="Invalid API key"):
                sc.fetch("t", "key")

    def test_page2_caches_independently_of_page1(self, tmp_path, monkeypatch):
        """BUI-523 AC: page param joins the cache key so each page caches
        independently under the same TTL. Money invariant: a repeat fetch of
        either page within the TTL window must be a cache hit, not a re-bill."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        good_url = "https://www.ebay.com/sch/i.html?_nkw=t&LH_Sold=1"
        with patch("sold_comps.requests.get",
                   return_value=self._mock_response(ebay_url=good_url)) as m:
            sc.fetch("t", "key", page=1)
            sc.fetch("t", "key", page=2)
            assert m.call_count == 2, "page 2 must not be served from page 1's cache entry"
            sc.fetch("t", "key", page=1)
            sc.fetch("t", "key", page=2)
            assert m.call_count == 2, "a repeat fetch of either page must be a cache hit"


# ─── BUI-614: Tier-0 raw response capture ─────────────────────────────────────

def _read_capture_lines(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestRawResponseCapture:
    """conftest.py's autouse _isolate_raw_response_capture fixture already
    redirects CAPTURE_DIR/CAPTURE_PATH to a per-test tmp path — no explicit
    monkeypatch needed here for that half; CACHE_DIR still needs its own
    per-test isolation like every other TestFetch case."""

    def test_serpapi_success_appends_one_record(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        good_url = "https://www.ebay.com/sch/i.html?_nkw=t&LH_Sold=1"
        with patch("sold_comps.requests.get",
                   return_value=self._mock_serpapi(good_url, [{"product_id": "1"}])):
            sc.fetch("test query", "key")

        records = _read_capture_lines(sc.CAPTURE_PATH)
        assert len(records) == 1
        record = records[0]
        assert record["provider"] == sc.PROVIDER_SERPAPI
        assert record["query"] == "test query"
        assert isinstance(record["canonical_url"], str) and record["canonical_url"]
        assert isinstance(record["timestamp"], (int, float))
        assert record["response"]["organic_results"] == [{"product_id": "1"}]

    def test_sold_comps_success_appends_one_record(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        with patch("sold_comps.requests.get",
                   return_value=_sold_comps_good([_sc_item()])):
            sc.fetch_sold_comps("test query", "sc_key")

        records = _read_capture_lines(sc.CAPTURE_PATH)
        assert len(records) == 1
        assert records[0]["provider"] == sc.PROVIDER_SOLD_COMPS
        assert records[0]["query"] == "test query"
        assert len(records[0]["response"]["items"]) == 1

    def test_append_only_survives_a_requery_that_would_overwrite_the_cache(
            self, tmp_path, monkeypatch):
        """The entire point of BUI-614: CACHE_DIR overwrites the same
        canonical-URL key on a re-query (same digest path); the capture file
        must instead accumulate BOTH responses as separate lines."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        good_url = "https://www.ebay.com/sch/i.html?_nkw=t&LH_Sold=1"
        with patch("sold_comps.requests.get",
                   return_value=self._mock_serpapi(good_url, [{"product_id": "1"}])):
            sc.fetch("same query", "key")
            sc.fetch("same query", "key", force=True)  # bypasses cache -> re-fetch

        # Cache still holds exactly one file (the second fetch overwrote it).
        assert len(list(tmp_path.glob("*.json"))) == 1
        # Capture file has both, in append order.
        records = _read_capture_lines(sc.CAPTURE_PATH)
        assert len(records) == 2

    def test_capture_failure_does_not_break_the_fetch(self, tmp_path, monkeypatch, capsys):
        """Hard constraint (BUI-614): a capture failure must never fail the
        fetch it's shadowing. Force _capture_raw_response's write to blow up
        and confirm fetch() still returns normally with the right data."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc, "CAPTURE_DIR", tmp_path / "unwritable-capture")

        def boom(*a, **k):
            raise OSError("disk full (simulated)")

        monkeypatch.setattr(sc.os, "open", boom)
        good_url = "https://www.ebay.com/sch/i.html?_nkw=t&LH_Sold=1"
        with patch("sold_comps.requests.get",
                   return_value=self._mock_serpapi(good_url, [{"product_id": "1"}])):
            data, cache_hit = sc.fetch("test", "key")

        assert cache_hit is False
        assert data["organic_results"] == [{"product_id": "1"}]
        assert "BUI-614" in capsys.readouterr().err

    def test_capture_failure_does_not_break_sold_comps_fetch(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc, "CAPTURE_DIR", tmp_path / "unwritable-capture")

        def boom(*a, **k):
            raise OSError("disk full (simulated)")

        monkeypatch.setattr(sc.os, "open", boom)
        with patch("sold_comps.requests.get",
                   return_value=_sold_comps_good([_sc_item()])):
            data, cache_hit = sc.fetch_sold_comps("test", "sc_key")

        assert cache_hit is False
        assert len(data["items"]) == 1
        assert "BUI-614" in capsys.readouterr().err

    @staticmethod
    def _mock_serpapi(ebay_url, organic_results):
        m = MagicMock()
        m.raise_for_status = MagicMock()
        m.status_code = 200
        m.json = MagicMock(return_value={
            "organic_results": organic_results,
            "search_metadata": {"ebay_url": ebay_url},
        })
        return m


# ─── Tiered query strategy ────────────────────────────────────────────────────

class TestTieredStrategy:
    def _wire(self, tmp_path, monkeypatch, results_per_query):
        """Make fetch() return a different result list per call. Calls past
        the end of the fixture list return an empty result (so tests don't
        need to pad for tiers that may or may not fire)."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        calls = []

        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0, page=1, record_attempt=None, breaker=None):
            calls.append(nkw)
            idx = len(calls) - 1
            results = results_per_query[idx] if idx < len(results_per_query) else []
            return ({
                "organic_results": results,
                "search_metadata": {"ebay_url": "ok&LH_Sold=1"},
            }, False)

        monkeypatch.setattr(sc, "fetch", fake_fetch)
        return calls

    def _comp(self, pid, title="ASM #142 FN+ Marvel 1975", price=10.0):
        return {
            "product_id": pid,
            "title": title,
            "price": {"extracted": price},
            "sold_date": "",
            "buying_format": "auction",
        }

    def test_only_base_when_results_plentiful(self, tmp_path, monkeypatch):
        # 12 grade-tagged comps from base — no broaden, no grade-targeted
        results = [[self._comp(str(i), f"ASM #142 NM {i}.0 Marvel") for i in range(12)]]
        calls = self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps({"title": "ASM", "issue": "142", "year": 1975, "grade": 6.5},
                                  "key")
        assert len(calls) == 1
        assert len(out["comps"]) == 12

    def test_string_year_runs_its_queries_end_to_end(self, tmp_path, monkeypatch):
        """BUI-565 end-to-end, on the ticket's literal repro shape: a string
        `year` must run the pipeline normally, not abort on tier 1 and return
        an empty `queries_used` (which comic-fmv reads as a clean n=0)."""
        results = [[self._comp(str(i), f"X-Men #101 NM {i}.0 Marvel 1976")
                    for i in range(12)]]
        calls = self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps(
            {"item_id": "800411934143", "title": "X-Men", "issue": "101",
             "grade": 7.5, "year": "1976"},
            "key",
        )
        assert "error" not in out
        assert len(calls) == 1
        assert len(out["comps"]) == 12
        assert len(out["queries_used"]) == 1
        # The BUI-347 vintage hardening must fire off the string year too.
        assert "-variant" in calls[0]
        # ...and the echoed input carries the coerced int, so downstream
        # `isinstance(year, (int, float))` gates (comic-fmv's `_is_vintage`)
        # see a year at all.
        assert out["input"]["year"] == 1976

    def test_unparseable_year_is_a_tagged_error_not_a_silent_zero(
            self, tmp_path, monkeypatch):
        """BUI-565: a year that can't be read must HARD-FAIL visibly. An empty
        `queries_used` with no `error` is exactly the shape comic-fmv
        misclassifies as a genuine no-comps book."""
        calls = self._wire(tmp_path, monkeypatch, [])
        out = sc.fetch_book_comps(
            {"title": "X-Men", "issue": "101", "grade": 7.5, "year": "n/a"},
            "key",
        )
        assert calls == []          # nothing was queried...
        assert out["comps"] == []
        assert "error" in out       # ...and the caller is TOLD that
        assert "n/a" in out["error"]

    def test_broadens_when_thin(self, tmp_path, monkeypatch):
        # 2 base, then plenty when broader
        results = [
            [self._comp(str(i)) for i in range(2)],
            [self._comp(str(100 + i)) for i in range(8)],
        ]
        calls = self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps({"title": "X", "issue": "1", "year": 1990, "grade": 9.2},
                                  "key")
        assert len(calls) == 2  # base + broader (grade-targeted may also fire)

    def test_grade_targeted_when_few_grade_tagged(self, tmp_path, monkeypatch):
        # 8 results from base but only 2 have grades parsed — should fire grade-targeted
        ungraded_titles = [self._comp(str(i), title="ASM #142 Marvel 1975") for i in range(8)]
        graded = [self._comp("g1", title="ASM #142 NM Marvel 1975")]
        results = [ungraded_titles + graded, []]
        calls = self._wire(tmp_path, monkeypatch, results)
        sc.fetch_book_comps({"title": "ASM", "issue": "142", "year": 1975, "grade": 9.2},
                            "key")
        # base + grade-targeted (results > 5 so no broaden)
        assert len(calls) == 2
        assert "NM" in calls[1]

    def test_self_exclusion(self, tmp_path, monkeypatch):
        results = [[
            self._comp("147295505028"),  # the listing being valued
            self._comp("aaa", title="ASM #142 FN Marvel"),
        ] * 3]
        self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps({
            "title": "ASM", "issue": "142", "year": 1975, "grade": 6.5,
            "item_id": "147295505028",
        }, "key")
        # Self-listing dropped; only 'aaa' kept once due to dedup
        ids = {c["product_id"] for c in out["comps"]}
        assert "147295505028" not in ids
        assert "aaa" in ids

    def test_self_exclusion_misses_when_product_id_differs(self, tmp_path, monkeypatch):
        """BUI-160: comps are keyed by SerpApi product_id, a DIFFERENT identifier
        namespace from the eBay item_id the --batch path carries. When the
        self-listing surfaces under a product_id that isn't the seeded eBay
        item_id, self-exclusion silently misses it. This locks that documented,
        best-effort contract (self-exclusion is reliable only on a product_id
        match) so a future change to the keying is caught."""
        results = [[
            # The self-listing's relist — but SerpApi gave it product_id "999",
            # not the eBay item_id we seed below.
            self._comp("999", title="ASM #142 FN Marvel"),
            self._comp("aaa", title="ASM #142 VF Marvel"),
        ] * 3]
        self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps({
            "title": "ASM", "issue": "142", "year": 1975, "grade": 6.5,
            "item_id": "147295505028",  # eBay item_id — different namespace
        }, "key")
        ids = {c["product_id"] for c in out["comps"]}
        # Not excluded: product_id 999 != the seeded eBay item_id.
        assert "999" in ids

    def test_dedup_across_tiers(self, tmp_path, monkeypatch):
        # Same comp returned in tier 1 and tier 2 → only counted once
        c1 = self._comp("dup", title="ASM #142 FN+ Marvel 1975")
        results = [[c1], [c1]]
        self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps({"title": "ASM", "issue": "142", "year": 1975, "grade": 6.5},
                                  "key")
        assert len([c for c in out["comps"] if c["product_id"] == "dup"]) == 1

    def test_echoes_req_id_when_present(self, tmp_path, monkeypatch):
        """BUI-174/187: a caller-threaded correlation id round-trips in the echoed
        input so a batch driver can map results by identity, not list position."""
        self._wire(tmp_path, monkeypatch, [[self._comp("1")]])
        out = sc.fetch_book_comps(
            {"title": "ASM", "issue": "142", "year": 1975, "grade": 6.5, "_req_id": 7},
            "key",
        )
        assert out["input"]["_req_id"] == 7

    def test_omits_req_id_when_absent(self, tmp_path, monkeypatch):
        """A standalone caller (no _req_id) gets a clean input echo, no null key."""
        self._wire(tmp_path, monkeypatch, [[self._comp("1")]])
        out = sc.fetch_book_comps(
            {"title": "ASM", "issue": "142", "year": 1975, "grade": 6.5}, "key",
        )
        assert "_req_id" not in out["input"]

    def test_variant_threaded_into_every_tier(self, tmp_path, monkeypatch):
        """BUI-304: a book's `variant` must reach the actual eBay search on ALL
        three tiers (base, broader, grade-targeted) — not just build_query in
        isolation. Force all tiers to fire (thin base, few grade-tagged) and
        assert the variant keyword lands in every query the pipeline runs."""
        results = [
            [self._comp(str(i)) for i in range(2)],          # thin base → broaden
            [self._comp(str(100 + i)) for i in range(3)],    # broader (still ungraded)
        ]
        calls = self._wire(tmp_path, monkeypatch, results)
        sc.fetch_book_comps(
            {"title": "X-Men", "issue": "123", "year": 1991, "grade": 6.5,
             "variant": "Newsstand"},
            "key",
        )
        # base + broader + grade-targeted all fired, each carrying the variant.
        assert len(calls) == 3
        assert all("Newsstand" in nkw for nkw in calls)

    _GRADED_TERMS = ("-cgc", "-cbcs", "-graded", "-slab")

    def test_default_excludes_graded_on_every_tier(self, tmp_path, monkeypatch):
        """BUI-348: a book WITHOUT include_graded keeps the graded-exclusion
        terms on every tier — byte-for-byte the pre-BUI-348 behavior."""
        results = [
            [self._comp(str(i)) for i in range(2)],          # thin base → broaden
            [self._comp(str(100 + i)) for i in range(3)],    # broader (ungraded)
        ]
        calls = self._wire(tmp_path, monkeypatch, results)
        sc.fetch_book_comps(
            {"title": "Amazing Spider-Man", "issue": "50", "year": 1967,
             "grade": 6.5},
            "key",
        )
        assert len(calls) == 3  # all tiers fired
        for nkw in calls:
            assert all(t in nkw for t in self._GRADED_TERMS)

    def test_include_graded_drops_exclusion_on_every_tier(self, tmp_path, monkeypatch):
        """BUI-348: include_graded=True fetches CGC/CBCS slab comps by dropping
        the graded-exclusion terms — on every tier that fires."""
        results = [
            [self._comp(str(i)) for i in range(2)],          # thin base → broaden
            [self._comp(str(100 + i)) for i in range(3)],    # broader
        ]
        calls = self._wire(tmp_path, monkeypatch, results)
        sc.fetch_book_comps(
            {"title": "Amazing Spider-Man", "issue": "50", "year": 1967,
             "grade": 6.5, "include_graded": True},
            "key",
        )
        assert len(calls) == 3
        for nkw in calls:
            assert not any(t in nkw for t in self._GRADED_TERMS)
        # The vintage-masthead hardening (BUI-347) is INDEPENDENT of the graded
        # switch — it must still fire on this pre-2000 rebootable masthead so a
        # modern slab reprint doesn't pollute the ladder.
        assert "-variant" in calls[0]

    def test_graded_broaden_query_keeps_vintage_hardening(self, tmp_path, monkeypatch):
        """BUI-350 (issue 1): the tier-2 "broaden" query drops `year` from its
        query TEXT to widen recall, but that must not also drop the BUI-347
        vintage-masthead exclusion terms — a rebootable-masthead vintage key's
        graded ladder (the CGC-proxy tier's `include_graded=True` pass) could
        otherwise blend in modern CGC/CBCS-slabbed variant covers. Force the
        broaden tier to fire (thin base) and assert the SECOND (broader) call
        — not just the first — still carries the full exclusion lexicon."""
        results = [
            [self._comp(str(i)) for i in range(2)],          # thin base → broaden
            [self._comp(str(100 + i)) for i in range(3)],    # broader
        ]
        calls = self._wire(tmp_path, monkeypatch, results)
        sc.fetch_book_comps(
            {"title": "Amazing Spider-Man", "issue": "50", "year": 1967,
             "grade": 6.5, "include_graded": True},
            "key",
        )
        assert len(calls) >= 2
        broader_nkw = calls[1]
        assert "1967" not in broader_nkw  # year IS dropped from the query text
        for term in ("-variant", "-foil", "-virgin", "-reprint", "-facsimile",
                     "-homage", "-timeless"):
            assert term in broader_nkw, f"{term!r} missing from broader query: {broader_nkw}"


# ─── Gated pagination (BUI-523) ──────────────────────────────────────────────
#
# The gate is a spend protection against the 250/month SerpApi quota: page 2
# of the base query may fire ONLY when SerpApi confirms page 1 was full (a
# next page genuinely exists) AND the comp pool is still short on
# grade-tagged comps. Every test below is really pinning a money invariant,
# not just an output shape — a false-positive gate silently doubles SerpApi
# spend on every liquid-book fetch.

class TestGatedPagination:
    def _comp(self, pid, title="ASM #142 Marvel 1975", price=10.0):
        return {
            "product_id": pid,
            "title": title,
            "price": {"extracted": price},
            "sold_date": "",
            "buying_format": "auction",
        }

    def _page(self, results, *, has_next=False):
        body = {
            "organic_results": results,
            "search_metadata": {"ebay_url": "ok&LH_Sold=1"},
        }
        if has_next:
            body["serpapi_pagination"] = {
                "current": 1,
                "next": "https://serpapi.com/search.json?engine=ebay&_nkw=x&_pgn=2",
            }
        return body

    def test_page2_fires_when_full_and_grade_thin(self, tmp_path, monkeypatch):
        """Page 1 full (SerpApi says a next page exists) + comp pool still
        short on grade-tagged comps -> page 2 is fetched, reusing the exact
        same base query text (only the page differs)."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        calls = []

        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0, page=1, record_attempt=None, breaker=None):
            calls.append({"nkw": nkw, "page": page})
            if page == 1:
                # No grade tokens in the titles -> 0 grade-tagged, well under
                # GRADE_TAGGED_THRESHOLD.
                results = [self._comp(str(i)) for i in range(55)]
                return self._page(results, has_next=True), False
            results = [self._comp(str(100 + i)) for i in range(10)]
            return self._page(results, has_next=False), False

        monkeypatch.setattr(sc, "fetch", fake_fetch)
        out = sc.fetch_book_comps(
            {"title": "ASM", "issue": "142", "year": 1975, "grade": 6.5}, "key",
        )

        page2_calls = [c for c in calls if c["page"] == 2]
        assert len(page2_calls) == 1, f"expected exactly one page-2 fetch, got: {calls}"
        assert page2_calls[0]["nkw"] == calls[0]["nkw"], (
            "page 2 must reuse the SAME base query text as page 1 — a "
            "different nkw would miss the intended cache key / query pairing"
        )
        assert len(out["comps"]) == 65  # 55 (page 1) + 10 (page 2)
        page2_entries = [q for q in out["queries_used"] if q.get("page") == 2]
        assert len(page2_entries) == 1
        assert page2_entries[0]["tier"] == "base"

    def test_page2_skipped_when_page1_not_full(self, tmp_path, monkeypatch):
        """Money invariant: a thin vintage book (no next page) must NEVER
        trigger a page-2 fetch — zero quota increase for it, regardless of
        how few grade-tagged comps it has."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        calls = []

        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0, page=1, record_attempt=None, breaker=None):
            calls.append(page)
            assert page == 1, "must never request page 2 when page 1 wasn't full"
            results = [self._comp(str(i)) for i in range(3)]
            return self._page(results, has_next=False), False

        monkeypatch.setattr(sc, "fetch", fake_fetch)
        sc.fetch_book_comps(
            {"title": "X", "issue": "1", "year": 1965, "grade": 9.0}, "key",
        )
        assert 2 not in calls
        assert all(p == 1 for p in calls)

    def test_page2_skipped_when_grade_pool_already_thick(self, tmp_path, monkeypatch):
        """Page 1 full (next page exists) but already >= GRADE_TAGGED_THRESHOLD
        grade-tagged comps -> the extra page isn't worth the spend, skip it."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        calls = []

        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0, page=1, record_attempt=None, breaker=None):
            calls.append(page)
            n = sc.GRADE_TAGGED_THRESHOLD + 2  # comfortably >= threshold
            results = [self._comp(str(i), title="ASM #142 NM Marvel 1975")
                       for i in range(n)]
            return self._page(results, has_next=True), False

        monkeypatch.setattr(sc, "fetch", fake_fetch)
        sc.fetch_book_comps(
            {"title": "ASM", "issue": "142", "year": 1975, "grade": 9.2}, "key",
        )
        assert 2 not in calls, (
            "grade-tagged pool already thick enough — page 2 must not fire "
            "even though SerpApi says more pages exist"
        )

    def test_page2_dedup_via_seen_ids(self, tmp_path, monkeypatch):
        """Cross-page dedup reuses the existing seen_ids set: product_ids
        that reappear on page 2 must not be double-counted."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)

        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0, page=1, record_attempt=None, breaker=None):
            if page == 1:
                results = [self._comp(str(i)) for i in range(55)]
                return self._page(results, has_next=True), False
            # 5 duplicates of page-1 ids + 5 genuinely new ones
            dup = [self._comp(str(i)) for i in range(5)]
            new = [self._comp(str(200 + i)) for i in range(5)]
            return self._page(dup + new, has_next=False), False

        monkeypatch.setattr(sc, "fetch", fake_fetch)
        out = sc.fetch_book_comps(
            {"title": "ASM", "issue": "142", "year": 1975, "grade": 6.5}, "key",
        )
        ids = [c["product_id"] for c in out["comps"]]
        assert len(ids) == len(set(ids)), "product_ids must be unique across pages"
        assert len(out["comps"]) == 60  # 55 + 5 new (5 dupes correctly dropped)

    def test_page2_fetch_error_recorded_not_crash(self, tmp_path, monkeypatch):
        """A transient failure fetching page 2 must degrade gracefully:
        page-1 comps are kept, the error is recorded, nothing crashes."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)

        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0, page=1, record_attempt=None, breaker=None):
            if page == 1:
                results = [self._comp(str(i)) for i in range(55)]
                return self._page(results, has_next=True), False
            raise requests.ConnectionError("refused")

        monkeypatch.setattr(sc, "fetch", fake_fetch)
        out = sc.fetch_book_comps(
            {"title": "ASM", "issue": "142", "year": 1975, "grade": 6.5}, "key",
        )
        assert len(out["comps"]) == 55
        page2_errors = [q for q in out["queries_used"]
                        if q.get("page") == 2 and "error" in q]
        assert len(page2_errors) == 1
        assert "refused" in page2_errors[0]["error"]

    def test_has_next_page_fails_closed_on_missing_pagination(self):
        """No serpapi_pagination key at all (e.g. an unusual/edge-case
        response) must resolve to 'no next page' — fail closed toward NOT
        spending extra quota, never fail open toward spending it."""
        assert sc._has_next_page({"organic_results": []}) is False
        assert sc._has_next_page({"serpapi_pagination": {}}) is False
        assert sc._has_next_page({"serpapi_pagination": {"next": None}}) is False
        assert sc._has_next_page({"serpapi_pagination": {"current": 1, "next": "url"}}) is True


# ─── Conditional inclusive tier (BUI-524) ────────────────────────────────────
#
# A 4th tier that fires ONLY for a vintage book whose raw pool is still thin
# after tiers 1-3, re-querying without the graded excludes so ONE extra query
# serves both raw and slab needs (feeding BUI-529's cross-check). Same posture
# as TestGatedPagination above: every test here pins a money invariant (spend
# gate + raw-pool purity), not just an output shape.

class TestIsSlabComp:
    def test_true_for_cgc_or_cbcs_with_grade_and_price(self):
        assert sc._is_slab_comp(
            {"grade": 6.5, "price": 100, "title": "ASM 50 CGC 6.5"})
        assert sc._is_slab_comp(
            {"grade": 6.0, "price": 100, "title": "ASM 50 CBCS 6.0"})

    def test_false_for_raw_or_missing_fields(self):
        assert not sc._is_slab_comp(
            {"grade": 6.5, "price": 100, "title": "ASM 50 FN raw, ungraded"})
        assert not sc._is_slab_comp(
            {"grade": None, "price": 100, "title": "ASM 50 CGC"})
        assert not sc._is_slab_comp(
            {"grade": 6.5, "price": None, "title": "ASM 50 CGC"})


class TestInclusiveTier:
    def _comp(self, pid, title="ASM #142 Marvel 1975", price=10.0):
        return {
            "product_id": pid,
            "title": title,
            "price": {"extracted": price},
            "sold_date": "",
            "buying_format": "auction",
        }

    def _wire(self, tmp_path, monkeypatch, results_per_query):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        calls = []

        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0, page=1, record_attempt=None, breaker=None):
            calls.append(nkw)
            idx = len(calls) - 1
            results = results_per_query[idx] if idx < len(results_per_query) else []
            return ({
                "organic_results": results,
                "search_metadata": {"ebay_url": "ok&LH_Sold=1"},
            }, False)

        monkeypatch.setattr(sc, "fetch", fake_fetch)
        return calls

    def test_fires_for_vintage_thin_book_and_splits_raw_vs_slab(self, tmp_path, monkeypatch):
        results = [
            [self._comp("r1"), self._comp("r2")],   # tier 1 base: thin (2 < 5)
            [self._comp("r3")],                       # tier 2 broader: still thin
            [                                          # tier 4 inclusive
                self._comp("r4"),
                self._comp("s1", title="ASM #142 CGC 6.5 1975", price=1200.0),
                self._comp("s2", title="ASM #142 CBCS 7.0 1975", price=1800.0),
            ],
        ]
        calls = self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps(
            {"title": "ASM", "issue": "142", "year": 1975}, "key",
        )
        assert len(calls) == 3  # base + broader + inclusive
        raw_ids = {c["product_id"] for c in out["comps"]}
        slab_ids = {c["product_id"] for c in out["slab_comps"]}
        assert raw_ids == {"r1", "r2", "r3", "r4"}
        assert slab_ids == {"s1", "s2"}
        for t in ("-cgc", "-cbcs", "-graded", "-slab"):
            assert t not in calls[2]  # the inclusive query itself

    def test_fires_for_a_string_vintage_year(self, tmp_path, monkeypatch):
        """BUI-565: the tier-4 gate used to be
        `isinstance(year, (int, float)) and year < CUTOFF`, which is silently
        False for a string year — so this tier never fired for exactly the
        vintage books it exists to rescue. (Same class of bug as the
        build_query TypeError, but silent rather than raising.)"""
        results = [
            [self._comp("r1"), self._comp("r2")],   # tier 1 base: thin
            [self._comp("r3")],                     # tier 2 broader: still thin
            [self._comp("r4"),
             self._comp("s1", title="ASM #142 CGC 6.5 1975", price=1200.0)],
        ]
        calls = self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps(
            {"title": "ASM", "issue": "142", "year": "1975"}, "key",
        )
        assert {q["tier"] for q in out["queries_used"]} >= {"inclusive"}
        assert len(calls) == 3
        assert {c["product_id"] for c in out["slab_comps"]} == {"s1"}

    def test_does_not_fire_for_modern_book(self, tmp_path, monkeypatch):
        # Thin raw pool, but modern — the 0.50-0.55 factor is vintage-only, so
        # a slab ladder here would be actively misleading. Zero extra spend.
        results = [
            [self._comp("r1"), self._comp("r2")],
            [self._comp("r3")],
        ]
        calls = self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps(
            {"title": "X-Men", "issue": "1", "year": 2015}, "key",
        )
        tiers = {q["tier"] for q in out["queries_used"]}
        assert "inclusive" not in tiers
        assert len(calls) == 2  # base + broader only

    def test_does_not_fire_when_raw_pool_healthy(self, tmp_path, monkeypatch):
        # Vintage, but tier 1 alone already cleared the thin-pool threshold —
        # the common "liquid vintage book" case must add ZERO extra searches
        # and leave `comps` byte-identical to the pre-BUI-524 3-tier output.
        results = [[self._comp(str(i)) for i in range(8)]]
        calls = self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps(
            {"title": "ASM", "issue": "142", "year": 1975}, "key",
        )
        assert len(calls) == 1  # base only
        tiers = {q["tier"] for q in out["queries_used"]}
        assert "inclusive" not in tiers
        assert out["slab_comps"] == []
        assert len(out["comps"]) == 8

    def test_does_not_fire_when_include_graded_already_set(self, tmp_path, monkeypatch):
        # An explicit graded-only pass (BUI-348) already runs every tier
        # inclusive — a 4th inclusive tier there is pure duplicate spend.
        results = [[self._comp("r1"), self._comp("r2")]]
        calls = self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps(
            {"title": "ASM", "issue": "142", "year": 1975, "include_graded": True},
            "key",
        )
        tiers = {q["tier"] for q in out["queries_used"]}
        assert "inclusive" not in tiers

    def test_dedup_with_earlier_tiers(self, tmp_path, monkeypatch):
        dup = self._comp("dup")
        results = [
            [dup],                              # tier 1 base: 1 raw (thin)
            [],                                   # tier 2 broader: nothing new
            [dup, self._comp("new")],            # tier 4 inclusive: dup + new
        ]
        calls = self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps(
            {"title": "ASM", "issue": "142", "year": 1975}, "key",
        )
        assert len(calls) == 3
        ids = [c["product_id"] for c in out["comps"]]
        assert len(ids) == len(set(ids))
        assert set(ids) == {"dup", "new"}

    def test_inclusive_query_keeps_vintage_hardening_and_drops_excludes(
        self, tmp_path, monkeypatch,
    ):
        results = [
            [self._comp("r1", title="Amazing Spider-Man 50 Marvel"),
             self._comp("r2", title="Amazing Spider-Man 50 Marvel")],
            [],
            [],
        ]
        calls = self._wire(tmp_path, monkeypatch, results)
        sc.fetch_book_comps(
            {"title": "Amazing Spider-Man", "issue": "50", "year": 1967}, "key",
        )
        inclusive_nkw = calls[2]
        for t in ("-cgc", "-cbcs", "-graded", "-slab"):
            assert t not in inclusive_nkw
        for t in ("-variant", "-foil", "-virgin", "-reprint", "-facsimile",
                  "-homage", "-timeless"):
            assert t in inclusive_nkw, f"{t!r} missing from inclusive query: {inclusive_nkw}"


# ─── End-to-end-ish: batch driver ────────────────────────────────────────────

class TestBatch:
    def test_runs_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0, page=1, record_attempt=None, breaker=None):
            return ({
                "organic_results": [{
                    "product_id": "1",
                    "title": "ASM #142 FN+",
                    "price": {"extracted": 12.0},
                }],
                "search_metadata": {"ebay_url": "ok&LH_Sold=1"},
            }, False)
        monkeypatch.setattr(sc, "fetch", fake_fetch)

        books = [
            {"title": "ASM", "issue": "142", "year": 1975, "grade": 6.5},
            {"title": "ASM", "issue": "151", "year": 1975, "grade": 7.0},
        ]
        results = sc.run_batch(books, "key", max_workers=2)
        assert len(results) == 2
        assert all(len(r["comps"]) == 1 for r in results)

    def test_records_errors_per_book(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0, page=1, record_attempt=None, breaker=None):
            raise RuntimeError("kaboom")
        monkeypatch.setattr(sc, "fetch_book_comps",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

        books = [{"title": "X", "issue": "1", "year": 1990, "grade": 9.0}]
        results = sc.run_batch(books, "key")
        assert len(results) == 1
        assert "error" in results[0]


# ─── Retry / transient-error tests ───────────────────────────────────────────

class TestFetchRetry:
    """Tests for the bounded retry/backoff added to fetch() for transient errors."""

    def _mock_response(self, organic_results=None, ebay_url=None, status_code=200):
        """Build a mock requests.Response for a successful (2xx) reply."""
        m = MagicMock()
        body = {
            "organic_results": organic_results or [],
            "search_metadata": {"ebay_url": ebay_url or "https://www.ebay.com/?LH_Sold=1"},
        }
        m.json = MagicMock(return_value=body)
        if status_code == 200:
            m.raise_for_status = MagicMock()  # no-op
        else:
            http_err = requests.HTTPError(response=MagicMock(status_code=status_code))
            m.raise_for_status = MagicMock(side_effect=http_err)
        m.status_code = status_code
        return m

    def test_retry_then_succeed_timeout(self, tmp_path, monkeypatch):
        """fetch() retries on Timeout and returns data when the second call succeeds."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)

        good_response = self._mock_response(
            ebay_url="https://www.ebay.com/?LH_Sold=1",
            organic_results=[{"product_id": "42"}],
        )
        call_count = {"n": 0}

        def fake_get(url, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise requests.Timeout("timed out")
            return good_response

        with patch("sold_comps.requests.get", side_effect=fake_get):
            data, cache_hit = sc.fetch("test", "key")

        assert cache_hit is False
        assert data["organic_results"] == [{"product_id": "42"}]
        assert call_count["n"] == 2  # failed once, then succeeded

    def test_retry_then_succeed_503(self, tmp_path, monkeypatch):
        """fetch() retries on a 503 HTTPError and returns data on the second call."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)

        good_response = self._mock_response(
            ebay_url="https://www.ebay.com/?LH_Sold=1",
            organic_results=[{"product_id": "7"}],
        )
        call_count = {"n": 0}

        def fake_get(url, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                bad = MagicMock()
                bad.status_code = 503
                err = requests.HTTPError(response=bad)
                bad.raise_for_status = MagicMock(side_effect=err)
                bad.json = MagicMock(return_value={})
                # raise_for_status is called by fetch() on the response
                bad.raise_for_status.side_effect = err
                # Return the bad mock so fetch calls raise_for_status on it
                return bad
            return good_response

        with patch("sold_comps.requests.get", side_effect=fake_get):
            data, cache_hit = sc.fetch("test", "key")

        assert data["organic_results"] == [{"product_id": "7"}]
        assert call_count["n"] == 2

    def test_exhausted_transient_error_reraises(self, tmp_path, monkeypatch):
        """fetch() re-raises after exhausting all retries."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)

        with patch("sold_comps.requests.get",
                   side_effect=requests.ConnectionError("no route to host")):
            with pytest.raises(requests.ConnectionError):
                sc.fetch("test", "key")

    def test_exhausted_retryable_status_reraises_http_error(self, tmp_path, monkeypatch):
        """BUI-333: a persistent 503 across every retry attempt exercises the
        RetryExhausted(response=...) branch — fetch() must still raise an
        HTTPError carrying the original status code, and requests.get must be
        called exactly FETCH_MAX_RETRIES times (no over/under-retrying)."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)

        def make_bad():
            bad = MagicMock()
            bad.status_code = 503
            bad.raise_for_status = MagicMock(side_effect=requests.HTTPError(response=bad))
            return bad

        with patch("sold_comps.requests.get", side_effect=lambda *a, **k: make_bad()) as mock_get:
            with pytest.raises(requests.HTTPError) as excinfo:
                sc.fetch("test", "key")
            assert excinfo.value.response.status_code == 503
            assert mock_get.call_count == sc.FETCH_MAX_RETRIES

    def test_retry_then_succeed_other_request_exception_type(self, tmp_path, monkeypatch):
        """BUI-333: the shared retry_request() helper widens the retryable
        network-error catch from (Timeout, ConnectionError) to any
        requests.exceptions.RequestException. Confirm a different subtype
        (ChunkedEncodingError) is now retried rather than propagating
        immediately — the old hand-rolled loop would NOT have caught this."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)

        good_response = self._mock_response(
            ebay_url="https://www.ebay.com/?LH_Sold=1",
            organic_results=[{"product_id": "99"}],
        )
        call_count = {"n": 0}

        def fake_get(url, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise requests.exceptions.ChunkedEncodingError("connection broken")
            return good_response

        with patch("sold_comps.requests.get", side_effect=fake_get):
            data, cache_hit = sc.fetch("test", "key")

        assert data["organic_results"] == [{"product_id": "99"}]
        assert call_count["n"] == 2

    def test_non_retryable_4xx_not_retried(self, tmp_path, monkeypatch):
        """A 404 is NOT retried — requests.get is called exactly once."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)

        bad = MagicMock()
        bad.status_code = 404
        http_err = requests.HTTPError(response=bad)
        bad.raise_for_status = MagicMock(side_effect=http_err)
        bad.json = MagicMock(return_value={})

        with patch("sold_comps.requests.get", return_value=bad) as mock_get:
            with pytest.raises(requests.HTTPError):
                sc.fetch("test", "key")
            assert mock_get.call_count == 1

    def test_run_records_connection_error_not_crash(self, tmp_path, monkeypatch):
        """_run in fetch_book_comps records a RequestException as a query error,
        not as a top-level crash. comps remains empty and no exception propagates."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)

        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0, page=1, record_attempt=None, breaker=None):
            raise requests.ConnectionError("refused")

        monkeypatch.setattr(sc, "fetch", fake_fetch)

        out = sc.fetch_book_comps(
            {"title": "ASM", "issue": "142", "year": 1975, "grade": 6.5},
            "key",
        )

        # No exception propagated; comps empty
        assert out["comps"] == []
        # At least the base query recorded the error
        assert len(out["queries_used"]) >= 1
        errors = [q for q in out["queries_used"] if "error" in q]
        assert len(errors) >= 1
        assert "refused" in errors[0]["error"]


# ─── BUI-537: full attempt trail (page/outcome, retry-attempt recording) ─────

class TestAttemptTrail:
    def _bad_503(self):
        bad = MagicMock()
        bad.status_code = 503
        bad.raise_for_status = MagicMock(side_effect=requests.HTTPError(response=bad))
        return bad

    def _good(self, organic_results=None, ebay_url=None):
        good = MagicMock()
        good.status_code = 200
        good.raise_for_status = MagicMock()
        good.json = MagicMock(return_value={
            "organic_results": organic_results or [],
            "search_metadata": {"ebay_url": ebay_url or "ok&LH_Sold=1"},
        })
        return good

    def test_success_entry_always_has_page_and_outcome(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        with patch("sold_comps.requests.get", return_value=self._good()):
            out = sc.fetch_book_comps({"title": "X", "issue": "1"}, "key")
        assert len(out["queries_used"]) == 1
        entry = out["queries_used"][0]
        assert entry["page"] == 1
        assert entry["outcome"] == "live"

    def test_cache_hit_entry_has_hit_outcome(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        with patch("sold_comps.requests.get", return_value=self._good()) as mock_get:
            sc.fetch_book_comps({"title": "X", "issue": "1"}, "key")
            out = sc.fetch_book_comps({"title": "X", "issue": "1"}, "key")
        assert mock_get.call_count == 1, "second call should be a cache hit"
        entry = out["queries_used"][0]
        assert entry["outcome"] == "hit"
        assert entry["page"] == 1

    def test_error_entry_always_has_page_and_outcome(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)
        with patch("sold_comps.requests.get", side_effect=lambda *a, **k: self._bad_503()):
            out = sc.fetch_book_comps({"title": "X", "issue": "1"}, "key")
        assert len(out["queries_used"]) == sc.FETCH_MAX_RETRIES
        for entry in out["queries_used"]:
            assert entry["page"] == 1
            assert entry["outcome"].startswith("error:")
            assert "error" in entry

    def test_fetch_err_book_records_full_attempt_trail(self, tmp_path, monkeypatch):
        """BUI-537 acceptance: a fetch-err book shows its full attempt trail —
        every physical SerpApi charge for the (only) tier that fires, not
        just the last one. `title`/`issue` only (no year/grade) keeps tiers
        2-4 from firing so the count is exactly FETCH_MAX_RETRIES."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)
        with patch("sold_comps.requests.get",
                  side_effect=lambda *a, **k: self._bad_503()) as mock_get:
            out = sc.fetch_book_comps({"title": "ASM", "issue": "142"}, "key")

        assert out["comps"] == []
        assert mock_get.call_count == sc.FETCH_MAX_RETRIES
        # One queries_used entry per physical charge — no undercounting.
        assert len(out["queries_used"]) == sc.FETCH_MAX_RETRIES
        assert all(q["tier"] == "base" for q in out["queries_used"])
        assert all("error" in q for q in out["queries_used"])

    def test_retry_then_succeed_records_full_trail_no_double_count(
        self, tmp_path, monkeypatch,
    ):
        """BUI-537 adversarial check: a query that fails twice then succeeds
        must produce exactly 3 queries_used entries (2 error + 1 live) — one
        per physical SerpApi charge, never double-counting the winning
        (terminal) attempt via both the retry-attempt hook AND the normal
        success path."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)
        call_count = {"n": 0}

        def fake_get(url, **kwargs):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return self._bad_503()
            return self._good()

        with patch("sold_comps.requests.get", side_effect=fake_get):
            out = sc.fetch_book_comps({"title": "X", "issue": "1"}, "key")

        assert call_count["n"] == 3
        assert len(out["queries_used"]) == 3, out["queries_used"]
        errors = [q for q in out["queries_used"] if "error" in q]
        lives = [q for q in out["queries_used"] if q.get("outcome") == "live"]
        assert len(errors) == 2
        assert len(lives) == 1
        assert all(q["page"] == 1 for q in out["queries_used"])

    def test_record_attempt_only_fires_for_superseded_attempts(
        self, tmp_path, monkeypatch,
    ):
        """Unit-level check directly on fetch(): record_attempt must be
        called for the 503 that gets retried, but NOT for the eventual
        successful (terminal) attempt — that one is reported via the normal
        return value instead, so the caller (here, a plain collector) must
        see exactly one call."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)
        calls = []

        def fake_get(url, **kwargs):
            if len(calls) == 0:
                return self._bad_503()
            return self._good()

        with patch("sold_comps.requests.get", side_effect=fake_get):
            data, cache_hit = sc.fetch(
                "t", "key", record_attempt=lambda outcome, detail: calls.append((outcome, detail)),
            )
        assert cache_hit is False
        assert len(calls) == 1
        assert calls[0][0].startswith("error:")

    def test_exhausted_retryable_status_not_double_recorded_by_run(
        self, tmp_path, monkeypatch,
    ):
        """The terminal (exhausted) attempt inside fetch() is recorded ONCE
        by _run's except-clause, not also by record_attempt — this test
        exercises fetch_book_comps end to end (not fetch() directly) so both
        recording paths are live simultaneously; a double-count bug would
        show up as FETCH_MAX_RETRIES + 1 entries instead of exactly
        FETCH_MAX_RETRIES."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)
        with patch("sold_comps.requests.get",
                  side_effect=lambda *a, **k: self._bad_503()) as mock_get:
            out = sc.fetch_book_comps({"title": "X", "issue": "1"}, "key")
        assert mock_get.call_count == sc.FETCH_MAX_RETRIES
        assert len(out["queries_used"]) == sc.FETCH_MAX_RETRIES


# ─── BUI-535: batch circuit breaker ──────────────────────────────────────────

class TestCircuitBreakerUnit:
    def test_trips_after_threshold_consecutive_errors(self):
        breaker = sc._CircuitBreaker(threshold=3)
        breaker.record_error()
        assert breaker.tripped is False
        breaker.record_error()
        assert breaker.tripped is False
        breaker.record_error()
        assert breaker.tripped is True
        assert breaker.should_skip_live() is True

    def test_success_resets_counter(self):
        breaker = sc._CircuitBreaker(threshold=3)
        breaker.record_error()
        breaker.record_error()
        breaker.record_success()  # reset — back to 0
        breaker.record_error()
        breaker.record_error()
        assert breaker.tripped is False, "2 errors post-reset must not trip a threshold-3 breaker"
        breaker.record_error()
        assert breaker.tripped is True

    def test_cache_hits_never_trip_or_reset(self, tmp_path, monkeypatch):
        """Cache hits must not touch the breaker at all — fetch() should
        never call record_error/record_success on a cache hit."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        breaker = sc._CircuitBreaker(threshold=1)
        good = MagicMock()
        good.status_code = 200
        good.raise_for_status = MagicMock()
        good.json = MagicMock(return_value={
            "organic_results": [], "search_metadata": {"ebay_url": "ok&LH_Sold=1"},
        })
        with patch("sold_comps.requests.get", return_value=good) as mock_get:
            sc.fetch("t", "key", breaker=breaker)  # live — resets/no-ops
            sc.fetch("t", "key", breaker=breaker)  # cache hit
        assert mock_get.call_count == 1
        assert breaker.tripped is False

    def test_warning_printed_exactly_once_on_crossing(self, capsys):
        breaker = sc._CircuitBreaker(threshold=5)
        for _ in range(5):
            breaker.record_error()
        # Further errors past the trip must not print again.
        for _ in range(10):
            breaker.record_error()
        captured = capsys.readouterr()
        assert captured.err.count("SerpApi appears down") == 1

    def test_success_after_trip_does_not_untrip(self):
        breaker = sc._CircuitBreaker(threshold=2)
        breaker.record_error()
        breaker.record_error()
        assert breaker.tripped is True
        breaker.record_success()
        assert breaker.tripped is True, "breaker must not un-trip mid-batch"

    def test_concurrent_errors_trip_exactly_once(self, capsys):
        """Adversarial: many threads hammer record_error() concurrently —
        the lock must ensure exactly one thread ever observes 'I just
        crossed the threshold' and exactly one warning is printed, no matter
        how the errors interleave."""
        breaker = sc._CircuitBreaker(threshold=5)
        barrier = threading.Barrier(20)

        def hammer():
            barrier.wait()
            for _ in range(10):
                breaker.record_error()

        threads = [threading.Thread(target=hammer) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert breaker.tripped is True
        captured = capsys.readouterr()
        assert captured.err.count("SerpApi appears down") == 1

    def test_concurrent_success_and_error_interleaving_stays_consistent(self):
        """Adversarial: threads calling record_error() and record_success()
        concurrently must never corrupt internal state or raise — this is an
        invariant/fuzz check (exact tripped value is inherently racy), not a
        deterministic-outcome check."""
        breaker = sc._CircuitBreaker(threshold=10)
        barrier = threading.Barrier(10)

        def errors():
            barrier.wait()
            for _ in range(25):
                breaker.record_error()

        def successes():
            barrier.wait()
            for _ in range(25):
                breaker.record_success()

        threads = (
            [threading.Thread(target=errors) for _ in range(5)]
            + [threading.Thread(target=successes) for _ in range(5)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No crash, and internal state stays sane regardless of the race.
        assert isinstance(breaker.tripped, bool)
        assert breaker._consecutive_errors >= 0


class TestCircuitBreakerFetchIntegration:
    def _bad_503(self):
        bad = MagicMock()
        bad.status_code = 503
        bad.raise_for_status = MagicMock(side_effect=requests.HTTPError(response=bad))
        return bad

    def _good(self, ebay_url=None):
        good = MagicMock()
        good.status_code = 200
        good.raise_for_status = MagicMock()
        good.json = MagicMock(return_value={
            "organic_results": [], "search_metadata": {"ebay_url": ebay_url or "ok&LH_Sold=1"},
        })
        return good

    def test_tripped_breaker_serves_cache_without_live_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        breaker = sc._CircuitBreaker(threshold=1)
        with patch("sold_comps.requests.get", return_value=self._good()) as mock_get:
            sc.fetch("t", "key")  # populate cache (no breaker on this one)
            assert mock_get.call_count == 1
            breaker.record_error()  # trips immediately (threshold=1)
            assert breaker.tripped is True
            data, cache_hit = sc.fetch("t", "key", breaker=breaker)
        assert cache_hit is True
        assert mock_get.call_count == 1, "tripped breaker must not issue a live call on a cache hit"

    def test_tripped_breaker_cache_miss_raises_without_live_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        breaker = sc._CircuitBreaker(threshold=1)
        breaker.record_error()
        assert breaker.tripped is True
        with patch("sold_comps.requests.get", return_value=self._good()) as mock_get:
            with pytest.raises(sc.BreakerTrippedError):
                sc.fetch("uncached query", "key", breaker=breaker)
        assert mock_get.call_count == 0, "no live charge for a breaker-tripped cache miss"

    def test_force_does_not_bypass_tripped_breaker(self, tmp_path, monkeypatch):
        """BUI-535 acceptance: --force must NOT bypass a tripped breaker."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        breaker = sc._CircuitBreaker(threshold=1)
        with patch("sold_comps.requests.get", return_value=self._good()) as mock_get:
            sc.fetch("t", "key")  # populate cache
            assert mock_get.call_count == 1
            breaker.record_error()
            assert breaker.tripped is True
            # force=True would normally bypass the cache and go live — the
            # breaker must still win, serving the cache instead.
            data, cache_hit = sc.fetch("t", "key", force=True, breaker=breaker)
            assert cache_hit is True
            assert mock_get.call_count == 1, "force must not buy a live call past a tripped breaker"
            with pytest.raises(sc.BreakerTrippedError):
                sc.fetch("uncached and forced", "key", force=True, breaker=breaker)
            assert mock_get.call_count == 1, "force must not buy a live call on an uncached query either"

    def test_breaker_tripped_field_on_fetch_book_comps_result(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        breaker = sc._CircuitBreaker(threshold=1)
        breaker.record_error()
        with patch("sold_comps.requests.get", return_value=self._good()):
            out = sc.fetch_book_comps({"title": "X", "issue": "1"}, "key", breaker=breaker)
        assert out["breaker_tripped"] is True
        # Every entry for this book is a synthetic breaker-skip error.
        assert all("error" in q for q in out["queries_used"])
        assert all(q["outcome"] == "error:BreakerTrippedError" for q in out["queries_used"])

    def test_breaker_trips_mid_book_between_tiers(self, tmp_path, monkeypatch):
        """Adversarial: the breaker can trip (due to some OTHER concurrent
        book's errors) in the gap between THIS book's own tier 1 (which
        succeeds before the trip) and tier 2+ (which must then see
        should_skip_live()=True and record a breaker-skip error — never
        silently attempt a live call). The mock raises if a live call is
        ever attempted after the simulated trip, so any regression that
        re-checks the breaker too late (or not at all) fails loudly here."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        breaker = sc._CircuitBreaker(threshold=5)
        call_state = {"n": 0}

        def fake_get(url, **kwargs):
            call_state["n"] += 1
            if call_state["n"] == 1:
                # Tier 1 (base) succeeds but the pool stays thin (0 comps),
                # so tier 2 (broader) is guaranteed to fire next. Simulate
                # some OTHER concurrent book tripping the shared breaker in
                # the gap between tier 1 and tier 2.
                for _ in range(5):
                    breaker.record_error()
                good = MagicMock()
                good.status_code = 200
                good.raise_for_status = MagicMock()
                good.json = MagicMock(return_value={
                    "organic_results": [],
                    "search_metadata": {"ebay_url": "ok&LH_Sold=1"},
                })
                return good
            raise AssertionError(
                "no live call should ever be attempted once the breaker is tripped"
            )

        with patch("sold_comps.requests.get", side_effect=fake_get):
            out = sc.fetch_book_comps(
                {"title": "ASM", "issue": "142", "year": 1975, "grade": 6.5},
                "key", breaker=breaker,
            )

        assert breaker.tripped is True
        tiers_seen = [q["tier"] for q in out["queries_used"]]
        assert tiers_seen[0] == "base"
        later_entries = [q for q in out["queries_used"] if q["tier"] != "base"]
        assert later_entries, "expected at least one post-trip tier to fire"
        assert all(q["outcome"] == "error:BreakerTrippedError" for q in later_entries)
        assert out["breaker_tripped"] is True


class TestCircuitBreakerBatchIntegration:
    def _bad_503(self):
        bad = MagicMock()
        bad.status_code = 503
        bad.raise_for_status = MagicMock(side_effect=requests.HTTPError(response=bad))
        return bad

    def _good(self):
        good = MagicMock()
        good.status_code = 200
        good.raise_for_status = MagicMock()
        good.json = MagicMock(return_value={
            "organic_results": [], "search_metadata": {"ebay_url": "ok&LH_Sold=1"},
        })
        return good

    def test_full_outage_trips_and_caps_spend(self, tmp_path, monkeypatch):
        """BUI-535 acceptance: replay of a full-outage batch (every live call
        errors) trips the breaker well before every book gets its full
        FETCH_MAX_RETRIES worth of charges — total spend stays a small
        fraction of the no-breaker cost."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)
        books = [{"title": "Book", "issue": str(i)} for i in range(20)]
        no_breaker_cost = len(books) * sc.FETCH_MAX_RETRIES  # 60

        with patch("sold_comps.requests.get",
                  side_effect=lambda *a, **k: self._bad_503()) as mock_get:
            results = sc.run_batch(books, "key", max_workers=5)

        assert len(results) == len(books)
        assert mock_get.call_count < no_breaker_cost / 2, (
            f"expected the breaker to meaningfully cap spend below "
            f"{no_breaker_cost / 2}, got {mock_get.call_count}"
        )
        assert any(r.get("breaker_tripped") for r in results)

    def test_healthy_mixed_run_never_trips(self, tmp_path, monkeypatch):
        """BUI-535 acceptance: a healthy run with occasional isolated
        single-attempt errors (each immediately recovers on retry) must
        never trip the breaker. max_workers=1 keeps call ordering
        deterministic so the "isolated" failures are genuinely isolated."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)
        books = [{"title": "Book", "issue": str(i)} for i in range(20)]
        # Every 7th physical call fails once, then the immediate retry (and
        # every other call) succeeds — sporadic, not consecutive.
        call_count = {"n": 0}

        def fake_get(url, **kwargs):
            call_count["n"] += 1
            if call_count["n"] % 7 == 0:
                return self._bad_503()
            return self._good()

        with patch("sold_comps.requests.get", side_effect=fake_get):
            results = sc.run_batch(books, "key", max_workers=1)

        assert len(results) == len(books)
        assert not any(r.get("breaker_tripped") for r in results), (
            "sporadic isolated errors among successes must never trip the breaker"
        )

    def test_tripped_breaker_does_not_recover_mid_batch(self, tmp_path, monkeypatch):
        """Reliability: even if SerpApi 'recovers' partway through the batch
        (later requests would succeed), once tripped this run_batch() call
        stays cache-only for the remainder — no in-batch auto-heal. Recovery
        is a fresh run (a new run_batch() call, a new breaker), matching the
        printed warning's 're-run later' guidance. max_workers=1 keeps this
        deterministic: the first several books exhaust their retries against
        a persistent 503 (tripping the breaker), then later books' physical
        calls would all succeed if attempted — the test's own mock instead
        raises if any of THOSE later calls is ever actually made."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)
        books = [{"title": "Book", "issue": str(i)} for i in range(20)]
        call_state = {"n": 0, "tripped_at": None}

        def fake_get(url, **kwargs):
            call_state["n"] += 1
            # Once several books have exhausted retries the breaker will have
            # tripped (threshold=5, single-tier books, no cache) — from that
            # point on, fetch() must never even call this again for a live
            # attempt (it should short-circuit to BreakerTrippedError first).
            if call_state["n"] > sc.CIRCUIT_BREAKER_THRESHOLD * sc.FETCH_MAX_RETRIES:
                raise AssertionError(
                    "a live call was attempted long after the breaker should "
                    "have tripped and stayed tripped — 'recovery' must not "
                    "happen mid-batch"
                )
            return self._bad_503()

        with patch("sold_comps.requests.get", side_effect=fake_get):
            results = sc.run_batch(books, "key", max_workers=1)

        assert len(results) == len(books)
        assert any(r.get("breaker_tripped") for r in results)
        # Every book after the trip is a fetch-err (every entry carries
        # 'error') — none silently produced comps from a "recovered" call.
        assert all(
            all("error" in q for q in r["queries_used"])
            for r in results if r.get("breaker_tripped")
        )




# ─── BUI-545: secondary provider (sold-comps.com) failover ───────────────────

def _sc_item(item_id="111", title="ASM #142 FN+ Marvel 1975", price=25.0,
             ended="2026-07-25", listing_type="sold"):
    """A sold-comps.com response item in the shape the live API returns."""
    return {
        "itemId": item_id,
        "title": title,
        "soldPrice": price,
        "endedAt": ended,
        "listingType": listing_type,
        "buyingFormat": "auction",
        "url": f"https://www.ebay.com/itm/{item_id}",
    }


def _sold_comps_good(items):
    m = MagicMock()
    m.status_code = 200
    m.raise_for_status = MagicMock()
    m.json = MagicMock(return_value={"items": items})
    return m


def _serpapi_down():
    """SerpApi's post-login-wall failure mode: HTTP 200 with an error field."""
    m = MagicMock()
    m.status_code = 200
    m.raise_for_status = MagicMock()
    m.json = MagicMock(return_value={"error": "eBay hasn't returned any results"})
    return m


def _serpapi_good(results):
    m = MagicMock()
    m.status_code = 200
    m.raise_for_status = MagicMock()
    m.json = MagicMock(return_value={
        "organic_results": results,
        "search_metadata": {"ebay_url": "ok&LH_Sold=1"},
    })
    return m


def _route(serpapi=None, sold_comps=None):
    """A requests.get side_effect that answers each provider's endpoint with
    its factory — and fails LOUDLY if a provider that shouldn't be called is
    (factory left as None)."""
    def fake_get(url, **kwargs):
        if url.startswith(sc.SERPAPI_ENDPOINT):
            assert serpapi is not None, f"unexpected SerpApi call: {url}"
            return serpapi()
        if url.startswith(sc.SOLD_COMPS_ENDPOINT):
            assert sold_comps is not None, f"unexpected sold-comps.com call: {url}"
            return sold_comps()
        raise AssertionError(f"unexpected URL: {url}")
    return fake_get


class TestProviderOrder:
    def test_default(self, monkeypatch):
        monkeypatch.delenv(sc.PROVIDERS_ENV_VAR, raising=False)
        assert sc._provider_order() == sc.DEFAULT_PROVIDER_ORDER

    def test_override_reorders(self, monkeypatch):
        monkeypatch.setenv(sc.PROVIDERS_ENV_VAR, "sold-comps.com")
        assert sc._provider_order() == ("sold-comps.com",)

    def test_unknown_name_fails_loudly(self, monkeypatch):
        monkeypatch.setenv(sc.PROVIDERS_ENV_VAR, "serpapi,typo-provider")
        with pytest.raises(ValueError, match="typo-provider"):
            sc._provider_order()


class TestParseCompSoldComps:
    def test_maps_all_fields(self):
        comp = sc.parse_comp_sold_comps(_sc_item())
        assert comp == {
            "product_id": "111",
            "title": "ASM #142 FN+ Marvel 1975",
            "price": 25.0,
            "grade": 6.5,
            "sold_date": "2026-07-25",  # ISO passthrough — fmv_math parses it
            "buying_format": "auction",
            "link": "https://www.ebay.com/itm/111",
        }

    def test_item_id_falls_back_to_url(self):
        item = _sc_item()
        item["itemId"] = None
        assert sc.parse_comp_sold_comps(item)["product_id"] == "111"

    def test_missing_price_returns_none(self):
        assert sc.parse_comp_sold_comps(_sc_item(price=None)) is None

    def test_price_bounds_apply(self):
        # Same 0.50–50000 sanity bounds as the SerpApi parse path.
        assert sc.parse_comp_sold_comps(_sc_item(price=0.25)) is None


class TestFetchSoldComps:
    def test_live_fetch_caches_and_keeps_key_out_of_url(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        with patch("sold_comps.requests.get",
                   return_value=_sold_comps_good([_sc_item()])) as m:
            data, hit = sc.fetch_sold_comps("t", "sc_key")
            assert hit is False and len(data["items"]) == 1
            data2, hit2 = sc.fetch_sold_comps("t", "sc_key")
            assert hit2 is True
            assert m.call_count == 1, "second call must be a cache hit"
        args, kwargs = m.call_args
        assert "sc_key" not in args[0], "API key must never enter the URL"
        assert kwargs["headers"]["Authorization"] == "Bearer sc_key"

    def test_cache_key_disjoint_from_serpapi(self):
        assert (sc._cache_path(sc.canonical_sold_comps_url("t"))
                != sc._cache_path(sc.canonical_serpapi_url("t")))

    def test_active_item_in_sold_response_is_provider_failure(self, tmp_path, monkeypatch):
        """The generalized LH_Sold=1 trap: one active-shaped item in a
        sold=true response fails the WHOLE response loudly — and nothing is
        cached, so the bad payload can't be served later."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        items = [_sc_item(),
                 _sc_item("222", price=None, ended=None, listing_type="active")]
        breaker = sc._CircuitBreaker(threshold=1, provider_name="sold-comps.com")
        with patch("sold_comps.requests.get", return_value=_sold_comps_good(items)):
            with pytest.raises(sc.SoldCompsError, match="not sold-shaped"):
                sc.fetch_sold_comps("t", "k", breaker=breaker)
        assert breaker.tripped is True
        assert list(tmp_path.glob("*.json")) == []

    def test_interim_429_does_not_count_toward_breaker(self, tmp_path, monkeypatch):
        """Terminal-failures-only accounting: a 429 that recovers on retry is
        a rate-limit blip, not outage evidence — with threshold=1, ANY
        counted error would trip."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)
        calls = {"n": 0}

        def fake_get(url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                bad = MagicMock()
                bad.status_code = 429
                bad.raise_for_status = MagicMock(
                    side_effect=requests.HTTPError(response=bad))
                return bad
            return _sold_comps_good([_sc_item()])

        breaker = sc._CircuitBreaker(threshold=1, provider_name="sold-comps.com")
        attempts = []
        with patch("sold_comps.requests.get", side_effect=fake_get):
            data, hit = sc.fetch_sold_comps(
                "t", "k", breaker=breaker,
                record_attempt=lambda o, d: attempts.append(o))
        assert hit is False
        assert breaker.tripped is False
        # ...but the superseded 429 attempt IS still in the trail (BUI-537).
        assert len(attempts) == 1 and attempts[0].startswith("error:")

    def test_terminal_failure_counts_toward_breaker(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        bad = MagicMock()
        bad.status_code = 403  # quota exhausted — non-retryable
        bad.raise_for_status = MagicMock(
            side_effect=requests.HTTPError("quota", response=bad))
        breaker = sc._CircuitBreaker(threshold=1, provider_name="sold-comps.com")
        with patch("sold_comps.requests.get", return_value=bad):
            with pytest.raises(requests.HTTPError):
                sc.fetch_sold_comps("t", "k", breaker=breaker)
        assert breaker.tripped is True

    def test_tripped_breaker_serves_cache_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        with patch("sold_comps.requests.get",
                   return_value=_sold_comps_good([_sc_item()])):
            sc.fetch_sold_comps("t", "k")  # seed cache
        breaker = sc._CircuitBreaker(threshold=1, provider_name="sold-comps.com")
        breaker.record_error()
        assert breaker.tripped is True
        with patch("sold_comps.requests.get") as m:
            data, hit = sc.fetch_sold_comps("t", "k", breaker=breaker)
            assert hit is True
            with pytest.raises(sc.BreakerTrippedError):
                sc.fetch_sold_comps("uncached", "k", breaker=breaker)
        assert m.call_count == 0, "no live call past a tripped breaker"


class TestProviderFallback:
    """End-to-end failover at the fetch_book_comps level, through
    _fetch_with_fallback, with requests.get routed per endpoint."""

    def test_default_order_serves_sold_comps_directly(self, tmp_path, monkeypatch):
        """Default order is sold-comps.com FIRST (SerpApi's sold engine is
        login-walled indefinitely): a healthy primary serves with ZERO
        SerpApi calls — the router asserts none happen."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        with patch("sold_comps.requests.get",
                   side_effect=_route(sold_comps=lambda: _sold_comps_good([_sc_item()]))):
            out = sc.fetch_book_comps({"title": "ASM", "issue": "142"}, "key",
                                      sold_comps_key="sc_k")
        assert len(out["comps"]) == 1
        comp = out["comps"][0]
        assert comp["product_id"] == "111"
        assert comp["sold_date"] == "2026-07-25"
        entries = out["queries_used"]
        assert len(entries) == 1, entries
        assert entries[0]["provider"] == "sold-comps.com"
        assert entries[0]["outcome"] == "live"
        assert "error" not in entries[0]

    def test_sold_comps_failure_falls_back_to_serpapi(self, tmp_path, monkeypatch):
        """Reverse failover under the default order: a sold-comps.com failure
        falls through to SerpApi, with both attempts provider-tagged."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)

        def sold_comps_403():
            bad = MagicMock()
            bad.status_code = 403
            bad.raise_for_status = MagicMock(
                side_effect=requests.HTTPError("quota", response=bad))
            return bad

        with patch("sold_comps.requests.get",
                   side_effect=_route(serpapi=lambda: _serpapi_good([{
                       "product_id": "555",
                       "title": "ASM #142 FN Marvel",
                       "price": {"extracted": 20.0},
                   }]), sold_comps=sold_comps_403)):
            out = sc.fetch_book_comps({"title": "ASM", "issue": "142"}, "key",
                                      sold_comps_key="sc_k")
        assert [c["product_id"] for c in out["comps"]] == ["555"]
        entries = out["queries_used"]
        assert len(entries) == 2, entries
        assert entries[0]["provider"] == "sold-comps.com"
        assert "error" in entries[0]
        assert entries[1]["provider"] == "serpapi"
        assert entries[1]["outcome"] == "live"
        assert "error" not in entries[1]

    def test_explicit_serpapi_first_order_fails_over(self, tmp_path, monkeypatch):
        """The pre-flip order still works when pinned explicitly (the
        EBAY_SOLD_COMPS_PROVIDERS revert path if eBay drops the wall):
        SerpApi errors → sold-comps.com serves, trail tagged in order."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        with patch("sold_comps.requests.get",
                   side_effect=_route(serpapi=_serpapi_down,
                                      sold_comps=lambda: _sold_comps_good([_sc_item()]))):
            out = sc.fetch_book_comps({"title": "ASM", "issue": "142"}, "key",
                                      sold_comps_key="sc_k",
                                      providers=("serpapi", "sold-comps.com"))
        assert len(out["comps"]) == 1
        entries = out["queries_used"]
        assert len(entries) == 2, entries
        assert entries[0]["provider"] == "serpapi"
        assert "error" in entries[0]
        assert entries[1]["provider"] == "sold-comps.com"
        assert entries[1]["outcome"] == "live"

    def test_both_fail_still_classifies_fetch_err(self, tmp_path, monkeypatch):
        """BUI-545 AC: a batch where BOTH providers fail must keep the
        BUI-536 fetch-err signal — zero comps AND every queries_used entry
        carrying an 'error' key."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc.time, "sleep", lambda *_: None)

        def sold_comps_403():
            bad = MagicMock()
            bad.status_code = 403
            bad.raise_for_status = MagicMock(
                side_effect=requests.HTTPError("quota", response=bad))
            return bad

        with patch("sold_comps.requests.get",
                   side_effect=_route(serpapi=_serpapi_down,
                                      sold_comps=sold_comps_403)):
            out = sc.fetch_book_comps({"title": "ASM", "issue": "142"}, "key",
                                      sold_comps_key="sc_k")
        assert out["comps"] == []
        assert out["queries_used"], "trail must not be empty"
        assert all("error" in q for q in out["queries_used"])
        assert ({q["provider"] for q in out["queries_used"]}
                == {"serpapi", "sold-comps.com"})

    def test_no_key_means_no_failover(self, tmp_path, monkeypatch):
        """Absent SOLD_COMPS_KEY → byte-for-byte pre-BUI-545 behavior: the
        SerpApi failure surfaces as the only trail entry and the router
        asserts no sold-comps.com call was ever attempted."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        with patch("sold_comps.requests.get",
                   side_effect=_route(serpapi=_serpapi_down)):
            out = sc.fetch_book_comps({"title": "ASM", "issue": "142"}, "key")
        assert out["comps"] == []
        assert all("error" in q for q in out["queries_used"])
        assert all(q["provider"] == "serpapi" for q in out["queries_used"])

    def test_serpapi_breaker_tripped_goes_straight_to_secondary(self, tmp_path, monkeypatch):
        """A tripped SerpApi breaker must not dead-end the query (the old
        BreakerTrippedError path) — it should fail over with zero SerpApi
        HTTP calls, and the output must still flag breaker_tripped."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        breaker = sc._CircuitBreaker(threshold=1)
        breaker.record_error()
        assert breaker.tripped is True
        with patch("sold_comps.requests.get",
                   side_effect=_route(sold_comps=lambda: _sold_comps_good([_sc_item()]))):
            out = sc.fetch_book_comps({"title": "ASM", "issue": "142"}, "key",
                                      breaker=breaker, sold_comps_key="sc_k",
                                      providers=("serpapi", "sold-comps.com"))
        assert len(out["comps"]) == 1
        assert out["breaker_tripped"] is True
        assert "BreakerTrippedError" in out["queries_used"][0]["outcome"]
        assert out["queries_used"][0]["provider"] == "serpapi"
        assert out["queries_used"][1]["provider"] == "sold-comps.com"

    def test_genuine_zero_results_does_not_failover(self, tmp_path, monkeypatch):
        """BUI-536's error-vs-empty distinction survives the failover: a
        200 with zero items from the primary is a genuine n=0, never a
        second-provider probe (the router asserts no SerpApi call — and the
        trail must show a real success entry, not a swallowed error)."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        with patch("sold_comps.requests.get",
                   side_effect=_route(sold_comps=lambda: _sold_comps_good([]))):
            out = sc.fetch_book_comps({"title": "Obscurity", "issue": "1"}, "key",
                                      sold_comps_key="sc_k")
        assert "error" not in out, out.get("error")
        assert out["comps"] == []
        assert len(out["queries_used"]) == 1
        entry = out["queries_used"][0]
        assert entry["provider"] == "sold-comps.com"
        assert entry["outcome"] == "hit" or entry["outcome"] == "live"
        assert "error" not in entry

    def test_provider_order_override_skips_serpapi(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        with patch("sold_comps.requests.get",
                   side_effect=_route(sold_comps=lambda: _sold_comps_good([_sc_item()]))):
            out = sc.fetch_book_comps({"title": "ASM", "issue": "142"}, "key",
                                      sold_comps_key="sc_k",
                                      providers=("sold-comps.com",))
        assert len(out["comps"]) == 1
        assert all(q["provider"] == "sold-comps.com" for q in out["queries_used"])

    def test_cross_provider_dedupe_shares_item_id_namespace(self, tmp_path, monkeypatch):
        """SerpApi product_id and sold-comps.com itemId are both the eBay
        /itm/ number: a comp served by SerpApi in tier 1 must dedupe the same
        item served by sold-comps.com in a later tier."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        serpapi_calls = {"n": 0}

        def serpapi_then_down():
            serpapi_calls["n"] += 1
            if serpapi_calls["n"] == 1:
                return _serpapi_good([{
                    "product_id": "111",
                    "title": "ASM #142 FN Marvel",
                    "price": {"extracted": 20.0},
                }, {
                    "product_id": "999",
                    "title": "ASM #142 VG Marvel",
                    "price": {"extracted": 8.0},
                }])
            return _serpapi_down()

        with patch("sold_comps.requests.get",
                   side_effect=_route(serpapi=serpapi_then_down,
                                      sold_comps=lambda: _sold_comps_good([_sc_item()]))):
            # year present → tier 2 (broaden) fires because tier 1 found <5;
            # SerpApi pinned first so tier 1 = SerpApi live, tier 2 = SerpApi
            # down → sold-comps.com serves the overlapping item.
            out = sc.fetch_book_comps(
                {"title": "ASM", "issue": "142", "year": 1975}, "key",
                sold_comps_key="sc_k",
                providers=("serpapi", "sold-comps.com"))
        ids = [c["product_id"] for c in out["comps"]]
        assert ids.count("111") == 1, "same eBay item must not appear twice"
        assert "999" in ids

    def test_run_batch_threads_secondary_config(self, tmp_path, monkeypatch):
        """run_batch resolves the key + default order itself — and under the
        sold-comps-first default a healthy batch spends ZERO SerpApi calls
        (the router asserts none happen)."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(sc, "load_sold_comps_key", lambda: "sc_k")
        with patch("sold_comps.requests.get",
                   side_effect=_route(sold_comps=lambda: _sold_comps_good([_sc_item()]))):
            results = sc.run_batch([{"title": "ASM", "issue": "142"}], "key",
                                   max_workers=1)
        assert len(results[0]["comps"]) == 1
        assert results[0]["queries_used"][-1]["provider"] == "sold-comps.com"

    def test_run_batch_without_key_warns_once(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        with patch("sold_comps.requests.get",
                   side_effect=_route(serpapi=lambda: _serpapi_good([]))):
            sc.run_batch([{"title": "X", "issue": "1"}], "key", max_workers=1)
        err = capsys.readouterr().err
        assert err.count("SOLD_COMPS_KEY not set") == 1


# ─── BUI-581: masthead-rename probe ───────────────────────────────────────────

class TestMastheadAlias:
    """`_alias_masthead_title` is a pure NAME substitution — it must never imply
    a claim about which issues carried which masthead (BUI-581)."""

    def test_modern_masthead_maps_to_the_original(self):
        assert sc._alias_masthead_title("Uncanny X-Men") == "X-Men"

    def test_original_masthead_maps_to_the_modern_one(self):
        assert sc._alias_masthead_title("X-Men") == "Uncanny X-Men"

    def test_specific_name_wins_over_the_bare_one_it_contains(self):
        """Pair order is load-bearing: matched against the bare "x-men" first,
        "Uncanny X-Men" would rewrite to "Uncanny Uncanny X-Men"."""
        assert sc._alias_masthead_title("uncanny x-men") == "X-Men"

    def test_leading_article_is_stripped_before_matching(self):
        assert sc._alias_masthead_title("The Uncanny X-Men") == "X-Men"

    def test_trailing_issue_text_is_preserved(self):
        assert sc._alias_masthead_title("Uncanny X-Men #69") == "X-Men #69"

    def test_title_that_merely_contains_the_masthead_is_untouched(self):
        """Anchored at the start: swapping mid-title produces a name no listing
        ever carried ("Giant-Size Uncanny X-Men"), so it must not fire."""
        assert sc._alias_masthead_title("Giant-Size X-Men") is None
        assert sc._alias_masthead_title("Wolverine and the X-Men") is None

    def test_masthead_must_be_a_whole_token(self):
        assert sc._alias_masthead_title("X-Menace") is None

    def test_unrelated_title_has_no_counterpart(self):
        assert sc._alias_masthead_title("Amazing Spider-Man") is None
        assert sc._alias_masthead_title("") is None

    def test_rename_pairs_are_not_synced_with_the_reboot_list(self):
        """The two tables answer different questions (substitution vs
        exclusion). Pin that they're independent, so a future edit to
        `_REBOOTABLE_MASTHEADS` (which locg-cli mirrors, BUI-577) doesn't get
        "helpfully" propagated here."""
        rename_names = {name for name, _ in sc._MASTHEAD_RENAME_PAIRS}
        assert rename_names != set(sc._REBOOTABLE_MASTHEADS)


class TestAltMastheadTier:
    def _comp(self, pid, title="X-Men #69 FN 6.0 Marvel 1970", price=15.0):
        return {
            "product_id": pid,
            "title": title,
            "price": {"extracted": price},
            "sold_date": "",
            "buying_format": "auction",
        }

    def _wire(self, tmp_path, monkeypatch, results_per_query):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        calls = []

        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0, page=1,
                       record_attempt=None, breaker=None):
            calls.append(nkw)
            idx = len(calls) - 1
            results = results_per_query[idx] if idx < len(results_per_query) else []
            return ({
                "organic_results": results,
                "search_metadata": {"ebay_url": "ok&LH_Sold=1"},
            }, False)

        monkeypatch.setattr(sc, "fetch", fake_fetch)
        return calls

    def test_deeper_alias_pool_replaces_a_collapsed_one(self, tmp_path, monkeypatch):
        """BUI-581's live case: Uncanny X-Men #69 returns n=0 under the modern
        masthead and a real pool under the original one."""
        results = [
            [],                                             # base (Uncanny): 0
            [],                                             # broader: still 0
            [self._comp(str(i)) for i in range(6)],         # alt-masthead: 6
        ]
        calls = self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps(
            {"title": "Uncanny X-Men", "issue": "69", "year": 1970, "grade": 5.0},
            "key",
        )
        assert out["masthead_swapped_to"] == "X-Men"
        assert len(out["comps"]) == 6
        assert '"X-Men 69"' in calls[2]
        # The echoed input still reports what the CALLER asked for.
        assert out["input"]["title"] == "Uncanny X-Men"
        assert "alt-masthead" in {q["tier"] for q in out["queries_used"]}

    def test_shallower_alias_pool_is_discarded(self, tmp_path, monkeypatch):
        """Fail-safe direction: the probe never costs comps. A thin-but-real
        primary pool survives a worse alias probe untouched."""
        results = [
            [self._comp("p1"), self._comp("p2")],   # base under "X-Men": 2
            [],                                     # broader: nothing new
            [self._comp("a1")],                     # alt-masthead: only 1
        ]
        self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps(
            {"title": "X-Men", "issue": "69", "year": 1970}, "key",
        )
        assert out["masthead_swapped_to"] is None
        assert {c["product_id"] for c in out["comps"]} == {"p1", "p2"}

    def test_equal_depth_keeps_the_callers_masthead(self, tmp_path, monkeypatch):
        """Strictly-greater, not >=: a tie is no evidence, so don't rewrite the
        book's identity on one."""
        results = [
            [self._comp("p1")],   # base: 1
            [],                   # broader
            [self._comp("a1")],   # alt-masthead: also 1
        ]
        self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps(
            {"title": "X-Men", "issue": "69", "year": 1970}, "key",
        )
        assert out["masthead_swapped_to"] is None
        assert {c["product_id"] for c in out["comps"]} == {"p1"}

    def test_pools_are_never_merged(self, tmp_path, monkeypatch):
        """The winning pool REPLACES the loser — blending the two mastheads
        would quietly mix in same-numbered issues of the other volume."""
        results = [
            [self._comp("p1")],                                      # base: 1
            [],                                                      # broader
            [self._comp("a1"), self._comp("a2"), self._comp("a3")],  # alt: 3
        ]
        self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps(
            {"title": "Uncanny X-Men", "issue": "69", "year": 1970}, "key",
        )
        assert {c["product_id"] for c in out["comps"]} == {"a1", "a2", "a3"}

    def test_probe_pool_is_not_dedup_suppressed_by_the_primary(
            self, tmp_path, monkeypatch):
        """The two pools overlap heavily by construction (an eBay phrase match
        on the shorter masthead also matches the longer one). If the probe were
        deduped against the primary it would score ~0 and always lose."""
        shared = [self._comp("s1"), self._comp("s2")]
        results = [
            shared,                                          # base: 2 (thin)
            [],                                              # broader
            shared + [self._comp("s3"), self._comp("s4")],   # alt: same 2 + 2
        ]
        self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps(
            {"title": "Uncanny X-Men", "issue": "69", "year": 1970}, "key",
        )
        assert out["masthead_swapped_to"] == "X-Men"
        assert {c["product_id"] for c in out["comps"]} == {"s1", "s2", "s3", "s4"}

    def test_later_tiers_query_the_winning_masthead(self, tmp_path, monkeypatch):
        """The point of probing before tiers 3/4 is that the rest of the ladder
        stops spending queries on the dead name."""
        results = [
            [],                       # base (Uncanny)
            [],                       # broader
            [self._comp("a1")],       # alt-masthead → wins
        ]
        calls = self._wire(tmp_path, monkeypatch, results)
        sc.fetch_book_comps(
            {"title": "Uncanny X-Men", "issue": "69", "year": 1970, "grade": 5.0},
            "key",
        )
        assert len(calls) > 3
        for nkw in calls[3:]:
            assert '"X-Men 69"' in nkw
            assert "Uncanny" not in nkw

    def test_healthy_pool_never_probes(self, tmp_path, monkeypatch):
        results = [[self._comp(str(i)) for i in range(12)]]
        calls = self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps(
            {"title": "Uncanny X-Men", "issue": "142", "year": 1981}, "key",
        )
        assert len(calls) == 1
        assert out["masthead_swapped_to"] is None

    def test_modern_book_never_probes(self, tmp_path, monkeypatch):
        """Post-cutoff, "X-Men #1" and "Uncanny X-Men #1" are DIFFERENT books —
        swapping there would price the wrong comic."""
        calls = self._wire(tmp_path, monkeypatch, [[], []])
        out = sc.fetch_book_comps(
            {"title": "Uncanny X-Men", "issue": "1", "year": 2019}, "key",
        )
        assert "alt-masthead" not in {q["tier"] for q in out["queries_used"]}
        assert out["masthead_swapped_to"] is None
        for nkw in calls:
            assert "Uncanny" in nkw

    def test_year_less_book_never_probes(self, tmp_path, monkeypatch):
        """Without a year there is no way to tell the vintage run from the
        modern relaunch, so the probe stays off rather than guessing."""
        calls = self._wire(tmp_path, monkeypatch, [[]])
        out = sc.fetch_book_comps({"title": "Uncanny X-Men", "issue": "1"}, "key")
        assert out["masthead_swapped_to"] is None
        for nkw in calls:
            assert "Uncanny" in nkw

    def test_string_year_still_probes(self, tmp_path, monkeypatch):
        """/comic:identify emits `year` as a string (BUI-565); the gate must
        read it as vintage rather than silently disabling this tier."""
        results = [[], [], [self._comp("a1"), self._comp("a2")]]
        self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps(
            {"title": "Uncanny X-Men", "issue": "69", "year": "1970"}, "key",
        )
        assert out["masthead_swapped_to"] == "X-Men"

    def test_title_without_a_counterpart_never_probes(self, tmp_path, monkeypatch):
        calls = self._wire(tmp_path, monkeypatch, [[], []])
        out = sc.fetch_book_comps(
            {"title": "Amazing Spider-Man", "issue": "142", "year": 1975}, "key",
        )
        assert "alt-masthead" not in {q["tier"] for q in out["queries_used"]}
        assert out["masthead_swapped_to"] is None
        assert calls  # sanity: the ladder did run

    def test_probe_query_carries_the_year(self, tmp_path, monkeypatch):
        """MONEY GUARD. Both names in a rename pair are also rebootable
        mastheads, so an unyeared probe could win the depth comparison on the
        OTHER volume's same-numbered issue — X-Men Vol.2 #1 (1991, a common $10
        book) priced off Uncanny X-Men #1 (1963) comps is a four-figure
        over-bid. The year is what keeps the two volumes apart, so the probe
        must never broaden the way tier 2 does."""
        results = [[], [], [self._comp("a1")]]
        calls = self._wire(tmp_path, monkeypatch, results)
        sc.fetch_book_comps(
            {"title": "X-Men", "issue": "1", "year": 1991}, "key",
        )
        alt_nkw = calls[2]
        assert '"Uncanny X-Men 1"' in alt_nkw
        assert "1991" in alt_nkw

    def test_probe_query_keeps_the_vintage_exclusion_terms(self, tmp_path, monkeypatch):
        """The alias is itself a rebootable masthead, so the BUI-347 hardening
        has to survive the substitution — otherwise the probe could win on a
        pool of modern variant/facsimile printings."""
        results = [[], [], [self._comp("a1")]]
        calls = self._wire(tmp_path, monkeypatch, results)
        sc.fetch_book_comps(
            {"title": "Uncanny X-Men", "issue": "69", "year": 1970}, "key",
        )
        for term in sc._VINTAGE_EXCLUSION_TERMS:
            assert term in calls[2]


# ─── BUI-588: variant-drop retry ──────────────────────────────────────────────

class TestVariantDropRetry:
    def _comp(self, pid, title="Uncanny X-Men #281 NM 9.2 Marvel", price=20.0):
        return {
            "product_id": pid,
            "title": title,
            "price": {"extracted": price},
            "sold_date": "",
            "buying_format": "auction",
        }

    def _wire(self, tmp_path, monkeypatch, results_per_query):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        calls = []

        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0, page=1,
                       record_attempt=None, breaker=None):
            calls.append(nkw)
            idx = len(calls) - 1
            results = results_per_query[idx] if idx < len(results_per_query) else []
            return ({
                "organic_results": results,
                "search_metadata": {"ebay_url": "ok&LH_Sold=1"},
            }, False)

        monkeypatch.setattr(sc, "fetch", fake_fetch)
        return calls

    def test_dead_descriptor_is_dropped_and_reported(self, tmp_path, monkeypatch):
        """BUI-588's confirmed case: "White Logo 1st Print" is a LOCG label no
        seller types, so it zeroes 37 comps to 0 at every tier."""
        results = [
            [], [], [], [],       # base / broader / alt-masthead / inclusive
            [self._comp(str(i)) for i in range(4)],    # no-variant retry
        ]
        calls = self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps(
            {"title": "Uncanny X-Men", "issue": "281", "year": 1991,
             "variant": "White Logo 1st Print"},
            "key",
        )
        assert out["variant_dropped"] == "White Logo 1st Print"
        assert len(out["comps"]) == 4
        assert "White Logo 1st Print" in calls[0]
        assert "White Logo" not in calls[-1]
        assert "no-variant" in {q["tier"] for q in out["queries_used"]}

    def test_thin_but_nonempty_variant_pool_is_left_alone(self, tmp_path, monkeypatch):
        """A valid variant term costs real depth even when it works (Newsstand:
        32 comps → 13). Chasing depth by dropping it would trade
        identity-correctness for apparent confidence, so the retry fires only on
        an EXACTLY empty pool."""
        results = [[self._comp("v1")], [], []]
        calls = self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps(
            {"title": "Daredevil", "issue": "168", "year": 1981,
             "variant": "Newsstand"},
            "key",
        )
        assert out["variant_dropped"] is None
        assert {c["product_id"] for c in out["comps"]} == {"v1"}
        for nkw in calls:
            assert "Newsstand" in nkw

    def test_genuine_no_comps_book_is_not_blamed_on_its_variant(
            self, tmp_path, monkeypatch):
        """The retry also came back empty ⇒ the variant was not the cause. This
        is a real illiquid book and must not be flagged as a variant problem."""
        self._wire(tmp_path, monkeypatch, [])
        out = sc.fetch_book_comps(
            {"title": "Daredevil", "issue": "168", "year": 1981,
             "variant": "Newsstand"},
            "key",
        )
        assert out["comps"] == []
        assert out["variant_dropped"] is None

    def test_no_variant_book_never_retries(self, tmp_path, monkeypatch):
        calls = self._wire(tmp_path, monkeypatch, [])
        out = sc.fetch_book_comps(
            {"title": "Daredevil", "issue": "168", "year": 1981}, "key",
        )
        assert out["variant_dropped"] is None
        assert "no-variant" not in {q["tier"] for q in out["queries_used"]}
        assert calls  # sanity: the ladder did run

    def test_retry_composes_with_the_masthead_probe(self, tmp_path, monkeypatch):
        """BUI-581 and BUI-588 compose in the only way they can: a masthead
        probe that WINS leaves a non-empty pool, so the variant retry is
        reached exactly when the probe found nothing either. It then re-queries
        the masthead still in force, minus the variant."""
        results = [
            [], [],                # base + broader (Uncanny + variant)
            [],                    # alt-masthead (X-Men + variant): also 0
            [],                    # inclusive
            [self._comp("a1")],    # no-variant retry
        ]
        calls = self._wire(tmp_path, monkeypatch, results)
        out = sc.fetch_book_comps(
            {"title": "Uncanny X-Men", "issue": "69", "year": 1970,
             "variant": "White Logo 1st Print"},
            "key",
        )
        assert out["masthead_swapped_to"] is None   # probe tied at 0, no swap
        assert out["variant_dropped"] == "White Logo 1st Print"
        assert '"Uncanny X-Men 69"' in calls[-1]
        assert "White Logo" not in calls[-1]

    def test_both_signals_present_on_the_error_return(self, tmp_path, monkeypatch):
        """Shape parity — a caller reading `.get("variant_dropped")` must see the
        same keys on the BUI-537 partial-trail return."""
        self._wire(tmp_path, monkeypatch, [])
        out = sc.fetch_book_comps({"title": "X", "issue": "1", "year": "n/a"}, "key")
        assert "error" in out
        assert out["variant_dropped"] is None
        assert out["masthead_swapped_to"] is None
