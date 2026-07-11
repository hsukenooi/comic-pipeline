"""Tests for comic_identity's BUI-253 Step 2 additions: identify_comic() and
score_against_wish(). Step 1 (the moved helpers) is exercised indirectly by
the full test_seller_scan.py / test_wishlist_sellers.py suites, which import
those symbols through seller_scan's re-exports — this file covers only the
genuinely new title→identity extraction logic.
"""

import comic_identity as ci
import seller_scan


# ─── Clean single-issue extraction ────────────────────────────────────────


class TestIdentifyComicCleanSingleIssue:
    def test_basic_hash_issue(self):
        ident = ci.identify_comic("Amazing Spider-Man #300 NM Marvel 1988")
        assert ident.series == "Amazing Spider-Man"
        assert ident.issue == "300"
        assert ident.year == 1988
        assert ident.volume is None
        assert ident.edition == "single-issue"
        assert ident.is_lot is False
        assert ident.constituent_issues == []
        assert ident.reject_reasons == []
        assert ident.confidence == 1.0

    def test_issue_with_trailing_letter_suffix(self):
        ident = ci.identify_comic("Batman #1A Variant Cover")
        assert ident.issue == "1A"

    def test_decimal_point_issue_captured_in_full(self):
        """PR-review nit (N1): a real Marvel point-issue numbering convention
        ("#700.1") must not be silently truncated to "700"."""
        ident = ci.identify_comic("Amazing Spider-Man #700.1 Marvel 2011")
        assert ident.issue == "700.1"
        assert ident.series == "Amazing Spider-Man"

    def test_half_issue_slash_still_reports_whole_number(self):
        """N1 was deliberately NOT extended to half-issue slashes — "Batman
        #1/2" must keep reporting issue="1" (the locked-in BUI-261 behavior:
        a half-issue scores against the whole-number wish item), not "1/2"."""
        ident = ci.identify_comic("Batman #1/2")
        assert ident.issue == "1"

    def test_case_preserved_in_series(self):
        ident = ci.identify_comic("fantastic four #48 vf")
        assert ident.series == "fantastic four"

    def test_title_field_is_unmodified_original(self):
        raw = "  Weird Spacing  #7  "
        ident = ci.identify_comic(raw)
        assert ident.title == raw


# ─── Bare/embedded-year handling ──────────────────────────────────────────


class TestIdentifyComicBareEmbeddedYear:
    def test_parenthesized_year_preferred_and_stripped_from_series(self):
        ident = ci.identify_comic("Amazing Spider-Man (2022) #7")
        assert ident.series == "Amazing Spider-Man"
        assert ident.year == 2022
        assert ident.confidence == 1.0

    def test_bare_trailing_year_extracted(self):
        ident = ci.identify_comic("Amazing Spider-Man #300 NM Marvel 1988")
        assert ident.year == 1988

    def test_no_year_present_is_none(self):
        ident = ci.identify_comic("Amazing Spider-Man #300 NM Marvel")
        assert ident.year is None

    def test_run_year_span_treated_as_ambiguous_but_still_confident(self):
        """"1962-1963" is a bare, non-parenthesized run-year span (BUI-243's
        own carve-out example) — two distinct year mentions lowers confidence
        to the 0.8 tier but does NOT prevent series/issue extraction."""
        ident = ci.identify_comic("Amazing Spider-Man #4 1962-1963 Silver Age Marvel")
        assert ident.series == "Amazing Spider-Man"
        assert ident.issue == "4"
        assert ident.year == 1962
        assert ident.confidence == 0.8

    def test_single_repeated_year_not_ambiguous(self):
        """The SAME year mentioned twice (e.g. paren + bare) is not ambiguous —
        only genuinely DIFFERENT year mentions lower confidence."""
        ident = ci.identify_comic("Amazing Spider-Man (1988) #300 NM Marvel 1988")
        assert ident.confidence == 1.0

    def test_year_never_misreads_a_grade_digit(self):
        """A raw grade like '7.0' must never leak into year (it isn't even a
        4-digit token, but this locks in that _strip_grades runs first)."""
        ident = ci.identify_comic("Moon Knight #15 Marvel 1982 VF/NM 9.0 White Pages")
        assert ident.year == 1982
        assert ident.issue == "15"

    def test_sku_digit_not_misread_as_year_or_issue(self):
        """BUI-261's SKU-token guard ("X6") applies here too: a letter-glued
        digit is never a plausible issue number, and it's not 4 digits so it
        can't be a year either."""
        ident = ci.identify_comic("Moon Knight X6 Marvel 1982")
        assert ident.issue is None
        assert ident.year == 1982
        assert "no issue number found" in ident.reject_reasons
        assert ident.confidence == 0.3


# ─── Volume extraction ─────────────────────────────────────────────────────


class TestIdentifyComicVolume:
    def test_explicit_volume_extracted(self):
        ident = ci.identify_comic("X-Men Volume 1 #94")
        assert ident.volume == 1
        assert ident.issue == "94"

    def test_no_volume_is_none(self):
        ident = ci.identify_comic("X-Men #94")
        assert ident.volume is None

    def test_bare_issue_fallback_does_not_confuse_volume_for_issue(self):
        """No '#' present — the bare-issue fallback must find '175', not the
        '3' from 'Vol 3', and the series text must not retain 'Vol 3' either."""
        ident = ci.identify_comic("Amazing Spider-Man Vol 3 175 NM")
        assert ident.series == "Amazing Spider-Man"
        assert ident.volume == 3
        assert ident.issue == "175"
        assert ident.confidence == 0.5


# ─── Annual-nests-in-parent vs Giant-Size/King-Size-own-series ────────────


class TestIdentifyComicAnnualVsGiantSize:
    """BUI-253's explicitly-called-out sharp edge: LOCG catalogs an Annual
    under its PARENT series' full_title (so "Annual" must NOT appear in the
    extracted series), but Giant-Size/King-Size are their OWN series_name
    (so that word MUST stay in the extracted series)."""

    def test_annual_stripped_from_series_nests_in_parent(self):
        ident = ci.identify_comic("The Amazing Spider-Man Annual #5 Marvel 1968")
        assert ident.series == "The Amazing Spider-Man"
        assert ident.issue == "5"
        assert ident.edition == "annual"
        assert ident.confidence == 1.0

    def test_giant_size_kept_in_series_own_series(self):
        ident = ci.identify_comic("Giant-Size X-Men #1 Marvel 1975")
        assert ident.series == "Giant-Size X-Men"
        assert ident.issue == "1"
        assert ident.edition == "giant-size"

    def test_giant_size_spaced_form_kept_in_series(self):
        ident = ci.identify_comic("Giant Size X-Men #1")
        assert "Giant Size" in ident.series

    def test_king_size_kept_in_series_own_series(self):
        ident = ci.identify_comic("King-Size Spider-Man Special #1")
        assert ident.series == "King-Size Spider-Man Special"
        assert ident.edition == "king-size"

    def test_treasury_stripped_from_series_nests_in_parent(self):
        ident = ci.identify_comic("Superman Treasury Edition #1")
        assert ident.series == "Superman"
        assert ident.edition == "treasury"

    def test_annual_edition_alone_is_not_a_reject_reason(self):
        """Being an annual is classification, not inherently a reject signal
        (hard_reject only rejects an annual against a WISH item that doesn't
        want one — identify_comic has no wish item to compare to)."""
        ident = ci.identify_comic("The Amazing Spider-Man Annual #5")
        assert ident.reject_reasons == []


# ─── X-Men vs Uncanny X-Men series-name boundary ──────────────────────────


class TestIdentifyComicXMenUncanny:
    """BUI-253's other explicitly-called-out sharp edge: these are two
    different LOCG series (different histories/volumes) — identify_comic
    must never collapse or normalize away the "Uncanny" prefix."""

    def test_xmen_and_uncanny_xmen_produce_different_series(self):
        xmen = ci.identify_comic("X-Men #94 Marvel 1975")
        uncanny = ci.identify_comic("Uncanny X-Men #142 Marvel 1981")
        assert xmen.series == "X-Men"
        assert uncanny.series == "Uncanny X-Men"
        assert xmen.series != uncanny.series

    def test_uncanny_prefix_not_stripped_as_noise(self):
        ident = ci.identify_comic("Uncanny X-Men #142")
        assert ident.series.lower().startswith("uncanny")


# ─── Collected edition (HC/TPB/Omnibus) single-SKU handling ───────────────


class TestIdentifyComicCollectedEdition:
    def test_omnibus_classified_as_collected(self):
        ident = ci.identify_comic("X-Men Omnibus Vol 1 HC")
        assert ident.edition == "collected"
        assert ident.is_lot is False
        assert ident.issue is None
        assert ident.volume == 1
        assert "collected edition" in ident.reject_reasons[0]

    def test_tpb_classified_as_collected(self):
        ident = ci.identify_comic("Amazing Spider-Man Vol 1 TPB")
        assert ident.edition == "collected"

    def test_trade_paperback_classified_as_collected(self):
        ident = ci.identify_comic("Saga Trade Paperback Vol 1")
        assert ident.edition == "collected"

    def test_epic_collection_classified_as_collected(self):
        ident = ci.identify_comic("X-Men Epic Collection: Second Genesis")
        assert ident.edition == "collected"
        # PR-review fix (S2): "Collection" also matches _LOT_RE's bare
        # \bcollection\b branch — must not also come back is_lot=True.
        assert ident.is_lot is False

    def test_epic_collection_not_misdetected_as_lot(self):
        """PR-review fix (S2, should-fix): "X-Men Epic Collection Vol 3" was
        getting is_lot=True (via _LOT_RE's bare \\bcollection\\b branch
        matching "Collection" inside "Epic Collection") AND edition=
        "collected" simultaneously — a contradictory identity (empty
        constituent_issues plus the wrong confidence tier). A collected
        edition is a single SKU, never a multi-issue lot."""
        ident = ci.identify_comic("X-Men Epic Collection Vol 3")
        assert ident.is_lot is False
        assert ident.edition == "collected"
        assert ident.confidence == 0.6
        assert ident.constituent_issues == []
        assert ident.volume == 3

    def test_collected_edition_missing_issue_is_not_a_parse_failure(self):
        """A collected edition legitimately has no single issue number — this
        must NOT get the same low-confidence 'no issue number found' penalty
        as an unparseable single-issue title."""
        ident = ci.identify_comic("X-Men Omnibus Vol 1 HC")
        assert ident.confidence == 0.6
        assert "no issue number found" not in ident.reject_reasons

    def test_collected_edition_is_not_treated_as_a_lot(self):
        """A single HC/TPB SKU is not a multi-comic bundle in the _LOT_RE
        sense — it must not trigger lot expansion."""
        ident = ci.identify_comic("X-Men Omnibus Vol 1 HC")
        assert ident.is_lot is False
        assert ident.constituent_issues == []


# ─── Facsimile / later-printing reprint classification ────────────────────


class TestIdentifyComicFacsimileAndReprint:
    def test_facsimile_classified_and_flagged(self):
        ident = ci.identify_comic("Amazing Spider-Man #1 Facsimile Edition")
        assert ident.edition == "facsimile"
        assert ident.series == "Amazing Spider-Man"
        assert ident.issue == "1"
        assert "facsimile" in ident.reject_reasons[0].lower()
        # Still confidently extracted — facsimile is a genuineness signal,
        # not an extraction-confidence signal.
        assert ident.confidence == 1.0

    def test_second_printing_classified_as_reprint(self):
        ident = ci.identify_comic("Batman: Vengeance of Bane #1 2nd Printing")
        assert ident.edition == "reprint"
        assert "later printing" in ident.reject_reasons[0]

    def test_true_believers_classified_as_reprint(self):
        ident = ci.identify_comic("Amazing Spider-Man #1 True Believers Reprint")
        assert ident.edition == "reprint"

    def test_marvel_tales_classified_as_reprint(self):
        ident = ci.identify_comic("Spider-Man #1 Marvel Tales Reprint Edition")
        assert ident.edition == "reprint"

    def test_newsstand_is_not_a_reprint(self):
        """Conservative keep-list carried over from _second_print_reject:
        Newsstand/Direct are original print-run variants, not reprints."""
        ident = ci.identify_comic("Amazing Spider-Man #300 Newsstand Edition")
        assert ident.edition == "single-issue"
        assert ident.reject_reasons == []

    def test_retold_classified_as_reprint(self):
        """PR-review fix (S1): "retold" was in sold_comps.py's manual-fallback
        lexicon but missing entirely from comic_identity — a real coverage
        gap. Now in both _REPRINT_MARKERS (should_reject's deterministic
        path) and _PROMO_REPRINT_MARKERS (identify_comic's edition
        classification)."""
        ident = ci.identify_comic("Amazing Spider-Man #300 Retold Edition")
        assert ident.edition == "reprint"
        assert any("later printing" in r for r in ident.reject_reasons)

    def test_retold_rejected_by_should_reject(self):
        assert ci.should_reject(
            "Amazing Spider-Man #300 Retold Edition", "Amazing Spider-Man", "300"
        ) is True


# ─── Lot detection + expansion (every BUI-261 format) ─────────────────────


class TestIdentifyComicLotExpansion:
    def test_slash_list_expanded_as_literal_members(self):
        """Slash lists must NOT be range-computed — "164/165/166/168"
        deliberately skips 167."""
        ident = ci.identify_comic("STRANGE TALES 164/165/166/168")
        assert ident.is_lot is True
        assert ident.series == "STRANGE TALES"
        assert ident.issue is None
        assert ident.constituent_issues == ["164", "165", "166", "168"]
        assert "167" not in ident.constituent_issues

    def test_ampersand_pair_expanded(self):
        ident = ci.identify_comic("Dark Knight Returns # 1 & 3")
        assert ident.is_lot is True
        assert ident.series == "Dark Knight Returns"
        assert ident.constituent_issues == ["1", "3"]

    def test_comma_ampersand_mixed_list_expanded(self):
        ident = ci.identify_comic("ASM #64, #65 & #66")
        assert ident.is_lot is True
        assert ident.series == "ASM"
        assert ident.constituent_issues == ["64", "65", "66"]

    def test_bare_comma_list_expanded(self):
        ident = ci.identify_comic(
            "Avengers 33,45,50,53,63,81,86 Marvel Silver/Bronze Age Lot"
        )
        assert ident.is_lot is True
        assert ident.series == "Avengers"
        assert ident.constituent_issues == ["33", "45", "50", "53", "63", "81", "86"]

    def test_dash_separated_list_expanded(self):
        ident = ci.identify_comic("AVENGERS 92-93-94-95-96 Marvel Bronze Age Lot")
        assert ident.is_lot is True
        assert ident.series == "AVENGERS"
        assert ident.constituent_issues == ["92", "93", "94", "95", "96"]

    def test_bare_two_member_dash_range_expanded_inclusive(self):
        """PR-review fix (B1, blocking): a bare "A-B" dash range with no
        '#'/quantity-word/'through' anchor at all was previously falling
        through to the LIST parser and returning only ["48","50"] — silently
        DROPPING #49. This is a real data-loss bug (collection-add records
        one entry per constituent, so #49 would never get recorded as
        owned). Must now expand inclusively like any other range."""
        ident = ci.identify_comic("Amazing Spider-Man 48-50 comic lot")
        assert ident.is_lot is True
        assert ident.series == "Amazing Spider-Man"
        assert ident.constituent_issues == ["48", "49", "50"]

    def test_bare_two_member_dash_range_minimal_title(self):
        ident = ci.identify_comic("ASM 48-50 lot")
        assert ident.constituent_issues == ["48", "49", "50"]

    def test_nonconsecutive_three_member_dash_chain_stays_literal_list(self):
        """The B1 fix must NOT regress the 3+-member dash CHAIN case into a
        computed range: "92-94-96" must stay the literal list ["92","94","96"]
        (proving 96 does NOT get treated as a range endpoint with 92, which
        would wrongly imply 93 exists) — this is what distinguishes a bare
        2-member range (expand) from a 3+-member chain (literal list)."""
        ident = ci.identify_comic("Avengers 92-94-96 lot")
        assert ident.is_lot is True
        assert ident.series == "Avengers"
        assert ident.constituent_issues == ["92", "94", "96"]
        assert "93" not in ident.constituent_issues
        assert "95" not in ident.constituent_issues

    def test_consecutive_five_member_dash_chain_still_literal_list(self):
        """Re-assertion: the B1 fix must not change the existing consecutive
        5-member dash-chain case (test_dash_separated_list_expanded above) —
        it must still resolve via the literal-list path, not get short-
        circuited into a range by matching an arbitrary internal pair."""
        ident = ci.identify_comic("AVENGERS 92-93-94-95-96 Marvel Bronze Age Lot")
        assert ident.constituent_issues == ["92", "93", "94", "95", "96"]

    def test_bare_dash_year_span_in_non_lot_title_untouched(self):
        """A bare 2-member dash pair that's actually a 4-digit year span in a
        genuine single-issue (non-lot) title must remain completely
        unaffected by the B1 fix — is_lot stays False (no lot signal at all
        in this title) so lot expansion never even runs, and the pattern
        itself can't match 4-digit numbers regardless (same \\d{1,3} bound
        used everywhere else in this module)."""
        ident = ci.identify_comic("Amazing Spider-Man #4 1962-1963 Silver Age Marvel")
        assert ident.is_lot is False
        assert ident.issue == "4"
        assert ident.constituent_issues == []

    def test_hash_range_expanded_inclusive(self):
        ident = ci.identify_comic("Amazing Spider-Man #1-#10 Bronze Age Lot")
        assert ident.is_lot is True
        assert ident.constituent_issues == [str(n) for n in range(1, 11)]

    def test_quantity_word_range_expanded_inclusive(self):
        ident = ci.identify_comic(
            "Batman: The Dark Knight Returns Books 1-4"
        )
        assert ident.is_lot is True
        assert ident.series == "Batman: The Dark Knight Returns"
        assert ident.constituent_issues == ["1", "2", "3", "4"]

    def test_bare_hash_through_expanded_inclusive(self):
        """No 'issues'/'books' word required — real titles often drop it."""
        ident = ci.identify_comic("The Eternals #1 through 10")
        assert ident.is_lot is True
        assert ident.series == "The Eternals"
        assert ident.issue is None
        assert ident.constituent_issues == [str(n) for n in range(1, 11)]

    def test_bare_no_hash_through_expanded_inclusive(self):
        ident = ci.identify_comic("Eternals 1 through 10 Marvel")
        assert ident.is_lot is True
        assert ident.constituent_issues == [str(n) for n in range(1, 11)]

    def test_unparseable_lot_has_empty_constituents_low_confidence(self):
        """A lot with no extractable numbers at all: known bundle, unknown
        contents — [] means that, NOT "not a lot"."""
        ident = ci.identify_comic("Huge Spider-Man Comic Lot!!")
        assert ident.is_lot is True
        assert ident.constituent_issues == []
        assert "could not be parsed" in ident.reject_reasons[0]
        assert ident.confidence == 0.3

    def test_lot_never_has_a_single_issue_value(self):
        """A lot has no single canonical issue — always None regardless of
        whether a stray '#N' appears somewhere in the title."""
        ident = ci.identify_comic("Amazing Spider-Man #1-#10 Bronze Age Lot")
        assert ident.issue is None


class TestIdentifyComicLotCountMismatch:
    def test_count_mismatch_flagged_and_lowers_confidence(self):
        ident = ci.identify_comic("Lot of 11 Comics Amazing Spider-Man #1-10")
        assert ident.is_lot is True
        assert ident.series == "Amazing Spider-Man"
        assert ident.constituent_issues == [str(n) for n in range(1, 11)]
        assert any("count/range mismatch" in r for r in ident.reject_reasons)
        assert ident.confidence == 0.35

    def test_matching_count_not_flagged(self):
        ident = ci.identify_comic("Lot of 10 Comics Amazing Spider-Man #1-10")
        assert ident.is_lot is True
        assert not any("count/range mismatch" in r for r in ident.reject_reasons)
        assert ident.confidence == 0.5

    def test_lot_boilerplate_stripped_from_series(self):
        """"Lot of N Comics" is boilerplate, not part of the series name."""
        ident = ci.identify_comic("Lot of 11 Comics Amazing Spider-Man #1-10")
        assert "Lot" not in ident.series
        assert ident.series == "Amazing Spider-Man"

    def test_mid_string_lot_boilerplate_stripped_from_series(self):
        """PR-review polish: the boilerplate must be stripped wherever it
        falls, not just when it's anchored at the very start of the title —
        "Avengers Lot of 11 Comics #1-10" must yield series="Avengers", not
        "Avengers Lot of 11 Comics"."""
        ident = ci.identify_comic("Avengers Lot of 11 Comics #1-10")
        assert ident.series == "Avengers"
        assert "Lot" not in ident.series
        assert ident.constituent_issues == [str(n) for n in range(1, 11)]


# ─── Deterministic reject-reason population (mirrors should_reject's lexicons) ─


class TestIdentifyComicRejectReasons:
    def test_cgc_slab_flagged(self):
        ident = ci.identify_comic("Amazing Spider-Man #300 CGC 9.8")
        assert "CGC slab" in ident.reject_reasons

    def test_digital_only_flagged(self):
        ident = ci.identify_comic("Amazing Spider-Man #300 Digital Only")
        assert "digital-only listing" in ident.reject_reasons

    def test_trading_card_flagged(self):
        ident = ci.identify_comic("1990 Fleer Marvel Trading Card #1")
        assert "trading card / TCG product" in ident.reject_reasons

    def test_donruss_trading_card_flagged(self):
        """PR-review fix (S1): Donruss/Impel/Keepsake/Signagraph were in
        sold_comps.py's manual-fallback lexicon but missing from
        _TRADING_CARD_MARKERS — a real coverage gap on the primary path."""
        ident = ci.identify_comic("1991 Donruss Marvel Universe Card #5")
        assert "trading card / TCG product" in ident.reject_reasons

    def test_impel_trading_card_flagged(self):
        ident = ci.identify_comic("Impel Marvel Universe Series 2 Card #10")
        assert "trading card / TCG product" in ident.reject_reasons

    def test_keepsake_trading_card_flagged(self):
        ident = ci.identify_comic("Keepsake Comics Marvel Card")
        assert "trading card / TCG product" in ident.reject_reasons

    def test_signagraph_trading_card_flagged(self):
        ident = ci.identify_comic("Signagraph Marvel Card")
        assert "trading card / TCG product" in ident.reject_reasons

    def test_foreign_edition_flagged(self):
        ident = ci.identify_comic("Amazing Spider-Man #10 EDICION mexican la prensa")
        assert "foreign-language/-market edition" in ident.reject_reasons

    def test_clean_single_issue_has_no_reject_reasons(self):
        ident = ci.identify_comic("Amazing Spider-Man #300 NM Marvel 1988")
        assert ident.reject_reasons == []

    def test_reasons_are_additive_not_exclusive(self):
        """A facsimile that's ALSO a CGC slab gets both reasons — reject
        reasons accumulate, they don't short-circuit each other."""
        ident = ci.identify_comic("Amazing Spider-Man #1 Facsimile Edition CGC 9.8")
        assert "CGC slab" in ident.reject_reasons
        assert any("facsimile" in r for r in ident.reject_reasons)


# ─── Confidence tiers (explicit, one test per documented tier) ────────────


class TestIdentifyComicConfidenceTiers:
    def test_tier_1_0_clean_single_issue(self):
        assert ci.identify_comic("Amazing Spider-Man #300 NM Marvel 1988").confidence == 1.0

    def test_tier_0_8_ambiguous_year(self):
        assert ci.identify_comic(
            "Amazing Spider-Man #4 1962-1963 Silver Age Marvel"
        ).confidence == 0.8

    def test_tier_0_6_collected_edition(self):
        assert ci.identify_comic("X-Men Omnibus Vol 1 HC").confidence == 0.6

    def test_tier_0_5_bare_digit_issue_fallback(self):
        assert ci.identify_comic("Amazing Spider-Man Vol 3 175 NM").confidence == 0.5

    def test_tier_0_5_lot_with_parsed_constituents(self):
        assert ci.identify_comic("STRANGE TALES 164/165/166/168").confidence == 0.5

    def test_tier_0_35_lot_with_count_mismatch(self):
        assert ci.identify_comic(
            "Lot of 11 Comics Amazing Spider-Man #1-10"
        ).confidence == 0.35

    def test_tier_0_3_lot_unparseable_constituents(self):
        assert ci.identify_comic("Huge Spider-Man Comic Lot!!").confidence == 0.3

    def test_tier_0_3_no_issue_number_found(self):
        assert ci.identify_comic("Moon Knight X6 Marvel 1982").confidence == 0.3

    def test_tier_0_0_empty_title(self):
        assert ci.identify_comic("").confidence == 0.0

    def test_tier_0_0_whitespace_only_title(self):
        assert ci.identify_comic("   ").confidence == 0.0


# ─── EDITION_LABELS / _EDITION_PATTERNS drift guard ────────────────────────


class TestEditionLabelsInSyncWithEditionPatterns:
    """PR-review nit (N3): EDITION_LABELS is a hand-maintained parallel to
    _EDITION_PATTERNS (Step 1, byte-identical — can't move or change), used
    only to render the Haiku verify-prompt's edition-mismatch bullet (BUI-253
    Step 5). Nothing enforces the two stay in sync if a future edit adds/
    removes an edition word from one without the other — these tests catch
    that drift."""

    def test_same_count(self):
        assert len(ci.EDITION_LABELS) == len(ci._EDITION_PATTERNS)

    def test_every_label_matches_exactly_one_edition_pattern(self):
        for label in ci.EDITION_LABELS:
            sample = f"Test {label} #1"
            matches = [p for p in ci._EDITION_PATTERNS if p.search(sample)]
            assert len(matches) == 1, (
                f"{label!r} should match exactly one _EDITION_PATTERNS entry, "
                f"matched {len(matches)}"
            )

    def test_every_edition_pattern_has_a_matching_label(self):
        """The reverse direction — every _EDITION_PATTERNS entry must be
        represented by at least one EDITION_LABELS sample, so a newly-added
        pattern with no corresponding label is caught too."""
        for pat in ci._EDITION_PATTERNS:
            assert any(
                pat.search(f"Test {label} #1") for label in ci.EDITION_LABELS
            ), f"pattern {pat.pattern!r} has no matching EDITION_LABELS entry"


# ─── Edge cases ─────────────────────────────────────────────────────────────


class TestIdentifyComicEdgeCases:
    def test_none_title_does_not_raise(self):
        ident = ci.identify_comic(None)
        assert ident.confidence == 0.0
        assert ident.series is None

    def test_empty_title_reject_reason(self):
        ident = ci.identify_comic("")
        assert ident.reject_reasons == ["empty title"]

    def test_hash_only_no_series_text(self):
        ident = ci.identify_comic("#5 NM")
        assert ident.issue == "5"
        assert ident.series is None
        assert "no series text before issue number" in ident.reject_reasons
        assert ident.confidence == 0.5

    def test_ratio_variant_not_misread_as_lot(self):
        """BUI-261 PR-review fix carried through: a ratio variant must not be
        mistaken for a 2-member lot."""
        ident = ci.identify_comic("Amazing Spider-Man #300 1/100 variant")
        assert ident.is_lot is False
        assert ident.issue == "300"

    def test_half_issue_slash_not_misread_as_lot(self):
        ident = ci.identify_comic("Batman #1/2")
        assert ident.is_lot is False
        assert ident.issue == "1"


# ─── score_against_wish() ──────────────────────────────────────────────────


class TestScoreAgainstWish:
    """score_against_wish is the extracted-out scorer match_listing now
    delegates to. These mirror TestMatchListing's cases directly against the
    new function, confirming the math itself (not just match_listing's outer
    loop) is intact."""

    def _wish(self, name):
        return seller_scan.prepare_wish_items([{"id": 1, "name": name}])[0]

    def test_exact_match_scores_1_0(self):
        identity = ci.identify_comic("AMAZING SPIDER-MAN #300 NM Marvel 1988 VENOM")
        wish = self._wish("Amazing Spider-Man #300")
        assert ci.score_against_wish(identity, wish) == 1.0

    def test_wrong_issue_scores_0(self):
        identity = ci.identify_comic("AMAZING SPIDER-MAN #299 NM Marvel 1988")
        wish = self._wish("Amazing Spider-Man #300")
        assert ci.score_against_wish(identity, wish) == 0.0

    def test_wrong_series_scores_0(self):
        identity = ci.identify_comic("Batman #300 NM DC")
        wish = self._wish("Amazing Spider-Man #300")
        assert ci.score_against_wish(identity, wish) == 0.0

    def test_partial_token_overlap_scores_fraction(self):
        identity = ci.identify_comic("Spider-Man #300 NM")  # missing "amazing"
        wish = self._wish("Amazing Spider-Man #300")
        score = ci.score_against_wish(identity, wish)
        assert 0.0 < score < 1.0

    def test_empty_tokens_defensively_returns_zero(self):
        """Defensive guard — prepare_wish_items never produces empty tokens
        in practice (it filters those out), but score_against_wish must not
        raise a ZeroDivisionError if ever handed one."""
        identity = ci.identify_comic("Something #1")
        wish = {"issue": "1", "_tokens": []}
        assert ci.score_against_wish(identity, wish) == 0.0


# ─── match_listing byte-identical-behavior smoke check ─────────────────────
# The full BUI-233..244/BUI-245/BUI-261 suites in test_seller_scan.py and
# test_wishlist_sellers.py already exercise match_listing end-to-end with
# zero test edits (the Step 2 acceptance bar) — this class just adds a
# couple of belt-and-suspenders checks at the identify_comic/score_against_wish
# seam specifically, since that's the new code path match_listing now runs
# through.


class TestMatchListingDelegatesCorrectly:
    def test_match_listing_still_picks_best_of_multiple_wishes(self):
        items = seller_scan.prepare_wish_items([
            {"id": 1, "name": "Amazing Spider-Man #300"},
            {"id": 2, "name": "Spectacular Spider-Man #300"},
        ])
        wish, score = seller_scan.match_listing(
            "AMAZING SPIDER-MAN #300 NM Marvel 1988", items
        )
        assert wish is not None
        assert "Amazing" in wish["name"]
        assert score == 1.0

    def test_match_listing_empty_title_no_exception(self):
        items = seller_scan.prepare_wish_items(
            [{"id": 1, "name": "Amazing Spider-Man #300"}]
        )
        wish, score = seller_scan.match_listing("", items)
        assert wish is None
        assert score == 0.0


# ─── confident_cover_year() — the BUI-316 strict gate ──────────────────────
# The proper fix for BUI-308's single-owned-wrong-volume false positive: only
# forward a per-issue cover year to /comic:collection-check when the title's
# parenthesized year AND item-specifics Publication Year corroborate it within
# ±1, and never for a facsimile/reprint. A correct year is Pareto-better (it
# lets the matcher's year gate reject the wrong volume); a wrong/uncertain one
# is never emitted, so it cannot reintroduce BUI-129.


class TestConfidentCoverYear:
    def test_agreeing_signals_emit_the_pub_year(self):
        # Title paren year and Publication Year agree exactly → emit.
        assert (
            ci.confident_cover_year(
                "Fantastic Four #18 (1963) Kirby", {"Publication Year": "1963"}
            )
            == 1963
        )

    def test_agreement_within_plus_or_minus_one_still_emits(self):
        # ±1 cover-vs-onsale skew tolerance (BUI-214/251): paren 2015 vs
        # Publication Year 2016 still corroborate; the Publication Year is
        # returned as the authoritative cover year.
        assert (
            ci.confident_cover_year("Thor #5 (2015)", {"Publication Year": "2016"})
            == 2016
        )
        assert (
            ci.confident_cover_year("Thor #5 (2015)", {"Publication Year": "2014"})
            == 2014
        )

    def test_disagreeing_signals_suppress_volume_start_year_trap(self):
        # The BUI-129 trap: the title paren carries the VOLUME start year (1963)
        # while the issue actually shipped 1983 (Publication Year 1983). The two
        # disagree by 20 years → emit NOTHING, so the check stays year-agnostic
        # (never forwards the wrong 1963 that would hide every owned mid-run issue).
        assert (
            ci.confident_cover_year(
                "Amazing Spider-Man (1963) #238", {"Publication Year": "1983"}
            )
            is None
        )

    def test_two_year_gap_suppresses(self):
        # A 2-year gap is outside the ±1 window → suppress.
        assert (
            ci.confident_cover_year("Thor #5 (2015)", {"Publication Year": "2017"})
            is None
        )

    def test_facsimile_never_emits_even_when_signals_agree(self):
        # A facsimile's Publication Year is the ORIGINAL issue's year; forwarding
        # it would falsely match the owned original volume. Refuse outright even
        # though both signals say 1963.
        assert (
            ci.confident_cover_year(
                "Amazing Spider-Man #1 (1963) Facsimile Edition",
                {"Publication Year": "1963"},
            )
            is None
        )

    def test_reprint_never_emits_even_when_signals_agree(self):
        # Same hazard for a later printing (2nd/3rd print, true believers, etc.).
        assert (
            ci.confident_cover_year(
                "Batman #1 (1940) 2nd Printing", {"Publication Year": "1940"}
            )
            is None
        )

    def test_no_paren_year_in_title_suppresses(self):
        # Only one signal (Publication Year) → no corroboration → suppress.
        assert (
            ci.confident_cover_year(
                "Amazing Spider-Man #300 Marvel", {"Publication Year": "1988"}
            )
            is None
        )

    def test_no_publication_year_suppresses(self):
        # Only one signal (title paren) → no corroboration → suppress.
        assert ci.confident_cover_year("Amazing Spider-Man #300 (1988)", {}) is None
        assert (
            ci.confident_cover_year("Amazing Spider-Man #300 (1988)", None) is None
        )

    def test_unparseable_or_implausible_publication_year_suppresses(self):
        assert (
            ci.confident_cover_year(
                "Amazing Spider-Man #300 (1988)", {"Publication Year": "n/a"}
            )
            is None
        )
        # Out of the 1930–2035 plausibility window.
        assert (
            ci.confident_cover_year(
                "Amazing Spider-Man #300 (1899)", {"Publication Year": "1899"}
            )
            is None
        )

    def test_integer_publication_year_value_accepted(self):
        # item_specifics values are normally strings, but tolerate an int too.
        assert (
            ci.confident_cover_year(
                "Fantastic Four #1 (1961)", {"Publication Year": 1961}
            )
            == 1961
        )

    def test_empty_or_none_title_suppresses(self):
        assert ci.confident_cover_year(None, {"Publication Year": "1988"}) is None
        assert ci.confident_cover_year("", {"Publication Year": "1988"}) is None

    def test_multiple_paren_years_one_corroborates_emits(self):
        # A title can carry two parenthetical groups (a volume year + a
        # grading/cert year, e.g. "(2018) (CGC 2024)"). Corroboration is
        # satisfied if ANY of them is within ±1 of the Publication Year — here
        # 2018 does, so the true cover year is emitted.
        assert (
            ci.confident_cover_year(
                "Amazing Spider-Man #798 (2018) (CGC 2024)",
                {"Publication Year": "2018"},
            )
            == 2018
        )

    def test_correlated_wrong_year_is_the_accepted_residual(self):
        # ACCEPTED RESIDUAL (BUI-316 design): the two signals are both
        # seller-entered, so a seller who stamps the SAME volume-start year in
        # BOTH the title paren and the Publication Year aspect for a mid-run
        # issue defeats the corroboration — the gate cannot tell this apart from
        # a genuine agreement and forwards the (wrong) year. This is a conscious
        # tradeoff (matcher softening was rejected); the test PINS the behavior
        # so any future hardening that changes it is a visible, deliberate
        # change, not an accidental one. Contrast with
        # test_disagreeing_signals_suppress_volume_start_year_trap, the common
        # case the gate DOES catch (Publication Year carries the true 1983).
        assert (
            ci.confident_cover_year(
                "Amazing Spider-Man (1963) #238", {"Publication Year": "1963"}
            )
            == 1963
        )
