"""Tests for sold_comps.py — eBay sold-listings via SerpApi.

Pure-function tests only. Network calls are exercised through fetch() with
a mocked requests.get; no real SerpApi calls happen in CI.
"""

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import requests

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
    ])
    def test_keeps(self, title):
        assert not sc.hard_exclude(title)


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

    def test_masthead_gate_sees_the_bui_346_normalized_title(self):
        # BUI-346 + BUI-347 interaction: the un-normalized "The Amazing
        # Spider-Man #50" must still trip the vintage gate — the masthead
        # check runs on the title AFTER the leading-article/embedded-issue
        # strip, not the raw input.
        q = sc.build_query("The Amazing Spider-Man #50", "50", year=1967)
        assert "-variant" in q
        assert q == sc.build_query("Amazing Spider-Man", "50", year=1967)

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


# ─── Tiered query strategy ────────────────────────────────────────────────────

class TestTieredStrategy:
    def _wire(self, tmp_path, monkeypatch, results_per_query):
        """Make fetch() return a different result list per call. Calls past
        the end of the fixture list return an empty result (so tests don't
        need to pad for tiers that may or may not fire)."""
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        calls = []

        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0):
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


# ─── End-to-end-ish: batch driver ────────────────────────────────────────────

class TestBatch:
    def test_runs_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0):
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
        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0):
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

        def fake_fetch(nkw, api_key, *, force=False, ttl_sec=0):
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


