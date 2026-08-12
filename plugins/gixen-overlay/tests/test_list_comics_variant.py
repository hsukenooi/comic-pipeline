"""`GET /api/comics` serves and filters `comics.variant` (BUI-777).

`upsert_comic` keys a comics row on `(title, issue, variant)`. Until this
ticket `list_comics` neither returned `variant` nor filtered on it, so every
consumer was blind to the one field that decides WHICH row a write lands on.
`comic-fmv`'s hand-priced guard therefore had to key on a prefix of the write's
key and fail closed whenever the prefix matched several rows (BUI-775) — safe,
but it left a hand-priced book unpriceable without `--force` merely because it
had a variant sibling.

These tests cover the SERVER half: the column is selected onto every row, the
filter is exact, and the endpoint stays backward-compatible for a caller that
omits the new param. The guard's own bucketing — the only evidence that the
protection actually fires — lives in `apps/fmv/tests/test_fmv_runner.py`. A
green suite here is not evidence that the guard fires; that is the BUI-775
trap, and BUI-759 is the incident that proved it.
"""
from __future__ import annotations

import sqlite3

import pytest

from gixen_overlay.db import (
    _find_issue_token,
    create_tables,
    list_comics,
    upsert_comic,
    upsert_fmv,
)


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "CREATE TABLE bids (id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, "
        "max_bid REAL NOT NULL, fmv_id INTEGER)"
    )
    conn.commit()
    return conn


@pytest.fixture
def db():
    conn = _make_db()
    create_tables(conn)
    yield conn
    conn.close()


@pytest.fixture
def variant_siblings(db):
    """A base cover and its Newsstand sibling: same (title, issue, year), two
    comic ids, distinguished ONLY by `variant`. This is the live shape — 7 of
    the 8 title/issue/year groups that span two comic_ids look like this."""
    base_id = upsert_comic(db, title="X-Men", issue="96", year=1975)
    news_id = upsert_comic(db, title="X-Men", issue="96", year=1975,
                           variant="Newsstand")
    assert base_id != news_id, "variant must produce a distinct comics row"
    upsert_fmv(db, comic_id=base_id, grade=4.0, low=35.0, high=45.0,
               notes="hand §direct: VG 4.0 $35")
    upsert_fmv(db, comic_id=news_id, grade=4.0, low=80.0, high=95.0,
               notes="window=±0.5 | cv=20%")
    return db, base_id, news_id


class TestListComicsServesVariant:
    def test_variant_is_on_every_row(self, variant_siblings):
        """Served unconditionally, like `fmv_provenance` (BUI-769) — a field
        the guard reads must be on every row this endpoint returns, not only
        the ones the dashboard renders."""
        db, base_id, news_id = variant_siblings
        rows = list_comics(db, title="X-Men", issue="96", grade=4.0)
        assert len(rows) == 2
        assert all("variant" in dict(r) for r in rows)
        assert {dict(r)["variant"] for r in rows} == {None, "Newsstand"}

    def test_the_base_edition_reports_a_null_variant_not_an_absent_key(
            self, variant_siblings):
        """The KEY's presence is the discriminator `comic-fmv` uses to tell a
        genuine base-edition row from a pre-BUI-777 server that cannot answer
        about variants at all. Conflating those would un-protect a hand-priced
        variant row under deploy skew, so the base row must carry the key with
        a None value — never omit it."""
        db, base_id, _ = variant_siblings
        base = [dict(r) for r in list_comics(db, title="X-Men", issue="96",
                                             grade=4.0)
                if dict(r)["id"] == base_id]
        assert len(base) == 1
        assert "variant" in base[0] and base[0]["variant"] is None

    def test_a_row_with_no_fmv_still_reports_variant(self, db):
        """`list_comics` LEFT JOINs fmv, so a comic with no priced row is still
        returned — and must still carry the identity field."""
        upsert_comic(db, title="Daredevil", issue="1", variant="Facsimile")
        rows = [dict(r) for r in list_comics(db, title="Daredevil")]
        assert len(rows) == 1
        assert rows[0]["variant"] == "Facsimile"
        assert rows[0]["fmv_id"] is None


class TestListComicsFiltersOnVariant:
    def test_filtering_selects_exactly_that_variant(self, variant_siblings):
        db, _, news_id = variant_siblings
        rows = [dict(r) for r in list_comics(db, title="X-Men", issue="96",
                                             variant="Newsstand")]
        assert [r["id"] for r in rows] == [news_id]

    def test_the_filter_is_case_sensitive_like_the_identity_index(
            self, variant_siblings):
        """`comics.variant` has no COLLATE NOCASE, and both identity indexes
        key on `COALESCE(variant,'')` with no LOWER — unlike `title`, which
        they do lower. Matching more loosely here would report a row the write
        itself would not resolve onto."""
        db, _, _ = variant_siblings
        assert list_comics(db, title="X-Men", issue="96",
                           variant="newsstand") == []

    def test_omitting_the_param_returns_every_variant(self, variant_siblings):
        """The API-contract guarantee: an existing caller that never learned
        about this param keeps its exact previous behavior — `variant=None`
        means NO FILTER, never 'the base edition'."""
        db, base_id, news_id = variant_siblings
        rows = list_comics(db, title="X-Men", issue="96", grade=4.0)
        assert {dict(r)["id"] for r in rows} == {base_id, news_id}

    def test_the_param_cannot_express_the_base_edition(self, variant_siblings):
        """Documented limitation, asserted so it cannot silently change: an
        absent query param cannot mean `variant IS NULL` (same as BUI-139's
        `locg_variant_id`). Callers that must isolate the base edition narrow
        on the returned field client-side, which is what `comic-fmv` does."""
        db, base_id, news_id = variant_siblings
        # There is no argument to list_comics that yields the base row alone.
        assert {dict(r)["id"] for r in
                list_comics(db, title="X-Men", issue="96", variant="")} == set()


class TestIssueTokenRegexCrossPackageContract:
    """The twin of `_ISSUE_TOKEN_PRESENT`/`_ISSUE_TOKEN_ABSENT` in
    `apps/fmv/tests/test_fmv_runner.py`.

    `comic-fmv` predicts the variant this server will store as
    `(book["variant"] or "").strip() or None`. That prediction is only sound
    while BUI-625's `_extract_edition_designation` cannot fire, and that rule
    runs iff `_find_issue_token` matches — the same `#<issue>` token the client
    declines on. apps/fmv is not a workspace member, so there is no import edge
    to enforce the pairing; both sides pin the same table instead, exactly as
    `FMV_PROVENANCES` is pinned for BUI-769.

    If these two ever diverge, the client predicts a variant the server does not
    store and the guard looks up the wrong row — the BUI-775 failure class.
    """

    PRESENT = [
        ("Iron Man #126 (Marvel Comics 1979) VF", "126"),
        ("Absolute Flash #10 Nick Robles Cover", "10"),
        ("X-Men # 96", "96"),
        ("x-men #96 newsstand", "96"),
    ]
    ABSENT = [
        ("Amazing Spider-Man", "50"),
        ("Amazing Spider-Man 300", "300"),
        ("X-Men #960", "96"),
        ("Batman", "245"),
    ]

    def test_the_token_is_found_exactly_where_the_client_declines(self):
        for title, issue in self.PRESENT:
            assert _find_issue_token(title, issue) is not None, title
        for title, issue in self.ABSENT:
            assert _find_issue_token(title, issue) is None, title

    def test_a_caller_supplied_variant_is_never_overwritten_by_the_title_rule(
            self, db):
        """The other half of the prediction: when the client DOES send a
        variant, `_extract_edition_designation` must not replace it — the
        stored value has to be the one the client predicted."""
        comic_id = upsert_comic(db, title="Absolute Flash #10 Nick Robles Cover",
                                issue="10", variant="Newsstand")
        row = db.execute("SELECT variant FROM comics WHERE id=?",
                         (comic_id,)).fetchone()
        assert row["variant"] == "Newsstand"

    def test_a_title_with_no_issue_token_stores_the_variant_verbatim(self, db):
        """The production shape: `run()` strips the `#<issue>` token from every
        title before `comic-fmv` posts it, so this is the only branch a guarded
        write can reach, and the stored variant is exactly what was sent."""
        comic_id = upsert_comic(db, title="X-Men", issue="96",
                                variant="Newsstand")
        row = db.execute("SELECT title, variant FROM comics WHERE id=?",
                         (comic_id,)).fetchone()
        assert (row["title"], row["variant"]) == ("X-Men", "Newsstand")

    def test_a_blank_variant_normalizes_to_the_base_edition(self, db):
        """`_upsert_fmv` posts a whitespace-only variant (it is truthy), and
        the server strips it to NULL. `comic-fmv`'s `_variant_key` reproduces
        both steps."""
        comic_id = upsert_comic(db, title="X-Men", issue="97", variant="   ")
        row = db.execute("SELECT variant FROM comics WHERE id=?",
                         (comic_id,)).fetchone()
        assert row["variant"] is None
