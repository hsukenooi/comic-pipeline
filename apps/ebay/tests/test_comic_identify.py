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
