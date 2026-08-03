"""BUI-601: the rejected-writes ledger.

BUI-593 is the motivating incident — an FMV fetch succeeded, the write 422'd,
the book was stored nowhere, and nothing persisted the refusal (BUI-585 then
sat blocked for days on the wrong theory). These tests pin the three things
that make the ledger worth having:

1. it records the refusal, with the status the CLIENT actually saw;
2. it covers routes it has never heard of, with zero per-endpoint code;
3. it can never turn a working request into a broken one.
"""
from __future__ import annotations

import json
import os
import sqlite3
from unittest.mock import patch

import pytest
from fastapi import APIRouter, Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

from gixen_overlay.db import record_rejected_write, rejected_writes_report
from gixen_overlay.ledger import LedgerRoute


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# `api` fixture: see conftest.py (BUI-630 de-duplicated the three hand-copies).


class _ProbeBody(BaseModel):
    n: int


class _ProbeSecretBody(BaseModel):
    api_token: int


def _probe_app() -> FastAPI:
    """A throwaway app whose routes the ledger has never heard of.

    This is the "new overlay endpoints are covered with zero per-endpoint
    code" acceptance criterion, tested literally: nothing below imports,
    references, or is referenced by the ledger — the only wiring is
    `route_class=LedgerRoute`, exactly as `routes.router` declares it.

    It is a separate app rather than routes bolted onto the real one so the
    probes cannot leak into the module-global `server.main.app` that every
    other test in the suite shares. The ledger writes through
    `server.main`'s module globals (`_get_db_path` / `_write_locked`), which
    the `api` fixture's lifespan has already initialised, so the rows still
    land in the real test DB.
    """
    probe = APIRouter(route_class=LedgerRoute)

    @probe.post("/probe/validate")
    async def _validate(body: _ProbeBody):  # pragma: no cover - 422s before entry
        return {"ok": body.n}

    @probe.post("/probe/http422")
    async def _http422(body: dict | None = Body(default=None)):
        raise HTTPException(status_code=422, detail="explicitly refused")

    @probe.post("/probe/nobody")
    async def _nobody():
        raise HTTPException(status_code=422, detail="takes no body")

    @probe.post("/probe/secretfield")
    async def _secretfield(body: _ProbeSecretBody):  # pragma: no cover - 422s first
        return {"ok": True}

    @probe.post("/probe/returned409")
    async def _returned409():
        return JSONResponse(status_code=409, content={"detail": "returned not raised"})

    @probe.post("/probe/boom")
    async def _boom():
        raise RuntimeError("unhandled crash")

    @probe.post("/probe/ok")
    async def _ok():
        return {"ok": True}

    @probe.get("/probe/get404")
    async def _get404():
        raise HTTPException(status_code=404, detail="a read, not a write")

    @probe.post("/probe/upload")
    async def _upload(f: UploadFile = File(...)):
        raise HTTPException(status_code=400, detail="upload refused")

    app = FastAPI()
    app.include_router(probe)
    return app


@pytest.fixture
def probe(api):
    """Probe client. Depends on `api` so the host lifespan globals are live."""
    with TestClient(_probe_app(), raise_server_exceptions=False) as client:
        yield client


def _ledger(api) -> list[dict]:
    return api.get("/api/comics/health/rejections?hours=24").json()["rejections"]


# ---------------------------------------------------------------------------
# The acceptance criterion: any 422 on a write endpoint produces a row
# ---------------------------------------------------------------------------


def test_real_overlay_422_is_persisted(api):
    """A pydantic rejection on a REAL overlay write endpoint lands in the ledger.

    `/api/comics/seller-scan/seen` takes `{item_ids: [...]}`; a string where the
    list belongs is refused by validation before the handler runs — the shape of
    failure that used to leave no trace anywhere.
    """
    r = api.post("/api/comics/seller-scan/seen", json={"item_ids": "not-a-list"})
    assert r.status_code == 422

    rows = _ledger(api)
    assert len(rows) == 1
    assert rows[0]["path"] == "/api/comics/seller-scan/seen"
    assert rows[0]["method"] == "POST"
    assert rows[0]["status"] == 422


def test_validation_error_records_422_not_500(probe, api):
    """THE trap (BUI-601 Fact 3).

    At the route-handler layer FastAPI has not yet mapped RequestValidationError
    onto a 422 response, so a naive `getattr(exc, "status_code", 500)` records
    **500** while the client is handed **422** — mis-filing precisely the class
    of failure this ledger was built for. Assert the recorded status matches
    what the client saw.
    """
    r = probe.post("/probe/validate", json={"n": "not-an-int"})
    assert r.status_code == 422, "client sees 422"

    rows = [x for x in _ledger(api) if x["path"] == "/probe/validate"]
    assert len(rows) == 1
    assert rows[0]["status"] == 422, (
        "ledger recorded the pre-mapping 500 instead of the 422 the client got"
    )
    # The detail must carry the validation errors, not a bare repr.
    assert "not-an-int" in rows[0]["detail"] or "int" in rows[0]["detail"]


def test_raised_http_exception_is_recorded_with_its_status(probe, api):
    assert probe.post("/probe/http422").status_code == 422
    rows = [x for x in _ledger(api) if x["path"] == "/probe/http422"]
    assert len(rows) == 1
    assert rows[0]["status"] == 422
    assert "explicitly refused" in rows[0]["detail"]


def test_returned_error_response_is_recorded(probe, api):
    """A handler that RETURNS a 4xx (rather than raising) is recorded too."""
    assert probe.post("/probe/returned409").status_code == 409
    rows = [x for x in _ledger(api) if x["path"] == "/probe/returned409"]
    assert len(rows) == 1
    assert rows[0]["status"] == 409
    assert "returned not raised" in rows[0]["detail"]


def test_unhandled_exception_is_recorded_as_500(probe, api):
    assert probe.post("/probe/boom").status_code == 500
    rows = [x for x in _ledger(api) if x["path"] == "/probe/boom"]
    assert len(rows) == 1
    assert rows[0]["status"] == 500
    assert "RuntimeError" in rows[0]["detail"]


def test_healthy_write_is_not_recorded(probe, api):
    assert probe.post("/probe/ok").status_code == 200
    assert _ledger(api) == []


def test_failed_read_is_not_recorded(probe, api):
    """Only MUTATING requests are ledgered — a rejected GET loses no data."""
    assert probe.get("/probe/get404").status_code == 404
    assert _ledger(api) == []


def test_query_string_is_recorded(probe, api):
    probe.post("/probe/http422?trace=abc")
    rows = [x for x in _ledger(api) if x["path"] == "/probe/http422"]
    assert rows[0]["query"] == "trace=abc"


def test_route_without_a_body_param_records_the_query_instead(probe, api):
    """A documented boundary of the snapshot.

    The ledger reads only the body FastAPI already buffered, never the raw
    stream (see `_ledger_payload_snapshot` — re-reading a consumed stream
    raises, and awaiting an unread one could block on a slow client while the
    response sits ready to send). A handler that declares no body parameter
    never causes a read, so there is nothing to snapshot. That costs nothing
    in practice: such endpoints carry their input in the query string, which
    IS recorded, and every write that can fail the BUI-593 way declares a body
    model — FastAPI reads it before it can decide the request is invalid.
    """
    assert probe.post("/probe/nobody?series=X-Men&issue=238").status_code == 422
    row = [x for x in _ledger(api) if x["path"] == "/probe/nobody"][0]
    assert row["payload"] is None
    assert row["query"] == "series=X-Men&issue=238"


# ---------------------------------------------------------------------------
# Hard constraint 1: recording must never change response behaviour
# ---------------------------------------------------------------------------


def test_ledger_write_failure_leaves_the_response_untouched(probe, api, caplog):
    """A broken ledger degrades to a log line, never to a broken request.

    A locked DB, a full disk, or a version-skewed deploy missing the table must
    not convert an ordinary 422 into a 500 — nor an ordinary 200 into anything
    at all.
    """
    def _explode(*a, **kw):
        raise sqlite3.OperationalError("no such table: rejected_writes")

    with patch("gixen_overlay.ledger.record_rejected_write", side_effect=_explode):
        rejected = probe.post("/probe/http422")
        healthy = probe.post("/probe/ok")

    assert rejected.status_code == 422
    assert rejected.json() == {"detail": "explicitly refused"}
    assert healthy.status_code == 200
    assert healthy.json() == {"ok": True}
    # Nothing was persisted, and the failure was surfaced in the log rather
    # than swallowed silently.
    assert _ledger(api) == []
    assert any(
        "rejected-writes ledger failed to record" in r.message for r in caplog.records
    )


def test_endpoint_that_raises_inside_the_write_lock_does_not_deadlock(api):
    """The ledger writes under the same app-wide `_write_lock` the handlers
    use (BUI-408). `api_link_locg` raises its 409 from INSIDE its own
    `async with _write_locked()` block — if the lock were still held when the
    ledger tried to take it, the server would hang forever on this request
    rather than return.

    Seeded with a title carrying no grade, so extract-comics creates no fmv
    row and `fmv_id` stays NULL, which is the 409's trigger.
    """
    import sqlite3 as _sqlite3

    api.post("/api/bids", json={"item_id": "555000601", "max_bid": 30.0})
    raw = _sqlite3.connect(os.environ["DB_PATH"])
    raw.execute(
        "UPDATE bids SET ebay_title=? WHERE item_id=?",
        ("Daredevil #1 1993", "555000601"),
    )
    raw.commit()
    raw.close()
    api.post("/api/extract-comics")

    r = api.post("/api/bids/555000601/comics/locg", json={"locg_id": 1931245})
    assert r.status_code == 409, "test no longer exercises the in-lock raise"

    rows = [x for x in _ledger(api) if x["path"].endswith("/comics/locg")]
    assert len(rows) == 1
    assert rows[0]["status"] == 409


def test_original_exception_type_propagates_unchanged(probe):
    """The wrapper re-raises with a bare `raise` — no wrapping, no substitution."""
    with TestClient(_probe_app(), raise_server_exceptions=True) as strict:
        with pytest.raises(RuntimeError, match="unhandled crash"):
            strict.post("/probe/boom")


# ---------------------------------------------------------------------------
# Hard constraints 2 + 3: bounded payloads, no secrets
# ---------------------------------------------------------------------------


def test_multipart_upload_body_is_never_buffered(probe, api):
    """The overlay has a real XLSX upload endpoint. Its bytes must never be
    read into memory or into SQLite just to log a rejection."""
    blob = b"x" * (512 * 1024)
    assert probe.post("/probe/upload", files={"f": ("big.xlsx", blob)}).status_code == 400

    rows = [x for x in _ledger(api) if x["path"] == "/probe/upload"]
    assert len(rows) == 1
    assert "not captured" in rows[0]["payload"]
    assert "multipart" in rows[0]["payload"]
    assert len(rows[0]["payload"]) < 200


def test_large_json_payload_is_truncated(probe, api):
    probe.post("/probe/http422", json={"blob": "y" * 20000})
    rows = [x for x in _ledger(api) if x["path"] == "/probe/http422"]
    payload = rows[0]["payload"]
    assert len(payload) < 2200, "payload snapshot is not bounded"
    assert "truncated" in payload


def test_oversized_body_is_not_parsed_at_all(probe, api):
    probe.post("/probe/http422", json={"blob": "z" * 200_000})
    rows = [x for x in _ledger(api) if x["path"] == "/probe/http422"]
    assert "exceeds snapshot cap" in rows[0]["payload"]


@pytest.mark.parametrize(
    "key", ["password", "api_key", "GIXEN_PASSWORD", "auth_token", "sessionId"]
)
def test_credential_like_keys_are_redacted(probe, api, key):
    probe.post("/probe/http422", json={key: "hunter2", "series": "X-Men"})
    rows = [x for x in _ledger(api) if x["path"] == "/probe/http422"]
    payload = rows[-1]["payload"]
    assert "hunter2" not in payload
    assert "<redacted>" in payload
    assert "X-Men" in payload, "redaction must not blank the diagnostic content"


def test_nested_credentials_are_redacted(probe, api):
    probe.post(
        "/probe/http422",
        json={"outer": [{"inner": {"secret": "s3kr1t", "issue": "238"}}]},
    )
    payload = [x for x in _ledger(api) if x["path"] == "/probe/http422"][0]["payload"]
    assert "s3kr1t" not in payload
    assert "238" in payload


def test_author_is_not_mistaken_for_a_credential(probe, api):
    """Over-redaction is the safe direction, but not at the cost of blanking
    real comic metadata — a bare `auth` pattern would eat `author`."""
    probe.post("/probe/http422", json={"author": "Chris Claremont"})
    payload = [x for x in _ledger(api) if x["path"] == "/probe/http422"][0]["payload"]
    assert "Chris Claremont" in payload


def test_credential_in_the_query_string_is_redacted(probe, api):
    """The query string is as much "the payload" as the body for the write
    endpoints that take their input there — storing it raw would leak a
    `?token=...` straight past the body redaction."""
    probe.post("/probe/nobody?series=X-Men&api_key=sk-live-abc123")
    row = [x for x in _ledger(api) if x["path"] == "/probe/nobody"][0]
    assert "sk-live-abc123" not in row["query"]
    assert "api_key=<redacted>" in row["query"]
    assert "series=X-Men" in row["query"]


def test_validation_error_does_not_echo_the_rejected_secret_back(probe, api):
    """Pydantic errors carry the value that failed under `input`. When the
    failing field IS the credential, key-based redaction misses it — the key
    in the error dict is the innocuous string "input" — so the `loc` has to
    decide. Without this the ledger would store the very secret it refused.
    """
    r = probe.post("/probe/secretfield", json={"api_token": "sk-live-abc123"})
    assert r.status_code == 422
    row = [x for x in _ledger(api) if x["path"] == "/probe/secretfield"][0]
    assert "sk-live-abc123" not in (row["detail"] or ""), "secret echoed in detail"
    assert "sk-live-abc123" not in (row["payload"] or ""), "secret echoed in payload"
    assert "<redacted>" in row["detail"]
    # The diagnostic value survives: we still know which field failed and why.
    assert "api_token" in row["detail"]


def test_non_json_body_with_credential_is_withheld_wholesale(probe, api):
    """We can't tell a credential field from a data field in a form body, so a
    body that looks like it holds one is withheld entirely rather than risked."""
    probe.post(
        "/probe/http422",
        content=b"user=hsu&password=hunter2",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    payload = [x for x in _ledger(api) if x["path"] == "/probe/http422"][0]["payload"]
    assert "hunter2" not in payload
    assert "withheld" in payload


# ---------------------------------------------------------------------------
# The report endpoint + retention
# ---------------------------------------------------------------------------


def test_rejections_endpoint_shape(api):
    body = api.get("/api/comics/health/rejections").json()
    assert body["count"] == 0
    assert body["rejections"] == []
    assert body["window_hours"] == 24.0
    assert body["retention_days"] == 30


def test_rejections_endpoint_rejects_a_nonsense_window(api):
    assert api.get("/api/comics/health/rejections?hours=0").status_code == 422


def test_count_is_not_capped_by_limit(tmp_path):
    """The dashboard badge reads `count`; it must not under-report just because
    the row list is capped."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from gixen_overlay.db import create_tables
    create_tables(conn)
    for i in range(12):
        record_rejected_write(
            conn, method="POST", path=f"/p/{i}", status=422, detail="d"
        )
    report = rejected_writes_report(conn, hours=24, limit=3)
    assert report["count"] == 12
    assert len(report["rejections"]) == 3
    # Newest first.
    assert report["rejections"][0]["path"] == "/p/11"


def test_window_excludes_older_rows(tmp_path):
    from datetime import datetime, timedelta, timezone

    from gixen_overlay.db import create_tables
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_tables(conn)
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    record_rejected_write(
        conn, method="POST", path="/recent", status=422,
        at=(now - timedelta(hours=1)).isoformat(),
    )
    record_rejected_write(
        conn, method="POST", path="/old", status=422,
        at=(now - timedelta(hours=48)).isoformat(),
    )
    report = rejected_writes_report(conn, hours=24, now=now)
    assert report["count"] == 1
    assert report["rejections"][0]["path"] == "/recent"


def test_row_cap_bounds_the_table_independently_of_age(tmp_path):
    """Retention is time-based, so a caller stuck in a retry loop could still
    pile up unboundedly *within* the 30-day window — and a retry loop against
    a rejecting endpoint is exactly the traffic this ledger attracts."""
    from gixen_overlay import db as overlay_db
    from gixen_overlay.db import create_tables

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_tables(conn)
    with patch.object(overlay_db, "REJECTED_WRITES_MAX_ROWS", 10):
        for i in range(25):
            record_rejected_write(
                conn, method="POST", path=f"/p/{i}", status=422, detail="d"
            )
    total = conn.execute("SELECT COUNT(*) FROM rejected_writes").fetchone()[0]
    assert total <= 11, f"table grew to {total} rows despite the cap"
    # The newest rows are the ones kept.
    newest = conn.execute(
        "SELECT path FROM rejected_writes ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert newest == "/p/24"


def test_retention_prunes_on_insert(tmp_path):
    from datetime import datetime, timedelta, timezone

    from gixen_overlay.db import (
        REJECTED_WRITES_RETENTION_DAYS,
        create_tables,
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_tables(conn)
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    ancient = now - timedelta(days=REJECTED_WRITES_RETENTION_DAYS + 1)
    record_rejected_write(
        conn, method="POST", path="/ancient", status=422, at=ancient.isoformat()
    )
    assert conn.execute("SELECT COUNT(*) FROM rejected_writes").fetchone()[0] == 1
    # The next insert sweeps it.
    record_rejected_write(
        conn, method="POST", path="/fresh", status=422, at=now.isoformat()
    )
    paths = [r[0] for r in conn.execute("SELECT path FROM rejected_writes")]
    assert paths == ["/fresh"]


# ---------------------------------------------------------------------------
# Schema is purely additive (hard constraint 4)
# ---------------------------------------------------------------------------


def test_create_tables_is_additive_on_a_populated_db(api, tmp_path):
    """Re-running create_tables against a DB that already has comic rows must
    not rebuild the comic tables or disturb the partial unique indexes."""
    from gixen_overlay.db import create_tables, upsert_comic

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    comic_id = upsert_comic(conn, title="X-Men", issue="1", year=1963)
    before = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    create_tables(conn)
    conn.commit()
    after = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert before <= after, "an existing index was dropped"
    assert "idx_comics_tiyv" in after
    assert conn.execute(
        "SELECT COUNT(*) FROM comics WHERE id=?", (comic_id,)
    ).fetchone()[0] == 1
    conn.close()


def test_ledger_row_payload_is_valid_json_when_body_was_json(probe, api):
    probe.post("/probe/http422", json={"series": "X-Men", "issue": "238"})
    payload = [x for x in _ledger(api) if x["path"] == "/probe/http422"][0]["payload"]
    assert json.loads(payload) == {"series": "X-Men", "issue": "238"}
