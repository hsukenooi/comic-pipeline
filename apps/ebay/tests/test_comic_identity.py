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
