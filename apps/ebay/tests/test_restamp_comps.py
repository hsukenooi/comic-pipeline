"""Tests for restamp_comps.py — the BUI-998/BUI-1008 client that recomputes
the comps ledger's grade/label/page_quality against today's parsers and
posts only the diff to `POST /api/comics/comps/restamp`.

Diff-logic tests use the REAL `sold_comps.parse_grade`/`parse_slab_fields` —
the point is to prove this script's diff computation agrees with the actual
parsers, not a stand-in for them (both real titles below are pinned corpus
examples from test_sold_comps.py: the BUI-1003 F/VF regression and a real
BUI-929 spike title). Everything HTTP-shaped is faked (MagicMock/patch) —
this script must never touch a real server in tests.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import restamp_comps as rc


# ---------------------------------------------------------------------------
# _grade_diff — BUI-1008
# ---------------------------------------------------------------------------


class TestGradeDiff:
    def test_stored_grade_matches_current_parser_no_diff(self):
        row = {"id": 1, "title": "Amazing Spider-Man #300 CGC 9.8", "grade": 9.8}
        item, unresolved = rc._grade_diff(row)
        assert item is None
        assert unresolved is None

    def test_bui1003_split_grade_regression_produces_a_diff(self):
        """Real BUI-1003 corpus title: pre-fix parse_grade read this as bare
        VF (8.0); the fix reads F/VF as 7.0. A row stored at the OLD wrong
        value must diff against the NEW correct one."""
        row = {
            "id": 42,
            "title": "Amazing Spider-Man #300 F/VF, 1st full Venom, Todd McFarlane",
            "grade": 8.0,
        }
        item, unresolved = rc._grade_diff(row)
        assert item == {"id": 42, "field": "grade", "expected": 8.0, "new": 7.0}
        assert unresolved is None

    def test_a_previously_ungraded_row_that_now_parses_is_a_diff(self):
        row = {"id": 7, "title": "Amazing Spider-Man #300 F/VF", "grade": None}
        item, unresolved = rc._grade_diff(row)
        assert item == {"id": 7, "field": "grade", "expected": None, "new": 7.0}
        assert unresolved is None

    def test_no_grade_anywhere_is_not_a_diff(self):
        row = {"id": 9, "title": "Amazing Spider-Man #300 lot bundle", "grade": None}
        item, unresolved = rc._grade_diff(row)
        assert item is None
        assert unresolved is None

    def test_stored_grade_with_no_current_parse_is_reported_not_diffed(self):
        """The one case that must NEVER become a restamp item: a stored,
        non-null grade whose title no longer parses to anything. Writing
        `new: None` would erase a real stored grade."""
        row = {"id": 13, "title": "totally ungradeable junk", "grade": 6.0}
        item, unresolved = rc._grade_diff(row)
        assert item is None
        assert unresolved is row


# ---------------------------------------------------------------------------
# _slab_diffs — BUI-998
# ---------------------------------------------------------------------------


class TestSlabDiffs:
    def test_matching_label_and_page_quality_produce_no_diffs(self):
        row = {
            "id": 1, "title": "Ultimate Fallout 4 CGC 9.8 Marvel Comics 2011",
            "certifier": "cgc", "label": "universal", "page_quality": "unknown",
        }
        assert rc._slab_diffs(row) == []

    def test_bui998_missing_page_quality_produces_exactly_one_diff(self):
        """Real BUI-929 spike title. This pins BUI-998's own bug shape: a
        title that names OW/W pages, stored as `page_quality='unknown'`
        because the include_graded-only writer never called
        parse_slab_fields at all before the fix."""
        row = {
            "id": 5, "title": "Batman #227 1970 CGC 8.5 VF+ OW/W",
            "certifier": "cgc", "label": "universal", "page_quality": "unknown",
        }
        diffs = rc._slab_diffs(row)
        assert diffs == [
            {"id": 5, "field": "page_quality", "expected": "unknown", "new": "ow_w"},
        ]

    def test_label_and_page_quality_can_both_diff(self):
        row = {
            "id": 8,
            "title": "X-MEN #13 CGC 6.0 OW-W 1965 KIRBY, Goldberg autograph/signature 2nd JUGGERNAUT",
            "certifier": "cgc", "label": "universal", "page_quality": "unknown",
        }
        diffs = rc._slab_diffs(row)
        by_field = {d["field"]: d["new"] for d in diffs}
        assert by_field == {"label": "qualified", "page_quality": "ow_w"}

    def test_certifier_is_never_included_in_slab_diffs(self):
        """BUI-997 already fixed certifier; this script must not re-touch it
        even if (hypothetically) it disagreed."""
        row = {
            "id": 2, "title": "Ultimate Fallout 4 CGC 9.8 Marvel Comics 2011",
            "certifier": "other", "label": "universal", "page_quality": "unknown",
        }
        diffs = rc._slab_diffs(row)
        assert all(d["field"] != "certifier" for d in diffs)


# ---------------------------------------------------------------------------
# _iter_all_comps — pagination over a fake HTTP layer
# ---------------------------------------------------------------------------


class TestIterAllComps:
    def test_pages_until_an_empty_page_using_the_last_id_as_cursor(self):
        pages = {
            0: [{"id": 1}, {"id": 2}],
            2: [{"id": 3}],
            3: [],
        }

        def fake_get(url, params, timeout):
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = pages[params["after_id"]]
            return resp

        with patch("restamp_comps.requests.get", side_effect=fake_get) as get:
            rows = list(rc._iter_all_comps("http://x", page_size=2))
        assert [r["id"] for r in rows] == [1, 2, 3]
        assert get.call_count == 3

    def test_empty_first_page_yields_nothing(self):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = []
        with patch("restamp_comps.requests.get", return_value=resp):
            assert list(rc._iter_all_comps("http://x")) == []


# ---------------------------------------------------------------------------
# main() — end to end over a fake HTTP layer
# ---------------------------------------------------------------------------


_RAW_ROW = {
    "id": 1, "pool": "raw",
    "title": "Amazing Spider-Man #300 F/VF, 1st full Venom, Todd McFarlane",
    "grade": 8.0,
}
_SLAB_ROW = {
    "id": 2, "pool": "slab", "title": "Batman #227 1970 CGC 8.5 VF+ OW/W",
    "certifier": "cgc", "label": "universal", "page_quality": "unknown",
}
_UNGRADEABLE_ROW = {
    "id": 3, "pool": "raw", "title": "totally ungradeable junk", "grade": 6.0,
}


def _fake_get_one_page(rows):
    """A `requests.get` stand-in that serves `rows` once (after_id=0) then an
    empty page forever after — enough for one `_scan` pass."""
    def fake_get(url, params=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        if url.endswith("/health"):
            resp.json.return_value = {"status": "ok"}
            return resp
        resp.json.return_value = rows if params.get("after_id") == 0 else []
        return resp
    return fake_get


class TestMainDryRunDefault:
    def test_dry_run_posts_dry_run_true_and_does_not_rescan(self, monkeypatch, capsys):
        monkeypatch.setenv("COMICS_SERVER_URL", "http://mac-mini.example:8080")
        get_fn = _fake_get_one_page([_RAW_ROW, _SLAB_ROW, _UNGRADEABLE_ROW])
        post_resp = MagicMock()
        post_resp.raise_for_status.return_value = None
        post_resp.json.return_value = {
            "matched": 2, "changed": 2, "skipped_stale": 0, "not_found": 0,
        }
        with patch("restamp_comps.requests.get", side_effect=get_fn) as get, \
             patch("restamp_comps.requests.post", return_value=post_resp) as post:
            rc.main([])

        assert post.call_count == 1
        assert post.call_args[1]["json"]["dry_run"] is True
        # Only the BEFORE scan runs in dry-run mode — no AFTER re-read. One
        # scan pages twice (a data page, then the empty page that ends the
        # cursor) — the assertion is on SCANS, not raw GET-call count.
        comps_all_calls = [c for c in get.call_args_list
                            if c.args[0].endswith("/comps/all")]
        assert len(comps_all_calls) == 2  # one full scan, not re-run

        out = capsys.readouterr()
        assert "BEFORE: grade diffs=1 label diffs=0 page_quality diffs=1 total=2" in out.out
        assert "DRY RUN" in out.out

    def test_ungradeable_row_is_reported_and_never_posted(self, monkeypatch, capsys):
        monkeypatch.setenv("COMICS_SERVER_URL", "http://mac-mini.example:8080")
        get_fn = _fake_get_one_page([_UNGRADEABLE_ROW])
        with patch("restamp_comps.requests.get", side_effect=get_fn), \
             patch("restamp_comps.requests.post") as post:
            rc.main([])
        post.assert_not_called()  # nothing to restamp
        err = capsys.readouterr().err
        assert "1 raw row(s) carry a stored grade" in err
        assert "[3]" in err


class TestMainApply:
    def test_apply_posts_dry_run_false_and_rescans_for_after_count(self, monkeypatch, capsys):
        monkeypatch.setenv("COMICS_SERVER_URL", "http://mac-mini.example:8080")
        call_count = {"n": 0}

        def fake_get(url, params=None, timeout=None):
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            if url.endswith("/health"):
                resp.json.return_value = {"status": "ok"}
                return resp
            if params.get("after_id") != 0:
                resp.json.return_value = []
                return resp
            call_count["n"] += 1
            # First full scan (BEFORE) sees the stale grade; after apply the
            # server would have written 7.0 — simulate that by having the
            # SECOND scan return the corrected row.
            if call_count["n"] == 1:
                resp.json.return_value = [_RAW_ROW]
            else:
                resp.json.return_value = [{**_RAW_ROW, "grade": 7.0}]
            return resp

        post_resp = MagicMock()
        post_resp.raise_for_status.return_value = None
        post_resp.json.return_value = {
            "matched": 1, "changed": 1, "skipped_stale": 0, "not_found": 0,
        }
        with patch("restamp_comps.requests.get", side_effect=fake_get), \
             patch("restamp_comps.requests.post", return_value=post_resp) as post:
            rc.main(["--apply"])

        assert post.call_args[1]["json"]["dry_run"] is False
        assert call_count["n"] == 2  # BEFORE scan + AFTER re-scan
        out = capsys.readouterr().out
        assert "APPLIED" in out
        assert "AFTER (re-read from the server): grade diffs=0 label diffs=0 page_quality diffs=0" in out


class TestMainServerResolution:
    def test_fails_loudly_when_server_url_unset(self, monkeypatch):
        monkeypatch.delenv("COMICS_SERVER_URL", raising=False)
        monkeypatch.delenv("GIXEN_SERVER_URL", raising=False)
        with pytest.raises(SystemExit):
            rc.main([])

    def test_fails_loudly_when_server_unreachable(self, monkeypatch):
        monkeypatch.setenv("COMICS_SERVER_URL", "http://mac-mini.example:8080")
        with patch(
            "restamp_comps.requests.get",
            side_effect=rc.requests.exceptions.ConnectionError("down"),
        ):
            with pytest.raises(SystemExit):
                rc.main([])

    def test_nothing_to_restamp_short_circuits_before_any_post(self, monkeypatch, capsys):
        monkeypatch.setenv("COMICS_SERVER_URL", "http://mac-mini.example:8080")
        get_fn = _fake_get_one_page([])
        with patch("restamp_comps.requests.get", side_effect=get_fn), \
             patch("restamp_comps.requests.post") as post:
            rc.main([])
        post.assert_not_called()
        assert "Nothing to restamp." in capsys.readouterr().out
