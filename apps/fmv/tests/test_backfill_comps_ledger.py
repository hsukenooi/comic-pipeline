"""BUI-661 — tests for the one-shot comps-ledger backfill.

The script under test lives in `apps/fmv/scripts/`, which is not on the test
pythonpath (`pyproject.toml` puts only `src` there) and is not shipped in the
wheel — so it is loaded by path, exactly the way an operator runs it. That
load also exercises the script's own `_load_live_sold_comps` bootstrap, which
is the whole import-boundary decision: if apps/ebay's live parser ever stops
being importable from a checkout, every test in this file fails loudly rather
than the backfill silently falling back to a stale copy of the parse rules.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_comps_ledger.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("backfill_comps_ledger", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bf = _load_script()
sc = bf._load_live_sold_comps()


# ---------------------------------------------------------------------------
# Fixture builders — minimal but real-shaped provider responses
# ---------------------------------------------------------------------------

def serpapi_result(product_id, title, price="12.50", sold_date="Aug 1, 2026"):
    return {
        "product_id": product_id,
        "title": title,
        "price": {"raw": f"${price}", "extracted": float(price)},
        "sold_date": sold_date,
        "buying_format": "Auction",
        "link": f"https://www.ebay.com/itm/{product_id}",
    }


def serpapi_response(nkw, results):
    return {
        "search_parameters": {"engine": "ebay", "_nkw": nkw, "show_only": "Sold"},
        "organic_results": results,
        "search_metadata": {"ebay_url": "https://www.ebay.com/sch/i.html"},
    }


def sold_comps_item(item_id, title, price="12.50", ended="2026-08-01"):
    return {
        "itemId": item_id,
        "title": title,
        "soldPrice": price,
        "endedAt": ended,
        "buyingFormat": "auction",
        "url": f"https://www.ebay.com/itm/{item_id}",
    }


def sold_comps_response(keyword, items):
    return {"keyword": keyword, "items": items, "page": 1, "totalItems": len(items)}


EXCLUDING_Q = '"Uncanny X-Men 94" 1975 marvel comics -cgc -cbcs -graded -slab'
INCLUSIVE_Q = '"Uncanny X-Men 94" 1975 marvel comics'


def write_cache(cache_dir: Path, response: dict, *, name: str | None = None,
                mtime: float | None = None) -> Path:
    """Write a response into a cache dir under its real digest-derived name."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    if name is None:
        if "items" in response:
            url = sc.canonical_sold_comps_url(response["keyword"])
        else:
            url = sc.canonical_serpapi_url(response["search_parameters"]["_nkw"])
        name = sc._cache_path(url).name
    path = cache_dir / name
    path.write_text(json.dumps(response), encoding="utf-8")
    if mtime is not None:
        import os
        os.utime(path, (mtime, mtime))
    return path


def capture_record(response, query, provider=None, timestamp=1_700_000_000.0,
                   validation=None, canonical_url=None):
    record = {
        "timestamp": timestamp,
        "provider": provider or (sc.PROVIDER_SOLD_COMPS if "items" in response
                                 else sc.PROVIDER_SERPAPI),
        "query": query,
        "canonical_url": canonical_url or "https://example.invalid/x",
        "response": response,
    }
    if validation is not None:
        record["validation"] = validation
    return record


def write_capture(capture_dir: Path, records, *, name="raw_responses.jsonl",
                  gzipped=False):
    capture_dir.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(r) + "\n" for r in records)
    path = capture_dir / name
    if gzipped:
        path.write_bytes(gzip.compress(body.encode("utf-8")))
    else:
        path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Corpus reading
# ---------------------------------------------------------------------------

def test_cache_recovers_query_and_provider_from_both_shapes(tmp_path):
    import collections
    cache = tmp_path / "cache"
    write_cache(cache, serpapi_response(EXCLUDING_Q, [serpapi_result("1", "X-Men #94")]))
    write_cache(cache, sold_comps_response(INCLUSIVE_Q, [sold_comps_item("2", "X-Men #94")]))
    stats = collections.Counter()
    responses = bf.read_cache(sc, cache, stats)
    by_provider = {r.provider: r for r in responses}
    assert by_provider[sc.PROVIDER_SERPAPI].query == EXCLUDING_Q
    assert by_provider[sc.PROVIDER_SOLD_COMPS].query == INCLUSIVE_Q
    assert stats["malformed"] == 0


def test_malformed_cache_file_is_counted_and_skipped_never_fatal(tmp_path):
    import collections
    cache = tmp_path / "cache"
    good = serpapi_response(EXCLUDING_Q, [serpapi_result("1", "X-Men #94")])
    write_cache(cache, good)
    cache.joinpath("torn.json").write_text('{"organic_results": [', encoding="utf-8")
    cache.joinpath("nolist.json").write_text('[1, 2, 3]', encoding="utf-8")
    cache.joinpath("noecho.json").write_text('{"organic_results": []}', encoding="utf-8")
    stats = collections.Counter()
    responses = bf.read_cache(sc, cache, stats)
    assert len(responses) == 1, "the one good file must still import"
    assert stats["malformed"] == 3
    assert stats["cache_files"] == 4


def test_absent_corpora_are_reported_not_fatal(tmp_path):
    import collections
    stats = collections.Counter()
    assert bf.read_cache(sc, tmp_path / "nope", stats) == []
    assert bf.read_capture(sc, tmp_path / "also-nope", stats) == []
    assert stats["cache_absent"] == 1
    assert stats["capture_absent"] == 1


def test_capture_reads_every_rotated_segment_oldest_first(tmp_path):
    """BUI-628: reading only the live segment would silently drop all rotated
    history, and a short read looks exactly like a small corpus."""
    import collections
    cap = tmp_path / "capture"
    write_capture(cap, [capture_record(serpapi_response('"A 1"', []), '"A 1"')],
                  name="raw_responses.2026-01-01T00-aaa.jsonl.gz", gzipped=True)
    write_capture(cap, [capture_record(serpapi_response('"B 1"', []), '"B 1"')],
                  name="raw_responses.2026-02-01T00-bbb.jsonl")
    # A retired segment mid-compression exists in BOTH forms; the plaintext
    # copy is the one known to be complete, so the .gz must be skipped.
    write_capture(cap, [capture_record(serpapi_response('"C 1"', []), '"C 1"')],
                  name="raw_responses.2026-03-01T00-ccc.jsonl")
    write_capture(cap, [capture_record(serpapi_response('"WRONG 1"', []), '"WRONG 1"')],
                  name="raw_responses.2026-03-01T00-ccc.jsonl.gz", gzipped=True)
    write_capture(cap, [capture_record(serpapi_response('"D 1"', []), '"D 1"')])

    assert [p.name for p in bf.capture_segments(cap)] == [
        "raw_responses.2026-01-01T00-aaa.jsonl.gz",
        "raw_responses.2026-02-01T00-bbb.jsonl",
        "raw_responses.2026-03-01T00-ccc.jsonl",
        "raw_responses.jsonl",
    ]
    stats = collections.Counter()
    responses = bf.read_capture(sc, cap, stats)
    assert [r.query for r in responses] == ['"A 1"', '"B 1"', '"C 1"', '"D 1"']
    assert stats["capture_segments"] == 4


def test_same_second_rotations_order_by_record_timestamp_not_segment_name(tmp_path):
    """BUI-680: segment names carry a UTC stamp only to the SECOND, plus a
    random collision-avoidance token — so two rotations inside one second sort
    by two random tokens, an order uncorrelated with which was retired first.
    Segment order therefore cannot carry KTD4's earliest-observation-wins.
    `read_capture` orders the RECORDS by their own captured timestamp instead,
    and that list order is exactly the order `main` iterates and POSTs in.
    """
    import collections
    cap = tmp_path / "capture"
    # One shared second; tokens picked so lexical order is the exact REVERSE
    # of the order the two segments were actually retired in.
    older = "raw_responses.20260804T120000Z-ffffffff.jsonl"
    newer = "raw_responses.20260804T120000Z-00000000.jsonl"
    write_capture(cap, [capture_record(serpapi_response('"First 1"', []), '"First 1"',
                                       timestamp=1_700_000_000.0)], name=older)
    write_capture(cap, [capture_record(serpapi_response('"Second 1"', []), '"Second 1"',
                                       timestamp=1_700_000_050.0)], name=newer)

    assert [p.name for p in bf.capture_segments(cap)] == [newer, older], (
        "pins the gap this test exists for: within a shared second the name "
        "sort compares random tokens, and here it puts the NEWER segment first"
    )
    stats = collections.Counter()
    responses = bf.read_capture(sc, cap, stats)
    assert [r.query for r in responses] == ['"First 1"', '"Second 1"'], (
        "the earlier observation must be presented first regardless of which "
        "way the segment names happened to sort"
    )
    assert [r.observed_at for r in responses] == [1_700_000_000.0, 1_700_000_050.0]


def test_equal_capture_timestamps_keep_a_stable_deterministic_order(tmp_path):
    """The BUI-680 sort must not make a re-run reorder itself: two records
    sharing a timestamp (the capture clock is coarse enough for a burst to do
    this) fall back on segment-then-line order, because the sort is stable."""
    import collections
    cap = tmp_path / "capture"
    write_capture(cap, [capture_record(serpapi_response('"A 1"', []), '"A 1"',
                                       timestamp=1_700_000_000.0)],
                  name="raw_responses.20260804T120000Z-aaaaaaaa.jsonl")
    write_capture(cap, [
        capture_record(serpapi_response('"B 1"', []), '"B 1"', timestamp=1_700_000_000.0),
        capture_record(serpapi_response('"C 1"', []), '"C 1"', timestamp=1_700_000_000.0),
    ], name="raw_responses.20260804T120000Z-bbbbbbbb.jsonl")

    order = [r.query for r in bf.read_capture(sc, cap, collections.Counter())]
    reread = [r.query for r in bf.read_capture(sc, cap, collections.Counter())]
    assert order == ['"A 1"', '"B 1"', '"C 1"']
    assert reread == order, "a second read must present the same order"


def test_shape_invalid_capture_records_are_skipped_and_counted(tmp_path):
    """BUI-628 captures bodies the live path REFUSED. Importing one would
    manufacture rows the pipeline never treated as comps — and KTD4's
    keep-the-first rule means those rows would then beat the real ones."""
    import collections
    cap = tmp_path / "capture"
    resp = serpapi_response(EXCLUDING_Q, [serpapi_result("1", "X-Men #94")])
    write_capture(cap, [
        capture_record(resp, EXCLUDING_Q, validation="ok"),
        capture_record(resp, '"Bad 1"', validation="LH_Sold=1 missing"),
        capture_record(resp, '"Legacy 1"'),  # pre-BUI-628: no validation key
    ])
    stats = collections.Counter()
    responses = bf.read_capture(sc, cap, stats)
    assert [r.query for r in responses] == [EXCLUDING_Q, '"Legacy 1"'], (
        "a record with no validation key predates BUI-628, when capture ran "
        "only after validation passed — absence positively means valid"
    )
    assert stats["capture_invalid"] == 1
    assert stats["capture_records"] == 3


def test_malformed_capture_line_is_counted_and_skipped(tmp_path):
    import collections
    cap = tmp_path / "capture"
    cap.mkdir(parents=True)
    good = json.dumps(capture_record(serpapi_response('"A 1"', []), '"A 1"'))
    cap.joinpath("raw_responses.jsonl").write_text(
        good + "\n" + "{not json\n" + json.dumps({"timestamp": 1}) + "\n",
        encoding="utf-8")
    stats = collections.Counter()
    responses = bf.read_capture(sc, cap, stats)
    assert len(responses) == 1
    assert stats["malformed"] == 2


# ---------------------------------------------------------------------------
# Comp extraction — the KTD1 shape
# ---------------------------------------------------------------------------

def _raw(response, query, *, source="cache", observed_at=1_700_000_000.0,
         provider=None, from_cache=True, provenance=None):
    return bf.RawResponse(
        source=source, ident="fixture", query=query,
        provider=provider or (sc.PROVIDER_SOLD_COMPS if "items" in response
                              else sc.PROVIDER_SERPAPI),
        data=response, observed_at=observed_at, from_cache=from_cache,
        provenance=provenance or (bf.PROVENANCE_CACHE if source == "cache"
                                  else bf.PROVENANCE_CAPTURE),
    )


def test_extract_applies_the_live_hard_exclude_and_dedupe(tmp_path):
    response = serpapi_response(EXCLUDING_Q, [
        serpapi_result("100", "Uncanny X-Men #94 1975 Marvel"),
        serpapi_result("100", "Uncanny X-Men #94 duplicate id"),
        serpapi_result("101", "Uncanny X-Men #94 95 96 97 lot of 4 comics"),
        serpapi_result("", "Uncanny X-Men #94 no product id"),
    ])
    comps = bf.extract_comps(sc, _raw(response, EXCLUDING_Q))
    assert [c["product_id"] for c in comps] == ["100"]
    assert sc.hard_exclude("Uncanny X-Men #94 95 96 97 lot of 4 comics"), (
        "fixture must actually be excluded BY THE LIVE RULE, not by this test"
    )


def test_extract_stamps_observed_at_from_the_response_never_now():
    """KTD6/KTD7: stamping the run time would restate a two-month-old comp as
    freshly observed — the exact input the recency weighting consumes."""
    response = serpapi_response(EXCLUDING_Q, [serpapi_result("100", "X-Men #94")])
    comps = bf.extract_comps(sc, _raw(response, EXCLUDING_Q, observed_at=1_700_000_000.0))
    assert comps[0]["observed_at"] == "2023-11-14T22:13:20+00:00"
    assert comps[0]["tier"] is None
    assert comps[0]["from_cache"] is True
    assert comps[0]["provenance"] == bf.PROVENANCE_CACHE


def test_pool_routing_follows_the_query_not_a_tier_guess():
    slab = serpapi_result("200", "Uncanny X-Men #94 CGC 8.0 Marvel 1975")
    plain = serpapi_result("201", "Uncanny X-Men #94 VF 8.0 Marvel 1975")
    assert sc._is_slab_comp(sc.parse_comp(slab)), "fixture must be a live slab comp"

    inclusive = bf.extract_comps(sc, _raw(
        serpapi_response(INCLUSIVE_Q, [slab, plain]), INCLUSIVE_Q))
    assert {c["product_id"]: c["pool"] for c in inclusive} == {"200": "slab", "201": "raw"}

    # An exclusion query is a tier that passes route_slabs=False: live puts
    # everything it keeps in the raw pool, slab-looking title or not.
    excluding = bf.extract_comps(sc, _raw(
        serpapi_response(EXCLUDING_Q, [slab, plain]), EXCLUDING_Q))
    assert {c["pool"] for c in excluding} == {"raw"}


def test_sold_comps_items_parse_through_the_live_extractor():
    response = sold_comps_response(EXCLUDING_Q, [
        sold_comps_item("300", "Uncanny X-Men #94 VG 4.0", price="88.00"),
    ])
    comps = bf.extract_comps(sc, _raw(response, EXCLUDING_Q))
    assert comps[0]["provider"] == sc.PROVIDER_SOLD_COMPS
    assert comps[0]["price"] == 88.0
    assert comps[0]["sold_date"] == "2026-08-01"
    assert comps[0]["grade"] == 4.0


# ---------------------------------------------------------------------------
# Identity resolution (KTD3 / KTD10)
# ---------------------------------------------------------------------------

def _resolver(rows):
    return bf.ComicResolver(sc, rows)


def test_unique_phrase_match_attaches_that_comic_id():
    resolver = _resolver([{"id": 7, "title": "The Uncanny X-Men", "issue": "94",
                           "year": 1975}])
    assert resolver.resolve(EXCLUDING_Q) == (7, "resolved")


def test_ambiguous_phrase_imports_with_comic_id_null():
    """AE2 / KTD3: identity is recorded or absent, never attached to a
    plausible book."""
    resolver = _resolver([
        {"id": 7, "title": "X-Men", "issue": "1", "year": 1963},
        {"id": 8, "title": "X-Men", "issue": "1", "year": 1991},
    ])
    assert resolver.resolve('"X-Men 1" -cgc') == (None, "ambiguous")


def test_unmatched_phrase_and_missing_phrase_are_unresolved():
    resolver = _resolver([{"id": 7, "title": "X-Men", "issue": "1", "year": 1963}])
    assert resolver.resolve('"Nothing Like This 5"') == (None, "unresolved")
    assert resolver.resolve("no quoted phrase at all") == (None, "unresolved")


def test_masthead_alias_recovers_a_swapped_query():
    """BUI-581's masthead-swap tier queries the run's OTHER name, which the
    comics row does not carry. Measured +19 uniquely-resolved responses."""
    resolver = _resolver([{"id": 9, "title": "X-Men", "issue": "93", "year": 1975}])
    assert resolver.resolve('"Uncanny X-Men 93" -cgc') == (9, "resolved")


def test_alias_is_only_a_fallback_never_an_override():
    """A phrase that matches a real row directly must resolve to THAT row even
    when some other row's alias would also match it."""
    resolver = _resolver([
        {"id": 1, "title": "Uncanny X-Men", "issue": "93", "year": 1975},
        {"id": 2, "title": "X-Men", "issue": "93", "year": 1991},
    ])
    assert resolver.resolve('"Uncanny X-Men 93"') == (1, "resolved")


# ---------------------------------------------------------------------------
# Shape identity — the KTD11 replay, and proof it can fail
# ---------------------------------------------------------------------------

def test_replay_proves_the_backfill_shape_matches_the_live_pool(tmp_path):
    import collections
    cache = tmp_path / "cache"
    write_cache(cache, serpapi_response(EXCLUDING_Q, [
        serpapi_result("100", "Uncanny X-Men #94 VG 4.0 Marvel 1975"),
        serpapi_result("101", "Uncanny X-Men #94 95 96 lot of 3"),  # hard-excluded
        serpapi_result("102", "Uncanny X-Men #94 CGC 8.0"),         # raw: exclusion tier
    ]))
    write_cache(cache, sold_comps_response(INCLUSIVE_Q, [
        sold_comps_item("200", "Uncanny X-Men #94 CGC 8.0", price="900.00"),
        sold_comps_item("201", "Uncanny X-Men #94 VG 4.0", price="88.00"),
    ]))
    responses = bf.read_cache(sc, cache, collections.Counter())
    assert len(responses) == 2
    assert bf.verify_shape(sc, responses) == 0


def test_replay_actually_detects_a_shape_divergence(tmp_path, monkeypatch):
    """The check must be able to FAIL, or it proves nothing (the
    solutions-lint --self-test posture)."""
    import collections
    cache = tmp_path / "cache"
    write_cache(cache, serpapi_response(EXCLUDING_Q, [
        serpapi_result("100", "Uncanny X-Men #94 VG 4.0"),
    ]))
    responses = bf.read_cache(sc, cache, collections.Counter())
    real = bf.extract_comps

    def drifted(module, raw):
        comps = real(module, raw)
        for comp in comps:
            comp["price"] = (comp["price"] or 0) + 1  # a "harmless" reformat
        return comps

    monkeypatch.setattr(bf, "extract_comps", drifted)
    assert bf.verify_shape(sc, responses) == 1


def test_replay_fails_when_the_live_path_grows_an_unclassified_field(tmp_path, monkeypatch):
    """The anti-drift property: a NEW stamped field on the live path is in
    neither VERIFIED_FIELDS nor UNREPRODUCED_FIELDS, so the replay must stop
    being green rather than quietly ignore it."""
    import collections
    cache = tmp_path / "cache"
    write_cache(cache, serpapi_response(EXCLUDING_Q, [
        serpapi_result("100", "Uncanny X-Men #94 VG 4.0"),
    ]))
    responses = bf.read_cache(sc, cache, collections.Counter())
    real_parse = sc.parse_comp

    def stamped(result):
        comp = real_parse(result)
        if comp is not None:
            comp["seller_reputation"] = 99  # a plausible future BUI-6xx addition
        return comp

    monkeypatch.setattr(sc, "parse_comp", stamped)
    assert bf.verify_shape(sc, responses) == 1


def test_query_echo_re_derives_the_url_the_response_came_from(tmp_path):
    import collections
    cache = tmp_path / "cache"
    write_cache(cache, serpapi_response(EXCLUDING_Q, []))
    honest = bf.read_cache(sc, cache, collections.Counter())[0]
    assert bf._verify_query_echo(sc, honest) is True

    other = tmp_path / "other"
    write_cache(other, serpapi_response(EXCLUDING_Q, []), name="not-the-digest.json")
    liar = bf.read_cache(sc, other, collections.Counter())[0]
    assert bf._verify_query_echo(sc, liar) is False


# ---------------------------------------------------------------------------
# Driver: dry run, capture-first ordering, idempotency, failure posture
# ---------------------------------------------------------------------------

@pytest.fixture
def server(monkeypatch):
    """A recording stand-in for the comics server. Records every POST body so
    a test can assert what WOULD land, without a live server or a live DB."""

    class Server:
        def __init__(self):
            self.posts: list[dict] = []
            self.comics: list[dict] = []
            self.fail_on: set[int] = set()
            self.reply = {"inserted": 1, "updated": 0, "conflicts": 0}

        def get(self, url, timeout=60.0):
            if url.endswith("/health"):
                return {"status": "ok"}
            if url.endswith("/api/comics"):
                return self.comics
            raise AssertionError(f"unexpected GET {url}")

        def post(self, url, payload, timeout=60.0):
            assert url.endswith("/api/comics/comps")
            if len(self.posts) in self.fail_on:
                self.posts.append(payload)
                raise RuntimeError("boom")
            self.posts.append(payload)
            return {"comic_id": payload["comic_id"], **self.reply}

    stub = Server()
    monkeypatch.setattr(bf, "_http_get_json", stub.get)
    monkeypatch.setattr(bf, "_http_post_json", stub.post)
    monkeypatch.setenv("COMICS_SERVER_URL", "http://server.invalid")
    return stub


def _argv(tmp_path, *extra):
    return ["--cache-dir", str(tmp_path / "cache"),
            "--capture-dir", str(tmp_path / "capture"), *extra]


def test_dry_run_writes_nothing(tmp_path, server, capsys):
    write_cache(tmp_path / "cache",
                serpapi_response(EXCLUDING_Q, [serpapi_result("100", "X-Men #94")]))
    assert bf.main(_argv(tmp_path, "--dry-run")) == 0
    assert server.posts == []
    assert "DRY RUN" in capsys.readouterr().out


def test_capture_is_imported_before_the_cache(tmp_path, server):
    """A response present in BOTH corpora must land under the capture's real
    fetch timestamp, not the cache file's mtime: `upsert_comps` keeps the
    first answer (KTD4), so whichever corpus posts first owns `observed_at`
    and `provenance` forever."""
    response = serpapi_response(EXCLUDING_Q, [serpapi_result("100", "X-Men #94")])
    write_cache(tmp_path / "cache", response, mtime=1_800_000_000.0)
    write_capture(tmp_path / "capture",
                  [capture_record(response, EXCLUDING_Q, timestamp=1_700_000_000.0)])

    assert bf.main(_argv(tmp_path)) == 0
    assert len(server.posts) == 2
    first, second = server.posts
    assert first["comps"][0]["provenance"] == bf.PROVENANCE_CAPTURE
    assert first["comps"][0]["observed_at"] == "2023-11-14T22:13:20+00:00"
    assert first["comps"][0]["from_cache"] is False
    assert second["comps"][0]["provenance"] == bf.PROVENANCE_CACHE
    assert second["comps"][0]["from_cache"] is True
    assert first["comps"][0]["product_id"] == second["comps"][0]["product_id"], (
        "same comp both times — the ledger's unique index is what collapses "
        "them, and the capture's row is the one that survives"
    )


def test_capture_still_precedes_the_cache_when_its_timestamp_is_later(tmp_path, server):
    """BUI-680's record sort is confined to the capture corpus, deliberately.
    `main` puts the WHOLE capture list before the WHOLE cache list because the
    capture's real fetch time is better provenance than the cache's mtime
    fallback — a sort grown to span both corpora would silently undo that
    design whenever the cache file's mtime happened to be the earlier of the
    two, handing KTD4's surviving row to the weaker source."""
    response = serpapi_response(EXCLUDING_Q, [serpapi_result("100", "X-Men #94")])
    write_cache(tmp_path / "cache", response, mtime=1_700_000_000.0)
    write_capture(tmp_path / "capture",
                  [capture_record(response, EXCLUDING_Q, timestamp=1_800_000_000.0)])

    assert bf.main(_argv(tmp_path)) == 0
    first, second = server.posts
    assert first["comps"][0]["provenance"] == bf.PROVENANCE_CAPTURE
    assert second["comps"][0]["provenance"] == bf.PROVENANCE_CACHE


def test_rerunning_emits_a_byte_identical_batch_set(tmp_path, server):
    """The backfill's half of idempotency. `upsert_comps` guarantees the other
    half (BUI-656 tests it: re-observation bumps seen_count and never rewrites
    price/sold_date/title/link), and it can only do so if the second run
    presents exactly the same rows — including `observed_at`, which is read
    from the corpus and must NOT drift to the run's own clock."""
    write_cache(tmp_path / "cache", serpapi_response(EXCLUDING_Q, [
        serpapi_result("100", "Uncanny X-Men #94 VG 4.0"),
        serpapi_result("101", "Uncanny X-Men #94 FN 6.0"),
    ]), mtime=1_800_000_000.0)
    server.comics = [{"id": 7, "title": "Uncanny X-Men", "issue": "94", "year": 1975}]

    assert bf.main(_argv(tmp_path)) == 0
    first = json.dumps(server.posts, sort_keys=True)
    server.posts = []
    server.reply = {"inserted": 0, "updated": 2, "conflicts": 0}
    assert bf.main(_argv(tmp_path)) == 0
    assert json.dumps(server.posts, sort_keys=True) == first


def test_resolved_and_ambiguous_batches_carry_the_right_comic_id(tmp_path, server):
    cache = tmp_path / "cache"
    write_cache(cache, serpapi_response(EXCLUDING_Q, [serpapi_result("100", "X-Men #94")]))
    write_cache(cache, serpapi_response('"X-Men 1" -cgc -cbcs -graded -slab',
                                        [serpapi_result("200", "X-Men #1")]))
    server.comics = [
        {"id": 7, "title": "Uncanny X-Men", "issue": "94", "year": 1975},
        {"id": 8, "title": "X-Men", "issue": "1", "year": 1963},
        {"id": 9, "title": "X-Men", "issue": "1", "year": 1991},
    ]
    assert bf.main(_argv(tmp_path)) == 0
    by_pid = {p["comps"][0]["product_id"]: p["comic_id"] for p in server.posts}
    assert by_pid == {"100": 7, "200": None}


def test_a_failed_post_is_counted_continues_and_fails_the_run(tmp_path, server, capsys):
    """A silent partial import is a failed import."""
    cache = tmp_path / "cache"
    write_cache(cache, serpapi_response(EXCLUDING_Q, [serpapi_result("100", "X-Men #94")]))
    write_cache(cache, serpapi_response('"X-Men 1" -cgc -cbcs -graded -slab',
                                        [serpapi_result("200", "X-Men #1")]))
    server.fail_on = {0}
    assert bf.main(_argv(tmp_path)) == 1
    assert len(server.posts) == 2, "the second batch must still be attempted"
    out = capsys.readouterr()
    assert "POST failures     : 1" in out.out
    assert "PARTIAL IMPORT" in out.err


def test_a_response_with_no_surviving_comps_is_never_posted(tmp_path, server):
    """`CompsIngestRequest` requires a non-empty list, so an empty batch would
    422 the whole call and land in `rejected_writes` looking like a defect."""
    write_cache(tmp_path / "cache", serpapi_response(EXCLUDING_Q, [
        serpapi_result("100", "Uncanny X-Men #94 95 96 lot of 3 comics"),
    ]))
    assert bf.main(_argv(tmp_path)) == 0
    assert server.posts == []


def test_a_dead_endpoint_aborts_instead_of_erroring_once_per_response(tmp_path, server, capsys):
    """A server without BUI-656's endpoint 404s every batch. Emitting one error
    line per response buries the signal; there is nothing to lose by stopping,
    since a re-run is a no-op for whatever already landed."""
    cache = tmp_path / "cache"
    for n in range(9):
        write_cache(cache, serpapi_response(
            f'"Book {n}" -cgc -cbcs -graded -slab',
            [serpapi_result(str(n), f"Book #{n} VF 8.0")]))
    server.fail_on = set(range(9))
    assert bf.main(_argv(tmp_path)) == 1
    assert len(server.posts) == bf._ABORT_AFTER_CONSECUTIVE_FAILURES
    assert "ABORTING" in capsys.readouterr().err


def test_summary_reports_the_observed_at_span_per_corpus(tmp_path, server, capsys):
    """KTD6/KTD7 made visible: a corpus copied without preserving mtimes shows
    up as a span of today..today instead of the weeks it should cover."""
    write_cache(tmp_path / "cache",
                serpapi_response(EXCLUDING_Q, [serpapi_result("100", "X-Men #94")]),
                mtime=1_800_000_000.0)
    write_capture(tmp_path / "capture",
                  [capture_record(serpapi_response('"A 1" -cgc -cbcs -graded -slab',
                                                   [serpapi_result("1", "A #1")]),
                                  '"A 1" -cgc -cbcs -graded -slab',
                                  timestamp=1_700_000_000.0)])
    assert bf.main(_argv(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "observed_at capture : 2023-11-14T22:13:20+00:00" in out
    assert "observed_at cache   : 2027-01-15T08:00:00+00:00" in out


def test_empty_corpus_is_a_clean_no_op(tmp_path, server):
    assert bf.main(_argv(tmp_path)) == 0
    assert server.posts == []


def test_server_url_must_be_set_rather_than_guessed(tmp_path, server, monkeypatch):
    """A bare unset var would address an empty host — the BUI-352 trap."""
    monkeypatch.delenv("COMICS_SERVER_URL", raising=False)
    monkeypatch.delenv("GIXEN_SERVER_URL", raising=False)
    write_cache(tmp_path / "cache",
                serpapi_response(EXCLUDING_Q, [serpapi_result("100", "X-Men #94")]))
    with pytest.raises(SystemExit):
        bf.main(_argv(tmp_path))
    assert server.posts == []
