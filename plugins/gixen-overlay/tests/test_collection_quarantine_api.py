"""The served quarantine write path (BUI-648).

`POST /api/comics/collection/quarantine` and `.../unquarantine` wrap locg-cli's
`cmd_collection_(un)quarantine` — the identity resolution, the
`CollectionCache.apply` write and the last-owned-row guard all live there and
are covered by `packages/locg-cli/tests/test_quarantine_write_path.py`. What
this file pins down is the part only the endpoint owns: that each refusal
reaches the caller as its own HTTP status with the store untouched, rather than
as a 200 body that reads like success.

The store lives on the Mac Mini, which is why `unquarantine` is served at all —
a reversal reachable only from a CLI on the right host is not a reversal.
"""
from __future__ import annotations

import json

import pytest

from .conftest import _seed_collection, _seeded_client

# The real shape this state exists for (BUI-563): an Italian Panini licensed
# edition our own record-win push minted, standing beside the US copy. The
# Panini row is the ordinary quarantine target — the US row still answers
# ownership, so no guard fires.
_US_ASM_238 = {
    "full_title": "The Amazing Spider-Man #238",
    "series_name": "The Amazing Spider-Man (Vol. 1) (1963 - 1998)",
    "publisher_name": "Marvel Comics",
    "release_date": "1983-03-01",
    "in_collection": 1,
    "source": "locg_export",
}
_IT_ASM_238 = {
    "full_title": "The Amazing Spider-Man #238",
    "series_name": "L'Uomo Ragno (Vol. 1) (1994 - 2000)",
    "publisher_name": "Panini Comics",
    "release_date": "1994-06-01",
    "in_collection": 1,
    "source": "agent_win",
}

_IT_IDENTITY = {
    "publisher_name": "Panini Comics",
    "series_name": "L'Uomo Ragno (Vol. 1) (1994 - 2000)",
    "full_title": "The Amazing Spider-Man #238",
    "release_date": "1994-06-01",
}
_MARKER_FIELDS = {
    "reason": "Italian licensed edition, not the US book",
    "ticket": "BUI-648",
    "by": "tester",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    _seed_collection(store, [_IT_ASM_238, _US_ASM_238])

    with _seeded_client(tmp_path, monkeypatch, store) as c:
        yield c


def _comics(client) -> list[dict]:
    return json.loads((client.store / "collection.json").read_text())["comics"]


def _quarantine(client, **overrides):
    return client.post(
        "/api/comics/collection/quarantine",
        json={**_IT_IDENTITY, **_MARKER_FIELDS, **overrides},
    )


# ---------------------------------------------------------------------------
# The ordinary path
# ---------------------------------------------------------------------------

def test_quarantine_marks_the_row_and_returns_the_audit_record(client):
    r = _quarantine(client)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["forced"] is False
    assert body["audit"]["type"] == "collection_quarantine"
    assert body["audit"]["details"]["identity"] == _IT_IDENTITY
    # The rows the guard leaned on, carried through to the caller — the
    # pass-branch counterpart to `forced`.
    assert [c["series_name"] for c in body["covering_rows"]] == [
        _US_ASM_238["series_name"]
    ]

    rows = _comics(client)
    assert rows[0]["quarantined"]["reason"] == _MARKER_FIELDS["reason"]
    assert "quarantined" not in rows[1], "the US copy must be untouched"


def test_unquarantine_lifts_it_and_returns_its_own_audit_record(client):
    assert _quarantine(client).status_code == 200

    r = client.post(
        "/api/comics/collection/unquarantine",
        json={**_IT_IDENTITY, "by": "tester"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["audit"]["type"] == "collection_unquarantine"
    assert body["removed"]["ticket"] == "BUI-648"
    assert "quarantined" not in _comics(client)[0]


# ---------------------------------------------------------------------------
# Refusals — each one its own status, and nothing written
# ---------------------------------------------------------------------------

def test_last_owned_copy_is_409_and_writes_nothing(client):
    """The refusal that costs money if it is ever served as a 200: hiding the
    only owned copy makes `collection check` report the book not-owned and the
    buy path re-buys it. 409 (not 422) — the request is well-formed, it is the
    store's state that makes it unsafe."""
    _seed_collection(client.store, [_IT_ASM_238])
    before = (client.store / "collection.json").read_bytes()

    r = _quarantine(client)

    assert r.status_code == 409
    assert r.json()["detail"]["status"] == "last_owned_row"
    assert r.json()["detail"]["reason_detail"] == "no_covering_row"
    assert (client.store / "collection.json").read_bytes() == before


def test_force_overrides_the_guard_and_stores_the_reason(client):
    _seed_collection(client.store, [_IT_ASM_238])

    r = _quarantine(client, force=True, force_reason="verified sold, no copy remains")

    assert r.status_code == 200, r.text
    assert r.json()["forced"] is True
    assert _comics(client)[0]["quarantined"]["forced"] == {
        "guard": "last_owned_row",
        "reason": "verified sold, no copy remains",
    }


def test_force_without_a_reason_is_422(client):
    _seed_collection(client.store, [_IT_ASM_238])
    before = (client.store / "collection.json").read_bytes()

    r = _quarantine(client, force=True)

    assert r.status_code == 422
    assert (client.store / "collection.json").read_bytes() == before


def test_ambiguous_identity_is_409_and_writes_nothing(client):
    _seed_collection(client.store, [_IT_ASM_238, _IT_ASM_238, _US_ASM_238])
    before = (client.store / "collection.json").read_bytes()

    r = _quarantine(client)

    assert r.status_code == 409
    assert r.json()["detail"]["status"] == "ambiguous"
    assert r.json()["detail"]["count"] == 2
    assert (client.store / "collection.json").read_bytes() == before


def test_unknown_identity_is_404(client):
    r = _quarantine(client, full_title="Nobody Owns This #1")
    assert r.status_code == 404


def test_re_quarantining_is_409_and_keeps_the_original_marker(client):
    assert _quarantine(client).status_code == 200
    original = _comics(client)[0]["quarantined"]

    r = _quarantine(client, reason="a different story", by="someone else")

    assert r.status_code == 409
    assert r.json()["detail"]["status"] == "already_quarantined"
    assert _comics(client)[0]["quarantined"] == original


def test_unquarantining_a_clean_row_is_409(client):
    r = client.post(
        "/api/comics/collection/unquarantine",
        json={**_IT_IDENTITY, "by": "tester"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["status"] == "not_quarantined"


@pytest.mark.parametrize("blank", ["reason", "ticket", "by"])
def test_an_unattributable_quarantine_is_422(client, blank):
    """Rejected by the request model, before the store is opened at all — a
    marker that cannot say who hid the row or why is one nobody can lift."""
    r = _quarantine(client, **{blank: "  "})
    assert r.status_code == 422


@pytest.mark.parametrize("path", ["quarantine", "unquarantine"])
def test_an_empty_full_title_is_422(client, path):
    r = client.post(
        f"/api/comics/collection/{path}",
        json={**_IT_IDENTITY, **_MARKER_FIELDS, "full_title": " "},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# The status table
# ---------------------------------------------------------------------------

def test_quarantine_status_table_is_separate_from_the_remediation_one():
    """Four codes coincide, but `last_owned_row` / `not_quarantined` /
    `already_quarantined` mean nothing to the remediation ops. Kept apart so a
    future edit for one family cannot silently reach into the other's contract.
    """
    from gixen_overlay.routes import (
        _QUARANTINE_STATUS_CODES,
        _REMEDIATION_STATUS_CODES,
    )

    assert _QUARANTINE_STATUS_CODES is not _REMEDIATION_STATUS_CODES
    assert set(_QUARANTINE_STATUS_CODES) - set(_REMEDIATION_STATUS_CODES) == {
        "last_owned_row",
        "already_quarantined",
        "not_quarantined",
    }
    # `not_imported` belongs to remediation alone: quarantine deliberately has
    # no import gate (the record-win-only flow populates real rows with
    # last_full_import still unset), so mapping it here would advertise a
    # status the command can never return.
    assert "not_imported" not in _QUARANTINE_STATUS_CODES
