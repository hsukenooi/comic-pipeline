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
