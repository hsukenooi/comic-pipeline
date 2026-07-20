"""Tests for the comic-identify CLI (BUI-253 Step 3) — a thin argparse
wrapper around comic_identity.identify_comic() that prints JSON so the
/comic:* skills and LLM agents can shell out instead of re-deriving
title-parsing rules in prose."""

import json

import comic_identify


class TestComicIdentifyArgMode:
    def test_arg_in_json_out(self, capsys):
        rc = comic_identify.main(["The Amazing Spider-Man #300 (1988)"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["series"] == "The Amazing Spider-Man"
        assert data["issue"] == "300"
        assert data["year"] == 1988
        assert data["confidence"] == 1.0

    def test_title_norm_not_leaked_into_json(self, capsys):
        """_title_norm is an internal perf cache, not part of the public
        identity contract this CLI exposes."""
        comic_identify.main(["Amazing Spider-Man #300"])
        data = json.loads(capsys.readouterr().out)
        assert "_title_norm" not in data

    def test_lot_title_reports_constituent_issues(self, capsys):
        comic_identify.main(["The Eternals #1 through 10"])
        data = json.loads(capsys.readouterr().out)
        assert data["is_lot"] is True
        assert data["constituent_issues"] == [str(n) for n in range(1, 11)]
        assert data["issue"] is None

    def test_reject_case_reports_reject_reasons(self, capsys):
        comic_identify.main(["Amazing Spider-Man #300 CGC 9.8"])
        data = json.loads(capsys.readouterr().out)
        assert "CGC slab" in data["reject_reasons"]

    def test_output_is_single_line_json(self, capsys):
        comic_identify.main(["Batman #1"])
        out = capsys.readouterr().out
        assert out.count("\n") == 1  # one line + trailing newline from print

    def test_returns_zero_exit_code(self):
        assert comic_identify.main(["Batman #1"]) == 0


class TestComicIdentifyStdinMode:
    def test_reads_title_from_stdin_when_arg_omitted(self, monkeypatch, capsys):
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("Batman #1\n"))
        rc = comic_identify.main([])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["series"] == "Batman"
        assert data["issue"] == "1"

    def test_stdin_input_is_stripped(self, monkeypatch, capsys):
        """A trailing newline from a shell pipe must not leak into the title
        (it would otherwise survive into the .title field verbatim)."""
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("  Batman #1  \n"))
        comic_identify.main([])
        data = json.loads(capsys.readouterr().out)
        assert data["title"] == "Batman #1"


class TestComicIdentifyToDict:
    def test_identity_to_dict_drops_private_cache_field(self):
        from comic_identity import identify_comic

        identity = identify_comic("Batman #1")
        data = comic_identify.identity_to_dict(identity)
        assert "_title_norm" not in data
        assert data["series"] == "Batman"
        assert data["issue"] == "1"


class TestComicIdentifyBatchMode:
    """Batch mode (BUI-292): read newline-delimited titles from stdin, emit one
    JSONL object per line, so /comic:collection-add identifies many wins in one
    invocation instead of one process per title."""

    def _run_batch(self, monkeypatch, capsys, stdin_text):
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
        rc = comic_identify.main(["--batch"])
        out = capsys.readouterr().out
        lines = [ln for ln in out.splitlines() if ln.strip()]
        return rc, [json.loads(ln) for ln in lines]

    def test_batch_emits_one_line_per_title_in_order(self, monkeypatch, capsys):
        rc, rows = self._run_batch(
            monkeypatch, capsys, "Batman #1\nThe Amazing Spider-Man #300 (1988)\nX-Men #94\n"
        )
        assert rc == 0
        assert len(rows) == 3
        assert [r["series"] for r in rows] == ["Batman", "The Amazing Spider-Man", "X-Men"]
        assert [r["issue"] for r in rows] == ["1", "300", "94"]

    def test_batch_emits_one_row_per_line_including_blanks(self, monkeypatch, capsys):
        """1:1 line correspondence is the contract callers map on: a blank line
        must NOT be dropped (that would shift every later row), it yields a
        null-series row that keeps positions aligned."""
        rc, rows = self._run_batch(
            monkeypatch, capsys, "Batman #1\n\n   \nX-Men #94\n"
        )
        assert rc == 0
        assert len(rows) == 4  # 4 input lines -> 4 output rows, positions preserved
        assert rows[0]["series"] == "Batman"
        assert rows[1]["series"] is None  # blank line -> null-series row, not dropped
        assert rows[2]["series"] is None  # whitespace-only line, likewise
        assert rows[3]["series"] == "X-Men"

    def test_batch_reject_row_carries_reject_reasons(self, monkeypatch, capsys):
        """A rejectable title (CGC slab) is a normal return — it emits its own
        row with reject_reasons populated, in position."""
        rc, rows = self._run_batch(
            monkeypatch,
            capsys,
            "Batman #1\nAmazing Spider-Man #300 CGC 9.8\nX-Men #94\n",
        )
        assert rc == 0
        assert len(rows) == 3
        assert "CGC slab" in rows[1]["reject_reasons"]
        assert [r["series"] for r in rows] == ["Batman", "Amazing Spider-Man", "X-Men"]

    def test_batch_one_raising_title_does_not_abort_the_batch(
        self, monkeypatch, capsys
    ):
        """The isolation contract: if identify_comic raises on one line, the
        batch emits an error row for it and still processes the rest — it does
        not abort mid-stream (which would silently drop every later win)."""
        import comic_identify as ci

        real = ci.identify_comic

        def flaky(title):
            if "BOOM" in title:
                raise ValueError("synthetic parse failure")
            return real(title)

        monkeypatch.setattr(ci, "identify_comic", flaky)
        rc, rows = self._run_batch(
            monkeypatch, capsys, "Batman #1\nBOOM #99\nX-Men #94\n"
        )
        assert rc == 0
        assert len(rows) == 3  # all three lines emitted despite the middle raise
        assert rows[0]["series"] == "Batman"
        assert rows[1]["series"] is None and "error" in rows[1]  # error row, aligned
        assert rows[2]["series"] == "X-Men"  # line after the raise still processed

    def test_batch_output_is_jsonl_one_object_per_line(self, monkeypatch, capsys):
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("Batman #1\nX-Men #94\n"))
        comic_identify.main(["--batch"])
        out = capsys.readouterr().out
        # Exactly two non-empty lines, each independently parseable.
        assert out.count("\n") == 2
        for ln in out.splitlines():
            json.loads(ln)


class TestComicIdentifyVariantText:
    """variant_text (BUI-295): a short canonical distribution-variant label
    projected into the CLI output so collection-add reads it directly."""

    def test_newsstand_variant_detected(self, capsys):
        comic_identify.main(["Ghost Rider #1 Marvel 1973 Newsstand"])
        data = json.loads(capsys.readouterr().out)
        assert data["variant_text"] == "Newsstand"

    def test_direct_edition_variant_detected(self, capsys):
        comic_identify.main(["Amazing Spider-Man #300 Direct Edition"])
        data = json.loads(capsys.readouterr().out)
        assert data["variant_text"] == "Direct Edition"

    def test_no_variant_yields_empty_string(self, capsys):
        comic_identify.main(["Batman #1"])
        data = json.loads(capsys.readouterr().out)
        assert data["variant_text"] == ""

    def test_director_substring_does_not_false_match_direct(self, capsys):
        """'Director's Cut' must not be read as a Direct-edition variant."""
        comic_identify.main(["Spawn #1 Director's Cut"])
        data = json.loads(capsys.readouterr().out)
        assert data["variant_text"] == ""

    def test_bare_direct_filler_is_not_a_variant(self, capsys):
        """Bare 'direct' as listing filler ('ships direct', 'buy direct') must
        NOT be labeled Direct Edition — a false variant label corrupts the
        recorded collection. The qualifier (edition/market/sales) is required."""
        for title in [
            "Amazing Spider-Man #300 ships direct from estate",
            "Amazing Spider-Man #300 buy direct",
        ]:
            comic_identify.main([title])
            data = json.loads(capsys.readouterr().out)
            assert data["variant_text"] == "", title

    def test_direct_market_and_sales_qualifiers_detected(self, capsys):
        for title in ["X-Men #1 Direct Market", "X-Men #1 Direct Sales"]:
            comic_identify.main([title])
            data = json.loads(capsys.readouterr().out)
            assert data["variant_text"] == "Direct Edition", title

    def test_newsstand_hyphenated_spelling_detected(self, capsys):
        """'news-stand' (hyphenated) is a real listing spelling and must be
        caught, same as 'newsstand' / 'news stand'."""
        comic_identify.main(["Batman #1 news-stand copy"])
        data = json.loads(capsys.readouterr().out)
        assert data["variant_text"] == "Newsstand"

    def test_variant_text_present_in_batch_rows(self, monkeypatch, capsys):
        import io

        monkeypatch.setattr(
            "sys.stdin", io.StringIO("Ghost Rider #1 Newsstand\nBatman #1\n")
        )
        comic_identify.main(["--batch"])
        rows = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert rows[0]["variant_text"] == "Newsstand"
        assert rows[1]["variant_text"] == ""


class TestComicIdentifyEditionInJson:
    """BUI-426: the CLI JSON must expose `edition` so the win pipeline can carry
    the annual/king-size/giant-size qualifier through record-win-prep to the
    resolver. Dropping it filed "Uncanny X-Men Annual 6" as the Silver-Age
    regular "The X-Men #6" — a different, valuable book falsely claimed owned.
    These lock the UPSTREAM signal the fix depends on.
    """

    def test_edition_field_present_in_json(self, capsys):
        comic_identify.main(["Batman #1"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "single-issue"

    def test_uncanny_xmen_annual_6_ticket_case(self, capsys):
        comic_identify.main(["Uncanny X-men Annual 6 Marvel 1982 Dracula"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "annual"
        assert data["issue"] == "6"
        assert data["year"] == 1982
        # The qualifier is stripped OUT of the series text (it nests in the
        # parent series' full_title) — the whole reason `edition` must survive.
        assert "annual" not in data["series"].lower()

    def test_uncanny_xmen_annual_10_ticket_case(self, capsys):
        comic_identify.main(["Uncanny X-men Annual 10 Marvel 1986 1st X-Babies"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "annual"
        assert data["issue"] == "10"
        assert data["year"] == 1986

    def test_fantastic_four_annual_4_ticket_case(self, capsys):
        comic_identify.main(["Fantastic Four Annual #4 1st SA GA Human Torch"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "annual"
        assert data["issue"] == "4"
        assert data["series"] == "Fantastic Four"

    def test_giant_size_edition_in_json(self, capsys):
        comic_identify.main(["Giant-Size X-Men #1 Marvel 1975"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "giant-size"
        # Giant-Size stays IN the series (LOCG's own series) — a distinct
        # identity already, so it never collides with regular X-Men #1.
        assert data["series"] == "Giant-Size X-Men"

    def test_edition_present_in_batch_rows(self, monkeypatch, capsys):
        import io

        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO("Uncanny X-men Annual 6 Marvel 1982\nBatman #1\n"),
        )
        comic_identify.main(["--batch"])
        rows = [
            json.loads(ln)
            for ln in capsys.readouterr().out.splitlines()
            if ln.strip()
        ]
        assert rows[0]["edition"] == "annual"
        assert rows[1]["edition"] == "single-issue"


class TestComicIdentifyAnnualAdjacency:
    """BUI-450: a REGULAR listing that merely contains the standalone word
    "annual" for an unrelated reason must NOT be classified edition="annual".

    Before this fix the detector keyed off a bare ``\\bannual\\b`` match anywhere
    in the title, so "ASM #252 annual sale" became edition="annual" and — after
    BUI-426's qualifier threading — the win pipeline filed it as the nonexistent
    "ASM Annual #252" instead of the regular #252. The discriminator: a genuine
    annual writes its own sequence number AFTER the word ("Annual #N" / "Annual
    N"), whereas the false-positive class has the regular issue number sitting
    BEFORE an unrelated "annual". So the number must FOLLOW "annual" to classify.
    Severity is low/recoverable (real annuals have low issue numbers, so no
    valuable key is falsely claimed) — these lock the tightened boundary in both
    directions.
    """

    # --- false positives that must now be REGULAR (single-issue) ------------
    def test_annual_sale_ticket_case_is_regular(self, capsys):
        """The ticket's exact example: 'annual' is a stray word (a 'sale'),
        with the regular issue number BEFORE it, not an annual's number after."""
        comic_identify.main(["ASM #252 annual sale"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "single-issue"
        assert data["issue"] == "252"

    def test_number_before_annual_is_regular(self, capsys):
        """Direction matters: a number PRECEDING 'annual' (with a non-number
        word after) is the false-positive shape, not a genuine annual."""
        comic_identify.main(["Amazing Spider-Man #252 Annual Clearance Sale"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "single-issue"
        assert data["issue"] == "252"

    def test_stray_annual_word_after_issue_is_regular(self, capsys):
        comic_identify.main(["Batman #400 3rd annual event"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "single-issue"
        assert data["issue"] == "400"

    def test_bare_annual_no_number_is_not_classified_annual(self, capsys):
        """A title whose only 'annual' has no following number can't resolve to
        a wrong regular issue (issue is None), so classifying it single-issue is
        safe — and avoids the mis-file. (Behavioral nuance of the BUI-450 fix.)"""
        comic_identify.main(["Amazing Spider-Man Annual"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "single-issue"
        assert data["issue"] is None

    # --- genuine annuals that must STILL classify as annual -----------------
    def test_genuine_annual_hash_number(self, capsys):
        comic_identify.main(["Amazing Spider-Man Annual #1"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "annual"
        assert data["issue"] == "1"
        assert "annual" not in (data["series"] or "").lower()

    def test_genuine_annual_bare_number(self, capsys):
        comic_identify.main(["X-Men Annual 2"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "annual"
        assert data["issue"] == "2"

    def test_genuine_annual_leading_word(self, capsys):
        comic_identify.main(["Annual #14"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "annual"
        assert data["issue"] == "14"

    def test_genuine_annual_number_after_year_prefix(self, capsys):
        """A year before the word doesn't break it — the annual's own number
        still FOLLOWS 'annual' ('... Annual #1')."""
        comic_identify.main(["Star Wars 2021 Annual #1"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "annual"
        assert data["issue"] == "1"

    # --- BUI-456 case 1: promo/year digit right after "annual" --------------
    # A "#N" issue token BEFORE "annual" means the digit AFTER it is a promo
    # year, not a real annual number. Guarded so an existing issue number is not
    # reclassified as a nonexistent "<Series> Annual #N".
    def test_promo_year_after_annual_is_regular(self, capsys):
        """The ticket's exact case: '#252 annual 2024 sale' — the \\d matched the
        promo year 2024, but #252 is the real issue. Must stay single-issue."""
        comic_identify.main(["ASM #252 annual 2024 sale"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "single-issue"
        assert data["issue"] == "252"

    def test_hash_issue_before_annual_with_trailing_number_is_regular(self, capsys):
        """Generalizes the guard: any #-issue token before 'annual' demotes it,
        even when a real-looking small number follows ('#300 annual 5 left')."""
        comic_identify.main(["Uncanny X-Men #300 annual 5 left"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "single-issue"
        assert data["issue"] == "300"

    def test_marketing_hash_before_genuine_annual_is_accepted_false_negative(self, capsys):
        """ACCEPTED trade-off (BUI-456 case-1 guard): a listing that leads with a
        marketing '#N' and then carries a REAL annual is demoted to single-issue.
        This is the recoverable duplicate-buy direction and rarer than the misfile
        the guard prevents — locked here so the trade-off is explicit, not a
        surprise. If the guard is ever revisited, update this expectation."""
        comic_identify.main(["#1 seller Amazing Spider-Man Annual #1"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "single-issue"
        assert data["issue"] == "1"

    # --- BUI-456 case 2: non-space / non-"#" separators ---------------------
    # Genuine annuals whose number is joined by "No.", ":" or "-" must still
    # classify as annual (the separator class was broadened beyond "\\s*#?\\s*").
    def test_genuine_annual_no_dot_separator(self, capsys):
        comic_identify.main(["Amazing Spider-Man Annual No. 1"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "annual"
        assert data["issue"] == "1"
        # BUI-460: the "No." separator token must not survive into the series
        # text — it's classifier residue, not part of the series name.
        assert data["series"] == "Amazing Spider-Man"

    def test_genuine_annual_no_dot_missing_dot_separator(self, capsys):
        """The dot is optional — 'Annual No 1' classifies too."""
        comic_identify.main(["Amazing Spider-Man Annual No 1"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "annual"
        assert data["issue"] == "1"
        assert data["series"] == "Amazing Spider-Man"

    def test_genuine_annual_colon_separator(self, capsys):
        comic_identify.main(["X-Men Annual: 1"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "annual"
        assert data["issue"] == "1"
        # BUI-460: was "X-Men :" before the trailing-separator strip.
        assert data["series"] == "X-Men"

    def test_genuine_annual_hyphen_hash_separator(self, capsys):
        comic_identify.main(["Avengers Annual-#5"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "annual"
        assert data["issue"] == "5"
        # BUI-460: was "Avengers -" before the trailing-separator strip.
        assert data["series"] == "Avengers"

    def test_stray_word_after_separator_is_still_regular(self, capsys):
        """The number is still REQUIRED after the broadened separator — a colon
        followed by a non-number word must not become an annual."""
        comic_identify.main(["X-Men Annual: Special Issue"])
        data = json.loads(capsys.readouterr().out)
        assert data["edition"] == "single-issue"


class TestComicIdentifyAnnualSeparatorStrip:
    """BUI-460: BUI-456 broadened the annual separator to accept "No.", ":",
    "-" so these titles now correctly classify edition="annual" — but the
    separator itself was left dangling on the series text once the word
    "annual" is stripped ("X-Men Annual: 1" -> series "X-Men :"). This matters
    because collection_cache._normalize_series_key (packages/locg-cli) only
    strips year/vol/article decoration, not stray punctuation, so the
    un-cleaned series would thread to the wrong norm_key ("x-men :" instead of
    "x-men") and miss the local series_name_index, falling through to
    Metron/manual resolution instead. See
    test_normalize_series_key_does_not_strip_stray_punctuation in
    packages/locg-cli/tests/test_collection_cache.py for the locg-side half
    of this contract.
    """

    # --- the three example titles from the ticket, exact cleaned series ----
    def test_colon_separator_series_is_clean(self, capsys):
        comic_identify.main(["X-Men Annual: 1"])
        data = json.loads(capsys.readouterr().out)
        assert data["series"] == "X-Men"
        assert data["issue"] == "1"
        assert data["edition"] == "annual"

    def test_hyphen_hash_separator_series_is_clean(self, capsys):
        comic_identify.main(["Avengers Annual-#5"])
        data = json.loads(capsys.readouterr().out)
        assert data["series"] == "Avengers"
        assert data["issue"] == "5"
        assert data["edition"] == "annual"

    def test_no_dot_separator_series_is_clean(self, capsys):
        """'Annual No. 1' left the weak stray 'No.' token before the fix."""
        comic_identify.main(["Amazing Spider-Man Annual No. 1"])
        data = json.loads(capsys.readouterr().out)
        assert data["series"] == "Amazing Spider-Man"
        assert data["issue"] == "1"
        assert data["edition"] == "annual"

    # --- threaded norm_key ---------------------------------------------------
    def test_cleaned_series_normalizes_to_expected_key(self, capsys):
        """The cleaned series string threads to the SAME norm_key a plain
        "X-Men Annual #1"-style listing would produce — no stray-punctuation
        drift. This mirrors locg.collection_cache._normalize_series_key
        inline (apps/ebay is not a workspace member and cannot import
        packages/locg-cli — same boundary noted near comic_identity.py's
        series_year_range, "Series-volume (era) disambiguation" section)
        since only strip/lower is needed to observe the pre-fix vs. post-fix
        key difference; the full contract (that _normalize_series_key itself
        does no punctuation stripping) is locked on the locg side in
        test_normalize_series_key_does_not_strip_stray_punctuation.
        """

        def normalize_series_key(series_name: str) -> str:
            return series_name.strip().lower()

        for title, expected_key in [
            ("X-Men Annual: 1", "x-men"),
            ("Avengers Annual-#5", "avengers"),
            ("Amazing Spider-Man Annual No. 1", "amazing spider-man"),
        ]:
            comic_identify.main([title])
            data = json.loads(capsys.readouterr().out)
            assert normalize_series_key(data["series"]) == expected_key

    # --- non-regression: the accepted BUI-129/BUI-456 non-goal --------------
    def test_bare_number_before_annual_unaffected(self, capsys):
        """'ASM 252 annual 2024' (no '#') is a DELIBERATE accepted non-goal —
        distinguishing a bare issue number from a bare volume/promo year would
        reintroduce the BUI-129 hazard. This fix is confined to punctuation
        cleanup and must not change which edition/issue this resolves to."""
        comic_identify.main(["ASM 252 annual 2024"])
        data = json.loads(capsys.readouterr().out)
        assert data["series"] == "ASM"
        assert data["issue"] == "252"
        assert data["edition"] == "annual"

    # --- adversarial: a real trailing "No"/"no" word in the series name -----
    # Caught in review: an earlier draft stripped separator-looking characters
    # from wherever the series text happened to END, with no anchor to the
    # word "annual" itself. That is ambiguous — after "annual" is deleted, a
    # real word "No" that PRECEDES "annual" in the title (part of the actual
    # series name) and a genuine "No."/"No" separator that FOLLOWS "annual"
    # (real residue, as in "Annual No. 1") look identical from the end of the
    # string alone. The fix anchors the strip to "\bannual\b" and only
    # extends forward, so a real trailing "No" is never touched.
    def test_series_legitimately_ending_in_no_is_preserved(self, capsys):
        comic_identify.main(["Just Say No Annual #1"])
        data = json.loads(capsys.readouterr().out)
        assert data["series"] == "Just Say No"
        assert data["issue"] == "1"
        assert data["edition"] == "annual"

    def test_one_word_series_no_is_not_wiped(self, capsys):
        """A one-word series that IS 'No' must not be erased entirely."""
        comic_identify.main(["NO Annual #1"])
        data = json.loads(capsys.readouterr().out)
        assert data["series"] == "NO"
        assert data["issue"] == "1"
        assert data["edition"] == "annual"

    def test_double_annual_mention_still_fully_stripped(self, capsys):
        """A pathological double mention of 'annual' — the trailing-anchored
        pass only reaches the LAST occurrence, so the word-only strip must
        still run afterward to catch the other one, matching pre-BUI-460
        (global-strip) behavior rather than narrowing it."""
        comic_identify.main(["X-Men Annual Annual #1"])
        data = json.loads(capsys.readouterr().out)
        assert data["series"] == "X-Men"
        assert data["issue"] == "1"
        assert data["edition"] == "annual"
