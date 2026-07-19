"""Integration tests for the BUI-91/92 collection + wish-list API endpoints.

These exercise the real gixen-cli server (server.main.app) with the real
overlay plugin loaded, pointing locg-cli's store (LOCG_DATA_DIR) at a seeded
temp directory. Mirrors the `api` fixture pattern in
test_gixen_overlay_routes.py but adds the seeded collection/wish-list store.
"""
from __future__ import annotations

import json
from importlib.metadata import EntryPoint
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _install_real_plugin(monkeypatch):
    ep = EntryPoint(
        name="gixen-overlay",
        value="gixen_overlay.plugin:plugin",
        group="gixen.plugins",
    )
    monkeypatch.setattr(
        "gixen.plugins.entry_points",
        lambda group: [ep] if group == "gixen.plugins" else [],
    )


def _mock_gixen():
    m = MagicMock()
    m.list_snipes.return_value = []
    return m


def _seed_collection(store, comics):
    """Write a minimal collection.json the locg matcher can read."""
    payload = {
        "schema_version": 1,
        "last_full_import": "2026-06-01T00:00:00.000000Z",
        "last_import_source": "seed.xlsx",
        "migration_in_progress": False,
        "last_writer": None,
        "series_name_index": {},
        "comics": comics,
    }
    (store / "collection.json").write_text(json.dumps(payload))


def _seed_wish_list(store, items):
    (store / "wish-list.json").write_text(
        json.dumps({"updated_at": "2026-06-01T00:00:00Z", "items": items})
    )


# A small owned collection: one owned ASM #300, one *wish-list-only* row
# (in_collection=0) to prove the copies-owned gate is respected.
_OWNED = [
    {
        "full_title": "The Amazing Spider-Man #300",
        "series_name": "The Amazing Spider-Man",
        "publisher_name": "Marvel Comics",
        "release_date": "1988-05-01",
        "in_collection": 1,
    },
    {
        "full_title": "Fantastic Four #48",
        "series_name": "Fantastic Four",
        "publisher_name": "Marvel Comics",
        "release_date": "1966-03-01",
        "in_collection": 0,  # wish-list / read but not owned
    },
]

_WISH = [
    {"name": "Fantastic Four #48", "id": 6977652},
    {"name": "X-Men #1", "id": None},
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    _seed_collection(store, _OWNED)
    _seed_wish_list(store, _WISH)

    _install_real_plugin(monkeypatch)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("LOCG_DATA_DIR", str(store))  # overrides the server default
    monkeypatch.setenv("GIXEN_USERNAME", "testuser")
    monkeypatch.setenv("GIXEN_PASSWORD", "testpass")
    monkeypatch.setenv("GIXEN_SYNC_ENABLED", "false")
    monkeypatch.setenv("LOCAL_SNIPER_ENABLED", "false")
    with patch("server.main.GixenClient", return_value=_mock_gixen()):
        from server.main import app

        with TestClient(app) as c:
            c.store = store
            yield c


# --- collection check ------------------------------------------------------

def test_check_owned(client):
    r = client.get("/api/comics/collection/check", params={"series": "Amazing Spider-Man", "issue": "300", "year": "1988"})
    assert r.status_code == 200
    body = r.json()
    assert body["match_status"] == "in_collection"
    assert body["full_title_matched"] == "The Amazing Spider-Man #300"
    assert body["cache_age_days"] is not None
    # BUI-249: provenance fields — a direct series-key match is "exact", never
    # "alias", and carries the matched row's decorated series name + date.
    assert body["match_kind"] == "exact"
    assert body["matched_series_name"] == "The Amazing Spider-Man"
    assert body["matched_release_date"] == "1988-05-01"


def test_check_owned_with_leading_article_dropped(client):
    """Parity with the local matcher: identify often drops the leading article
    ("The"), and the series-key normalizer strips it — so the plain name still
    matches the owned "The Amazing Spider-Man" (the BUI-45 / 17-owned-Hulks
    failure class)."""
    r = client.get("/api/comics/collection/check", params={"series": "The Amazing Spider-Man", "issue": "300", "year": "1988"})
    assert r.json()["match_status"] == "in_collection"


def test_check_not_owned_returns_not_in_cache(client):
    r = client.get("/api/comics/collection/check", params={"series": "Batman", "issue": "1", "year": "1940"})
    assert r.status_code == 200
    body = r.json()
    assert body["match_status"] == "not_in_cache"
    assert body["full_title_matched"] is None
    # BUI-249: no verdict, no provenance to report (R11).
    assert body["matched_series_name"] is None
    assert body["matched_release_date"] is None
    assert body["match_kind"] is None
    # BUI-250: no row at all — genuinely untracked, not just unowned.
    assert body["in_wish_list"] is False


def test_check_wishlist_only_row_is_not_owned(client):
    """A row with in_collection=0 (wish-list/read but not owned) must NOT count
    as owned — the copies-owned gate (BUI-26 bug D)."""
    r = client.get("/api/comics/collection/check", params={"series": "Fantastic Four", "issue": "48", "year": "1966"})
    assert r.json()["match_status"] == "not_in_cache"


def test_check_wishlist_only_row_flags_in_wish_list(client):
    """BUI-250: the same in_collection=0 row from test_check_wishlist_only_row_is_not_owned
    is a tracked-but-not-owned row, not a genuinely untracked issue — in_wish_list
    distinguishes it from test_check_not_owned_returns_not_in_cache's true miss."""
    r = client.get("/api/comics/collection/check", params={"series": "Fantastic Four", "issue": "48", "year": "1966"})
    body = r.json()
    assert body["match_status"] == "not_in_cache"
    assert body["in_wish_list"] is True


def test_check_owned_row_reports_in_wish_list_false(client):
    """An owned row with no separate wish-list-only edition of the same issue
    reports in_wish_list False alongside match_status in_collection."""
    r = client.get("/api/comics/collection/check", params={"series": "Amazing Spider-Man", "issue": "300", "year": "1988"})
    assert r.json()["in_wish_list"] is False


def test_check_requires_series_and_issue(client):
    assert client.get("/api/comics/collection/check", params={"series": "Batman"}).status_code == 422
    assert client.get("/api/comics/collection/check", params={"issue": "1"}).status_code == 422


# --- collection check (batch, BUI-204) -------------------------------------

def test_check_batch_mixed_owned_and_not(client):
    """A multi-item happy path: each pair gets the same verdict the single-item
    endpoint would, echoed with its series+issue so the caller can correlate."""
    r = client.post(
        "/api/comics/collection/check/batch",
        json={"items": [
            {"series": "Amazing Spider-Man", "issue": "300", "year": "1988"},
            {"series": "Batman", "issue": "1", "year": "1940"},
            {"series": "Fantastic Four", "issue": "48", "year": "1966"},  # in_collection=0
        ]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 3
    by_key = {(x["series"], x["issue"]): x for x in body["results"]}
    assert by_key[("Amazing Spider-Man", "300")]["match_status"] == "in_collection"
    assert by_key[("Amazing Spider-Man", "300")]["full_title_matched"] == "The Amazing Spider-Man #300"
    assert by_key[("Batman", "1")]["match_status"] == "not_in_cache"
    # in_collection=0 row is not owned — the copies-owned gate holds in batch too.
    assert by_key[("Fantastic Four", "48")]["match_status"] == "not_in_cache"


def test_check_batch_per_item_matches_single_endpoint(client):
    """Each batch result must equal what the single-item endpoint returns for the
    same pair — the batch is a fan-out, not a reimplementation."""
    pairs = [
        {"series": "The Amazing Spider-Man", "issue": "300", "year": "1988"},
        {"series": "Batman", "issue": "1", "year": "1940"},
    ]
    batch = client.post(
        "/api/comics/collection/check/batch", json={"items": pairs}
    ).json()["results"]
    for pair, got in zip(pairs, batch):
        single = client.get("/api/comics/collection/check", params=pair).json()
        # The batch entry is the single verdict plus echoed series/issue.
        assert {k: got[k] for k in single} == single


def test_check_batch_rejects_empty_items(client):
    assert client.post(
        "/api/comics/collection/check/batch", json={"items": []}
    ).status_code == 422


def test_check_batch_rejects_blank_series_or_issue(client):
    assert client.post(
        "/api/comics/collection/check/batch",
        json={"items": [{"series": "  ", "issue": "1"}]},
    ).status_code == 422
    assert client.post(
        "/api/comics/collection/check/batch",
        json={"items": [{"series": "Batman", "issue": "  "}]},
    ).status_code == 422


def test_check_batch_refuses_when_store_never_imported(client):
    """R11 lifted to the batch boundary: a never-imported store must NOT answer a
    list of 'not owned' verdicts — the whole call 409s, like the single endpoint."""
    (client.store / "collection.json").unlink()
    r = client.post(
        "/api/comics/collection/check/batch",
        json={"items": [{"series": "Batman", "issue": "1", "year": "1940"}]},
    )
    assert r.status_code == 409


def test_check_batch_year_catches_masthead_owned_book(client):
    """Parity with the single-item year-gated masthead fallback (BUI-184): with
    the per-issue cover year, 'The Mighty Thor #154' resolves to owned 'Thor'."""
    _seed_collection(client.store, [{
        "full_title": "Thor #154",
        "series_name": "Thor",
        "publisher_name": "Marvel Comics",
        "release_date": "1968-07-01",
        "in_collection": 1,
    }])
    r = client.post(
        "/api/comics/collection/check/batch",
        json={"items": [{"series": "The Mighty Thor", "issue": "154", "year": "1968"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["match_status"] == "in_collection"


def test_check_batch_distinguishes_untracked_wishlisted_and_owned(client):
    """BUI-250: same three-way distinction as the single endpoint, via batch.
    Untracked (in_wish_list False), wishlisted-not-owned (still not_in_cache,
    in_wish_list True), and owned (in_wish_list False) must all be visible in
    one fan-out call."""
    r = client.post(
        "/api/comics/collection/check/batch",
        json={"items": [
            {"series": "Batman", "issue": "1", "year": "1940"},  # untracked
            {"series": "Fantastic Four", "issue": "48", "year": "1966"},  # wishlisted, in_collection=0
            {"series": "Amazing Spider-Man", "issue": "300", "year": "1988"},  # owned
        ]},
    )
    assert r.status_code == 200, r.text
    by_key = {(x["series"], x["issue"]): x for x in r.json()["results"]}

    untracked = by_key[("Batman", "1")]
    assert untracked["match_status"] == "not_in_cache"
    assert untracked["in_wish_list"] is False

    wishlisted = by_key[("Fantastic Four", "48")]
    assert wishlisted["match_status"] == "not_in_cache"
    assert wishlisted["in_wish_list"] is True

    owned = by_key[("Amazing Spider-Man", "300")]
    assert owned["match_status"] == "in_collection"
    assert owned["in_wish_list"] is False


def test_check_year_plus_one_skew_no_longer_false_negatives(client):
    """BUI-251: reproduces the BUI-247 audit finding at the HTTP layer —
    Avengers #1 (2013), confirmed owned, returned not_in_cache when queried
    WITH its year because the stored release_date sits one year LATER than the
    query year (the opposite skew direction from BUI-214's year-minus-1 case).
    The symmetric ±1 window must resolve it as in_collection."""
    _seed_collection(client.store, [{
        "full_title": "Avengers #1",
        "series_name": "Avengers (Vol. 5) (2013 - 2015)",
        "publisher_name": "Marvel Comics",
        "release_date": "2014-01-08",
        "in_collection": 1,
    }])
    r = client.get(
        "/api/comics/collection/check",
        params={"series": "Avengers", "issue": "1", "year": "2013"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["match_status"] == "in_collection"
    assert body["full_title_matched"] == "Avengers #1"


def test_check_alias_match_flags_wrong_volume(client):
    """BUI-249: the alias pass can land on an owned issue of the WRONG volume —
    querying 'The Mighty Thor #5' (Vol.3, 2015) with no year resolves to the
    owned 'Thor #5' (Vol.1, 1966) via the masthead alias in owned_match_keys.
    That's a silent false positive: the intended Mighty Thor Vol.3 #5 is NOT
    owned. match_kind == "alias" plus the matched row's decorated series name
    and release date are how a caller detects this and flags "confirm volume"
    instead of trusting the bare in_collection verdict."""
    _seed_collection(client.store, [{
        "full_title": "Thor #5",
        "series_name": "Thor (Vol. 1) (1966 - 1996)",
        "publisher_name": "Marvel Comics",
        "release_date": "1966-08-01",
        "in_collection": 1,
    }])
    r = client.get(
        "/api/comics/collection/check",
        params={"series": "The Mighty Thor", "issue": "5"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["match_status"] == "in_collection"
    assert body["full_title_matched"] == "Thor #5"
    assert body["match_kind"] == "alias"
    assert body["matched_series_name"] == "Thor (Vol. 1) (1966 - 1996)"
    assert body["matched_release_date"] == "1966-08-01"


def test_check_batch_alias_match_flags_wrong_volume(client):
    """Same false positive as test_check_alias_match_flags_wrong_volume, via the
    batch endpoint — the provenance fields must be present there too."""
    _seed_collection(client.store, [{
        "full_title": "Thor #5",
        "series_name": "Thor (Vol. 1) (1966 - 1996)",
        "publisher_name": "Marvel Comics",
        "release_date": "1966-08-01",
        "in_collection": 1,
    }])
    r = client.post(
        "/api/comics/collection/check/batch",
        json={"items": [{"series": "The Mighty Thor", "issue": "5"}]},
    )
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["match_status"] == "in_collection"
    assert result["match_kind"] == "alias"
    assert result["matched_series_name"] == "Thor (Vol. 1) (1966 - 1996)"


_CROSS_VOLUME_FF18 = [
    {
        "full_title": "Fantastic Four #18",
        "series_name": "Fantastic Four (Vol. 1) (1961 - 1996)",
        "publisher_name": "Marvel Comics",
        "release_date": "1963-09-01",
        "in_collection": 1,
    },
    {
        "full_title": "Fantastic Four #18",
        "series_name": "Fantastic Four (Vol. 7) (2022 - 2025)",
        "publisher_name": "Marvel Comics",
        "release_date": "2024-02-14",
        "in_collection": 1,
    },
]


def test_check_no_year_cross_volume_returns_ambiguous(client):
    """BUI-284: the same issue owned under two masthead volumes, checked with no
    year, comes back as `ambiguous_cross_volume` (a 200 passthrough) rather than
    a silent in_collection. The endpoint surfaces the colliding volumes so the
    caller can re-check WITH the cover year."""
    _seed_collection(client.store, _CROSS_VOLUME_FF18)
    r = client.get(
        "/api/comics/collection/check",
        params={"series": "Fantastic Four", "issue": "18"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["match_status"] == "ambiguous_cross_volume"
    assert body["match_kind"] == "cross_volume"
    names = {c["series_name"] for c in body["candidates"]}
    assert names == {
        "Fantastic Four (Vol. 1) (1961 - 1996)",
        "Fantastic Four (Vol. 7) (2022 - 2025)",
    }


def test_check_year_resolves_cross_volume(client):
    """BUI-284: supplying the cover year resolves the collision to one volume —
    unchanged in_collection behavior on the year-supplied path."""
    _seed_collection(client.store, _CROSS_VOLUME_FF18)
    r = client.get(
        "/api/comics/collection/check",
        params={"series": "Fantastic Four", "issue": "18", "year": "1963"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["match_status"] == "in_collection"
    assert body["matched_series_name"] == "Fantastic Four (Vol. 1) (1961 - 1996)"


# --- printing-marker conflict (BUI-364) --------------------------------------

# The confirmed BUI-364 incident state (Absolute Martian Manhunter #1, eBay
# 147434010581): the 2nd printing is owned; the base printing is tracked
# wish-list-only. A base-printing query must not read as unqualified ownership.
_AMM_PRINTINGS = [
    {
        "full_title": "Absolute Martian Manhunter #1 2nd Printing",
        "series_name": "Absolute Martian Manhunter (2025)",
        "publisher_name": "DC Comics",
        "release_date": "2025-06-18",
        "in_collection": 1,
    },
    {
        "full_title": "Absolute Martian Manhunter #1",
        "series_name": "Absolute Martian Manhunter (2025)",
        "publisher_name": "DC Comics",
        "release_date": "2025-03-19",
        "in_collection": 0,
        "in_wish_list": 1,
    },
]


def test_check_printing_conflict_surfaced(client):
    """BUI-364: an owned '2nd Printing' row satisfying a base-printing query is
    flagged mechanically — printing_conflict=True plus the conflicting rows,
    showing the base printing is wish-listed, not owned. match_status stays
    in_collection (the reprint IS owned); the flag qualifies, never flips (R11)."""
    _seed_collection(client.store, _AMM_PRINTINGS)
    r = client.get(
        "/api/comics/collection/check",
        params={"series": "Absolute Martian Manhunter", "issue": "1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["match_status"] == "in_collection"
    assert body["full_title_matched"] == "Absolute Martian Manhunter #1 2nd Printing"
    assert body["printing_conflict"] is True
    by_title = {c["full_title"]: c for c in body["printing_candidates"]}
    base = by_title["Absolute Martian Manhunter #1"]
    assert base["in_collection"] is False
    assert base["in_wish_list"] is True


def test_check_batch_printing_conflict_parity(client):
    """BUI-364/BUI-204 parity: the batch endpoint surfaces the same
    printing_conflict fields per item as the single-item endpoint."""
    _seed_collection(client.store, _AMM_PRINTINGS)
    r = client.post(
        "/api/comics/collection/check/batch",
        json={"items": [{"series": "Absolute Martian Manhunter", "issue": "1"}]},
    )
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["match_status"] == "in_collection"
    assert result["printing_conflict"] is True
    assert any(
        c["full_title"] == "Absolute Martian Manhunter #1"
        for c in result["printing_candidates"]
    )


def test_check_unmarked_owned_row_printing_conflict_false(client):
    """BUI-364: the field is present (False) on a clean owned verdict, so
    callers can read it unconditionally on every row."""
    r = client.get(
        "/api/comics/collection/check",
        params={"series": "Amazing Spider-Man", "issue": "300", "year": "1988"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["match_status"] == "in_collection"
    assert body["printing_conflict"] is False
    assert "printing_candidates" not in body


def test_check_batch_cross_volume_returns_ambiguous(client):
    """BUI-284: the batch endpoint carries the ambiguous verdict per-item too."""
    _seed_collection(client.store, _CROSS_VOLUME_FF18)
    r = client.post(
        "/api/comics/collection/check/batch",
        json={"items": [{"series": "Fantastic Four", "issue": "18"}]},
    )
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["match_status"] == "ambiguous_cross_volume"
    assert result["match_kind"] == "cross_volume"


# --- wish-list -------------------------------------------------------------

def test_wish_list_returns_items(client):
    r = client.get("/api/comics/wish-list")
    assert r.status_code == 200
    items = r.json()
    names = {i["name"] for i in items}
    assert names == {"Fantastic Four #48", "X-Men #1"}


def test_wish_list_title_filter(client):
    r = client.get("/api/comics/wish-list", params={"title": "Fantastic"})
    assert [i["name"] for i in r.json()] == ["Fantastic Four #48"]


def test_wish_list_empty_when_never_imported(client):
    (client.store / "wish-list.json").unlink()
    r = client.get("/api/comics/wish-list")
    assert r.status_code == 200
    assert r.json() == []


def test_wish_list_empty_on_corrupt_cache(client):
    """BUI-184: a corrupt wish-list JSON yields an empty list, not a 500 —
    seller-scan must not break entirely on a single bad write."""
    (client.store / "wish-list.json").write_text("{ this is not json")
    r = client.get("/api/comics/wish-list")
    assert r.status_code == 200
    assert r.json() == []


# --- status / export -------------------------------------------------------

def test_collection_status(client):
    r = client.get("/api/comics/collection/status")
    assert r.status_code == 200
    body = r.json()
    assert body["row_count"] == 2
    assert body["last_full_import"] == "2026-06-01T00:00:00.000000Z"
    assert "locg_cli_version" in body


def test_series_names_empty_by_default(client):
    """The seed fixture carries an empty series_name_index, so the endpoint
    answers with an empty list (BUI-129)."""
    r = client.get("/api/comics/collection/series-names")
    assert r.status_code == 200
    assert r.json() == {"series_names": [], "count": 0}


def test_series_names_returns_canonical_names(client):
    """Once the index is populated, the endpoint surfaces the catalog spellings
    a caller can resolve an ambiguous query against (BUI-129)."""
    payload = json.loads((client.store / "collection.json").read_text())
    payload["series_name_index"] = {
        "uncanny x-men": "Uncanny X-Men",
        "amazing spider-man": "The Amazing Spider-Man",
    }
    (client.store / "collection.json").write_text(json.dumps(payload))

    r = client.get("/api/comics/collection/series-names")
    assert r.status_code == 200
    body = r.json()
    assert body["series_names"] == ["The Amazing Spider-Man", "Uncanny X-Men"]
    assert body["count"] == 2


# --- series-names resolve (BUI-449) -----------------------------------------

def test_series_names_resolve_exact_match(client):
    """Thin-wrapper wiring check: the overlay endpoint delegates to locg-cli's
    resolver (the one tested place the matching logic lives) and round-trips
    its response shape."""
    payload = json.loads((client.store / "collection.json").read_text())
    payload["series_name_index"] = {"uncanny x-men": "Uncanny X-Men"}
    (client.store / "collection.json").write_text(json.dumps(payload))

    r = client.post(
        "/api/comics/collection/series-names/resolve",
        json={"names": ["Uncanny X-Men (Vol. 1)"]},
    )
    assert r.status_code == 200
    assert r.json() == {
        "results": [
            {
                "query": "Uncanny X-Men (Vol. 1)",
                "resolved": "Uncanny X-Men",
                "match_kind": "exact",
            }
        ]
    }


def test_series_names_resolve_no_confident_match(client):
    r = client.post(
        "/api/comics/collection/series-names/resolve",
        json={"names": ["Some Unknown Series"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["results"][0]["resolved"] is None
    assert body["results"][0]["match_kind"] is None


def test_series_names_resolve_rejects_empty_names(client):
    r = client.post("/api/comics/collection/series-names/resolve", json={"names": []})
    assert r.status_code == 422


def test_collection_export_returns_csv(client):
    r = client.get("/api/comics/collection/export")
    assert r.status_code == 200
    body = r.json()
    # The endpoint returns the file *contents* (read from the server store) so
    # the caller can save + upload them; plus the pending-push counts.
    assert isinstance(body["csv"], str) and body["csv"].strip()  # non-empty CSV (header at least)
    assert "notes_md" in body
    assert isinstance(body["ready_count"], int)


def test_collection_export_wins_only_by_default(client):
    """BUI-208: the default export is wins-only — no wish rows, so the CSV can
    never carry an In Collection=0 row (the LOCG-delete trigger)."""
    import csv
    import io

    body = client.get("/api/comics/collection/export").json()
    assert body["wish_list_count"] == 0
    assert body["pushed_wishes"] is False
    rows = list(csv.DictReader(io.StringIO(body["csv"])))
    assert all(row["In Collection"] != "0" for row in rows)


def test_collection_export_push_wishes_includes_local_only_wish(client):
    """?push_wishes=true is the explicit owned-safe wish mirror: the local-only,
    not-owned wish ("X-Men #1") ships as an In Collection=0 row."""
    import csv
    import io

    body = client.get("/api/comics/collection/export", params={"push_wishes": "true"}).json()
    assert body["pushed_wishes"] is True
    assert body["wish_list_count"] >= 1
    rows = list(csv.DictReader(io.StringIO(body["csv"])))
    wish_rows = [row for row in rows if row["In Wish List"] == "1"]
    assert any(row["In Collection"] == "0" for row in wish_rows)
    assert "X-Men #1" in {row["Full Title"] for row in wish_rows}


# ===========================================================================
# BUI-92: write endpoints
# ===========================================================================

def _reseed_with_index(store, index):
    """Re-seed collection.json with a series_name_index so record-win resolves
    canonical series without a Metron call."""
    payload = json.loads((store / "collection.json").read_text())
    payload["series_name_index"] = index
    (store / "collection.json").write_text(json.dumps(payload))


_ASM_INDEX = {"amazing spider-man": "The Amazing Spider-Man"}


def test_record_win_appends_and_is_readable(client):
    _reseed_with_index(client.store, _ASM_INDEX)
    win = {
        "item_id": "115500000001",
        "current_bid": "42.00",
        "end_date_iso": "2026-06-04T18:00:00Z",
        "identify_data": {"series": "Amazing Spider-Man", "issue": "301", "year": "1988"},
    }
    r = client.post("/api/comics/collection/record-win", json={"wins": [win]})
    assert r.status_code == 200, r.text
    assert r.json()["rows_written"] == 1

    # R8: the append is immediately visible on the next read from the same store.
    # No `year` filter here: a record-win row resolved via series_name_index has
    # no Metron data, so release_date is None — a year-gated check would miss it
    # (pre-existing locg-cli behavior, faithfully wrapped by the endpoint).
    chk = client.get("/api/comics/collection/check", params={"series": "Amazing Spider-Man", "issue": "301"})
    assert chk.json()["match_status"] == "in_collection"


def test_record_win_skips_already_owned(client):
    _reseed_with_index(client.store, _ASM_INDEX)
    win = {
        "item_id": "115500000002",
        "current_bid": "999.00",
        "end_date_iso": "2026-06-04T18:00:00Z",
        "identify_data": {"series": "Amazing Spider-Man", "issue": "300", "year": "1988"},
    }
    r = client.post("/api/comics/collection/record-win", json={"wins": [win]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows_written"] == 0
    assert body["skipped_already_owned"] >= 1


def test_record_win_partial_failure_returns_non_200(client):
    """BUI-137: a chunked record-win where a later chunk's write raises returns
    partial_failure=True with only the committed rows counted. The endpoint must
    surface that as a non-200 (not a misleading HTTP 200) so the skill's `curl
    -sf` halts instead of reporting success and silently dropping the lost wins.
    """
    partial = {
        "rows_written": 25,
        "partial_failure": True,
        "manual_variant_count": 0,
        "manual_series_count": 0,
        "metron_lookups_succeeded": 0,
        "skipped_already_owned": 0,
    }
    with patch("gixen_overlay.routes.cmd_collection_record_win", return_value=partial):
        win = {
            "item_id": "115500009999",
            "current_bid": "10.00",
            "end_date_iso": "2026-06-04T18:00:00Z",
            "identify_data": {"series": "Amazing Spider-Man", "issue": "400", "year": "1995"},
        }
        r = client.post("/api/comics/collection/record-win", json={"wins": [win]})
    assert r.status_code == 500, r.text
    # The partial result is carried through so the user sees what was/wasn't written.
    detail = r.json()["detail"]
    assert detail["error"] == "partial_failure"
    assert detail["rows_written"] == 25


def test_record_win_non_runtime_error_returns_useful_500(client):
    """BUI-184: a non-RuntimeError raised mid-batch must surface as a 500 with a
    useful detail (which says the commit state is uncertain), not an opaque 500.
    """
    with patch(
        "gixen_overlay.routes.cmd_collection_record_win",
        side_effect=ValueError("boom mid-batch"),
    ):
        win = {
            "item_id": "115500008888",
            "current_bid": "10.00",
            "end_date_iso": "2026-06-04T18:00:00Z",
            "identify_data": {"series": "Amazing Spider-Man", "issue": "401", "year": "1995"},
        }
        r = client.post("/api/comics/collection/record-win", json={"wins": [win]})
    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "record_win_failed"
    assert "uncertain" in detail["message"]
    assert "ValueError" in detail["exception"]


# ===========================================================================
# BUI-428: POST /api/comics/collection/record-win/commit — the atomic
# merge + record + mark-seen + status endpoint that collapses
# /comic:collection-add Steps 3/3b/5 into one call.
# ===========================================================================

def test_record_win_commit_merges_and_marks_exactly_the_committed_set(client):
    """The mark-seen set must equal the set the server actually merged and
    submitted to cmd_collection_record_win — the core BUI-428 fix. Verified
    directly by reading back GET .../record-win/seen, not by trusting the
    response body alone.
    """
    _reseed_with_index(client.store, _ASM_INDEX)
    win_a = {
        "item_id": "115500010001",
        "current_bid": "12.00",
        "end_date_iso": "2026-06-04T18:00:00Z",
        "identify_data": {"series": "Amazing Spider-Man", "issue": "310", "year": "1988"},
    }
    win_b = {
        "item_id": "115500010002",
        "current_bid": "15.00",
        "end_date_iso": "2026-06-04T18:00:00Z",
        "identify_data": {"series": "Amazing Spider-Man", "issue": "311", "year": "1988"},
    }
    r = client.post(
        "/api/comics/collection/record-win/commit",
        json={"wins": [win_a], "resolved_reviews": [win_b]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows_written"] == 2
    assert set(body["committed_item_ids"]) == {"115500010001", "115500010002"}
    assert body["marked_seen"] == 2
    assert "pending_push_count" in body
    assert "oldest_pending_days" in body

    seen = client.get("/api/comics/collection/record-win/seen").json()["item_ids"]
    assert set(seen) == {"115500010001", "115500010002"}


def test_record_win_commit_skipped_already_owned_is_still_marked_seen(client):
    """A win that matches an already-owned row (BUI-34) is fully processed —
    just not re-written — so it belongs in the committed/seen set too, or a
    future run would re-POST it forever.
    """
    _reseed_with_index(client.store, _ASM_INDEX)
    win = {
        "item_id": "115500010003",
        "current_bid": "999.00",
        "end_date_iso": "2026-06-04T18:00:00Z",
        "identify_data": {"series": "Amazing Spider-Man", "issue": "300", "year": "1988"},
    }
    r = client.post(
        "/api/comics/collection/record-win/commit",
        json={"wins": [win], "resolved_reviews": []},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows_written"] == 0
    assert body["skipped_already_owned"] >= 1
    assert body["committed_item_ids"] == ["115500010003"]
    assert body["marked_seen"] == 1

    seen = client.get("/api/comics/collection/record-win/seen").json()["item_ids"]
    assert "115500010003" in seen


def test_record_win_commit_empty_payload_returns_zero_and_status_scalars(client):
    """Nothing new to record (both lists empty or omitted) still returns the
    scalar shape — including the fresh status fields — with zero counts and
    marks nothing seen, matching the old skill's 'nothing to record, still
    report status' branch.
    """
    r = client.post("/api/comics/collection/record-win/commit", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows_written"] == 0
    assert body["committed_item_ids"] == []
    assert body["marked_seen"] == 0
    assert "pending_push_count" in body
    assert "oldest_pending_days" in body


def test_record_win_commit_partial_failure_returns_500_and_marks_nothing_seen(client):
    """BUI-137/BUI-428: a partial_failure must still 500 (never a misleading
    200), AND — the atomicity requirement this endpoint adds — must mark
    NOTHING seen, so a retry still finds these item_ids unprocessed.
    """
    partial = {
        "rows_written": 25,
        "partial_failure": True,
        "manual_variant_count": 0,
        "manual_series_count": 0,
        "metron_lookups_succeeded": 0,
        "skipped_already_owned": 0,
    }
    win = {
        "item_id": "115500019999",
        "current_bid": "10.00",
        "end_date_iso": "2026-06-04T18:00:00Z",
        "identify_data": {"series": "Amazing Spider-Man", "issue": "402", "year": "1995"},
    }
    with patch("gixen_overlay.routes.cmd_collection_record_win", return_value=partial):
        r = client.post(
            "/api/comics/collection/record-win/commit",
            json={"wins": [win], "resolved_reviews": []},
        )
    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "partial_failure"
    assert detail["rows_written"] == 25

    seen = client.get("/api/comics/collection/record-win/seen").json()["item_ids"]
    assert "115500019999" not in seen


def test_record_win_commit_unhandled_exception_marks_nothing_seen(client):
    """BUI-184/BUI-428: an unhandled mid-batch exception must 500 (commit
    state uncertain) and must not mark the attempted item_ids seen — the
    mark-seen step must never run past an exception.
    """
    win = {
        "item_id": "115500018888",
        "current_bid": "10.00",
        "end_date_iso": "2026-06-04T18:00:00Z",
        "identify_data": {"series": "Amazing Spider-Man", "issue": "403", "year": "1995"},
    }
    with patch(
        "gixen_overlay.routes.cmd_collection_record_win",
        side_effect=ValueError("boom mid-batch"),
    ):
        r = client.post(
            "/api/comics/collection/record-win/commit",
            json={"wins": [win], "resolved_reviews": []},
        )
    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "record_win_failed"

    seen = client.get("/api/comics/collection/record-win/seen").json()["item_ids"]
    assert "115500018888" not in seen


def test_record_win_commit_dedups_item_id_shared_across_wins_and_reviews(client):
    """A lot expanding into multiple entries (or the same item_id appearing in
    both lists for any other reason) shares one item_id — the committed/seen
    set must dedup it, not double-count.
    """
    _reseed_with_index(client.store, _ASM_INDEX)
    win = {
        "item_id": "115500010005",
        "current_bid": "20.00",
        "end_date_iso": "2026-06-04T18:00:00Z",
        "identify_data": {"series": "Amazing Spider-Man", "issue": "312", "year": "1988"},
    }
    review = {
        "item_id": "115500010005",
        "current_bid": "20.00",
        "end_date_iso": "2026-06-04T18:00:00Z",
        "identify_data": {"series": "Amazing Spider-Man", "issue": "313", "year": "1988"},
    }
    r = client.post(
        "/api/comics/collection/record-win/commit",
        json={"wins": [win], "resolved_reviews": [review]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["committed_item_ids"] == ["115500010005"]
    assert body["marked_seen"] == 1


def test_wish_list_add_appends(client):
    r = client.post("/api/comics/wish-list", json={"title": "Daredevil #1"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert "Daredevil #1" in names


def test_wish_list_add_persists_year(client):
    """BUI-387: POST /api/comics/wish-list stamps the per-issue cover year on the
    created entry (a separate `year` field) — not only consumed by the add-time
    owned-guard — and it round-trips on the wish-list read (also exposed for
    seller-scan precision)."""
    r = client.post("/api/comics/wish-list", json={"title": "Daredevil #1", "year": "1964"})
    assert r.status_code == 200, r.text
    assert r.json()["added"]["year"] == "1964"
    items = {i["name"]: i for i in client.get("/api/comics/wish-list").json()}
    assert items["Daredevil #1"]["year"] == "1964"


def test_wish_list_add_without_year_stores_no_year_field(client):
    """BUI-387: omitting `year` keeps the exact pre-387 entry shape (no `year`
    key) so an unstamped wish stays year-blind in the conflicts audit."""
    r = client.post("/api/comics/wish-list", json={"title": "Daredevil #1"})
    assert r.status_code == 200, r.text
    items = {i["name"]: i for i in client.get("/api/comics/wish-list").json()}
    assert "year" not in items["Daredevil #1"]


def test_wish_list_add_rejects_malformed_year(client):
    """BUI-387: a malformed `year` (a range paste / garbage) is rejected 422 at
    the endpoint — the shared 4-digit guard in cmd_wish_list_add surfaces through
    api_wish_list_add's error path — and nothing is persisted."""
    r = client.post("/api/comics/wish-list", json={"title": "Daredevil #1", "year": "1963 - 2011"})
    assert r.status_code == 422, r.text
    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert "Daredevil #1" not in names


def test_wish_list_add_rejects_empty_title(client):
    assert client.post("/api/comics/wish-list", json={"title": "   "}).status_code == 422
    assert client.post("/api/comics/wish-list", json={}).status_code == 422


def test_wish_list_remove_deletes_item(client):
    """BUI-128: DELETE removes the matching entry and returns the locg-cli
    success shape."""
    r = client.delete("/api/comics/wish-list", params={"title": "X-Men #1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["removed"]["name"] == "X-Men #1"
    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert "X-Men #1" not in names


def test_wish_list_remove_404_when_title_not_found(client):
    r = client.delete("/api/comics/wish-list", params={"title": "Nonexistent #999"})
    assert r.status_code == 404
    # The other items are untouched.
    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert "X-Men #1" in names


def test_wish_list_remove_422_when_title_blank(client):
    assert client.delete("/api/comics/wish-list", params={"title": "   "}).status_code == 422
    assert client.delete("/api/comics/wish-list").status_code == 422


def test_wish_list_remove_404_when_never_imported(client):
    (client.store / "wish-list.json").unlink()
    r = client.delete("/api/comics/wish-list", params={"title": "X-Men #1"})
    assert r.status_code == 404


def test_import_requires_a_file(client):
    assert client.post("/api/comics/collection/import").status_code == 422


def test_import_rejects_bad_upload(client):
    r = client.post(
        "/api/comics/collection/import",
        files={"file": ("junk.xlsx", b"not a real xlsx", "application/octet-stream")},
    )
    assert r.status_code == 422


def test_import_rejects_over_cap_upload_without_parsing(client):
    """BUI-106: an over-cap upload is rejected with 413 during streaming, before
    the whole body is buffered and handed to the parser. We assert the parser
    (cmd_collection_import) is never reached — proof the abort happens early, not
    after locg-cli's stat()-based 10 MB guard fires on an already-buffered file."""
    from unittest.mock import patch
    from locg.collection_io import MAX_XLSX_BYTES

    oversize = b"\0" * (MAX_XLSX_BYTES + 1)
    with patch("gixen_overlay.routes.cmd_collection_import") as parse:
        r = client.post(
            "/api/comics/collection/import",
            files={"file": ("huge.xlsx", oversize, "application/octet-stream")},
        )
    assert r.status_code == 413
    parse.assert_not_called()


def test_import_applies_xlsx_and_is_readable(client):
    import io
    import openpyxl
    from locg.collection_io import LOCG_XLSX_HEADERS

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(LOCG_XLSX_HEADERS))
    ws.append([
        "Marvel", "X-Men", "X-Men #1", "1963-09-01",
        1, 0, 0, None, "Print", None, None, None, None,
        None, None, None, None, None, None, None, None,
    ])
    buf = io.BytesIO()
    wb.save(buf)

    r = client.post(
        "/api/comics/collection/import",
        files={"file": ("import.xlsx", buf.getvalue(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert r.status_code == 200, r.text
    # import_xlsx merges; the new row is added.
    assert r.json().get("added", 0) >= 1

    chk = client.get("/api/comics/collection/check", params={"series": "X-Men", "issue": "1", "year": "1963"})
    assert chk.json()["match_status"] == "in_collection"


# ===========================================================================
# Review hardening: R11 endpoint guard + import error-class distinction
# ===========================================================================

def test_check_refuses_when_store_never_imported(client):
    """R11 defense-in-depth: an un-imported store must NOT answer 'not owned'
    for everything — the endpoint returns 409, not 200 not_in_cache, so a
    caller that skips the bootstrap guard can't be tricked into a dupe buy."""
    (client.store / "collection.json").unlink()  # never-imported store
    r = client.get("/api/comics/collection/check", params={"series": "Batman", "issue": "1", "year": "1940"})
    assert r.status_code == 409


def test_check_not_in_cache_still_200_when_imported(client):
    """A genuinely-not-owned comic against an *imported* store is still a normal
    200 not_in_cache (the guard only fires on a never-imported store)."""
    r = client.get("/api/comics/collection/check", params={"series": "Batman", "issue": "1", "year": "1940"})
    assert r.status_code == 200
    assert r.json()["match_status"] == "not_in_cache"


def test_import_server_fault_is_not_masked_as_422(client):
    """An unexpected server-side fault during import (e.g. OSError) must NOT be
    caught by the narrow 'bad upload' clause and mislabeled a 422 client error.
    It propagates as a server fault (a 500 in production; the TestClient, with
    raise_server_exceptions=True, re-raises it here) — the point is it is never
    returned as a 422."""
    from unittest.mock import patch
    with patch("gixen_overlay.routes.cmd_collection_import", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            client.post(
                "/api/comics/collection/import",
                files={"file": ("import.xlsx", b"anything", "application/octet-stream")},
            )


# --- BUI-130: wish-list conflict audit + bulk remove + add prevention --------

def test_conflicts_flags_owned_wish_list_item(client):
    """A wish-list entry the user owns is surfaced; an unowned one is not."""
    _seed_wish_list(client.store, [
        {"name": "Amazing Spider-Man #300", "id": 111},  # owned (matches ASM #300)
        {"name": "X-Men #1", "id": None},                # not owned
    ])
    r = client.get("/api/comics/wish-list/conflicts")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    assert body["checked"] == 2
    assert [c["name"] for c in body["conflicts"]] == ["Amazing Spider-Man #300"]
    assert body["conflicts"][0]["full_title_matched"] == "The Amazing Spider-Man #300"


def test_conflicts_excludes_wish_only_row(client):
    """The default seed wish-lists Fantastic Four #48, which is in_collection=0
    (wish/read but not owned). The copies-owned gate means it is NOT a conflict."""
    r = client.get("/api/comics/wish-list/conflicts")
    assert r.status_code == 200, r.text
    assert r.json()["conflicts"] == []


def test_conflicts_409_when_never_imported(client):
    """An un-imported collection can't answer ownership — refuse rather than
    report a false 'no conflicts' (R11)."""
    (client.store / "collection.json").write_text(json.dumps({
        "schema_version": 1,
        "last_full_import": None,
        "series_name_index": {},
        "comics": [],
    }))
    assert client.get("/api/comics/wish-list/conflicts").status_code == 409


def test_remove_conflicts_unscoped_without_confirm_is_dry_run(client):
    """BUI-266 (P1): an unscoped call with no ``confirm`` must NOT mutate — it
    returns the same preview as the GET audit. This is the safe default that
    closes the BUI-259 incident (114 removed when ~6 were intended)."""
    _seed_wish_list(client.store, [
        {"name": "Amazing Spider-Man #300", "id": None},  # owned
        {"name": "X-Men #1", "id": None},                 # not owned
    ])
    r = client.post("/api/comics/wish-list/remove-conflicts")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["removed_count"] == 0
    assert [c["name"] for c in body["conflicts"]] == ["Amazing Spider-Man #300"]
    # Nothing was mutated.
    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert names == {"Amazing Spider-Man #300", "X-Men #1"}


def test_remove_conflicts_confirm_true_sweeps_all(client):
    """The original global-sweep behavior is still reachable via an explicit
    ``confirm: true`` — for a caller that has already reviewed the preview."""
    _seed_wish_list(client.store, [
        {"name": "Amazing Spider-Man #300", "id": None},  # owned
        {"name": "X-Men #1", "id": None},                 # not owned
    ])
    r = client.post("/api/comics/wish-list/remove-conflicts", json={"confirm": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["removed_count"] == 1
    assert body["remaining"] == 1
    assert body["scoped"] is False
    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert names == {"X-Men #1"}


def test_remove_conflicts_scoped_by_names_touches_only_named_set(client):
    """BUI-266: passing ``names`` scopes removal to exactly that set, so a
    caller can review the audit's provenance and remove only the confirmed
    conflicts without sweeping any other pre-existing one."""
    _seed_wish_list(client.store, [
        {"name": "Amazing Spider-Man #300", "id": None},  # owned
        {"name": "X-Men #1", "id": None},                 # not owned
    ])
    r = client.post(
        "/api/comics/wish-list/remove-conflicts",
        json={"names": ["Amazing Spider-Man #300"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["removed_count"] == 1
    assert body["scoped"] is True
    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert names == {"X-Men #1"}


def test_remove_conflicts_rejects_non_string_names(client):
    _seed_wish_list(client.store, [{"name": "Amazing Spider-Man #300", "id": None}])
    r = client.post("/api/comics/wish-list/remove-conflicts", json={"names": [123]})
    assert r.status_code == 422


def test_remove_conflicts_409_when_never_imported(client):
    (client.store / "collection.json").write_text(json.dumps({
        "schema_version": 1,
        "last_full_import": None,
        "series_name_index": {},
        "comics": [],
    }))
    assert client.post("/api/comics/wish-list/remove-conflicts").status_code == 409


def test_wish_list_add_rejects_owned_title(client):
    """Part 3: the API boundary refuses to wish-list a book already owned."""
    r = client.post("/api/comics/wish-list", json={"title": "Amazing Spider-Man #300"})
    assert r.status_code == 409, r.text
    # The owned title was not appended.
    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert "Amazing Spider-Man #300" not in names


def test_wish_list_add_409_when_cross_volume_owned_no_year(client):
    """BUI-284: the owned-guard must still 409 a book owned under >1 volume even
    with no year — cmd_collection_check returns `ambiguous_cross_volume`, which
    counts as owned. Treating it as not-owned would let an owned book onto the
    wish-list and be exported In Collection=0 → deleted (BUI-122)."""
    _seed_collection(client.store, _CROSS_VOLUME_FF18)
    r = client.post("/api/comics/wish-list", json={"title": "Fantastic Four #18"})
    assert r.status_code == 409, r.text
    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert "Fantastic Four #18" not in names


def test_wish_list_add_force_overrides_owned(client):
    r = client.post(
        "/api/comics/wish-list",
        json={"title": "Amazing Spider-Man #300", "force": True},
    )
    assert r.status_code == 200, r.text
    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert "Amazing Spider-Man #300" in names


def test_wish_list_add_allows_unowned_title(client):
    r = client.post("/api/comics/wish-list", json={"title": "Daredevil #181"})
    assert r.status_code == 200, r.text


def test_wish_list_add_duplicate_is_noop(client):
    """BUI-285: adding a series+issue already on the wish-list is a 200 no-op —
    it returns the existing entry and does NOT append a duplicate row (a dup
    would be double-pushed to LOCG and defeat the BUI-266 scoped removal).
    'X-Men #1' is already in the seeded wish-list."""
    before = client.get("/api/comics/wish-list").json()
    r = client.post("/api/comics/wish-list", json={"title": "X-Men #1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "exists"
    assert body["existing"]["name"] == "X-Men #1"
    assert body["items"] == len(before)  # superset field, unchanged count
    after = client.get("/api/comics/wish-list").json()
    assert len(after) == len(before)  # no new row appended
    assert sum(1 for i in after if i["name"] == "X-Men #1") == 1


def test_wish_list_add_force_appends_duplicate(client):
    """BUI-285: force=true bypasses the idempotency dedup (as it bypasses the
    owned-guard) — the escape hatch for a genuinely distinct printing that
    happens to share series + issue."""
    r = client.post("/api/comics/wish-list", json={"title": "X-Men #1", "force": True})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    names = [i["name"] for i in client.get("/api/comics/wish-list").json()]
    assert names.count("X-Men #1") == 2  # duplicate appended under force


def test_wish_list_add_unparseable_title_appends(client):
    """BUI-285: a title with no issue token can't be dedup-compared, so it
    appends as before rather than erroring."""
    r = client.post("/api/comics/wish-list", json={"title": "Some Graphic Novel"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert "Some Graphic Novel" in names


def test_wish_list_add_dedup_normalizes_issue_and_case(client):
    """BUI-285: dedup keys on the normalized issue token (leading zeros stripped)
    and is case-insensitive on the series — 'x-men #001' duplicates 'X-Men #1'."""
    r = client.post("/api/comics/wish-list", json={"title": "x-men #001"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "exists"
    names = [i["name"] for i in client.get("/api/comics/wish-list").json()]
    assert names.count("X-Men #1") == 1
    assert "x-men #001" not in names


def test_wish_list_add_distinct_volume_still_appends(client):
    """BUI-285/BUI-284: a volume-decorated name is a DISTINCT entry from a bare
    masthead of the same issue — dedup must not collapse it (never uses the
    normalized key). 'X-Men (Vol. 2) #1' appends alongside 'X-Men #1'."""
    r = client.post("/api/comics/wish-list", json={"title": "X-Men (Vol. 2) #1"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert "X-Men (Vol. 2) #1" in names
    assert "X-Men #1" in names  # original untouched


def test_wish_list_add_owned_guard_precedes_dedup(client):
    """BUI-285: the owned-guard still runs BEFORE the idempotency check — an owned
    book is 409'd, not silently reported as an existing wish."""
    r = client.post("/api/comics/wish-list", json={"title": "Amazing Spider-Man #300"})
    assert r.status_code == 409, r.text


def test_wish_list_add_year_catches_masthead_owned_book(client):
    """BUI-184/BUI-197: the owned-guard catches a book stored under its base
    masthead. BUI-197 routed the masthead alias through owned_match_keys, so the
    guard now fires WITH or WITHOUT a year — the no-year case is the safe
    direction (it blocks a wished-already-owned book from entering the list and
    later being exported as In Collection=0)."""
    _seed_collection(client.store, [{
        "full_title": "Thor #154",
        "series_name": "Thor",
        "publisher_name": "Marvel Comics",
        "release_date": "1968-07-01",
        "in_collection": 1,
    }])
    # With the per-issue cover year, "The Mighty Thor" → owned "Thor" → 409.
    owned = client.post(
        "/api/comics/wish-list",
        json={"title": "The Mighty Thor #154", "year": "1968"},
    )
    assert owned.status_code == 409, owned.text

    # BUI-197: WITHOUT the year the masthead alias now also fires → still 409.
    # This is strictly safer — it closes the no-year hole that let an owned book
    # be wish-listed and then deleted on the next sync.
    no_year = client.post(
        "/api/comics/wish-list", json={"title": "The Mighty Thor #154"}
    )
    assert no_year.status_code == 409, no_year.text


def test_wish_list_add_wrong_year_fails_open_not_false_block(client):
    """BUI-129 contract: a non-matching year must not falsely report owned. Own
    Thor #154 (1968); a query with the wrong cover year doesn't 409 (the per-issue
    year gate excludes the mismatched row) — a wrong year fails OPEN, it never
    closes onto the wrong book. So the year must be the issue's actual cover
    year, never year_began."""
    _seed_collection(client.store, [{
        "full_title": "Thor #154",
        "series_name": "Thor",
        "publisher_name": "Marvel Comics",
        "release_date": "1968-07-01",
        "in_collection": 1,
    }])
    r = client.post(
        "/api/comics/wish-list",
        json={"title": "The Mighty Thor #154", "year": "1975"},
    )
    assert r.status_code == 200, r.text


# --- BUI-372: printing awareness on the wish-list paths ----------------------

def test_wish_list_add_409_detail_is_additive_dict(client):
    """BUI-372: the 409 detail is now a dict — additive over the prior plain
    string. A genuine duplicate (no printing marker involved) reports
    printing_conflict=False/printing_candidates=None, and the human-readable
    text still lives at detail['message'] for any consumer that used to read
    detail as a string."""
    r = client.post("/api/comics/wish-list", json={"title": "Amazing Spider-Man #300"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["error"] == "already_owned"
    assert "force=true" in detail["message"]
    assert detail["full_title_matched"] == "The Amazing Spider-Man #300"
    assert detail["printing_conflict"] is False
    assert detail["printing_candidates"] is None


def test_wish_list_add_409_detail_surfaces_printing_conflict(client):
    """BUI-372: a printing-conflict 409 (AMM #1 incident, reproduced at
    wish-list-add time) carries printing_conflict=True plus the
    printing_candidates list, so a caller can tell force=true is the CORRECT
    action here — a distinct printing, not a genuine duplicate."""
    _seed_collection(client.store, _AMM_PRINTINGS)
    r = client.post(
        "/api/comics/wish-list", json={"title": "Absolute Martian Manhunter #1"}
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["printing_conflict"] is True
    assert detail["full_title_matched"] == "Absolute Martian Manhunter #1 2nd Printing"
    titles = {c["full_title"] for c in detail["printing_candidates"]}
    assert "Absolute Martian Manhunter #1" in titles

    # force=true is the documented escape hatch and still works.
    forced = client.post(
        "/api/comics/wish-list",
        json={"title": "Absolute Martian Manhunter #1", "force": True},
    )
    assert forced.status_code == 200, forced.text


# --- wish-list add (batch, BUI-447) -----------------------------------------

def test_wish_list_add_batch_new_items_are_ok(client):
    """Two brand-new titles in one batch call both add successfully."""
    r = client.post(
        "/api/comics/wish-list/batch",
        json={"items": [{"title": "Daredevil #181"}, {"title": "Swamp Thing #21"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert [item["status"] for item in body["results"]] == ["ok", "ok"]
    assert [item["title"] for item in body["results"]] == ["Daredevil #181", "Swamp Thing #21"]

    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert {"Daredevil #181", "Swamp Thing #21"} <= names


def test_wish_list_add_batch_existing_item_is_idempotent(client):
    """BUI-285 idempotency carries into the batch path: re-adding 'X-Men #1'
    (already seeded) is a no-op reported as status 'exists', not a duplicate row."""
    before = client.get("/api/comics/wish-list").json()
    r = client.post("/api/comics/wish-list/batch", json={"items": [{"title": "X-Men #1"}]})
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["status"] == "exists"
    assert result["existing"]["name"] == "X-Men #1"

    after = client.get("/api/comics/wish-list").json()
    assert len(after) == len(before)  # no new row appended
    assert sum(1 for i in after if i["name"] == "X-Men #1") == 1


def test_wish_list_add_batch_owned_item_is_rejected(client):
    """The BUI-130 owned-guard fires per item — an already-owned title in the
    batch is reported as owned-409, not silently added (BUI-122 data-loss guard)."""
    r = client.post(
        "/api/comics/wish-list/batch",
        json={"items": [{"title": "Amazing Spider-Man #300"}]},
    )
    assert r.status_code == 200, r.text  # the batch call itself always 200s
    result = r.json()["results"][0]
    assert result["status"] == "owned-409"
    assert result["full_title_matched"] == "The Amazing Spider-Man #300"
    assert "force=true" in result["message"]
    assert result["printing_conflict"] is False

    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert "Amazing Spider-Man #300" not in names


def test_wish_list_add_batch_owned_item_with_force_is_added(client):
    """force=true on the OWNED item itself overrides the guard — per item, not
    a batch-wide switch."""
    r = client.post(
        "/api/comics/wish-list/batch",
        json={"items": [{"title": "Amazing Spider-Man #300", "force": True}]},
    )
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["status"] == "ok"

    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert "Amazing Spider-Man #300" in names


def test_wish_list_add_batch_partial_mix_reports_each_item_correctly(client):
    """Adversarial case: a batch with one new, one owned (no force), one owned
    WITH force, and one already-wishlisted item must report each item's own
    correct status — the owned-guard must never leak across items (an owned
    item's force=True must not waive the guard for a different owned item that
    didn't set force), and nothing is silently dropped."""
    r = client.post(
        "/api/comics/wish-list/batch",
        json={
            "items": [
                {"title": "Daredevil #181"},  # new -> ok
                {"title": "Amazing Spider-Man #300"},  # owned, no force -> owned-409
                {"title": "Amazing Spider-Man #300", "force": True},  # owned, force -> ok
                {"title": "X-Men #1"},  # already wishlisted -> exists
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 4
    statuses = [item["status"] for item in body["results"]]
    assert statuses == ["ok", "owned-409", "ok", "exists"]

    names = [i["name"] for i in client.get("/api/comics/wish-list").json()]
    assert "Daredevil #181" in names
    assert names.count("Amazing Spider-Man #300") == 1  # only the force=true add landed
    assert names.count("X-Men #1") == 1  # the exists no-op did not duplicate it


def test_wish_list_add_batch_rejects_empty_items(client):
    r = client.post("/api/comics/wish-list/batch", json={"items": []})
    assert r.status_code == 422


def test_wish_list_add_batch_rejects_blank_title(client):
    r = client.post(
        "/api/comics/wish-list/batch", json={"items": [{"title": "  "}]}
    )
    assert r.status_code == 422


def test_wish_list_add_batch_item_error_does_not_abort_others(client):
    """A malformed per-item year is reported as status 'error' for that item
    only — it must not fail the whole batch or block a valid sibling item."""
    r = client.post(
        "/api/comics/wish-list/batch",
        json={
            "items": [
                {"title": "Daredevil #181", "year": "1963 - 2011"},  # malformed year
                {"title": "Swamp Thing #21"},  # valid, unaffected
            ]
        },
    )
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results[0]["status"] == "error"
    assert results[1]["status"] == "ok"

    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert "Daredevil #181" not in names  # the malformed-year add did not persist
    assert "Swamp Thing #21" in names


def test_conflicts_endpoint_excludes_printing_decoy(client):
    """BUI-372: an owned reprint matching a wishlisted BASE printing is not a
    genuine conflict — GET .../conflicts puts it in printing_conflicts, not
    conflicts, so it can never be swept by remove-conflicts."""
    _seed_collection(client.store, _AMM_PRINTINGS)
    _seed_wish_list(client.store, [{"name": "Absolute Martian Manhunter #1", "id": None}])

    r = client.get("/api/comics/wish-list/conflicts")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["conflicts"] == []
    assert [c["name"] for c in body["printing_conflicts"]] == ["Absolute Martian Manhunter #1"]


def test_remove_conflicts_endpoint_never_removes_printing_decoy(client):
    """BUI-372: even an explicit confirm=true unscoped sweep must not remove a
    printing-conflict decoy — it was never in `conflicts` to begin with."""
    _seed_collection(client.store, _AMM_PRINTINGS)
    _seed_wish_list(client.store, [{"name": "Absolute Martian Manhunter #1", "id": None}])

    r = client.post("/api/comics/wish-list/remove-conflicts", json={"confirm": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["removed_count"] == 0
    assert len(body["printing_conflicts"]) == 1
    names = {i["name"] for i in client.get("/api/comics/wish-list").json()}
    assert names == {"Absolute Martian Manhunter #1"}
