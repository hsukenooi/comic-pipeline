"""Tests for the BUI-919 seller-disclosed condition-defect gate.

Three layers, deliberately:

1. The classifier on its own (vocabulary, the BUI-668 substring trap,
   negations, positive assertions, the explicitly out-of-scope terms).
2. The **seven-listing regression** from BUI-919's evidence table, using each
   listing's own seller note verbatim — four trip the standing rule, three do
   not, and the three non-trippers are the precision half of the test.
3. The **fetch → identify → gate seam**: that `parse_item` reads the raw
   `conditionDescription` key, that the field is always present, and that the
   identify table renders the Defects column the gate reads. A classifier that
   is right about text but never wired to the listing shape is the failure
   mode this layer exists to catch.
"""

import pytest

import condition_defects
import ebay_fetch
from condition_defects import (
    DEFECT_CODES,
    detect_condition_defects,
    format_defect_cell,
    listing_defect_findings,
)


def _codes(text):
    return detect_condition_defects(text)[0]


# ---------------------------------------------------------------------------
# 1. Vocabulary
# ---------------------------------------------------------------------------


class TestMoistureVocabulary:
    @pytest.mark.parametrize("text", [
        "moisture damage to bottom right corner",
        "moisture stains along the spine",
        "water damage lower left",
        "water damaged back cover",
        "water-damage on the last few pages",
        "water stain on back cover",
        "water stains on back cover",
        "slight water ring lower right",
        "small water spot front cover",
        "water mark across the logo",
        "waterlogged bottom third",
        "mildew smell from storage",
        "some mildewing on the interior",
        "mold on the back cover",
        "moldy interior pages",
        "tide line across back cover",
        "stored damp for years",
        "dampness affected the corners",
    ])
    def test_moisture_terms_fire(self, text):
        assert _codes(text) == ("moisture",)

    @pytest.mark.parametrize("text", [
        # Bare "water" is never enough on its own.
        "water colour cover art",
        "a waterfall scene on the cover",
        "Waterworld #1 movie adaptation",
        # "watermark" written solid is about the seller's photos, not the book.
        "photos are watermarked",
        "watermarked scan only",
        # "damp" must not reach inflections that mean something else.
        "dampened enthusiasm for the run",
        # Gold is not mold.
        "Goldmine copy, gold foil cover",
    ])
    def test_moisture_near_misses_stay_clean(self, text):
        assert _codes(text) == ()


class TestRustVocabulary:
    @pytest.mark.parametrize("text", [
        "rust on the staples",
        "rusty staple",
        "rusted staples",
        "staples are rusting",
        "rust migration around both staples",
        "rust stains at the spine",
        "some rustiness at the top staple",
    ])
    def test_rust_terms_fire(self, text):
        assert "rust" in _codes(text)

    @pytest.mark.parametrize("text", [
        # BUI-668: this is the exact trap that shipped there — a bare token
        # matching inside a longer, unrelated (often *reassuring*) word.
        "I would not trust the previous owner's grade",
        "trustworthy long-time seller",
        "a crusty old longbox find",
        "encrusted price sticker on the cover",
        "frustrating scarcity at this grade",
        "thrust into the spotlight",
        "adjustments made to the description",
    ])
    def test_rust_substring_trap_is_closed(self, text):
        assert _codes(text) == ()

    @pytest.mark.parametrize("text", [
        "rust colored logo",
        "rust-coloured cover art",
        "rust colour background",
    ])
    def test_rust_as_a_colour_is_not_corrosion(self, text):
        """"Rust" is also a colour, and a colour on the cover is not a defect.
        This is the substring trap's cousin: the token is whole-word correct
        and still means something else."""
        assert _codes(text) == ()

    def test_rusty_in_a_title_still_fires_and_says_where_it_came_from(self):
        """Known, accepted false-positive shape: "Rusty" is a New Mutants
        character. The finding carries `source: "title"` and the phrase, so the
        drop is printed with enough evidence for the user to override it on
        sight — which is the whole reason drops are never silent."""
        findings = listing_defect_findings(
            condition_description=None,
            title="NEW MUTANTS #18 1st appearance of Rusty Collins",
        )
        assert [(f["code"], f["source"], f["phrase"]) for f in findings] == [
            ("rust", "title", "NEW MUTANTS #18 1st appearance of Rusty Collins"),
        ]


class TestLooseStapleVocabulary:
    @pytest.mark.parametrize("text", [
        "cover detached both staples",
        "cover and 1st wrap detached top staple",
        "top staple detached from cover",
        "bottom staple missing",
        "staples missing entirely",
        "one staple has popped",
        "staple popped through the cover",
        "staples loose",
        "loose bottom staple",
        "staples are pulling away from the cover",
        "staple pull at the top",
        "staples pulled through the paper",
        "cover separated at the staples",
        "staples gone",
        "staple absent",
        "cover is starting to detach at the top staple",
        "staple has come away from the wrap",
        "the top staple fell out",
        # Phrasings the first-pass vocabulary missed.
        "staples are pulling away",
        "bottom staple torn free of the cover",
        "top staple torn loose",
        "staples have loosened over time",
    ])
    def test_loose_staple_terms_fire(self, text):
        assert _codes(text) == ("loose_staple",)

    @pytest.mark.parametrize("text", [
        # Positive assertions carry no defect term at all.
        "staples tight",
        "staples are tight and secure",
        "staples intact",
        "staples original and clean",
        "cover attached at both staples",
        "staples reattached professionally",
        # A manufacturing note on a perfectly sound book.
        "off center staples, otherwise sharp",
        "off-centre staple placement",
        # "popular" must not reach `pop`.
        "a popular key issue, staples tight",
        # Cross-clause co-occurrence is not a hit: this is BUI-919's own
        # X-Men #70 shape ("pieces missing back cover") with a reassuring
        # staple clause bolted on.
        "pieces missing back cover, staples intact",
        'GD condition, 2" spine split, pieces missing back cover',
        "spine split and staples tight",
        "sold separately; staples secure",
    ])
    def test_loose_staple_near_misses_stay_clean(self, text):
        assert _codes(text) == ()

    def test_a_defect_without_a_staple_word_is_out_of_scope(self):
        """The rule names staples. A detached cover or loose pages with no
        staple named is a hand-grading call, not a standing-rule drop — and
        widening the anchor to `cover` would fire on BUI-919's own X-Men #70
        ("pieces missing back cover"), which the ticket labels a non-tripper."""
        assert _codes("front cover detached") == ()
        assert _codes("interior pages loose") == ()


class TestOutOfScopeTerms:
    @pytest.mark.parametrize("text", [
        "staining",
        "VG condition, staining",
        "heavy staining to the cover",
        "foxing on the interior pages",
        "tanning along the edges",
        "spine roll",
        'tape along length of spine, 1" tear back cover',
        "large amount of spine is split",
        "writing on the cover in ink",
    ])
    def test_plain_staining_and_friends_never_fire(self, text):
        """BUI-919 § Out of Scope: the seller writes "moisture damage" when
        they mean water and plain "staining" otherwise, so the separate
        vocabulary IS the signal. Collapsing them would drop every mildly
        stained vintage book in the wish list."""
        assert _codes(text) == ()


# ---------------------------------------------------------------------------
# 2. Negation
# ---------------------------------------------------------------------------


class TestNegation:
    @pytest.mark.parametrize("text", [
        "no rust",
        "no rust at all",
        "no visible rust",
        "no signs of rust",
        "staples show no rust",
        "not rusty",
        "rust-free staples",
        "rust free",
        "no moisture damage",
        "no evidence of water damage",
        "free of rust",
        "free from mildew",
        "moisture-free storage",
        "no loose staples",
        "staples are not loose",
        "no missing staples",
        "without any water damage",
        "zero rust on the staples",
        "devoid of moisture damage",
        "there is no water damage whatsoever",
        # A negator distributes across "and"/"or".
        "free of rust and mildew",
        "no rust or water damage",
        # Contracted and verbal negators.
        "cannot find any rust",
        "can't see any water damage",
        "haven't found any rust",
        "this copy lacks any moisture damage",
        "we do not see any rust",
        "nothing loose about the staples",
        "never stored damp",
    ])
    def test_negated_text_is_not_a_hit(self, text):
        assert _codes(text) == ()

    @pytest.mark.parametrize("text,code", [
        # The negator belongs to a PREVIOUS clause — it must not reach across.
        ("no tape, rusty staples", "rust"),
        ("no writing. water damage to back cover", "moisture"),
        ("no tears; bottom staple missing", "loose_staple"),
        # "but" reverses polarity, so the negation stops there.
        ("no water damage but rust present", "rust"),
        ("staples tight but the cover is detached at the top staple",
         "loose_staple"),
        # Distance: a negator further back than the lookback cannot bind.
        ("no tears anywhere on this otherwise sharp vintage copy rust at spine",
         "rust"),
    ])
    def test_negation_does_not_leak_across_clauses(self, text, code):
        assert code in _codes(text)

    @pytest.mark.parametrize("text,code", [
        ("minimal rust at the staples", "rust"),
        ("light rust on the top staple", "rust"),
        ("a touch of rust", "rust"),
        ("slight moisture damage to the corner", "moisture"),
        ("barely any rust", "rust"),
        ("hardly noticeable water stain", "moisture"),
    ])
    def test_a_hedge_is_not_a_negation(self, text, code):
        """The standing rule has no severity threshold — "minimal rust" is rust,
        at any price and any grade. Treating a hedge as a negation is the
        money-losing direction, so hedges are deliberately absent from the
        negator vocabulary."""
        assert code in _codes(text)


# ---------------------------------------------------------------------------
# 3. Return shape
# ---------------------------------------------------------------------------


class TestReturnShape:
    def test_clean_text_returns_two_empty_tuples(self):
        assert detect_condition_defects("NM- condition, sharp corners") == ((), ())

    @pytest.mark.parametrize("value", [None, "", 0, [], {}])
    def test_absent_or_non_string_text_is_clean_not_an_error(self, value):
        assert detect_condition_defects(value) == ((), ())

    def test_phrases_quote_the_seller_by_the_clause_not_the_token(self):
        """The clause is what makes a printed drop reason readable — `rust:
        "rusty staple at top"` instead of `rust: "rusty"`."""
        codes, phrases = detect_condition_defects("VG, rusty staple at top")
        assert codes == ("rust",)
        assert phrases == ("rusty staple at top",)

    def test_an_over_long_clause_is_trimmed_with_ellipses(self):
        """A clause too long to quote (a title, usually) is windowed around the
        match and marked, so nobody reads a trimmed phrase as the full text."""
        text = (
            "absolutely stunning high grade vintage silver age key issue copy "
            "with rust at the bottom staple that otherwise presents beautifully "
            "for the grade in hand"
        )
        _, phrases = detect_condition_defects(text)
        assert len(phrases) == 1
        phrase = phrases[0]
        assert "rust" in phrase
        assert phrase.startswith("…") and phrase.endswith("…")
        assert len(phrase) <= 92

    def test_codes_are_emitted_in_a_stable_order(self):
        """Order follows DEFECT_CODES, not order of appearance, so a printed
        drop reason is byte-identical across two listings with the same
        defects written in a different order."""
        first = detect_condition_defects("loose staple, rust, moisture damage")[0]
        second = detect_condition_defects("moisture damage, rust, loose staple")[0]
        assert first == second == DEFECT_CODES

    def test_duplicate_phrases_are_deduplicated(self):
        _, phrases = detect_condition_defects("rust at the staples. rust at the staples")
        assert phrases == ("rust at the staples",)

    def test_distinct_phrases_for_one_code_are_all_reported(self):
        _, phrases = detect_condition_defects("rust at top staple. rust at bottom")
        assert phrases == ("rust at top staple", "rust at bottom")


class TestListingFindings:
    def test_condition_note_is_the_primary_source(self):
        findings = listing_defect_findings(
            condition_description="VG condition, rusty staple, staining",
            title="THE X-MEN #26 (1966) see condition description",
        )
        assert [f["code"] for f in findings] == ["rust"]
        assert findings[0]["source"] == "condition_description"
        assert findings[0]["phrase"] == "rusty staple"

    def test_title_is_scanned_too(self):
        """A seller who does not fill in the condition note sometimes names the
        defect in the title instead; the source is recorded so the user can
        weigh an override (a title hit on "Rusty" the New Mutants character
        reads very differently from a note reading "rusty staple")."""
        findings = listing_defect_findings(
            condition_description=None,
            title="AMAZING SPIDER-MAN #14 WATER DAMAGE readers copy",
        )
        assert [f["code"] for f in findings] == ["moisture"]
        assert findings[0]["source"] == "title"

    def test_clean_listing_yields_an_empty_list(self):
        assert listing_defect_findings(
            condition_description="NM- condition, staples tight, no rust",
            title="AMAZING SPIDER-MAN #300 NM Marvel 1988 VENOM",
        ) == []

    def test_the_same_defect_in_both_sources_is_reported_once(self):
        findings = listing_defect_findings(
            condition_description="rust", title="rust",
        )
        assert len(findings) == 1
        assert findings[0]["source"] == "condition_description"

    def test_boilerplate_short_description_is_not_a_source(self):
        """BUI-919 measured `shortDescription` returning the store's
        return-policy boilerplate, identical across all seven listings. One
        template sentence mentioning water damage would drop that seller's
        whole inventory, so it is not an input — the signature has no slot for
        it, and this test is what keeps someone from adding one quietly."""
        with pytest.raises(TypeError):
            listing_defect_findings(
                condition_description=None,
                title="clean",
                description_snippet="we note any water damage in the description",
            )


class TestFormatDefectCell:
    def test_empty_findings_render_as_none(self):
        assert format_defect_cell([]) is None
        assert format_defect_cell(None) is None

    def test_cell_names_the_defect_and_quotes_the_phrase(self):
        cell = format_defect_cell(
            [{"code": "rust", "phrase": "rusty", "source": "condition_description"}]
        )
        assert cell == 'rust: "rusty"'

    def test_multiple_defects_are_semicolon_joined(self):
        cell = format_defect_cell([
            {"code": "moisture", "phrase": "water stains", "source": "title"},
            {"code": "loose_staple", "phrase": "staples loose", "source": "title"},
        ])
        assert cell == 'moisture damage: "water stains"; loose/detached staple: "staples loose"'


# ---------------------------------------------------------------------------
# 4. The BUI-919 seven-listing regression
# ---------------------------------------------------------------------------

# Seller notes read by hand off the seven "see condition description"
# survivors of the 2026-09-16 timemachinecomics scan (BUI-919 § Evidence),
# verbatim. `expected` is the ticket's own labelling: "Four trip the rule
# directly. The other three carry spine tape or missing pieces."
BUI_919_LISTINGS = [
    ("The X-Men #26",
     "VG condition, rusty staple, manufactured with one staple, staining",
     ("rust",)),
    ("The X-Men #42",
     'GD+ condition, tape along length of spine, 1" tear back cover',
     ()),
    ("The X-Men #69",
     "FR/GD condition, tape along length of spine, large amount of spine is split",
     ()),
    ("The X-Men #70",
     'GD condition, 2" spine split, pieces missing back cover',
     ()),
    ("The X-Men #71",
     "GD+ condition, cover detached both staples, ink front cover, staining",
     ("loose_staple",)),
    ("Detective Comics #400",
     "cover and 1st wrap detached top staple",
     ("loose_staple",)),
    ("Detective Comics #477",
     "cover and 1st 5 wraps detached bottom staple",
     ("loose_staple",)),
]


class TestBui919SevenListingRegression:
    @pytest.mark.parametrize("name,note,expected", BUI_919_LISTINGS,
                             ids=[row[0] for row in BUI_919_LISTINGS])
    def test_each_listing_classifies_as_the_ticket_labelled_it(self, name, note, expected):
        assert _codes(note) == expected, name

    def test_four_of_the_seven_are_dropped_by_the_standing_rule(self):
        dropped = [name for name, note, _ in BUI_919_LISTINGS if _codes(note)]
        assert dropped == [
            "The X-Men #26", "The X-Men #71",
            "Detective Comics #400", "Detective Comics #477",
        ]

    def test_the_three_survivors_are_not_dropped_on_staining_or_tape(self):
        """The precision half. All three name real damage — spine tape, a
        spine split, a missing piece of back cover — and two of the four
        droppers also say "staining". If the gate fired on any of that it
        would be indistinguishable from a gate that works."""
        kept = [name for name, note, _ in BUI_919_LISTINGS if not _codes(note)]
        assert kept == ["The X-Men #42", "The X-Men #69", "The X-Men #70"]


# ---------------------------------------------------------------------------
# 5. The fetch -> identify -> gate seam
# ---------------------------------------------------------------------------


def _browse_item(**overrides):
    """A minimal Browse API `getItem` response, shaped like the live ones.

    Field names verified against a live `get_item_by_legacy_id` call on
    2026-09-21: `conditionDescription` is the key behind eBay's "Seller Notes"
    block, and `shortDescription` is the store boilerplate that is NOT it.
    """
    data = {
        "itemId": "v1|137743677922|0",
        "title": "The Amazing Spider-Man #103 (1971) GD/VG Condition see condition description",
        "buyingOptions": ["AUCTION"],
        "currentBidPrice": {"value": "9.99", "currency": "USD"},
        "bidCount": 3,
        "itemEndDate": "2026-09-26T13:00:00.000Z",
        "condition": "Good",
        "conditionId": "5000",
        "conditionDescription": "cover and 1st 6 wraps detached bottom staple",
        "shortDescription": "We send combined invoices daily for your convenience.",
        "localizedAspects": [],
        "seller": {"username": "timemachinecomics"},
        "itemWebUrl": "https://www.ebay.com/itm/137743677922",
    }
    data.update(overrides)
    return data


class TestParseItemSeam:
    def test_condition_description_is_echoed_from_the_raw_key(self):
        parsed = ebay_fetch.parse_item(_browse_item())
        assert parsed["condition_description"] == (
            "cover and 1st 6 wraps detached bottom staple"
        )

    def test_the_defect_is_classified_from_that_field(self):
        parsed = ebay_fetch.parse_item(_browse_item())
        assert parsed["condition_defects"] == [{
            "code": "loose_staple",
            "phrase": "1st 6 wraps detached bottom staple",
            "source": "condition_description",
        }]

    def test_absent_condition_note_is_none_with_an_empty_defect_list(self):
        """Most listings carry no `conditionDescription` — that is the seller
        writing nothing, not a failure. The key must still be present, and
        `condition_defects` must be `[]` (scanned, nothing found) rather than
        missing, so a caller can never read "no key" as "not checked"."""
        item = _browse_item()
        del item["conditionDescription"]
        parsed = ebay_fetch.parse_item(item)
        assert parsed["condition_description"] is None
        assert parsed["condition_defects"] == []

    def test_boilerplate_short_description_does_not_leak_into_the_gate(self):
        parsed = ebay_fetch.parse_item(_browse_item(
            conditionDescription=None,
            shortDescription="We note any water damage or rust in the description.",
        ))
        assert parsed["condition_defects"] == []

    def test_long_note_is_truncated_for_output_but_classified_in_full(self):
        """A cap applied BEFORE classification would hide a defect named at the
        end of a long note — the whole point of reading the field."""
        note = ("Nice bright copy. " * 60) + "bottom staple missing"
        assert len(note) > 1000
        parsed = ebay_fetch.parse_item(_browse_item(conditionDescription=note))
        assert len(parsed["condition_description"]) == 1000
        assert [f["code"] for f in parsed["condition_defects"]] == ["loose_staple"]

    def test_every_pre_existing_output_key_survives(self):
        """BUI-919 adds two keys and renames none — every downstream consumer
        of `ebay-fetch --json` keeps reading what it read before."""
        parsed = ebay_fetch.parse_item(_browse_item())
        for key in (
            "item_id", "title", "listing_type", "current_price", "bid_count",
            "end_date", "end_date_iso", "condition", "condition_id",
            "condition_note", "grade", "grade_source", "grade_from_description",
            "certifier", "cert_number", "label", "page_quality",
            "grade_mismatch_note", "variant", "cover_year", "item_specifics",
            "description_snippet", "listing_url", "seller",
        ):
            assert key in parsed, key


class TestIdentifyTableSeam:
    NOW = ebay_fetch.parse_utc_timestamp("2026-09-21T12:00:00Z")

    def _cells(self, row):
        return [c.strip() for c in row.strip().strip("|").split(" | ")]

    def test_defects_column_sits_between_notes_and_cert(self):
        columns = list(ebay_fetch.IDENTIFY_COLUMNS)
        assert columns[columns.index("Notes") + 1] == "Defects"
        assert columns[-2:] == ["Cert", "PQ"]

    def test_the_column_carries_the_reason_and_the_phrase(self):
        parsed = ebay_fetch.parse_item(_browse_item())
        cells = self._cells(ebay_fetch.identify_row(1, parsed, self.NOW))
        defect_cell = cells[list(ebay_fetch.IDENTIFY_COLUMNS).index("Defects")]
        assert "loose/detached staple" in defect_cell
        assert "detached bottom staple" in defect_cell
        assert defect_cell.startswith("⚠️")

    def test_a_clean_listing_renders_a_blank_defects_cell(self):
        parsed = ebay_fetch.parse_item(_browse_item(
            conditionDescription="VG condition, staining, staples tight",
        ))
        cells = self._cells(ebay_fetch.identify_row(1, parsed, self.NOW))
        assert cells[list(ebay_fetch.IDENTIFY_COLUMNS).index("Defects")] == "—"

    def test_a_pipe_in_the_seller_note_cannot_break_the_markdown_row(self):
        parsed = ebay_fetch.parse_item(_browse_item(
            conditionDescription="rust | heavy",
        ))
        row = ebay_fetch.identify_row(1, parsed, self.NOW)
        assert row.count(" | ") == len(ebay_fetch.IDENTIFY_COLUMNS) - 1

    @pytest.mark.parametrize("name,note,expected", BUI_919_LISTINGS,
                             ids=[row[0] for row in BUI_919_LISTINGS])
    def test_the_seven_listings_render_end_to_end(self, name, note, expected):
        """The gate reads the table, so the seven-listing verdict has to
        survive the whole fetch -> parse -> render path, not just the
        classifier call."""
        parsed = ebay_fetch.parse_item(_browse_item(
            title=f"{name} see condition description", conditionDescription=note,
        ))
        cells = self._cells(ebay_fetch.identify_row(1, parsed, self.NOW))
        cell = cells[list(ebay_fetch.IDENTIFY_COLUMNS).index("Defects")]
        if expected:
            assert cell != "—", name
            for code in expected:
                assert condition_defects.DEFECT_LABELS[code] in cell, name
        else:
            assert cell == "—", name
