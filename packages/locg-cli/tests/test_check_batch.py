"""Tests for the mechanized collection-check executor (BUI-504).

All HTTP is mocked — no live comics server. The focus is the R11 money gate:
every failure path must raise CheckBatchError (→ exit 1, no verdicts), and the
advisory flags must FLAG the right rows without ever flipping a verdict.
"""
from __future__ import annotations

import json
import sys

import pytest
import requests

from locg import check_batch as cb
from locg.check_batch import (
    CheckBatchError,
    compute_flags,
    compute_verdict,
    parse_items_file,
    render_table,
    resolve_server_url,
    run_check_batch,
)


# ---------------------------------------------------------------------------
# Fake HTTP layer
# ---------------------------------------------------------------------------

_NO_JSON = object()


class FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        if self._json is _NO_JSON:
            raise ValueError("no JSON body")
        return self._json


class FakeSession:
    """Route requests to canned responses by URL substring.

    A route value may be a FakeResponse, an Exception instance (raised), or a
    callable ``(method, url, body) -> FakeResponse``.
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        return self._respond("GET", url, None)

    def post(self, url, json=None, **kwargs):
        return self._respond("POST", url, json)

    def _respond(self, method, url, body):
        self.calls.append((method, url, body))
        for key, resp in self.routes.items():
            if key in url:
                if isinstance(resp, Exception):
                    raise resp
                if callable(resp) and not isinstance(resp, FakeResponse):
                    return resp(method, url, body)
                return resp
        raise AssertionError(f"no fake route matched {method} {url}")


SERVER = "http://comics.test"


def _status(cache_age_days=3, pending_push_count=0, oldest_pending_days=None,
            last_full_import="2026-07-20T00:00:00Z"):
    return FakeResponse(200, {
        "last_full_import": last_full_import,
        "cache_age_days": cache_age_days,
        "pending_push_count": pending_push_count,
        "oldest_pending_days": oldest_pending_days,
    })


def _routes(*, status=None, batch=None, resolve=None, health=None):
    return {
        "/health": health or FakeResponse(200, {"status": "ok"}),
        "/collection/status": status or _status(),
        "/check/batch": batch or FakeResponse(200, {"count": 0, "results": []}),
        "/series-names/resolve": resolve or FakeResponse(200, {"results": []}),
    }


def _run(items, batch_results, *, status=None, resolve_results=None):
    routes = _routes(
        status=status,
        batch=FakeResponse(200, {"count": len(batch_results), "results": batch_results}),
        resolve=FakeResponse(200, {"results": resolve_results or []}),
    )
    session = FakeSession(routes)
    payload = run_check_batch(items, server_url=SERVER, session=session)
    return payload, session


def _verdict_row(series, issue, match_status, **extra):
    """Build a batch result row echoing series/issue (as the server does)."""
    row = {
        "series": series,
        "issue": issue,
        "match_status": match_status,
        "full_title_matched": None,
        "matched_series_name": None,
        "matched_release_date": None,
        "match_kind": None,
        "in_wish_list": False,
        "printing_conflict": False,
        "cache_age_days": 3,
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# Verdict mapping
# ---------------------------------------------------------------------------

def test_verdict_in_collection():
    assert compute_verdict({"match_status": "in_collection"}, 3) == "✅ In collection"


def test_verdict_wishlisted():
    v = compute_verdict({"match_status": "not_in_cache", "in_wish_list": True}, 3)
    assert v.endswith("Wishlisted (not owned)")


def test_verdict_not_in_collection():
    assert compute_verdict(
        {"match_status": "not_in_cache", "in_wish_list": False}, 3
    ) == "❌ Not in collection"


def test_verdict_ambiguous_cross_volume():
    assert compute_verdict(
        {"match_status": "ambiguous_cross_volume"}, 3
    ) == "⚠️ Ambiguous (cross-volume)"


def test_verdict_stale_downgrade():
    v = compute_verdict({"match_status": "not_in_cache", "in_wish_list": False}, 20)
    assert "Not in cache" in v and "20 days stale" in v


def test_verdict_not_stale_at_threshold():
    # cache_age_days == 14 is NOT stale (strictly > 14).
    assert compute_verdict(
        {"match_status": "not_in_cache", "in_wish_list": False}, 14
    ) == "❌ Not in collection"


# ---------------------------------------------------------------------------
# Flag mapping — one test per pattern
# ---------------------------------------------------------------------------

def _codes(flags):
    return {f["pattern"] for f in flags}


def test_flag_pattern_a_giant_size():
    r = _verdict_row("Giant-Size Fantastic Four", "1", "in_collection", match_kind="exact")
    flags = compute_flags(r, {"series": "Giant-Size Fantastic Four", "issue": "1"}, {}, 3)
    assert "A" in _codes(flags)


def test_flag_pattern_a_annual():
    r = _verdict_row("X-Men Annual", "6", "in_collection", match_kind="exact")
    flags = compute_flags(r, {"series": "X-Men Annual", "issue": "6"}, {}, 3)
    assert "A" in _codes(flags)


def test_flag_pattern_a_space_variant():
    # "Giant Size" (space) must flag just like "Giant-Size" (hyphen).
    r = _verdict_row("Giant Size X-Men", "1", "in_collection", match_kind="exact")
    flags = compute_flags(r, {"series": "Giant Size X-Men", "issue": "1"}, {}, 3)
    assert "A" in _codes(flags)


def test_flag_pattern_d_alias():
    r = _verdict_row(
        "The Mighty Thor", "5", "in_collection", match_kind="alias",
        matched_series_name="Thor (Vol. 1) (1966 - 1996)", matched_release_date="1966-02-01",
    )
    flags = compute_flags(r, {"series": "The Mighty Thor", "issue": "5"}, {}, 3)
    assert "D" in _codes(flags)
    # D3 must NOT also fire on an alias match (elif branch).
    assert "D3" not in _codes(flags)


def test_flag_pattern_d2_cross_volume():
    r = _verdict_row(
        "Fantastic Four", "18", "ambiguous_cross_volume", match_kind="cross_volume",
        candidates=[
            {"series_name": "Fantastic Four (Vol. 1)", "full_title": "Fantastic Four #18"},
            {"series_name": "Fantastic Four (Vol. 7)", "full_title": "Fantastic Four #18"},
        ],
    )
    flags = compute_flags(r, {"series": "Fantastic Four", "issue": "18"}, {}, 3)
    codes = _codes(flags)
    assert "D2" in codes
    msg = next(f["message"] for f in flags if f["pattern"] == "D2")
    assert "Vol. 1" in msg and "Vol. 7" in msg


def test_flag_pattern_d3_no_year_rebootable():
    r = _verdict_row(
        "Uncanny X-Men", "179", "in_collection", match_kind="exact",
        matched_series_name="Uncanny X-Men (1981)",
    )
    # No year supplied → D3 fires.
    flags = compute_flags(r, {"series": "Uncanny X-Men", "issue": "179"}, {}, 3)
    assert "D3" in _codes(flags)


def test_flag_pattern_d3_suppressed_when_year_present():
    r = _verdict_row(
        "Uncanny X-Men", "179", "in_collection", match_kind="exact",
        matched_series_name="Uncanny X-Men (1981)",
    )
    flags = compute_flags(
        r, {"series": "Uncanny X-Men", "issue": "179", "year": "1984"}, {}, 3
    )
    assert "D3" not in _codes(flags)


def test_flag_pattern_d3_suppressed_for_non_rebootable():
    r = _verdict_row("Invincible", "1", "in_collection", match_kind="exact",
                     matched_series_name="Invincible (2021)")
    flags = compute_flags(r, {"series": "Invincible", "issue": "1"}, {}, 3)
    assert "D3" not in _codes(flags)


def test_flag_pattern_e_printing_conflict():
    r = _verdict_row(
        "Absolute Martian Manhunter", "1", "in_collection", match_kind="exact",
        full_title_matched="Absolute Martian Manhunter #1 (2nd Printing)",
        printing_conflict=True,
        printing_candidates=[
            {"printing_ordinal": 1, "in_collection": False, "in_wish_list": True},
            {"printing_ordinal": 2, "in_collection": True, "in_wish_list": False},
        ],
    )
    flags = compute_flags(r, {"series": "Absolute Martian Manhunter", "issue": "1"}, {}, 3)
    assert "E" in _codes(flags)
    msg = next(f["message"] for f in flags if f["pattern"] == "E")
    assert "base: wishlisted" in msg and "2nd printing: owned" in msg


def test_flag_pattern_c_fuzzy_resolution():
    r = _verdict_row("Xmen", "179", "not_in_cache")
    resolve_map = {"Xmen": {"query": "Xmen", "resolved": "Uncanny X-Men", "match_kind": "fuzzy"}}
    flags = compute_flags(r, {"series": "Xmen", "issue": "179"}, resolve_map, 3)
    assert "C" in _codes(flags)
    assert "Uncanny X-Men" in next(f["message"] for f in flags if f["pattern"] == "C")


def test_flag_pattern_c_not_flagged_on_exact_resolution():
    # An exact resolve means the same normalized key the matcher already used —
    # a genuine not-owned, not a spelling-drift miss. No flag.
    r = _verdict_row("Amazing Spider-Man", "300", "not_in_cache")
    resolve_map = {
        "Amazing Spider-Man": {
            "query": "Amazing Spider-Man",
            "resolved": "The Amazing Spider-Man (1963 - 1998)",
            "match_kind": "exact",
        }
    }
    flags = compute_flags(r, {"series": "Amazing Spider-Man", "issue": "300"}, resolve_map, 3)
    assert "C" not in _codes(flags)


def test_flag_pattern_c_not_flagged_on_null_resolution():
    r = _verdict_row("Nonexistent Series", "1", "not_in_cache")
    resolve_map = {"Nonexistent Series": {"query": "Nonexistent Series",
                                          "resolved": None, "match_kind": None}}
    flags = compute_flags(r, {"series": "Nonexistent Series", "issue": "1"}, resolve_map, 3)
    assert not flags


def test_flag_stale_note_on_wishlisted():
    r = _verdict_row("Batman", "608", "not_in_cache", in_wish_list=True)
    flags = compute_flags(r, {"series": "Batman", "issue": "608"}, {}, 20)
    assert "stale" in _codes(flags)


def test_no_flags_on_clean_in_collection():
    r = _verdict_row("Invincible", "1", "in_collection", match_kind="exact",
                     matched_series_name="Invincible (2021)")
    flags = compute_flags(r, {"series": "Invincible", "issue": "1", "year": "2021"}, {}, 3)
    assert flags == []


# ---------------------------------------------------------------------------
# End-to-end run_check_batch (happy paths)
# ---------------------------------------------------------------------------

def test_run_maps_verdicts_and_flags():
    items = [
        {"series": "Amazing Spider-Man", "issue": "300", "year": "1988"},
        {"series": "Uncanny X-Men", "issue": "179"},
    ]
    batch = [
        _verdict_row("Amazing Spider-Man", "300", "in_collection", match_kind="exact",
                     full_title_matched="Amazing Spider-Man #300",
                     matched_series_name="The Amazing Spider-Man (1963 - 1998)"),
        _verdict_row("Uncanny X-Men", "179", "not_in_cache"),
    ]
    payload, session = _run(items, batch)
    assert payload["count"] == 2
    assert payload["results"][0]["verdict"] == "✅ In collection"
    # ASM #300 has a year → no D3; exact → no D.
    assert payload["results"][0]["flags"] == []
    assert payload["results"][1]["verdict"] == "❌ Not in collection"
    # The batch item carried its year/variant forward for the caller.
    assert payload["results"][0]["year"] == "1988"


def test_run_calls_resolve_only_for_not_in_cache():
    items = [{"series": "Invincible", "issue": "1", "year": "2021"}]
    batch = [_verdict_row("Invincible", "1", "in_collection", match_kind="exact")]
    payload, session = _run(items, batch)
    # No not_in_cache rows → resolve endpoint never called.
    assert not any("series-names/resolve" in url for _, url, _ in session.calls)


def test_run_stale_banner_and_downgrade():
    items = [{"series": "Batman", "issue": "608"}]
    batch = [_verdict_row("Batman", "608", "not_in_cache")]
    payload, _ = _run(items, batch, status=_status(cache_age_days=16))
    assert "16 days stale" in payload["results"][0]["verdict"]
    assert any("Cache is 16 days old" in b for b in payload["banners"])


def test_run_pending_push_banner_escalates():
    items = [{"series": "Batman", "issue": "608"}]
    batch = [_verdict_row("Batman", "608", "not_in_cache")]
    payload, _ = _run(
        items, batch,
        status=_status(cache_age_days=3, pending_push_count=30, oldest_pending_days=25),
    )
    banner = next(b for b in payload["banners"] if "pending push" in b)
    assert banner.startswith("⚠️")
    assert "30 rows pending" in banner


def test_run_pattern_c_end_to_end():
    items = [{"series": "Xmen", "issue": "179"}]
    batch = [_verdict_row("Xmen", "179", "not_in_cache")]
    resolve = [{"query": "Xmen", "resolved": "Uncanny X-Men", "match_kind": "fuzzy"}]
    payload, _ = _run(items, batch, resolve_results=resolve)
    assert "C" in {f["pattern"] for f in payload["results"][0]["flags"]}


# ---------------------------------------------------------------------------
# Hard-fail paths (R11) — every one must raise, rendering NO verdicts
# ---------------------------------------------------------------------------

def test_hardfail_batch_non_200():
    routes = _routes(batch=FakeResponse(500, {"detail": "boom"}))
    with pytest.raises(CheckBatchError) as e:
        run_check_batch([{"series": "X", "issue": "1"}], server_url=SERVER,
                        session=FakeSession(routes))
    assert "500" in str(e.value)


def test_hardfail_batch_409_never_imported():
    routes = _routes(batch=FakeResponse(409, {"detail": "collection not imported"}))
    with pytest.raises(CheckBatchError) as e:
        run_check_batch([{"series": "X", "issue": "1"}], server_url=SERVER,
                        session=FakeSession(routes))
    assert "409" in str(e.value)


def test_hardfail_unreachable_server():
    routes = _routes(health=requests.ConnectionError("refused"))
    with pytest.raises(CheckBatchError) as e:
        run_check_batch([{"series": "X", "issue": "1"}], server_url=SERVER,
                        session=FakeSession(routes))
    assert "cannot reach" in str(e.value)


def test_hardfail_timeout():
    routes = _routes(health=requests.Timeout("slow"))
    with pytest.raises(CheckBatchError) as e:
        run_check_batch([{"series": "X", "issue": "1"}], server_url=SERVER,
                        session=FakeSession(routes))
    assert "timed out" in str(e.value)


def test_hardfail_status_non_200():
    routes = _routes(status=FakeResponse(503, {"detail": "unavailable"}))
    with pytest.raises(CheckBatchError):
        run_check_batch([{"series": "X", "issue": "1"}], server_url=SERVER,
                        session=FakeSession(routes))


def test_hardfail_never_imported_status_null():
    routes = _routes(status=_status(last_full_import=None, cache_age_days=None))
    with pytest.raises(CheckBatchError) as e:
        run_check_batch([{"series": "X", "issue": "1"}], server_url=SERVER,
                        session=FakeSession(routes))
    assert "never imported" in str(e.value)


def test_hardfail_health_non_200():
    routes = _routes(health=FakeResponse(502, {}))
    with pytest.raises(CheckBatchError):
        run_check_batch([{"series": "X", "issue": "1"}], server_url=SERVER,
                        session=FakeSession(routes))


def test_hardfail_resolve_non_200_aborts_whole_check():
    # A Pattern-C re-query failure is an R11 STOP, not a silently-dropped flag.
    items = [{"series": "Xmen", "issue": "179"}]
    batch = FakeResponse(200, {"count": 1, "results": [
        _verdict_row("Xmen", "179", "not_in_cache")]})
    routes = _routes(batch=batch, resolve=FakeResponse(500, {"detail": "boom"}))
    with pytest.raises(CheckBatchError) as e:
        run_check_batch(items, server_url=SERVER, session=FakeSession(routes))
    assert "resolve" in str(e.value)


def test_hardfail_unparseable_batch_body():
    routes = _routes(batch=FakeResponse(200, _NO_JSON))
    with pytest.raises(CheckBatchError):
        run_check_batch([{"series": "X", "issue": "1"}], server_url=SERVER,
                        session=FakeSession(routes))


def test_hardfail_result_count_mismatch():
    # Server returns fewer results than items → cannot correlate → STOP.
    items = [{"series": "A", "issue": "1"}, {"series": "B", "issue": "2"}]
    batch = FakeResponse(200, {"count": 1, "results": [
        _verdict_row("A", "1", "not_in_cache")]})
    routes = _routes(batch=batch)
    with pytest.raises(CheckBatchError) as e:
        run_check_batch(items, server_url=SERVER, session=FakeSession(routes))
    assert "correlate" in str(e.value)


def test_hardfail_result_order_mismatch():
    # Echoed series/issue don't line up with the request order → STOP, never a
    # silent mis-attribution of a verdict to the wrong comic.
    items = [{"series": "A", "issue": "1"}, {"series": "B", "issue": "2"}]
    batch = FakeResponse(200, {"count": 2, "results": [
        _verdict_row("B", "2", "in_collection"),
        _verdict_row("A", "1", "not_in_cache"),
    ]})
    routes = _routes(batch=batch)
    with pytest.raises(CheckBatchError) as e:
        run_check_batch(items, server_url=SERVER, session=FakeSession(routes))
    assert "order does not match" in str(e.value)


def test_hardfail_empty_items():
    with pytest.raises(CheckBatchError):
        run_check_batch([], server_url=SERVER, session=FakeSession(_routes()))


def test_hardfail_unknown_match_status():
    # A malformed verdict (missing/garbage match_status) must STOP, never
    # silently render as "not owned" and buy a duplicate.
    items = [{"series": "X", "issue": "1"}]
    batch = FakeResponse(200, {"count": 1, "results": [
        {"series": "X", "issue": "1", "match_status": "garbage", "in_wish_list": False}]})
    routes = _routes(batch=batch)
    with pytest.raises(CheckBatchError) as e:
        run_check_batch(items, server_url=SERVER, session=FakeSession(routes))
    assert "unknown match_status" in str(e.value)


def test_hardfail_missing_match_status():
    items = [{"series": "X", "issue": "1"}]
    batch = FakeResponse(200, {"count": 1, "results": [{"series": "X", "issue": "1"}]})
    routes = _routes(batch=batch)
    with pytest.raises(CheckBatchError):
        run_check_batch(items, server_url=SERVER, session=FakeSession(routes))


# ---------------------------------------------------------------------------
# parse_items_file
# ---------------------------------------------------------------------------

def test_parse_items_wrapped():
    items = parse_items_file(json.dumps({"items": [
        {"series": "ASM", "issue": "300", "year": "1988", "variant": "Newsstand"}]}))
    assert items == [{"series": "ASM", "issue": "300", "year": "1988", "variant": "Newsstand"}]


def test_parse_items_bare_list():
    items = parse_items_file(json.dumps([{"series": "ASM", "issue": "300"}]))
    assert items == [{"series": "ASM", "issue": "300"}]


def test_parse_items_omits_blank_year_and_variant():
    items = parse_items_file(json.dumps([{"series": "ASM", "issue": "300", "year": ""}]))
    assert items == [{"series": "ASM", "issue": "300"}]


def test_parse_items_coerces_numeric_issue():
    items = parse_items_file(json.dumps([{"series": "ASM", "issue": 300, "year": 1988}]))
    assert items == [{"series": "ASM", "issue": "300", "year": "1988"}]


def test_parse_items_rejects_missing_issue():
    with pytest.raises(CheckBatchError):
        parse_items_file(json.dumps([{"series": "ASM"}]))


def test_parse_items_rejects_empty():
    with pytest.raises(CheckBatchError):
        parse_items_file(json.dumps({"items": []}))


def test_parse_items_rejects_bad_json():
    with pytest.raises(CheckBatchError):
        parse_items_file("{not json")


# ---------------------------------------------------------------------------
# resolve_server_url
# ---------------------------------------------------------------------------

def test_resolve_prefers_comics_server_url():
    assert resolve_server_url({"COMICS_SERVER_URL": "http://a:8080/"}) == "http://a:8080"


def test_resolve_accepts_deprecated_alias(capsys):
    url = resolve_server_url({"GIXEN_SERVER_URL": "http://b:8080"})
    assert url == "http://b:8080"
    assert "deprecated" in capsys.readouterr().err


def test_resolve_comics_wins_over_alias():
    url = resolve_server_url({
        "COMICS_SERVER_URL": "http://canonical:8080",
        "GIXEN_SERVER_URL": "http://legacy:8080",
    })
    assert url == "http://canonical:8080"


def test_resolve_unknown_host_raises(monkeypatch):
    monkeypatch.setattr(cb.socket, "gethostname", lambda: "some-random-box")
    with pytest.raises(CheckBatchError):
        resolve_server_url({})


# ---------------------------------------------------------------------------
# render_table
# ---------------------------------------------------------------------------

def test_render_table_rows_and_banners():
    payload = {
        "cache_age_days": 3,
        "banners": ["⚠️ Cache is 3 days old"],
        "results": [
            {"series": "ASM", "issue": "300", "variant": None,
             "verdict": "❌ Not in collection", "full_title_matched": None,
             "matched_series_name": None, "flags": []},
            {"series": "Thor", "issue": "5", "variant": None,
             "verdict": "✅ In collection", "full_title_matched": "Thor #5",
             "matched_series_name": "Thor (Vol. 1)",
             "flags": [{"pattern": "D", "message": "alias match — confirm"}]},
        ],
    }
    out = render_table(payload)
    assert "| # | Comic | In Cache?" in out
    assert "ASM #300" in out
    assert "alias match — confirm" in out
    assert "3 days" in out
    assert "⚠️ Cache is 3 days old" in out


def test_render_table_variant_in_comic_cell():
    payload = {
        "cache_age_days": 5,
        "banners": [],
        "results": [
            {"series": "Uncanny X-Men", "issue": "179", "variant": "Newsstand",
             "verdict": "❌ Not in collection", "full_title_matched": None,
             "matched_series_name": None, "flags": []},
        ],
    }
    assert "Uncanny X-Men #179 (Newsstand)" in render_table(payload)


# ---------------------------------------------------------------------------
# CLI dispatch — exit codes (R11 in code)
# ---------------------------------------------------------------------------

def test_cli_hardfail_exits_1(monkeypatch, capsys):
    from locg.cli import main

    def boom(*a, **k):
        raise CheckBatchError("server unreachable")

    monkeypatch.setattr("locg.check_batch.run_check_batch", boom)
    monkeypatch.setattr("locg.check_batch.parse_items_file", lambda raw: [{"series": "X", "issue": "1"}])
    monkeypatch.setattr(sys, "stdin", _FakeStdin('{"items":[{"series":"X","issue":"1"}]}'))
    monkeypatch.setattr(sys, "argv", ["locg", "collection", "check-batch", "-"])

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    # No verdict/table rendered to stdout on a hard fail.
    assert "In Cache?" not in captured.out
    assert "unreachable" in captured.err


def test_cli_table_mode_prints_table(monkeypatch, capsys):
    from locg.cli import main

    payload = {
        "cache_age_days": 3, "banners": [],
        "results": [{"series": "ASM", "issue": "300", "variant": None,
                     "verdict": "❌ Not in collection", "full_title_matched": None,
                     "matched_series_name": None, "flags": []}],
    }
    monkeypatch.setattr("locg.check_batch.run_check_batch", lambda *a, **k: payload)
    monkeypatch.setattr("locg.check_batch.parse_items_file", lambda raw: [{"series": "ASM", "issue": "300"}])
    monkeypatch.setattr(sys, "stdin", _FakeStdin("[]"))
    monkeypatch.setattr(sys, "argv", ["locg", "collection", "check-batch", "-", "--table"])

    try:
        main()
    except SystemExit as e:
        assert e.code in (None, 0)
    captured = capsys.readouterr()
    assert "| # | Comic | In Cache?" in captured.out
    assert "ASM #300" in captured.out


def test_cli_json_mode_emits_json(monkeypatch, capsys):
    from locg.cli import main

    payload = {"count": 1, "cache_age_days": 3, "banners": [],
               "results": [{"series": "ASM", "issue": "300", "verdict": "❌ Not in collection",
                            "flags": []}]}
    monkeypatch.setattr("locg.check_batch.run_check_batch", lambda *a, **k: payload)
    monkeypatch.setattr("locg.check_batch.parse_items_file", lambda raw: [{"series": "ASM", "issue": "300"}])
    monkeypatch.setattr(sys, "stdin", _FakeStdin("[]"))
    monkeypatch.setattr(sys, "argv", ["locg", "collection", "check-batch", "-"])

    try:
        main()
    except SystemExit as e:
        assert e.code in (None, 0)
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["count"] == 1


class _FakeStdin:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data
