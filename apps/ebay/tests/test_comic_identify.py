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

    def test_batch_skips_blank_lines(self, monkeypatch, capsys):
        rc, rows = self._run_batch(
            monkeypatch, capsys, "Batman #1\n\n   \nX-Men #94\n"
        )
        assert rc == 0
        assert len(rows) == 2
        assert [r["series"] for r in rows] == ["Batman", "X-Men"]

    def test_batch_keeps_rejectable_title_and_does_not_drop_others(
        self, monkeypatch, capsys
    ):
        """A rejectable title (CGC slab) still emits its own line with
        reject_reasons — it must not swallow or abort the surrounding titles."""
        rc, rows = self._run_batch(
            monkeypatch,
            capsys,
            "Batman #1\nAmazing Spider-Man #300 CGC 9.8\nX-Men #94\n",
        )
        assert rc == 0
        assert len(rows) == 3
        assert "CGC slab" in rows[1]["reject_reasons"]
        assert [r["series"] for r in rows] == ["Batman", "Amazing Spider-Man", "X-Men"]

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

    def test_variant_text_present_in_batch_rows(self, monkeypatch, capsys):
        import io

        monkeypatch.setattr(
            "sys.stdin", io.StringIO("Ghost Rider #1 Newsstand\nBatman #1\n")
        )
        comic_identify.main(["--batch"])
        rows = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert rows[0]["variant_text"] == "Newsstand"
        assert rows[1]["variant_text"] == ""
