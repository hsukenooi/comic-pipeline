"""Tests for grade_tokens.py — the shared grade/certification token module
(BUI-923) that ebay_fetch.py (identify) and sold_comps.py (FMV comp parsing)
both import from.
"""

import pytest

import grade_tokens as gt


# ─── Moved tables (verbatim relocation from sold_comps.py) ─────────────────
# sold_comps.py's own TestParseGrade suite is the authority on parse_grade's
# behavior end to end (unchanged by the move); these are a light sanity check
# that the relocated objects themselves still work and still carry the
# BUI-183 price/measurement-context guards.

class TestNumericGradeRe:
    @pytest.mark.parametrize("title,expected", [
        ("Hulk #181 9.8 OW pages", "9.8"),
        ("ASM #142 5.5 OW pages", "5.5"),
    ])
    def test_matches_bare_numeric_grade(self, title, expected):
        m = gt._NUMERIC_GRADE_RE.search(title)
        assert m is not None
        assert m.group(1) == expected

    @pytest.mark.parametrize("title", [
        "$9.5 shipping",
        "price $9.5",
        "lot 3.5 inches",
        "ships 9.5 oz",
        "ruler 2.5 x 3.5",
        "comic 5.50 dollars",
    ])
    def test_price_measurement_context_excluded(self, title):
        """BUI-183 lookarounds, still in force after the BUI-923 relocation."""
        assert gt._NUMERIC_GRADE_RE.search(title) is None


class TestLetterPatterns:
    def test_nm_minus(self):
        assert any(p.search("Uncanny X-Men #185 NM-") for p, _ in gt._LETTER_PATTERNS)

    def test_table_still_ordered_slash_combo_before_component(self):
        # VF/NM must resolve via the Tier-1 slash-combo entry (9.0), not the
        # bare "NM" component (9.4) — same ordering sold_comps.parse_grade
        # depends on; a naive re-sort of the moved list would break it.
        for pattern, value in gt._LETTER_PATTERNS:
            if pattern.search("VF/NM bright cover"):
                assert value == 9.0
                return
        pytest.fail("no pattern matched VF/NM")


# ─── Certifier resolution ───────────────────────────────────────────────────

class TestResolveCertifierFromSpecificsValue:
    @pytest.mark.parametrize("value,expected", [
        ("Certified Guaranty Company (CGC)", "cgc"),
        ("CGC", "cgc"),
        ("CBCS", "cbcs"),
        ("PGX", "other"),
        ("Some Other Grading Co", "other"),  # BUI-923: unrecognized but non-blank -> other
    ])
    def test_recognized_values(self, value, expected):
        assert gt.resolve_certifier_from_specifics_value(value) == expected

    @pytest.mark.parametrize("value", [
        "Uncertified", "uncertified", "None", "Not Graded", "not graded", "Raw", "", None, "  ",
    ])
    def test_absent_sentinels(self, value):
        """'Uncertified'/'None'/'Not Graded'/'Raw'/blank all count as absent —
        raw listings routinely carry them (BUI-923 outcome text)."""
        assert gt.resolve_certifier_from_specifics_value(value) is None


class TestResolveCertifierToken:
    def test_finds_cgc(self):
        assert gt.resolve_certifier_token("2003 CGC 9.4") == "cgc"

    def test_no_match(self):
        assert gt.resolve_certifier_token("Amazing Spider-Man #300 NM") is None

    def test_empty(self):
        assert gt.resolve_certifier_token("") is None
        assert gt.resolve_certifier_token(None) is None


# ─── Label tokens ────────────────────────────────────────────────────────────

class TestResolveLabel:
    @pytest.mark.parametrize("text,expected", [
        ("CGC 9.8 SS", "signature_series"),
        ("CGC 9.8 Signature Series", "signature_series"),
        ("CGC 9.8 Signed Todd McFarlane", "signature_series"),
        ("CGC 9.8 Autograph", "signature_series"),
        ("CGC 9.8 Signature", "signature_series"),
        ("CGC 9.8 Qualified", "qualified"),
        ("CGC 9.8 (Q)", "qualified"),
        ("CGC 9.8 Restored", "restored"),
        ("CGC 9.8 (R)", "restored"),
        ("CGC 9.8 Conserved", "conserved"),
    ])
    def test_tokens(self, text, expected):
        assert gt.resolve_label(text) == expected

    def test_not_signed_does_not_match(self):
        """BUI-668's `(?<!not\\s)` guard, reused here — 'NOT signed' must not
        read as Signature Series."""
        assert gt.resolve_label("CGC 9.8 not signed") is None

    def test_no_label(self):
        assert gt.resolve_label("Invincible #1 2003 CGC 9.4") is None

    def test_blank(self):
        assert gt.resolve_label("") is None
        assert gt.resolve_label(None) is None


# ─── Page-quality tokens ─────────────────────────────────────────────────────

class TestResolvePageQuality:
    @pytest.mark.parametrize("text,expected", [
        ("CGC 9.4 W", "white"),
        ("CGC 9.4 WHITE PAGES", "white"),
        ("CGC 4.5 OW/W", "ow_w"),
        ("CGC 4.5 OWW", "ow_w"),
        ("CGC 4.5 OW-W", "ow_w"),
        ("CGC 9.4 OW", "ow"),
        ("CGC 9.4 C/OW", "c_ow"),
        ("CGC 9.4 CR/OW", "c_ow"),
        ("CGC 9.4 CREAM", "cream"),
    ])
    def test_tokens(self, text, expected):
        assert gt.resolve_page_quality(text) == expected

    def test_compound_forms_not_read_as_bare_ow(self):
        """OW/W and C/OW both contain a `\\bOW\\b`-matchable substring — the
        compound patterns must win by being checked first."""
        assert gt.resolve_page_quality("OW/W") == "ow_w"
        assert gt.resolve_page_quality("C/OW") == "c_ow"

    def test_no_page_quality(self):
        assert gt.resolve_page_quality("Invincible #1 2003 CGC 9.4") is None


# ─── Title certifier+grade adjacency ────────────────────────────────────────

class TestExtractTitleCertification:
    def test_cgc_aa_ss_grade(self):
        certifier, grade, bare = gt.extract_title_certification(
            "Amazing Spider-Man #50 CGC AA SS 4.5 OWW 1967"
        )
        assert (certifier, grade, bare) == ("cgc", 4.5, False)

    def test_cgc_directly_adjacent(self):
        certifier, grade, bare = gt.extract_title_certification("Invincible #1 First Print 2003 CGC 9.4")
        assert (certifier, grade, bare) == ("cgc", 9.4, False)

    def test_cbcs(self):
        assert gt.extract_title_certification("CBCS 9.8") == ("cbcs", 9.8, False)

    def test_pgx_maps_to_other(self):
        """Regression: PGX ends in 'X', and naively reusing
        `_NUMERIC_GRADE_RE`'s `(?<!X )` dimension-pair lookbehind made 'PGX
        9.8' false-reject (the guard read 'X ' immediately before the grade
        as a bare-dimension 'x', not the tail of the certifier name)."""
        assert gt.extract_title_certification("PGX 9.8") == ("other", 9.8, False)

    def test_bare_mention_no_adjacent_grade(self):
        """'CGC ready, would grade 9.6' — a certifier name is present but not
        next to a grade; the ticket's own outcome text calls this case raw
        with a missing grade, not a self-reported 9.6."""
        assert gt.extract_title_certification("CGC ready, would grade 9.6") == (None, None, True)

    def test_bare_mention_cgc_it(self):
        assert gt.extract_title_certification("CGC it") == (None, None, True)

    def test_no_certifier_mention(self):
        assert gt.extract_title_certification("Amazing Spider-Man #300 NM-") == (None, None, False)

    def test_price_adjacent_to_certifier_is_not_a_grade(self):
        """Mirrors `_NUMERIC_GRADE_RE`'s own price-context test: a dollar
        amount right after the certifier name must not read as its grade."""
        certifier, grade, bare = gt.extract_title_certification("CGC $9.80 shipping included")
        assert certifier is None
        assert grade is None

    def test_blank_title(self):
        assert gt.extract_title_certification("") == (None, None, False)
        assert gt.extract_title_certification(None) == (None, None, False)
