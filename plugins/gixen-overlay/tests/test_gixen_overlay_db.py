"""Tests for gixen_overlay.db — all use in-memory SQLite, no disk side effects."""
from __future__ import annotations

import sqlite3
import pytest

from gixen_overlay.db import (
    create_tables,
    upsert_comic,
    upsert_fmv,
    link_fmv_to_bid,
    get_primary_fmv_for_bid,
    list_comics,
    sweep_orphan_yearless_comics,
    get_seen_item_ids,
    mark_items_seen,
    remove_seen_for_seller,
    get_collection_wins_seen,
    mark_collection_wins_seen,
    _normalize_comic_title,
    multi_issue_lot_reason,
)


def _make_db(*, with_fmv_id: bool = True) -> sqlite3.Connection:
    """Create an in-memory DB with the minimal bids stub the plugin expects."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    cols = "id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, max_bid REAL NOT NULL"
    if with_fmv_id:
        cols += ", fmv_id INTEGER"
    conn.execute(f"CREATE TABLE bids ({cols})")
    conn.commit()
    return conn


@pytest.fixture
def db():
    conn = _make_db()
    create_tables(conn)
    yield conn
    conn.close()


def _insert_bid(conn, item_id="100000001", max_bid=50.0) -> int:
    cur = conn.execute(
        "INSERT INTO bids (item_id, max_bid) VALUES (?, ?)", (item_id, max_bid)
    )
    conn.commit()
    return cur.lastrowid


def _insert_comic(conn, title="X-Men", issue="1", year=1963) -> int:
    return upsert_comic(conn, title, issue, year)


# ---------------------------------------------------------------------------
# create_tables
# ---------------------------------------------------------------------------


def test_create_tables_creates_fmv_and_bid_fmvs():
    conn = _make_db()
    create_tables(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "fmv" in tables
    assert "bid_fmvs" in tables
    conn.close()


def test_create_tables_does_not_create_bid_comics():
    conn = _make_db()
    create_tables(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "bid_comics" not in tables
    conn.close()


def test_create_tables_is_idempotent(db):
    create_tables(db)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "fmv" in tables
    assert "bid_fmvs" in tables


def test_create_tables_adds_flag_reason_to_legacy_fmv(db):
    """BUI-132: a pre-BUI-132 fmv table (no flag_reason column) gets the column
    added by re-running create_tables, preserving existing rows. Version-skew
    tolerant and idempotent."""
    cid = _insert_comic(db)
    # Simulate an older fmv row written before the column existed.
    db.execute("ALTER TABLE fmv DROP COLUMN flag_reason")
    db.execute(
        "INSERT INTO fmv (comic_id, grade, low, high) VALUES (?, 9.2, 100, 200)",
        (cid,),
    )
    db.commit()
    cols = {r[1] for r in db.execute("PRAGMA table_info(fmv)")}
    assert "flag_reason" not in cols

    create_tables(db)  # re-run: should add the column
    cols = {r[1] for r in db.execute("PRAGMA table_info(fmv)")}
    assert "flag_reason" in cols
    row = db.execute("SELECT low, flag_reason FROM fmv WHERE grade=9.2").fetchone()
    assert row["low"] == 100  # existing row preserved
    assert row["flag_reason"] is None

    # Idempotent: a second run is a no-op (no error).
    create_tables(db)


# ---------------------------------------------------------------------------
# upsert_comic (identity-only)
# ---------------------------------------------------------------------------


def test_upsert_comic_inserts_new_record(db):
    cid = upsert_comic(db, "Amazing Spider-Man", "300", 1988)
    assert isinstance(cid, int) and cid > 0
    row = db.execute("SELECT * FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["title"] == "Amazing Spider-Man"
    assert row["issue"] == "300"
    assert row["year"] == 1988


def test_upsert_comic_returns_same_id_on_conflict(db):
    id1 = upsert_comic(db, "X-Men", "1", 1963)
    id2 = upsert_comic(db, "X-Men", "1", 1963)
    assert id1 == id2


# --- BUI-28: variant is part of comic identity ---

def test_upsert_comic_variant_gets_distinct_id(db):
    """Base cover and Newsstand variant of the same (title, issue, year) split."""
    base = upsert_comic(db, "Hulk", "332", 1986)
    news = upsert_comic(db, "Hulk", "332", 1986, variant="Newsstand")
    assert base != news
    rows = db.execute(
        "SELECT variant FROM comics WHERE LOWER(title)='hulk' AND issue='332' ORDER BY id"
    ).fetchall()
    assert [r["variant"] for r in rows] == [None, "Newsstand"]


def test_upsert_comic_same_variant_is_stable(db):
    a = upsert_comic(db, "Hulk", "332", 1986, variant="Newsstand")
    b = upsert_comic(db, "Hulk", "332", 1986, variant="Newsstand")
    assert a == b


def test_upsert_comic_blank_variant_is_base(db):
    """Empty/whitespace variant normalizes to NULL (the base edition)."""
    base = upsert_comic(db, "Hulk", "332", 1986)
    blank = upsert_comic(db, "Hulk", "332", 1986, variant="   ")
    assert base == blank
    assert db.execute(
        "SELECT variant FROM comics WHERE id=?", (base,)
    ).fetchone()["variant"] is None


def test_upsert_comic_variant_distinct_for_yearless(db):
    base = upsert_comic(db, "Spawn", "300")
    direct = upsert_comic(db, "Spawn", "300", variant="Direct")
    assert base != direct


def test_upsert_comic_variant_promotes_within_variant_only(db):
    """A yearless variant placeholder is promoted by a yeared insert of the same
    variant — and does not absorb a different variant."""
    yearless_news = upsert_comic(db, "Hulk", "332", variant="Newsstand")
    yeared_news = upsert_comic(db, "Hulk", "332", 1986, variant="Newsstand")
    assert yearless_news == yeared_news  # promoted in place
    base = upsert_comic(db, "Hulk", "332", 1986)  # base must be its own row
    assert base != yeared_news


def test_upsert_comic_updates_locg_id_via_coalesce(db):
    id1 = upsert_comic(db, "X-Men", "1", 1963, locg_id=12345)
    id2 = upsert_comic(db, "X-Men", "1", 1963, locg_id=None)
    assert id1 == id2
    row = db.execute("SELECT locg_id FROM comics WHERE id=?", (id1,)).fetchone()
    assert row["locg_id"] == 12345


def test_upsert_comic_no_grade_or_fmv_columns(db):
    cid = upsert_comic(db, "Hulk", "181", 1974)
    cols = {row[1] for row in db.execute("PRAGMA table_info(comics)")}
    assert "grade" not in cols
    assert "fmv_low" not in cols


def test_upsert_comic_case_insensitive_yeared(db):
    id1 = upsert_comic(db, "The Mighty Thor", "154", 1968)
    id2 = upsert_comic(db, "THE MIGHTY THOR", "154", 1968)
    assert id1 == id2
    assert db.execute("SELECT COUNT(*) FROM comics WHERE issue='154'").fetchone()[0] == 1


def test_upsert_comic_case_insensitive_yearless(db):
    id1 = upsert_comic(db, "Batman", "375")
    id2 = upsert_comic(db, "BATMAN", "375")
    assert id1 == id2
    assert db.execute("SELECT COUNT(*) FROM comics WHERE issue='375'").fetchone()[0] == 1


def test_upsert_comic_caps_insert_finds_canonical_yeared(db):
    id1 = upsert_comic(db, "Batman", "375", 1984)
    id2 = upsert_comic(db, "BATMAN", "375", 1984)
    assert id1 == id2


# ---------------------------------------------------------------------------
# upsert_comic — server-side title normalization (BUI-591)
#
# Mirrors the embedded-issue half of BUI-346's client-side normalizer
# (apps/fmv/src/fmv_runner.py's _strip_embedded_issue), moved here so every
# writer that reaches upsert_comic gets it, not just comic-fmv. Deliberately
# does NOT mirror the leading-article half — see _normalize_comic_title's
# docstring in db.py.
# ---------------------------------------------------------------------------


def test_upsert_comic_strips_doubled_issue_number(db):
    """The BUI-591 headline case: a writer that (unlike comic-fmv) never
    normalized client-side used to persist the issue doubled into the title
    (real incident: comics rows 640/642, 'X-Men #123'/'X-Men #127'). The
    server must now strip it itself."""
    cid = upsert_comic(db, "X-Men #123", "123", 1991)
    row = db.execute("SELECT * FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["title"] == "X-Men"
    assert row["issue"] == "123"


def test_upsert_comic_strips_trailing_bare_issue_token(db):
    cid = upsert_comic(db, "Amazing Spider-Man 300", "300", 1988)
    row = db.execute("SELECT * FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["title"] == "Amazing Spider-Man"


def test_upsert_comic_doubled_and_clean_titles_share_identity(db):
    """A doubled-title write and a clean write for the same (issue, year)
    must resolve to the SAME row — proof the normalization runs before the
    identity lookup, not just on display."""
    id1 = upsert_comic(db, "X-Men #123", "123", 1991)
    id2 = upsert_comic(db, "X-Men", "123", 1991)
    assert id1 == id2
    assert db.execute("SELECT COUNT(*) FROM comics WHERE issue='123'").fetchone()[0] == 1


def test_upsert_comic_clean_then_doubled_titles_share_identity(db):
    """Reverse-order variant of the above: a clean row created FIRST must
    still be found (not duplicated) by a later doubled-title write for the
    same (issue, year) — proves the merge is symmetric, not order-dependent."""
    id1 = upsert_comic(db, "X-Men", "123", 1991)
    id2 = upsert_comic(db, "X-Men #123", "123", 1991)
    assert id1 == id2
    assert db.execute("SELECT COUNT(*) FROM comics WHERE issue='123'").fetchone()[0] == 1


def test_upsert_comic_does_not_chew_into_longer_embedded_number(db):
    """issue='99' must not eat the '2099' in a title like 'X-Men 2099' —
    the (?<!\\d) guard mirrored from the client-side normalizer."""
    cid = upsert_comic(db, "X-Men 2099", "99", 2023)
    row = db.execute("SELECT * FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["title"] == "X-Men 2099"


def test_upsert_comic_preserves_leading_article(db):
    """Deliberately NOT mirroring _strip_leading_article (BUI-591 scope
    decision) — a title that legitimately starts with 'The' keeps it when
    there's no duplicated issue number to strip."""
    cid = upsert_comic(db, "The Mighty Thor", "154", 1968)
    row = db.execute("SELECT * FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["title"] == "The Mighty Thor"


def test_upsert_comic_full_listing_title_is_truncated_to_the_series(db):
    """BUI-599 closes what BUI-591 left open. This test previously asserted the
    *incomplete* result (`'Iron Man (Marvel Comics September 1979) VF
    Condition!'`) — a title that had lost its issue token but was still
    unreachable by any (title, issue) lookup. Truncating at the token instead of
    deleting it leaves the series name alone, which is what every consumer
    queries by."""
    cid = upsert_comic(
        db,
        "Iron Man #126 (Marvel Comics September 1979) VF Condition!",
        "126",
        1979,
    )
    row = db.execute("SELECT * FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["title"] == "Iron Man"
    assert row["issue"] == "126"


def test_upsert_comic_normalization_fails_open_on_blank_issue(db):
    """Fails open, matching fmv_runner.py's _normalize_book_title: an
    un-normalizable title (no issue to key off of) is stored as-is, never
    dropped or blanked."""
    cid = upsert_comic(db, "Some Weird Title #1", "", 2020)
    row = db.execute("SELECT * FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["title"] == "Some Weird Title #1"


def test_normalize_comic_title_fails_open_on_none_issue():
    """Unit-level check of the helper itself: issue=None (as opposed to the
    empty-string case exercised above, which is the only value `upsert_comic`
    can actually receive given its NOT NULL issue column) also fails open."""
    assert _normalize_comic_title("Some Weird Title #1", None) == "Some Weird Title #1"


# ---------------------------------------------------------------------------
# upsert_comic — listing-title truncation (BUI-599, BUI-596 class B)
#
# BUI-591 deleted the duplicated issue token and kept whatever followed it,
# which closed only the "doubled issue and nothing else" shape (BUI-596 class
# A, 99 rows). BUI-599 truncates AT the token instead, closing the "whole eBay
# listing title" shape (class B, 60 rows) — and deliberately DECLINES on the
# two shapes where cutting the tail would destroy meaning rather than junk:
# a multi-issue lot (class D) and an unrecorded variant designation (class C).
#
# The shapes below are verbatim rows from BUI-596's frozen measurement
# (docs/plans/bui-596-malformed-comic-titles/rows.tsv), not invented examples.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, issue, expected",
    [
        # Parenthesised publisher + month, then a grade and a condition shout.
        (
            "The Uncanny X-Men #212 (Marvel Comics December 1986) VF condition",
            "212",
            "The Uncanny X-Men",
        ),
        # Parenthesised grade, then a key-issue note.
        (
            "The Mighty Thor #127 (FN+) 1st App of Pluto & Hippolyta",
            "127",
            "The Mighty Thor",
        ),
        # Spaced `# 31`, and a dash left dangling on the kept prefix.
        (
            "FANTASTIC FOUR # 31 - (VG-) -THE MAD MENACE OF THE MACABRE MOLE MAN-THING-TORCH",
            "31",
            "FANTASTIC FOUR",
        ),
        # An apostrophe in the series name must survive the cut.
        (
            "WORLD'S FINEST # 186 - (FINE) -SUPERMAN/BATMAN-THE BAT-WITCH MUST BURN-HOT-ROD",
            "186",
            "WORLD'S FINEST",
        ),
        # No parentheses at all — bare trailing seller vocabulary.
        ("X-men #58 Silver age Neal Adams 1st Havok Key", "58", "X-men"),
        ("Amazing Spider-man #15 Dr. Doom NM Gem Wow", "15", "Amazing Spider-man"),
        # Class A still lands where BUI-591 put it: truncation and deletion
        # agree when there is no tail.
        ("Thor #130", "130", "Thor"),
    ],
)
def test_upsert_comic_truncates_listing_title_to_series(db, title, issue, expected):
    cid = upsert_comic(db, title, issue)
    row = db.execute("SELECT * FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["title"] == expected


def test_upsert_comic_listing_title_lands_on_the_existing_clean_row(db):
    """The point of the whole change, and the BUI-596 head-to-head in
    miniature: comics 311 held the real `Iron Man` #126 with priced comps, and
    a listing-titled write deposited placeholder 343 beside it ninety seconds
    later. The clean row must now absorb that write instead."""
    clean = upsert_comic(db, "Iron Man", "126", 1979)
    listing = upsert_comic(
        db,
        "Iron Man #126 (Marvel Comics September 1979) VF Condition!",
        "126",
        1979,
    )
    assert listing == clean
    assert db.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 1


def test_upsert_comic_listing_title_first_still_shares_identity(db):
    """Order-independent: the listing-titled write arriving FIRST must create
    the row under the clean title, so the later clean write finds it rather
    than making a second one."""
    listing = upsert_comic(
        db, "Green Lantern #86 (VF) Neal Adams Anti-Drug Story", "86", 1971
    )
    clean = upsert_comic(db, "Green Lantern", "86", 1971)
    assert listing == clean
    assert db.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 1


def test_normalize_comic_title_is_idempotent():
    """Hard requirement at a durable write boundary: this runs on every write,
    not once at migration time, so normalizing an already-normalized title must
    be a no-op. The composed `_strip_embedded_issue` call inside the truncation
    is what makes a prefix that itself ends in the issue token settle in one
    pass instead of eroding further on the next write."""
    for title, issue in [
        ("Iron Man #126 (Marvel Comics September 1979) VF Condition!", "126"),
        ("THE MIGHTY THOR # 130 - (NM-) -HERCULES-PLUTO-THUNDER IN THE NETHERWORLD", "130"),
        ("Amazing Spider-man #18,19,20,21,22 lot of 5 NM Gems Wow", "18"),
        ("Absolute Flash #10 Nick Robles Cover", "10"),
        ("Amazing Spider-Man 300", "300"),
        ("X-Men 2099", "99"),
        ("Thor 130 #130", "130"),
    ]:
        once = _normalize_comic_title(title, issue)
        assert _normalize_comic_title(once, issue) == once, title
        assert once.strip(), title


def test_normalize_comic_title_never_blanks_a_title():
    """Fails open when there is no series name to keep: a title that is nothing
    BUT the issue token has no prefix to truncate to, so it falls back to
    BUI-591 rather than being reduced to the empty string — `comics.title` is
    NOT NULL and an empty identity would be worse than a malformed one."""
    assert _normalize_comic_title("#126 VF Condition!", "126")
    assert _normalize_comic_title("#126", "126")


def test_normalize_comic_title_leaves_bui596_remediated_titles_alone():
    """The property that lets this change and BUI-596's cleanup compose instead
    of fighting: BUI-596 rewrites a malformed row to the text before its `#`,
    which is the same string this normalizer produces. So a remediated row that
    is written to again keeps its remediated title — if the two disagreed, every
    post-cleanup write would drag rows back off the remediated value.

    Titles below are verbatim `proposed_new_title` values from that plan."""
    for title, issue in [
        ("WORLD'S FINEST", "200"),
        ("THE MIGHTY THOR", "130"),
        ("Amazing Spider-man", "5"),
        ("Giant Size X-men", "1"),
        ("X-men", "12"),
        ("Iron Man", "126"),
    ]:
        assert _normalize_comic_title(title, issue) == title


def test_normalize_comic_title_declines_on_edition_markers():
    """The decline vocabulary covers printings and facsimiles alongside cover
    variants — all are edition distinctions that live in `variant`, and a
    printing marker is a documented data-loss class here (BUI-364/372/373)."""
    for title, issue in [
        ("Amazing Spider-Man #300 2nd Printing NM", "300"),
        ("Amazing Spider-Man #300 Facsimile Edition", "300"),
        ("Amazing Spider-Man #300 2nd Ptg VF", "300"),
    ]:
        assert _normalize_comic_title(title, issue) != "Amazing Spider-Man"


@pytest.mark.parametrize(
    "title, issue, detected",
    [
        # All 6 class-D rows from rows.tsv, hashed.
        ("Uncanny X-men #5,6,7,8,9  Bronze age lot of 5 Fine to VF", "5", True),
        ("Classic X-men #1,2,3,4,5,6,7,8 Bronze age lot of 8 New X-men Art Adams FVF -VF", "1", True),
        ("Amazing Spider-man #18,19,20,21,22 lot of 5 NM Gems Wow", "18", True),
        ("Amazing Spider-man #14,15,16,17,18 lot of 5 NM Gems Wow z", "14", True),
        ("Amazing Spider-man #19,20,21,22,23 lot of 5 NM Gems Wow z", "19", True),
        ("Uncanny X-men #146,147 Bronze age Dr. Doom lot of 2 Wow", "146", True),
        # BUI-625: the SAME listings appear HASHLESS in bids.ebay_title on the
        # live Mac Mini. The `#` is not part of the shape, so neither is it
        # part of the rule.
        ("Uncanny X-men  5,6,7,8,9  Bronze age lot of 5 Fine to VF", "5", True),
        ("Uncanny X-men  146,147 Bronze age Dr. Doom lot of 2 Wow", "146", True),
        # Hashless, and with NO `lot of N` phrase — reachable only by the run
        # test. Verbatim from bids.ebay_title.
        ("Akira 1,2,3,4,5,6,7,8,9 Marvel Epic Comics 1988 1st Prints High Grades", "1", True),
        ("Daredevil The Man Without Fear 1,2,3,4,5 Marvel Comics 1993 Limited Series", "1", True),
        ("Marvel Spotlight On GHOST RIDER #5,6,7,8,9,10,11 Full Run 1st App Ghost Rider!", "5", True),
        # `lot of N` with no enumerated run.
        ("Uncanny X-men #146 Bronze age Dr. Doom lot of 2 Wow", "146", True),
        # --- must NOT fire: verbatim single-issue rows from the live table ---
        ("Iron Man #126 (Marvel Comics September 1979) VF Condition!", "126", False),
        ("The Mighty Thor #127 (FN+) 1st App of Pluto & Hippolyta", "127", False),
        ("FANTASTIC FOUR # 31 - (VG-) -THE MAD MENACE OF THE MACABRE MOLE MAN", "31", False),
        ("Amazing Spider-man #20 Bermejo Variant NM Gem Wow", "20", False),
        # The BUI-591 `(?<!\d)` guard: issue 99 must not match inside "2099",
        # so the comma-digit AFTER the masthead year does not read as a run.
        ("X-Men 2099, 2 covers", "99", False),
        ("X-Men 2099", "99", False),
        # ...but a genuine run on the same issue number still fires.
        ("X-Men 99, 100 lot", "99", True),
        # Fails open on a blank issue rather than guessing.
        ("Amazing Spider-man 18,19,20 lot of 3", "", False),
        # --- the dash is NOT a separator, and this is why (BUI-625) ---------
        # One seller's house format puts the issue, a dash, then a "1st app"
        # note that STARTS WITH A DIGIT. ~100 of the 641 rows in the live
        # `bids.ebay_title` corpus look like this, and every one is a single
        # book. Adding `-` to the separator class would refuse them all — a
        # widening that measuring `comics` alone (0 rows affected) says is free.
        ("X-Men   96 - 1st Moira MacTaggert VG/Fine Cond", "96", False),
        ("Fantastic Four   50 - 3rd Silver Surfer & Galactus VG/Fine Cond", "50", False),
        ("Incredible Hulk # 330 - 1st Todd McFarlane pencils & cover NM- Cond", "330", False),
    ],
)
def test_multi_issue_lot_reason_detects_lots_without_false_positives(title, issue, detected):
    """BUI-625 class D. Measured read-only against the live DB before widening
    to the whole title: 0/666 `comics` rows match (no stored identity is
    affected) and 7/641 raw `bids.ebay_title` rows match — all 7 genuine lots.
    """
    assert (multi_issue_lot_reason(title, issue) is not None) is detected


def test_multi_issue_lot_reason_returns_a_usable_explanation():
    """The reason is surfaced to the caller in the 422 detail and persisted to
    the rejections ledger, so it has to name the shape, not just say 'no'."""
    reason = multi_issue_lot_reason("Amazing Spider-man #18,19,20 lot of 3", "18")
    assert reason and "18" in reason


def test_upsert_comic_declines_truncation_for_multi_issue_lot(db):
    """Class D stays open by design. Truncating would mint a clean-looking
    `Amazing Spider-man` #18 that silently asserts a five-book lot IS issue 18,
    and would merge it with the real single issue. Whether a lot should produce
    a comics row at all is an unmade product decision, so this preserves
    today's behaviour (BUI-591's strip) instead of pre-empting it."""
    cid = upsert_comic(db, "Amazing Spider-man #18,19,20,21,22 lot of 5 NM Gems Wow", "18")
    row = db.execute("SELECT * FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["title"] != "Amazing Spider-man"
    assert "19,20,21,22" in row["title"]


def test_upsert_comic_declines_truncation_for_space_separated_lot(db):
    """The lot guard is not only the comma run — `lot of N` catches the same
    shape written without the enumerated issue list."""
    cid = upsert_comic(db, "Uncanny X-men #146 Bronze age Dr. Doom lot of 2 Wow", "146")
    row = db.execute("SELECT * FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["title"] != "Uncanny X-men"


# ---------------------------------------------------------------------------
# upsert_comic — edition designation extraction (BUI-625, BUI-596 class C)
#
# BUI-599 DECLINED to truncate a title whose tail carried an edition
# designation, because cutting `Nick Robles Cover` away would merge a variant
# into its base edition. BUI-625 moves the designation into the `variant`
# column instead, which makes the truncation safe and keeps the two books
# distinct — the column where the distinction belongs.
#
# The rule fires only on a tail that is NOTHING BUT the designation, ending in
# the designation word. That is exactly the 8 rows rows.tsv classifies
# `C-variant-designation`; the 21 class-B rows with a designation buried in
# listing prose still decline (see the residue test below). Verified against
# the frozen 173-row corpus: 8 extracted, 6 refused as lots, 21 declined, 138
# truncated, and ZERO new identity merges versus BUI-599's behaviour.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, issue, expected_title, expected_variant",
    [
        # All four distinct series/artist pairs from rows.tsv's class C.
        ("Absolute Flash #10 Nick Robles Cover", "10",
         "Absolute Flash", "Nick Robles Cover"),
        ("Absolute Green Lantern #9 Jahnoy Lindsay Cover", "9",
         "Absolute Green Lantern", "Jahnoy Lindsay Cover"),
        ("Absolute Martian Manhunter #1 Javier Rodriguez Cover", "1",
         "Absolute Martian Manhunter", "Javier Rodriguez Cover"),
        # Two tokens is the floor: one name plus the designation word.
        ("Amazing Spider-man #20 Bermejo Variant", "20",
         "Amazing Spider-man", "Bermejo Variant"),
    ],
)
def test_upsert_comic_extracts_edition_designation_into_variant(
    db, title, issue, expected_title, expected_variant
):
    """The BUI-625 acceptance criterion: a listing-titled book with an edition
    designation round-trips with the designation in `variant`."""
    cid = upsert_comic(db, title, issue)
    row = db.execute("SELECT * FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["title"] == expected_title
    assert row["variant"] == expected_variant


def test_upsert_comic_extracted_variant_is_distinct_from_base_edition(db):
    """The whole point of extracting rather than truncating: the variant must
    NOT land on the base edition's row. `variant` is row identity (BUI-28)."""
    base = upsert_comic(db, "Absolute Flash", "10")
    var = upsert_comic(db, "Absolute Flash #10 Nick Robles Cover", "10")
    assert base != var
    assert db.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 2


def test_upsert_comic_extracted_variant_siblings_stay_distinct(db):
    """The merging direction is unrecoverable, so it gets its own test: two
    DIFFERENT cover artists on the same issue must never collapse into one row.
    The extracted value is verbatim, so distinct designations stay distinct."""
    a = upsert_comic(db, "Amazing Spider-man #20 Bermejo Variant", "20")
    b = upsert_comic(db, "Amazing Spider-man #20 Crain Variant", "20")
    assert a != b
    variants = {
        r["variant"] for r in db.execute("SELECT variant FROM comics").fetchall()
    }
    assert variants == {"Bermejo Variant", "Crain Variant"}


def test_upsert_comic_extraction_is_idempotent(db):
    """Runs on every write to a durable identity column, so re-posting the same
    listing title must land on the same row rather than eroding it further."""
    first = upsert_comic(db, "Absolute Flash #10 Nick Robles Cover", "10")
    again = upsert_comic(db, "Absolute Flash #10 Nick Robles Cover", "10")
    # And the already-extracted form must resolve to that same row.
    split = upsert_comic(db, "Absolute Flash", "10", variant="Nick Robles Cover")
    assert first == again == split
    assert db.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 1


def test_upsert_comic_caller_supplied_variant_is_never_overwritten(db):
    """Extraction is a fallback for a caller that named no variant. A caller
    that DID name one has already recorded the distinction and must win."""
    cid = upsert_comic(
        db, "Absolute Flash #10 Nick Robles Cover", "10", variant="Robles"
    )
    row = db.execute("SELECT * FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["variant"] == "Robles"
    assert row["title"] == "Absolute Flash"


def test_upsert_comic_does_not_extract_a_bare_designation_word(db):
    """A bare `"Variant"` names no distinguishing feature, so two different
    variants of one issue would both extract it and MERGE — the one outcome
    this boundary must not have. At least one name token is required."""
    a = upsert_comic(db, "Amazing Spider-man #20 Variant", "20")
    b = upsert_comic(db, "Amazing Spider-man #20 Cover", "20")
    rows = db.execute("SELECT title, variant FROM comics").fetchall()
    assert a != b
    assert all(r["variant"] is None for r in rows), "bare word must not become identity"
    assert all(r["title"] != "Amazing Spider-man" for r in rows)


@pytest.mark.parametrize(
    "title, issue",
    [
        # Verbatim rows.tsv class-B residue: a designation word buried in
        # listing prose, where no rule can say which words are the edition.
        # `McFarlane` is the interior artist, not part of the edition.
        ("Marvel Tales #238 McFarlane Newsstand X-men Spider-man FVF Beauty Wow", "238"),
        # A 1971 book with exactly ONE cover — `NEAL ADAMS COVER` is a credit,
        # not a variant, and extracting it would mint a phantom edition.
        ("WORLD'S FINEST # 200 - (VG+) -SUPERMAN/ROBIN-NEAL ADAMS COVER-ORGIN OF ROBIN", "200"),
        # Would yield `Newsstand Variant` while the live table spells the same
        # designation `Newsstand` — same book, two spellings, two rows.
        ("Iron Man #125 Newsstand Variant (Marvel Comics August 1979) FN/VF Condition!", "125"),
        # Grade tokens after the designation word: the span is not terminal.
        ("Amazing Spider-man #20 Bermejo Variant NM Gem Wow", "20"),
        # Over the token cap — listing prose must not be swallowed whole.
        ("X-men #58 Silver age Neal Adams Legendary Cover", "58"),
    ],
)
def test_upsert_comic_declines_extraction_for_unattributable_designation(db, title, issue):
    """The residue keeps BUI-599's behaviour rather than guessing a span.

    Declining leaves a malformed but RECOVERABLE title; guessing wrong either
    merges two books (unrecoverable) or mints a phantom edition. The title is
    deliberately not truncated here — truncating without recording the
    designation is the merge this whole rule exists to prevent."""
    cid = upsert_comic(db, title, issue)
    row = db.execute("SELECT * FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["variant"] is None
    # The series name alone would be the merged form; it must not be reached.
    assert row["title"] not in ("Marvel Tales", "WORLD'S FINEST", "Iron Man",
                                "Amazing Spider-man", "X-men")


def test_upsert_comic_extraction_does_not_fire_on_a_lot(db):
    """Belt and braces on the class C/D boundary: a lot tail is an enumerated
    run of digits, which the name-token rule forbids, so a lot can never be
    mistaken for an edition designation."""
    cid = upsert_comic(db, "Amazing Spider-man #18,19,20 lot of 3 Variant", "18")
    row = db.execute("SELECT * FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["variant"] is None
    assert row["title"] != "Amazing Spider-man"


def test_upsert_comic_variant_siblings_do_not_collide(db):
    """The adversarial case this write boundary must not have: two DIFFERENT
    cover variants of the same issue, neither carrying a `variant` value, must
    not collapse into one row. Truncating both would key them identically on
    (LOWER(title), issue, COALESCE(variant,'')) and silently merge two distinct
    books — so the guard declines and they stay distinct."""
    a = upsert_comic(db, "Amazing Spider-man #20 Bermejo Variant NM Gem Wow", "20")
    b = upsert_comic(db, "Amazing Spider-man #20 Crain Variant NM Gem Wow", "20")
    assert a != b
    assert db.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 2


def test_upsert_comic_truncates_when_the_variant_is_actually_recorded(db):
    """The variant guard is about the designation being LOST, not about the
    word appearing. A caller that puts the designation in its own column has
    already preserved the distinction, so the title is safe to truncate — and
    the two variants remain distinct rows via the variant column, which is
    exactly where the distinction belongs."""
    a = upsert_comic(
        db, "Amazing Spider-man #20 Bermejo Variant NM Gem Wow", "20", variant="Bermejo"
    )
    b = upsert_comic(
        db, "Amazing Spider-man #20 Crain Variant NM Gem Wow", "20", variant="Crain"
    )
    assert a != b
    titles = {
        r["title"] for r in db.execute("SELECT title FROM comics").fetchall()
    }
    assert titles == {"Amazing Spider-man"}


def test_upsert_comic_truncation_respects_variant_scoping(db):
    """A truncated variant write must not be absorbed by the base edition:
    reconciliation is variant-scoped (BUI-28), and normalizing the title does
    not weaken that."""
    base = upsert_comic(db, "Amazing Spider-man", "20", 2022)
    var = upsert_comic(
        db, "Amazing Spider-man #20 Bermejo Variant NM Gem Wow", "20", 2022,
        variant="Bermejo",
    )
    assert base != var


def test_upsert_comic_truncation_does_not_chew_a_longer_number(db):
    """`#99` must not match inside `#2099`, and the year-like token in the
    series name must survive the cut."""
    cid = upsert_comic(db, "X-Men 2099 #99 (VF) Some Key Note", "99")
    row = db.execute("SELECT * FROM comics WHERE id=?", (cid,)).fetchone()
    assert row["title"] == "X-Men 2099"


# ---------------------------------------------------------------------------
# upsert_comic — case-insensitive title matching (PER-123)
# ---------------------------------------------------------------------------


def test_upsert_comic_allcaps_yeared_hits_existing_yeared_row(db):
    id1 = upsert_comic(db, "The Mighty Thor", "154", 1968)
    id2 = upsert_comic(db, "THE MIGHTY THOR", "154", 1968)
    assert id1 == id2
    assert db.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 1


def test_upsert_comic_allcaps_yeared_promotes_existing_yearless(db):
    id_yearless = upsert_comic(db, "THE MIGHTY THOR", "154")
    id_yeared = upsert_comic(db, "The Mighty Thor", "154", 1968)
    assert id_yearless == id_yeared
    row = db.execute("SELECT year FROM comics WHERE id=?", (id_yeared,)).fetchone()
    assert row["year"] == 1968
    assert db.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 1


def test_upsert_comic_allcaps_yearless_defers_to_existing_yeared(db):
    id_yeared = upsert_comic(db, "The Mighty Thor", "154", 1968)
    id_yearless = upsert_comic(db, "THE MIGHTY THOR", "154")
    assert id_yearless == id_yeared
    assert db.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 1


def test_upsert_comic_allcaps_yearless_hits_existing_yearless(db):
    id1 = upsert_comic(db, "The Mighty Thor", "154")
    id2 = upsert_comic(db, "THE MIGHTY THOR", "154")
    assert id1 == id2
    assert db.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 1


def test_upsert_comic_allcaps_skips_yearless_promotion_on_yeared_sibling_conflict(db):
    # Set up: yearless row + yeared row at 1968 (bypassing upsert_comic to avoid
    # auto-promotion — simulates pre-existing split state in the DB).
    cur = db.execute(
        "INSERT INTO comics (title, issue, year) VALUES (?, ?, NULL)",
        ("The Mighty Thor", "154"),
    )
    db.commit()
    id_yearless = cur.lastrowid
    db.execute(
        "INSERT INTO comics (title, issue, year) VALUES (?, ?, ?)",
        ("The Mighty Thor", "154", 1968),
    )
    db.commit()

    # ALL-CAPS yeared insert at year=1999 — LOWER() finds 1968 as a conflicting
    # yeared sibling, so PER-104 guard fires and returns the yearless row unchanged.
    id_returned = upsert_comic(db, "THE MIGHTY THOR", "154", 1999)

    assert id_returned == id_yearless
    row = db.execute("SELECT year FROM comics WHERE id=?", (id_yearless,)).fetchone()
    assert row["year"] is None
    assert db.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 2


def test_sweep_orphan_yearless_comics_merges_allcaps_stubs(db):
    yeared_id = upsert_comic(db, "The Mighty Thor", "154", 1968)
    # Manually insert an ALL-CAPS yearless stub (bypassing upsert_comic which
    # now deduplicates — this simulates pre-PER-123 data in the DB).
    cur = db.execute(
        "INSERT INTO comics (title, issue, year) VALUES (?, ?, NULL)",
        ("THE MIGHTY THOR", "154"),
    )
    db.commit()
    stub_id = cur.lastrowid

    result = sweep_orphan_yearless_comics(db)

    assert result["dry_run"] is False
    assert result["merged"] == 1
    assert result["details"][0]["yearless_id"] == stub_id
    assert result["details"][0]["yeared_id"] == yeared_id
    assert db.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 1


def test_sweep_orphan_yearless_comics_dry_run_does_not_delete(db):
    upsert_comic(db, "The Mighty Thor", "154", 1968)
    db.execute(
        "INSERT INTO comics (title, issue, year) VALUES (?, ?, NULL)",
        ("THE MIGHTY THOR", "154"),
    )
    db.commit()

    result = sweep_orphan_yearless_comics(db, dry_run=True)

    assert result["dry_run"] is True
    assert result["would_merge"] == 1
    assert db.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# upsert_fmv
# ---------------------------------------------------------------------------


def test_upsert_fmv_inserts_and_returns_id(db):
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2, low=800.0, high=1000.0, comps=12, confidence="high")
    assert isinstance(fid, int) and fid > 0


def test_upsert_fmv_on_same_comic_grade_updates_nonnull_fields(db):
    cid = _insert_comic(db)
    fid1 = upsert_fmv(db, cid, 9.2, low=800.0)
    fid2 = upsert_fmv(db, cid, 9.2, low=850.0)
    assert fid1 == fid2
    row = db.execute("SELECT low FROM fmv WHERE id=?", (fid1,)).fetchone()
    assert row["low"] == 850.0


def test_upsert_fmv_coalesce_does_not_overwrite_with_none(db):
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2, low=800.0, high=1000.0)
    upsert_fmv(db, cid, 9.2, low=None)
    row = db.execute("SELECT low, high FROM fmv WHERE id=?", (fid,)).fetchone()
    assert row["low"] == 800.0
    assert row["high"] == 1000.0


def test_upsert_fmv_grade_none_raises(db):
    cid = _insert_comic(db)
    with pytest.raises(ValueError, match="grade is required"):
        upsert_fmv(db, cid, None)


def test_upsert_fmv_grade_only_stub_has_null_updated_at(db):
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2)
    row = db.execute("SELECT updated_at FROM fmv WHERE id=?", (fid,)).fetchone()
    assert row["updated_at"] is None


def test_upsert_fmv_subsequent_call_with_low_sets_updated_at(db):
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2)
    upsert_fmv(db, cid, 9.2, low=500.0)
    row = db.execute("SELECT updated_at FROM fmv WHERE id=?", (fid,)).fetchone()
    assert row["updated_at"] is not None


# ---------------------------------------------------------------------------
# upsert_fmv flag_reason (BUI-132)
# ---------------------------------------------------------------------------


def test_upsert_fmv_stores_flag_reason(db):
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.6, flag_reason="one_sided")
    row = db.execute("SELECT flag_reason, low FROM fmv WHERE id=?", (fid,)).fetchone()
    assert row["flag_reason"] == "one_sided"
    assert row["low"] is None


def test_upsert_fmv_newly_flagged_book_clears_stale_price(db):
    """BUI-132 residual #2: a previously-priced book that later flags must drop
    its now-stale auto-priced low/high/comps — not COALESCE-keep them."""
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.6, low=800.0, high=1000.0, comps=12, confidence="high")
    # Re-upsert as a needs_manual flag (no fresh price).
    upsert_fmv(db, cid, 9.6, flag_reason="too_wide")
    row = db.execute(
        "SELECT low, high, comps, flag_reason FROM fmv WHERE id=?", (fid,)
    ).fetchone()
    assert row["low"] is None
    assert row["high"] is None
    assert row["comps"] is None
    assert row["flag_reason"] == "too_wide"


def test_upsert_fmv_flag_only_post_clears_stale_confidence_and_notes(db):
    """BUI-132 code-review: a flag-only re-POST ({grade, flag_reason}) over a
    priced row must drop the old auto-price's confidence and notes too — not just
    low/high/comps — else a needs_manual book surfaces the stale 'high'
    confidence / auto-price notes on the /comics dashboard."""
    cid = _insert_comic(db)
    fid = upsert_fmv(
        db, cid, 9.6, low=800.0, high=1000.0, comps=12,
        confidence="high", notes="window=±0.2 | auto",
    )
    # Flag-only payload: no confidence, no notes (what verify.md/fmv.md instruct).
    upsert_fmv(db, cid, 9.6, flag_reason="one_sided")
    row = db.execute(
        "SELECT low, high, comps, confidence, notes, flag_reason FROM fmv WHERE id=?",
        (fid,),
    ).fetchone()
    assert row["low"] is None
    assert row["high"] is None
    assert row["comps"] is None
    assert row["confidence"] is None
    assert row["notes"] is None
    assert row["flag_reason"] == "one_sided"


def test_upsert_fmv_n0_stub_does_not_wipe_real_price(db):
    """BUI-132 constraint: the n=0 stub guard must survive. An unflagged stub
    (no comps, no flag) re-upserted over a real price keeps the price — only a
    flagged row clears it."""
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.6, low=800.0, high=1000.0, comps=12)
    # An n=0 stub: no FMV fields, no flag_reason.
    upsert_fmv(db, cid, 9.6)
    row = db.execute(
        "SELECT low, high, comps, flag_reason FROM fmv WHERE id=?", (fid,)
    ).fetchone()
    assert row["low"] == 800.0
    assert row["high"] == 1000.0
    assert row["comps"] == 12
    assert row["flag_reason"] is None


def test_upsert_fmv_n0_stub_does_not_zero_the_comps_of_a_priced_row(db):
    """BUI-599 co-fix, and the reason it is not optional: routing listing-titled
    writes onto their clean twin makes "a bare n=0 stub upserts over a priced
    row" the MODAL event, not a rare one — BUI-596 measured 181 of 183 FMV rows
    attached to malformed comics as empty shells. A stub is not empty on the
    wire: fmv_runner posts n=0 as `fmv_comps: 0`, which is not NULL, so the old
    COALESCE stored it and left the self-contradictory `low=15 high=20
    comps=0`. The guard now covers the metadata beside the price."""
    cid = _insert_comic(db)
    fid = upsert_fmv(
        db, cid, 8.0, low=15.0, high=20.0, comps=11, confidence="medium",
        notes="11 comps",
    )
    upsert_fmv(db, cid, 8.0, comps=0, confidence="low", notes="0 comps")
    row = db.execute(
        "SELECT low, high, comps, confidence, notes FROM fmv WHERE id=?", (fid,)
    ).fetchone()
    assert row["low"] == 15.0
    assert row["high"] == 20.0
    assert row["comps"] == 11
    assert row["confidence"] == "medium"
    assert row["notes"] == "11 comps"


def test_upsert_fmv_n0_stub_still_lands_on_an_unpriced_row(db):
    """The guard is scoped to a stored row that holds a real price — it must
    not freeze an unpriced row's metadata, or a first n=0 result could never
    record that it found nothing."""
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 8.0, comps=0, confidence="low", notes="0 comps")
    upsert_fmv(db, cid, 8.0, comps=0, confidence="low", notes="still 0 comps")
    row = db.execute("SELECT comps, notes FROM fmv WHERE id=?", (fid,)).fetchone()
    assert row["comps"] == 0
    assert row["notes"] == "still 0 comps"


def test_upsert_fmv_fresh_price_still_overwrites_metadata(db):
    """The guard must not block a real re-price: an incoming row carrying a
    price is not a stub, so its comps/confidence/notes replace the stored
    ones."""
    cid = _insert_comic(db)
    fid = upsert_fmv(
        db, cid, 8.0, low=15.0, high=20.0, comps=11, confidence="medium",
        notes="11 comps",
    )
    upsert_fmv(
        db, cid, 8.0, low=30.0, high=40.0, comps=25, confidence="high",
        notes="25 comps",
    )
    row = db.execute(
        "SELECT low, comps, confidence, notes FROM fmv WHERE id=?", (fid,)
    ).fetchone()
    assert row["low"] == 30.0
    assert row["comps"] == 25
    assert row["confidence"] == "high"
    assert row["notes"] == "25 comps"


def test_upsert_fmv_flagged_row_still_clears_a_priced_row(db):
    """The flag branch outranks the stub guard: a newly-flagged book still
    drops its stale auto-price AND its stale auto-price metadata (BUI-86
    residual #2), even though its `low` is NULL like a stub's."""
    cid = _insert_comic(db)
    fid = upsert_fmv(
        db, cid, 8.0, low=15.0, high=20.0, comps=11, confidence="medium",
        notes="11 comps",
    )
    upsert_fmv(db, cid, 8.0, flag_reason="too_sparse")
    row = db.execute(
        "SELECT low, high, comps, confidence, notes, flag_reason FROM fmv WHERE id=?",
        (fid,),
    ).fetchone()
    assert row["low"] is None
    assert row["high"] is None
    assert row["confidence"] is None
    assert row["notes"] is None
    assert row["flag_reason"] == "too_sparse"


def test_upsert_fmv_fresh_price_clears_prior_flag(db):
    """A book that was flagged needs_manual but later prices cleanly is no longer
    needs_manual: a fresh-price upsert (low set, no flag) stores the price AND
    clears the stale flag, so verify won't wrongly report it as needs_manual."""
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.6, flag_reason="too_sparse")
    upsert_fmv(db, cid, 9.6, low=500.0, high=700.0, comps=8)
    row = db.execute(
        "SELECT low, high, flag_reason FROM fmv WHERE id=?", (fid,)
    ).fetchone()
    assert row["low"] == 500.0
    assert row["high"] == 700.0
    assert row["flag_reason"] is None


# ---------------------------------------------------------------------------
# link_fmv_to_bid
# ---------------------------------------------------------------------------


def test_link_fmv_to_bid_sole_junction_is_promoted_to_primary(db):
    """BUI-82: a sole junction is always primary so the grade/FMV aggregates
    (which key off is_primary=1) don't blank, even when linked non-primary."""
    bid_id = _insert_bid(db)
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2, low=800.0)
    link_fmv_to_bid(db, bid_id, fid, is_primary=False)
    rows = db.execute("SELECT * FROM bid_fmvs WHERE bid_id=?", (bid_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["fmv_id"] == fid
    assert rows[0]["is_primary"] == 1
    assert db.execute(
        "SELECT fmv_id FROM bids WHERE id=?", (bid_id,)
    ).fetchone()["fmv_id"] == fid


def test_link_fmv_to_bid_nonprimary_lot_member_stays_nonprimary(db):
    """Once a primary exists, a non-primary link to a *different* comic stays
    a non-primary lot member — genuine lots are preserved."""
    bid_id = _insert_bid(db)
    cid1 = _insert_comic(db, issue="1")
    cid2 = _insert_comic(db, issue="2")
    primary = upsert_fmv(db, cid1, 9.2, low=800.0)
    member = upsert_fmv(db, cid2, 9.0, low=400.0)
    link_fmv_to_bid(db, bid_id, primary, is_primary=True)
    link_fmv_to_bid(db, bid_id, member, is_primary=False)
    rows = {r["fmv_id"]: r["is_primary"]
            for r in db.execute(
                "SELECT fmv_id, is_primary FROM bid_fmvs WHERE bid_id=?", (bid_id,))}
    assert rows[primary] == 1
    assert rows[member] == 0


def test_link_fmv_to_bid_primary_mirrors_to_bids_fmv_id(db):
    bid_id = _insert_bid(db)
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2, low=800.0)
    link_fmv_to_bid(db, bid_id, fid, is_primary=True)
    row = db.execute("SELECT fmv_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["fmv_id"] == fid


def test_link_fmv_to_bid_primary_demotes_prior_different_comic(db):
    """A primary re-link to a *different* comic demotes (but keeps) the prior
    junction — multi-comic lots are preserved."""
    bid_id = _insert_bid(db)
    cid1 = _insert_comic(db, issue="1")
    cid2 = _insert_comic(db, issue="2")
    fid1 = upsert_fmv(db, cid1, 9.0, low=700.0)
    fid2 = upsert_fmv(db, cid2, 9.2, low=800.0)
    link_fmv_to_bid(db, bid_id, fid1, is_primary=True)
    link_fmv_to_bid(db, bid_id, fid2, is_primary=True)
    rows = {r["fmv_id"]: r["is_primary"]
            for r in db.execute("SELECT fmv_id, is_primary FROM bid_fmvs WHERE bid_id=?", (bid_id,))}
    assert rows[fid1] == 0
    assert rows[fid2] == 1


def test_link_fmv_to_bid_primary_replaces_same_comic_grade_only_stub(db):
    """BUI-82: re-linking the *same comic* to a valued FMV must replace the
    prior grade-only junction, not leave a demoted null-valued duplicate.

    The duplicate inflates the dashboard's lot_count to 2 and trips the
    "unpriced lot member" guard, blanking the FMV of a single priced comic.
    """
    bid_id = _insert_bid(db)
    cid = _insert_comic(db)
    stub = upsert_fmv(db, cid, 8.0)  # grade-only stub, low IS NULL
    link_fmv_to_bid(db, bid_id, stub, is_primary=True)
    valued = upsert_fmv(db, cid, 9.2, low=800.0, high=1000.0)  # same comic, real FMV
    link_fmv_to_bid(db, bid_id, valued, is_primary=True)
    rows = db.execute(
        "SELECT fmv_id, is_primary FROM bid_fmvs WHERE bid_id=?", (bid_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["fmv_id"] == valued
    assert rows[0]["is_primary"] == 1
    assert db.execute(
        "SELECT fmv_id FROM bids WHERE id=?", (bid_id,)
    ).fetchone()["fmv_id"] == valued


def test_link_fmv_to_bid_idempotent(db):
    bid_id = _insert_bid(db)
    cid = _insert_comic(db)
    fid = upsert_fmv(db, cid, 9.2, low=800.0)
    link_fmv_to_bid(db, bid_id, fid)
    link_fmv_to_bid(db, bid_id, fid)
    count = db.execute(
        "SELECT COUNT(*) FROM bid_fmvs WHERE bid_id=?", (bid_id,)
    ).fetchone()[0]
    assert count == 1


def test_link_fmv_to_bid_nonexistent_fmv_raises_fk(db):
    bid_id = _insert_bid(db)
    with pytest.raises(sqlite3.IntegrityError):
        link_fmv_to_bid(db, bid_id, 9999, is_primary=False)


def test_get_primary_fmv_for_bid_integration(db):
    bid_id = _insert_bid(db)
    cid = upsert_comic(db, "Daredevil", "1", 1964, locg_id=12345)
    fid = upsert_fmv(db, cid, 9.4, low=600.0)
    link_fmv_to_bid(db, bid_id, fid, is_primary=True)
    row = get_primary_fmv_for_bid(db, bid_id)
    assert row is not None
    assert row["grade"] == 9.4
    assert row["low"] == 600.0
    assert row["title"] == "Daredevil"
    assert row["locg_id"] == 12345


# ---------------------------------------------------------------------------
# list_comics (joined read path)
# ---------------------------------------------------------------------------


def test_list_comics_returns_all(db):
    upsert_comic(db, "X-Men", "1", 1963)
    upsert_comic(db, "Hulk", "181", 1974)
    rows = list_comics(db)
    assert len(rows) == 2


def test_list_comics_one_row_per_fmv_grade(db):
    cid = upsert_comic(db, "X-Men", "1", 1963)
    upsert_fmv(db, cid, 9.0, low=700.0)
    upsert_fmv(db, cid, 9.2, low=800.0)
    rows = list_comics(db)
    assert len(rows) == 2
    grades = {r["grade"] for r in rows}
    assert grades == {9.0, 9.2}


def test_list_comics_comic_without_fmv_returns_one_null_row(db):
    upsert_comic(db, "X-Men", "1", 1963)
    rows = list_comics(db)
    assert len(rows) == 1
    assert rows[0]["grade"] is None
    assert rows[0]["fmv_low"] is None


def test_list_comics_filter_by_grade(db):
    cid = upsert_comic(db, "X-Men", "1", 1963)
    upsert_fmv(db, cid, 9.0, low=700.0)
    upsert_fmv(db, cid, 9.2, low=800.0)
    rows = list_comics(db, grade=9.2)
    assert len(rows) == 1
    assert rows[0]["fmv_low"] == 800.0


def test_list_comics_filter_by_title(db):
    upsert_comic(db, "X-Men", "1", 1963)
    upsert_comic(db, "Hulk", "181", 1974)
    rows = list_comics(db, title="X-Men")
    assert len(rows) == 1
    assert rows[0]["title"] == "X-Men"


def test_list_comics_empty_db(db):
    assert list_comics(db) == []


# ---------------------------------------------------------------------------
# list_comics — locg_id and max_age_days filters (FMV cache lookup path)
# ---------------------------------------------------------------------------


def test_list_comics_filters_by_locg_id(db):
    """A locg_id lookup returns rows for that canonical issue, regardless of
    title spelling. This is the lookup comic-fmv uses for cache reuse."""
    cid_asm = upsert_comic(db, "Amazing Spider-Man", "300", 1988, locg_id=6977652)
    upsert_fmv(db, cid_asm, 9.2, low=800.0, high=1000.0)
    cid_hulk = upsert_comic(db, "Hulk", "181", 1974, locg_id=12345)
    upsert_fmv(db, cid_hulk, 9.0, low=50.0, high=70.0)

    rows = list_comics(db, locg_id=6977652)
    assert len(rows) == 1
    assert rows[0]["title"] == "Amazing Spider-Man"


def test_list_comics_locg_id_plus_grade(db):
    """The fmv-cache lookup pattern: locg_id + grade pinpoints one row."""
    cid = upsert_comic(db, "Hulk", "181", 1974, locg_id=12345)
    upsert_fmv(db, cid, 9.0, low=50.0, high=70.0)
    upsert_fmv(db, cid, 9.2, low=100.0, high=130.0)

    rows = list_comics(db, locg_id=12345, grade=9.0)
    assert len(rows) == 1
    assert rows[0]["grade"] == 9.0


def test_list_comics_max_age_excludes_stale(db):
    """A row whose fmv updated_at is older than the cutoff is excluded."""
    from datetime import datetime, timedelta, timezone

    cid = upsert_comic(db, "Hulk", "181", 1974)
    upsert_fmv(db, cid, 9.0, low=50.0, high=70.0)
    # Backdate updated_at to 30 days ago
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    db.execute("UPDATE fmv SET updated_at = ?", (old,))
    db.commit()

    assert list_comics(db, max_age_days=7) == []   # 30d > 7d cutoff
    assert len(list_comics(db, max_age_days=60)) == 1  # 30d < 60d cutoff


def test_list_comics_max_age_keeps_fresh(db):
    """A row whose updated_at is within the cutoff is included."""
    cid = upsert_comic(db, "Hulk", "181", 1974)
    upsert_fmv(db, cid, 9.0, low=50.0, high=70.0)

    rows = list_comics(db, max_age_days=7)
    assert len(rows) == 1


def test_list_comics_max_age_excludes_null_updated_at(db):
    """A comic with no FMV value (updated_at IS NULL) doesn't satisfy the
    freshness predicate. Without this guard, callers would treat grade-only
    stub rows as cache hits and skip the real compute."""
    cid = upsert_comic(db, "Hulk", "181", 1974)
    upsert_fmv(db, cid, 9.0)  # grade-only stub, no FMV values → updated_at stays NULL

    # Confirm the stub really did leave updated_at NULL
    row = db.execute("SELECT updated_at FROM fmv WHERE comic_id=?",
                     (cid,)).fetchone()
    assert row["updated_at"] is None

    assert list_comics(db, max_age_days=365) == []


def test_list_comics_combines_locg_grade_and_freshness(db):
    """The end-to-end FMV-cache lookup: locg_id + grade + max_age_days."""
    from datetime import datetime, timedelta, timezone

    cid = upsert_comic(db, "ASM", "300", 1988, locg_id=6977652)
    upsert_fmv(db, cid, 9.2, low=800.0, high=1000.0)

    rows = list_comics(db, locg_id=6977652, grade=9.2, max_age_days=7)
    assert len(rows) == 1

    # Same lookup with a tighter freshness window after backdating
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    db.execute("UPDATE fmv SET updated_at = ?", (old,))
    db.commit()

    assert list_comics(db, locg_id=6977652, grade=9.2, max_age_days=7) == []


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------


def _make_legacy_db() -> sqlite3.Connection:
    """Build a pre-migration DB with the old comics schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""
        CREATE TABLE bids (
            id INTEGER PRIMARY KEY,
            item_id TEXT NOT NULL,
            comic_id INTEGER,
            fmv_id INTEGER,
            max_bid REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE comics (
            id              INTEGER PRIMARY KEY,
            title           TEXT NOT NULL,
            issue           TEXT NOT NULL,
            year            INTEGER NOT NULL,
            grade           REAL,
            fmv_low         REAL,
            fmv_high        REAL,
            fmv_comps       INTEGER,
            fmv_confidence  TEXT,
            fmv_notes       TEXT,
            fmv_updated_at  TEXT,
            locg_id         INTEGER,
            locg_variant_id INTEGER,
            created_at      TEXT DEFAULT (datetime('now')),
            UNIQUE(title, issue, year, grade)
        )
    """)
    conn.execute("""
        CREATE TABLE bid_comics (
            bid_id     INTEGER NOT NULL REFERENCES bids(id),
            comic_id   INTEGER NOT NULL REFERENCES comics(id),
            is_primary INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (bid_id, comic_id)
        )
    """)
    conn.commit()
    return conn


def test_migration_collapses_shadow_rows_to_one_comic():
    conn = _make_legacy_db()
    # Two comics rows: same title/issue/year, different grade (the classic shadow bug)
    conn.execute("INSERT INTO comics (id, title, issue, year, grade, fmv_low) VALUES (1, 'ASM', '300', 1988, 9.2, 800.0)")
    conn.execute("INSERT INTO comics (id, title, issue, year, grade, fmv_low) VALUES (2, 'ASM', '300', 1988, 9.0, 600.0)")
    conn.commit()
    create_tables(conn)
    comics = conn.execute("SELECT * FROM comics").fetchall()
    assert len(comics) == 1
    fmv_rows = conn.execute("SELECT * FROM fmv ORDER BY grade").fetchall()
    assert len(fmv_rows) == 2
    grades = {r["grade"] for r in fmv_rows}
    assert grades == {9.0, 9.2}


def test_migration_sets_bids_fmv_id():
    conn = _make_legacy_db()
    conn.execute("INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, 'item1', 1, 50.0)")
    conn.execute("INSERT INTO comics (id, title, issue, year, grade, fmv_low) VALUES (1, 'ASM', '300', 1988, 9.2, 800.0)")
    conn.commit()
    create_tables(conn)
    bid = conn.execute("SELECT fmv_id FROM bids WHERE id=1").fetchone()
    assert bid["fmv_id"] is not None
    fmv = conn.execute("SELECT * FROM fmv WHERE id=?", (bid["fmv_id"],)).fetchone()
    assert fmv["grade"] == 9.2


def test_migration_migrates_bid_comics_to_bid_fmvs():
    conn = _make_legacy_db()
    conn.execute("INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, 'item1', 1, 50.0)")
    conn.execute("INSERT INTO comics (id, title, issue, year, grade, fmv_low) VALUES (1, 'ASM', '300', 1988, 9.2, 800.0)")
    conn.execute("INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (1, 1, 1)")
    conn.commit()
    create_tables(conn)
    bf = conn.execute("SELECT * FROM bid_fmvs WHERE bid_id=1").fetchall()
    assert len(bf) == 1
    assert bf[0]["is_primary"] == 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "bid_comics" not in tables


def test_migration_fmv_bid_fmvs_survive_python_memory_roundtrip():
    """Verifies the Python-memory table rebuild preserves all field values."""
    conn = _make_legacy_db()
    conn.execute("INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, 'item1', 1, 50.0)")
    conn.execute("""
        INSERT INTO comics (id, title, issue, year, grade, fmv_low, fmv_high, fmv_comps, fmv_confidence, fmv_notes)
        VALUES (1, 'ASM', '300', 1988, 9.2, 800.0, 1000.0, 12, 'high', 'Key issue')
    """)
    conn.execute("INSERT INTO bid_comics (bid_id, comic_id, is_primary) VALUES (1, 1, 1)")
    conn.commit()
    create_tables(conn)
    fmv = conn.execute("SELECT * FROM fmv").fetchone()
    assert fmv["low"] == 800.0
    assert fmv["high"] == 1000.0
    assert fmv["comps"] == 12
    assert fmv["confidence"] == "high"
    assert fmv["notes"] == "Key issue"


def test_migration_bid_with_null_comic_id_stays_unlinked():
    conn = _make_legacy_db()
    conn.execute("INSERT INTO bids (id, item_id, comic_id, max_bid) VALUES (1, 'item1', NULL, 50.0)")
    conn.commit()
    create_tables(conn)
    bid = conn.execute("SELECT fmv_id FROM bids WHERE id=1").fetchone()
    assert bid["fmv_id"] is None


def test_migration_is_idempotent():
    conn = _make_legacy_db()
    conn.execute("INSERT INTO comics (id, title, issue, year, grade, fmv_low) VALUES (1, 'ASM', '300', 1988, 9.2, 800.0)")
    conn.commit()
    create_tables(conn)
    create_tables(conn)
    comics = conn.execute("SELECT * FROM comics").fetchall()
    assert len(comics) == 1
    fmv_rows = conn.execute("SELECT * FROM fmv").fetchall()
    assert len(fmv_rows) == 1


def test_migration_crash_recovery_raises_runtime_error():
    """If comics has no grade column but comics_old exists, raise RuntimeError."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE bids (id INTEGER PRIMARY KEY, item_id TEXT, fmv_id INTEGER, max_bid REAL)")
    conn.execute("""
        CREATE TABLE comics (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            issue TEXT NOT NULL,
            year INTEGER NOT NULL,
            locg_id INTEGER,
            locg_variant_id INTEGER,
            created_at TEXT,
            UNIQUE(title, issue, year)
        )
    """)
    # Simulate mid-rebuild crash: comics_old still present
    conn.execute("""
        CREATE TABLE comics_old (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            issue TEXT NOT NULL,
            year INTEGER NOT NULL,
            grade REAL
        )
    """)
    conn.commit()
    with pytest.raises(RuntimeError, match="crashed mid-migration state"):
        create_tables(conn)


def test_migration_fmv_split_crash_after_drop_old_raises_runtime_error():
    """A crash after DROP TABLE comics_old in fmv_split leaves the schema looking
    already-migrated. The marker guard must raise RuntimeError on next startup."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE bids (id INTEGER PRIMARY KEY, item_id TEXT, fmv_id INTEGER, max_bid REAL)")
    # Post-drop schema: no grade col, no comics_old — gate would return early without marker.
    conn.execute("""
        CREATE TABLE comics (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, issue TEXT NOT NULL,
            year INTEGER NOT NULL, locg_id INTEGER, locg_variant_id INTEGER,
            created_at TEXT, UNIQUE(title, issue, year)
        )
    """)
    conn.execute("""
        CREATE TABLE fmv (
            id INTEGER PRIMARY KEY,
            comic_id INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
            grade REAL NOT NULL, low REAL, high REAL, comps INTEGER,
            confidence TEXT, notes TEXT, updated_at TEXT, UNIQUE(comic_id, grade)
        )
    """)
    conn.execute("""
        CREATE TABLE bid_fmvs (
            bid_id INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
            fmv_id INTEGER NOT NULL REFERENCES fmv(id) ON DELETE CASCADE,
            is_primary INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (bid_id, fmv_id)
        )
    """)
    conn.execute("CREATE TABLE migration_state (migration TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO migration_state (migration) VALUES ('fmv_split')")
    conn.commit()
    with pytest.raises(RuntimeError, match="fmv_split"):
        create_tables(conn)


def test_migration_year_nullable_crash_after_drop_old_raises_runtime_error():
    """A crash after DROP TABLE comics_old in year_nullable leaves year already
    nullable. The marker guard must raise RuntimeError on next startup."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE bids (id INTEGER PRIMARY KEY, item_id TEXT, fmv_id INTEGER, max_bid REAL)")
    # Post-drop schema: year is nullable — gate would return early without marker.
    conn.execute("""
        CREATE TABLE comics (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, issue TEXT NOT NULL,
            year INTEGER, locg_id INTEGER, locg_variant_id INTEGER, created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE fmv (
            id INTEGER PRIMARY KEY,
            comic_id INTEGER NOT NULL REFERENCES comics(id) ON DELETE CASCADE,
            grade REAL NOT NULL, low REAL, UNIQUE(comic_id, grade)
        )
    """)
    conn.execute("""
        CREATE TABLE bid_fmvs (
            bid_id INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
            fmv_id INTEGER NOT NULL REFERENCES fmv(id) ON DELETE CASCADE,
            is_primary INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (bid_id, fmv_id)
        )
    """)
    conn.execute("CREATE TABLE migration_state (migration TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO migration_state (migration) VALUES ('year_nullable')")
    conn.commit()
    with pytest.raises(RuntimeError, match="year_nullable"):
        create_tables(conn)


def test_migration_fresh_db_no_legacy_data_is_noop():
    """Fresh DB (no comics rows, no bid_comics) migration gate returns immediately."""
    conn = _make_db()
    create_tables(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "comics_old" not in tables
    assert "bid_comics" not in tables
    assert "fmv" in tables
    assert "bid_fmvs" in tables


def test_migrate_sweep_allcaps_orphans_merges_on_startup():
    """create_tables() sweeps ALL-CAPS yearless stubs into their yeared siblings."""
    conn = _make_db()
    create_tables(conn)
    # Manually insert a yeared canonical row and an ALL-CAPS yearless stub
    # (bypasses upsert_comic which now deduplicates — simulates pre-PER-123 data).
    yeared_id = upsert_comic(conn, "The Mighty Thor", "154", 1968)
    cur = conn.execute(
        "INSERT INTO comics (title, issue, year) VALUES (?, ?, NULL)",
        ("THE MIGHTY THOR", "154"),
    )
    conn.commit()
    stub_id = cur.lastrowid
    # Clear the migration marker so the sweep runs again on next create_tables call.
    conn.execute("DELETE FROM migration_state WHERE migration='sweep_allcaps_orphans'")
    conn.commit()

    create_tables(conn)

    assert conn.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 1
    row = conn.execute("SELECT id FROM comics WHERE year=1968").fetchone()
    assert row["id"] == yeared_id
    assert conn.execute(
        "SELECT COUNT(*) FROM migration_state WHERE migration='sweep_allcaps_orphans'"
    ).fetchone()[0] == 1


def test_migrate_sweep_allcaps_orphans_is_idempotent():
    """create_tables() called twice does not double-count or re-run the sweep."""
    conn = _make_db()
    create_tables(conn)
    upsert_comic(conn, "X-Men", "1", 1963)
    cur = conn.execute(
        "INSERT INTO comics (title, issue, year) VALUES (?, ?, NULL)",
        ("X-MEN", "1"),
    )
    conn.commit()
    conn.execute("DELETE FROM migration_state WHERE migration='sweep_allcaps_orphans'")
    conn.commit()

    create_tables(conn)
    assert conn.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 1

    # Second call — stub is gone; no new merges.
    create_tables(conn)
    assert conn.execute("SELECT COUNT(*) FROM comics").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM migration_state WHERE migration='sweep_allcaps_orphans'"
    ).fetchone()[0] == 1


def test_migrate_lowercase_title_indexes_creates_lower_expression_indexes(db):
    idx = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_comics_tiyv'"
    ).fetchone()
    assert idx is not None
    assert "lower(" in idx["sql"].lower()
    # BUI-28: variant is part of the unique key.
    assert "variant" in idx["sql"].lower()


def test_migrate_lowercase_title_indexes_blocks_case_variant_yeared_duplicate(db):
    db.execute(
        "INSERT INTO comics (title, issue, year) VALUES (?, ?, ?)",
        ("The Mighty Thor", "154", 1968),
    )
    db.commit()
    with pytest.raises(Exception):
        db.execute(
            "INSERT INTO comics (title, issue, year) VALUES (?, ?, ?)",
            ("THE MIGHTY THOR", "154", 1968),
        )
        db.commit()


def test_migrate_lowercase_title_indexes_is_idempotent():
    conn = _make_db()
    create_tables(conn)
    create_tables(conn)
    idx = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_comics_tiyv'"
    ).fetchone()
    assert idx is not None
    assert "lower(" in idx["sql"].lower()
    assert conn.execute(
        "SELECT COUNT(*) FROM migration_state WHERE migration='lowercase_title_indexes'"
    ).fetchone()[0] == 1


def test_migration_regression_second_grade_revision_upserts_fmv_not_new_comic():
    """After migration, a grade revision updates the existing fmv row, not inserts a new comic."""
    conn = _make_legacy_db()
    conn.execute("INSERT INTO comics (id, title, issue, year, grade, fmv_low) VALUES (1, 'ASM', '300', 1988, 9.2, 800.0)")
    conn.commit()
    create_tables(conn)
    # Now use new API: upsert same identity, different grade — should create another fmv row
    cid = upsert_comic(conn, "ASM", "300", 1988)
    upsert_fmv(conn, cid, 9.4, low=900.0)
    comics = conn.execute("SELECT * FROM comics").fetchall()
    assert len(comics) == 1, "Must not create a second comics row"
    fmv_rows = conn.execute("SELECT * FROM fmv WHERE comic_id=?", (cid,)).fetchall()
    assert len(fmv_rows) == 2


# ---------------------------------------------------------------------------
# Integration: sqlite_master verification
# ---------------------------------------------------------------------------


def test_register_db_tables_creates_tables_via_sqlite_master():
    conn = _make_db()
    create_tables(conn)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "comics" in tables
    assert "fmv" in tables
    assert "bid_fmvs" in tables
    assert "seller_scan_seen" in tables
    assert "bid_comics" not in tables
    conn.close()


# ---------------------------------------------------------------------------
# BUI-113: seller-scan seen-tracking helpers
# ---------------------------------------------------------------------------


def test_seller_scan_seen_table_is_idempotent():
    # create_tables runs on every server start; calling it twice must not error.
    conn = _make_db()
    create_tables(conn)
    create_tables(conn)
    assert get_seen_item_ids(conn) == set()
    conn.close()


def test_mark_and_get_seen_item_ids(db):
    assert mark_items_seen(db, ["111", "222"], "tuners36") == 2
    assert get_seen_item_ids(db) == {"111", "222"}


def test_mark_items_seen_is_idempotent(db):
    mark_items_seen(db, ["111"], "tuners36")
    # Re-marking inserts nothing new and preserves the original row.
    assert mark_items_seen(db, ["111", "333"], "tuners36") == 1
    assert get_seen_item_ids(db) == {"111", "333"}


def test_get_seen_item_ids_filters_by_seller(db):
    mark_items_seen(db, ["111"], "tuners36")
    mark_items_seen(db, ["222"], "beatlebluecat")
    assert get_seen_item_ids(db) == {"111", "222"}
    assert get_seen_item_ids(db, "tuners36") == {"111"}


def test_mark_items_seen_preserves_first_seen_at(db):
    mark_items_seen(db, ["111"], "tuners36")
    first = db.execute(
        "SELECT first_seen_at FROM seller_scan_seen WHERE item_id='111'"
    ).fetchone()["first_seen_at"]
    mark_items_seen(db, ["111"], "someoneelse")
    row = db.execute(
        "SELECT first_seen_at, seller FROM seller_scan_seen WHERE item_id='111'"
    ).fetchone()
    # INSERT OR IGNORE keeps the original timestamp and seller.
    assert row["first_seen_at"] == first
    assert row["seller"] == "tuners36"


def test_remove_seen_for_seller_removes_only_that_seller(db):
    """BUI-542 (--forget): scoped to one seller — a different seller's seen
    entries must survive untouched."""
    mark_items_seen(db, ["111", "222"], "tuners36")
    mark_items_seen(db, ["333"], "beatlebluecat")
    removed = remove_seen_for_seller(db, "tuners36")
    assert removed == 2
    assert get_seen_item_ids(db) == {"333"}
    assert get_seen_item_ids(db, "tuners36") == set()
    assert get_seen_item_ids(db, "beatlebluecat") == {"333"}


def test_remove_seen_for_seller_returns_zero_when_nothing_to_remove(db):
    assert remove_seen_for_seller(db, "nosuchseller") == 0


def test_remove_seen_for_seller_is_reversible_by_a_later_scan(db):
    """After a forget, a subsequent mark_items_seen for the same item_id
    re-inserts it (not blocked by any leftover state) — this is the exact
    "resurface, then get re-marked seen on the next genuine match" flow."""
    mark_items_seen(db, ["111"], "tuners36")
    remove_seen_for_seller(db, "tuners36")
    assert get_seen_item_ids(db, "tuners36") == set()
    mark_items_seen(db, ["111"], "tuners36")
    assert get_seen_item_ids(db, "tuners36") == {"111"}


# ---------------------------------------------------------------------------
# BUI-121: collection-wins seen-tracking helpers
# ---------------------------------------------------------------------------


def test_collection_wins_seen_table_exists():
    conn = _make_db()
    create_tables(conn)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "collection_wins_seen" in tables
    conn.close()


def test_collection_wins_seen_table_is_idempotent():
    # create_tables runs on every server start; calling it twice must not error.
    conn = _make_db()
    create_tables(conn)
    create_tables(conn)
    assert get_collection_wins_seen(conn) == set()
    conn.close()


def test_mark_and_get_collection_wins_seen(db):
    assert mark_collection_wins_seen(db, ["111", "222"]) == 2
    assert get_collection_wins_seen(db) == {"111", "222"}


def test_mark_collection_wins_seen_is_idempotent(db):
    mark_collection_wins_seen(db, ["111"])
    # Re-marking inserts nothing new and preserves the original row.
    assert mark_collection_wins_seen(db, ["111", "333"]) == 1
    assert get_collection_wins_seen(db) == {"111", "333"}


def test_mark_collection_wins_seen_preserves_first_seen_at(db):
    mark_collection_wins_seen(db, ["111"])
    first = db.execute(
        "SELECT first_seen_at FROM collection_wins_seen WHERE item_id='111'"
    ).fetchone()["first_seen_at"]
    mark_collection_wins_seen(db, ["111"])
    row = db.execute(
        "SELECT first_seen_at FROM collection_wins_seen WHERE item_id='111'"
    ).fetchone()
    # INSERT OR IGNORE keeps the original timestamp.
    assert row["first_seen_at"] == first
