"""The quarantine WRITE path, its guard, and its surfaces (BUI-648).

BUI-647 made quarantine readable. This makes it settable, reversible, audited
and visible — and refuses the one application that costs real money.

The load-bearing piece is the **last-owned-row guard**. Quarantining pulls a
row out of every ownership-matcher pool, so quarantining the only owned copy of
a book makes ``collection check`` answer "not owned" and the buy path re-buys
it. Section 1 pins the predicate down before anything writes; the rest of the
file is the write path built around it.

Fail-closed applies ONLY to that branch. An ordinary quarantine — a foreign
Panini twin standing beside a real US copy — proceeds with no ceremony.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Optional

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
#
# The default shape is the real one this state exists for (BUI-563): a Panini
# Italian licensed edition our own record-win push minted, standing beside the
# US copy of the same issue. Quarantining the Panini row is the ORDINARY case —
# the US row still answers ownership, so no guard fires.

US_PUBLISHER = "Marvel Comics"
US_SERIES = "The Amazing Spider-Man (Vol. 1) (1963 - 1998)"
IT_PUBLISHER = "Panini Comics"
IT_SERIES = "L'Uomo Ragno (Vol. 1) (1994 - 2000)"
TITLE = "The Amazing Spider-Man #238"


def _row(
    *,
    publisher: str,
    series: str,
    full_title: str = TITLE,
    release_date: str = "1983-03-01",
    in_collection: int = 1,
    quarantined: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "publisher_name": publisher,
        "series_name": series,
        "full_title": full_title,
        "release_date": release_date,
        "in_collection": in_collection,
        "in_wish_list": 0,
        "source": "locg_export",
        "pushed_to_locg_at": "2024-01-01T00:00:00.000000Z",
        "local_added_at": "2024-01-01T00:00:00.000000Z",
    }
    row.update(extra)
    if quarantined:
        row["quarantined"] = _marker()
    return row


def _marker(reason: str = "foreign licensed edition minted by our own record-win push"):
    from locg.collection_cache import quarantine_marker

    return quarantine_marker(reason=reason, ticket="BUI-648", by="test")


def _us_row(**kw: Any) -> dict[str, Any]:
    return _row(publisher=US_PUBLISHER, series=US_SERIES, **kw)


def _it_row(**kw: Any) -> dict[str, Any]:
    return _row(publisher=IT_PUBLISHER, series=IT_SERIES, release_date="1994-06-01", **kw)


def _cache(tmp_path: Path, comics: list[dict[str, Any]]):
    from locg.collection_cache import CollectionCache

    cache = CollectionCache(
        path=tmp_path / "collection.json",
        lock_path=tmp_path / "collection.lock",
        audit_path=tmp_path / "import-history.jsonl",
    )
    cache.apply(lambda p: p["comics"].extend(comics), command="seed")
    return cache


def _it_identity() -> dict[str, str]:
    """Kwargs naming the Panini row by its full four-field identity."""
    return {
        "publisher_name": IT_PUBLISHER,
        "series_name": IT_SERIES,
        "full_title": TITLE,
        "release_date": "1994-06-01",
    }


def _quarantine_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        **_it_identity(),
        "reason": "Italian licensed edition, not the US book",
        "ticket": "BUI-648",
        "by": "tester",
    }
    kwargs.update(overrides)
    return kwargs


def _covering(comics: list[dict[str, Any]], target: Optional[dict[str, Any]] = None):
    from locg.commands import _owned_rows_covering

    return _owned_rows_covering(comics, target if target is not None else comics[0])


# ---------------------------------------------------------------------------
# 1. The predicate — written first, because it is the only piece here whose
#    failure costs money rather than time.
# ---------------------------------------------------------------------------

def test_a_sibling_owned_copy_covers_the_target():
    """The ordinary case: the US copy answers ownership for the Panini row."""
    comics = [_it_row(), _us_row()]
    assert [r["publisher_name"] for r in _covering(comics)] == [US_PUBLISHER]


def test_the_target_never_counts_as_its_own_cover():
    """The whole guard collapses if the row being quarantined is allowed to
    prove its own replaceability — every quarantine would be permitted."""
    assert _covering([_it_row()]) == []


def test_a_cross_masthead_copy_covers_the_target():
    """The reason coverage goes through ``owned_match_keys`` and not a literal
    series comparison (BUI-197/BUI-200): the surviving copy can be filed under
    a DIFFERENT masthead for the same run. ``The X-Men #107`` and
    ``Uncanny X-Men #107`` are the same book; a guard that compared normalized
    series names alone would call this the last owned row and refuse.
    """
    comics = [
        _row(
            publisher="Marvel Comics",
            series="Uncanny X-Men (Vol. 1) (1981 - 2011)",
            full_title="Uncanny X-Men #107",
            release_date="1977-10-01",
        ),
        _row(
            publisher="Marvel Comics",
            series="The X-Men (Vol. 1) (1963 - 1981)",
            full_title="The X-Men #107",
            release_date="1977-10-01",
        ),
    ]
    assert [r["full_title"] for r in _covering(comics)] == ["The X-Men #107"]


def test_a_different_issue_does_not_cover():
    comics = [_it_row(), _us_row(full_title="The Amazing Spider-Man #239")]
    assert _covering(comics) == []


def test_a_different_series_does_not_cover():
    comics = [
        _it_row(),
        _row(
            publisher=US_PUBLISHER,
            series="Daredevil (Vol. 1) (1964 - 1998)",
            full_title="Daredevil #238",
        ),
    ]
    assert _covering(comics) == []


def test_an_unowned_row_does_not_cover():
    """``in_collection`` is a copies-owned count; 0 is tracked-but-not-owned
    (BUI-249/250/251) and answers no ownership check."""
    comics = [_it_row(), _us_row(in_collection=0)]
    assert _covering(comics) == []


def test_an_already_quarantined_row_does_not_cover():
    """Coverage is drawn from the same quarantine-filtered pool
    ``cmd_collection_check`` reads, so a row that is itself hidden cannot
    vouch for another. Without this, two quarantines in sequence blind a book
    that neither one alone would have."""
    comics = [_it_row(), _us_row(quarantined=True)]
    assert _covering(comics) == []


def test_coverage_is_unformable_for_a_title_with_no_issue_token():
    """A TPB/OGN title yields no ``issue_key``, so there is no
    ``owned_match_keys × issue_key`` pair to compare and no way to PROVE a
    replacement exists. ``None`` is not "no guard needed" — callers must treat
    it exactly like an empty list."""
    from locg.commands import _owned_rows_covering

    comics = [_us_row(full_title="Spider-Man: Kraven's Last Hunt TPB")]
    assert _owned_rows_covering(comics, comics[0]) is None


def test_coverage_of_an_unowned_target_is_still_computed():
    """The predicate answers "who else covers this", not "is a guard needed" —
    that decision belongs to the command, which skips the guard for an unowned
    target. Keeping the two apart is what lets the guard be tested directly."""
    comics = [_it_row(in_collection=0), _us_row()]
    assert [r["publisher_name"] for r in _covering(comics)] == [US_PUBLISHER]


# ---------------------------------------------------------------------------
# 2. The refusal — the guard wired into the command
# ---------------------------------------------------------------------------

def test_quarantining_the_last_owned_copy_is_refused(tmp_path):
    from locg.commands import cmd_collection_quarantine

    cache = _cache(tmp_path, [_it_row()])
    result = cmd_collection_quarantine(**_quarantine_kwargs(), cache=cache)

    assert result["status"] == "last_owned_row"
    assert result["reason_detail"] == "no_covering_row"
    assert "quarantined" not in cache.load()["comics"][0], (
        "a refusal must not have written the marker"
    )


def test_an_unparseable_title_is_refused_as_unverifiable(tmp_path):
    """Fail-closed: coverage that cannot be COMPUTED is treated exactly like
    coverage that does not exist."""
    from locg.commands import cmd_collection_quarantine

    title = "Spider-Man: Kraven's Last Hunt TPB"
    cache = _cache(tmp_path, [_it_row(full_title=title), _us_row(full_title=title)])
    result = cmd_collection_quarantine(
        **_quarantine_kwargs(full_title=title), cache=cache
    )

    assert result["status"] == "last_owned_row"
    assert result["reason_detail"] == "unparseable_issue_token"


def test_an_ordinary_quarantine_proceeds_without_ceremony(tmp_path):
    """No force, no reason-for-the-force, no confirmation — the guard exists
    for one branch and must not tax the other."""
    from locg.commands import cmd_collection_quarantine

    cache = _cache(tmp_path, [_it_row(), _us_row()])
    result = cmd_collection_quarantine(**_quarantine_kwargs(), cache=cache)

    assert result["status"] == "ok"
    assert result["forced"] is False
    rows = cache.load()["comics"]
    assert "quarantined" in rows[0] and "quarantined" not in rows[1]
    assert "forced" not in rows[0]["quarantined"], (
        "an unforced quarantine must not be stamped as an override"
    )


def test_an_unowned_row_is_quarantined_without_the_guard(tmp_path):
    """A wish/pull row (``in_collection=0``) answers no ownership check, so
    hiding it cannot blind one — the guard must not fire."""
    from locg.commands import cmd_collection_quarantine

    cache = _cache(tmp_path, [_it_row(in_collection=0)])
    result = cmd_collection_quarantine(**_quarantine_kwargs(), cache=cache)

    assert result["status"] == "ok"


def test_force_without_a_reason_is_refused(tmp_path):
    from locg.commands import cmd_collection_quarantine

    cache = _cache(tmp_path, [_it_row()])
    result = cmd_collection_quarantine(
        **_quarantine_kwargs(force=True), cache=cache
    )

    assert result["status"] == "invalid_request"
    assert "force_reason" in result["error"]
    assert "quarantined" not in cache.load()["comics"][0]


def test_force_with_a_reason_overrides_and_records_it(tmp_path):
    """The override is only as good as its paper trail: the operator who later
    finds this book reported not-owned needs the row itself to say who hid the
    last copy and why."""
    from locg.commands import cmd_collection_quarantine

    cache = _cache(tmp_path, [_it_row()])
    result = cmd_collection_quarantine(
        **_quarantine_kwargs(force=True, force_reason="verified sold, no copy remains"),
        cache=cache,
    )

    assert result["status"] == "ok"
    assert result["forced"] is True
    marker = cache.load()["comics"][0]["quarantined"]
    assert marker["forced"] == {
        "guard": "last_owned_row",
        "reason": "verified sold, no copy remains",
    }


def test_force_is_not_stamped_when_the_guard_never_fired(tmp_path):
    """``forced`` means "an override happened", not "the flag was passed" — a
    stamp on a quarantine that needed no override would make the audit read as
    a last-copy override forever after."""
    from locg.commands import cmd_collection_quarantine

    cache = _cache(tmp_path, [_it_row(), _us_row()])
    result = cmd_collection_quarantine(
        **_quarantine_kwargs(force=True, force_reason="belt and braces"), cache=cache
    )

    assert result["status"] == "ok"
    assert result["forced"] is False
    assert "forced" not in cache.load()["comics"][0]["quarantined"]


def test_the_guard_is_re_evaluated_under_the_lock(tmp_path):
    """TOCTOU: the covering copy can be quarantined by a concurrent writer
    between the pre-check and the write. Re-running the guard inside
    ``CollectionCache.apply``'s exclusive lock is what makes the refusal a
    property of the store rather than of a stale snapshot.
    """
    from locg.collection_cache import CollectionCache
    from locg.commands import cmd_collection_quarantine

    cache = _cache(tmp_path, [_it_row(), _us_row()])

    real_apply = CollectionCache.apply
    raced: list[bool] = []

    def racing_apply(self, mutate_fn, command="unknown", timeout=30.0):
        if not raced:
            raced.append(True)
            # A concurrent writer removes the only covering copy after our
            # pre-check has already seen it and passed.
            real_apply(
                self,
                lambda p: p["comics"].__setitem__(1, _us_row(in_collection=0)),
                command="concurrent",
            )
        return real_apply(self, mutate_fn, command=command, timeout=timeout)

    CollectionCache.apply = racing_apply
    try:
        result = cmd_collection_quarantine(**_quarantine_kwargs(), cache=cache)
    finally:
        CollectionCache.apply = real_apply

    assert result["status"] == "last_owned_row", (
        "the pre-check passed on a snapshot that was already stale; the guard "
        "must be re-run against the payload loaded under the lock"
    )
    assert "quarantined" not in cache.load()["comics"][0]


# ---------------------------------------------------------------------------
# 3. Identity resolution
# ---------------------------------------------------------------------------

def test_two_identity_matches_are_refused_before_any_write(tmp_path):
    """The store genuinely holds duplicate identities (the import's own
    ``owned_duplicate_identities`` counts them), so "pick the first" would
    quarantine an arbitrary one of two indistinguishable rows."""
    from locg.commands import cmd_collection_quarantine

    cache = _cache(tmp_path, [_it_row(), _it_row(), _us_row()])
    result = cmd_collection_quarantine(**_quarantine_kwargs(), cache=cache)

    assert result["status"] == "ambiguous"
    assert result["count"] == 2
    assert not any("quarantined" in r for r in cache.load()["comics"])


def test_no_identity_match_is_not_found(tmp_path):
    from locg.commands import cmd_collection_quarantine

    cache = _cache(tmp_path, [_us_row()])
    result = cmd_collection_quarantine(**_quarantine_kwargs(), cache=cache)

    assert result["status"] == "not_found"


def test_identity_is_never_keyed_on_gixen_item_id(tmp_path):
    """BUI-500: a lot or run bought together shares ONE ``gixen_item_id``
    across every issue in it, so keying the mutation there would quarantine an
    arbitrary sibling. The identity is the four-field tuple; a shared item_id
    must not make two different issues look like one row."""
    import inspect

    from locg.commands import cmd_collection_quarantine, cmd_collection_unquarantine

    for fn in (cmd_collection_quarantine, cmd_collection_unquarantine):
        assert "gixen_item_id" not in inspect.signature(fn).parameters
        assert "gixen_item_id" not in inspect.getsource(fn)

    cache = _cache(
        tmp_path,
        [
            _it_row(gixen_item_id="123"),
            _it_row(full_title="The Amazing Spider-Man #239", gixen_item_id="123"),
            _us_row(),
        ],
    )
    result = cmd_collection_quarantine(**_quarantine_kwargs(), cache=cache)

    assert result["status"] == "ok"
    rows = cache.load()["comics"]
    assert "quarantined" in rows[0]
    assert "quarantined" not in rows[1], "the lot sibling must be untouched"


def test_a_missing_full_title_is_refused(tmp_path):
    from locg.commands import cmd_collection_quarantine

    cache = _cache(tmp_path, [_it_row(), _us_row()])
    result = cmd_collection_quarantine(
        **_quarantine_kwargs(full_title="  "), cache=cache
    )
    assert result["status"] == "invalid_request"


@pytest.mark.parametrize("field", ["reason", "ticket", "by"])
def test_an_unattributable_quarantine_is_refused(tmp_path, field):
    """``quarantine_marker``'s contract, surfaced as a command refusal rather
    than a traceback — and checked BEFORE the store is touched."""
    from locg.commands import cmd_collection_quarantine

    cache = _cache(tmp_path, [_it_row(), _us_row()])
    result = cmd_collection_quarantine(**_quarantine_kwargs(**{field: " "}), cache=cache)

    assert result["status"] == "invalid_request"
    assert not any("quarantined" in r for r in cache.load()["comics"])


def test_re_quarantining_is_refused_rather_than_overwriting(tmp_path):
    """Overwriting would discard the original marker — the only record of who
    hid the row and why."""
    from locg.commands import cmd_collection_quarantine

    cache = _cache(tmp_path, [_it_row(quarantined=True), _us_row()])
    original = cache.load()["comics"][0]["quarantined"]

    result = cmd_collection_quarantine(**_quarantine_kwargs(), cache=cache)

    assert result["status"] == "already_quarantined"
    assert cache.load()["comics"][0]["quarantined"] == original


# ---------------------------------------------------------------------------
# 4. Audit + reversal
# ---------------------------------------------------------------------------

def _audit_records(cache) -> list[dict[str, Any]]:
    if not cache.audit_path.exists():
        return []
    return [json.loads(line) for line in cache.audit_path.read_text().splitlines() if line]


def test_quarantine_appends_and_returns_its_audit_record(tmp_path):
    from locg.commands import cmd_collection_quarantine

    cache = _cache(tmp_path, [_it_row(), _us_row()])
    result = cmd_collection_quarantine(**_quarantine_kwargs(), cache=cache)

    records = [r for r in _audit_records(cache) if r["type"] == "collection_quarantine"]
    assert len(records) == 1
    assert records[0] == result["audit"], "the returned record must BE the stored one"
    assert records[0]["details"]["identity"] == _it_identity()
    assert records[0]["details"]["quarantined"]["reason"]


def test_the_audit_names_the_rows_the_guard_leaned_on(tmp_path):
    """The pass-branch counterpart to ``forced``. Coverage is year-blind by
    construction, so the copy that made a quarantine safe can be a different
    era than the row hidden — naming it is what makes that visible afterwards
    rather than assumed away."""
    from locg.commands import cmd_collection_quarantine

    cache = _cache(tmp_path, [_it_row(), _us_row()])
    result = cmd_collection_quarantine(**_quarantine_kwargs(), cache=cache)

    assert result["covering_rows"] == [{
        "full_title": TITLE,
        "series_name": US_SERIES,
        "release_date": "1983-03-01",
    }]
    assert result["audit"]["details"]["covering_rows"] == result["covering_rows"]


def test_a_forced_quarantine_records_no_cover(tmp_path):
    """There was nothing to lean on — that is what made it a force."""
    from locg.commands import cmd_collection_quarantine

    cache = _cache(tmp_path, [_it_row()])
    result = cmd_collection_quarantine(
        **_quarantine_kwargs(force=True, force_reason="verified sold"), cache=cache
    )

    assert result["forced"] is True
    assert result["covering_rows"] == []


def test_unquarantine_removes_the_key_and_audits_it(tmp_path):
    from locg.commands import cmd_collection_unquarantine

    cache = _cache(tmp_path, [_it_row(quarantined=True), _us_row()])
    marker = cache.load()["comics"][0]["quarantined"]

    result = cmd_collection_unquarantine(**_it_identity(), by="tester", cache=cache)

    assert result["status"] == "ok"
    assert "quarantined" not in cache.load()["comics"][0], (
        "the KEY must go, not just its contents — an empty object would read "
        "as unquarantined to is_quarantined but still linger in the store"
    )
    records = [r for r in _audit_records(cache) if r["type"] == "collection_unquarantine"]
    assert len(records) == 1
    assert records[0] == result["audit"]
    assert records[0]["details"]["removed"] == marker, (
        "the lifted marker is the only record of why the row was ever hidden"
    )


def test_unquarantining_a_clean_row_is_refused(tmp_path):
    from locg.commands import cmd_collection_unquarantine

    cache = _cache(tmp_path, [_it_row(), _us_row()])
    result = cmd_collection_unquarantine(**_it_identity(), by="tester", cache=cache)

    assert result["status"] == "not_quarantined"
    assert not any(r["type"] == "collection_unquarantine" for r in _audit_records(cache))


def test_quarantine_round_trips(tmp_path):
    from locg.commands import cmd_collection_quarantine, cmd_collection_unquarantine

    cache = _cache(tmp_path, [_it_row(), _us_row()])
    before = cache.load()["comics"][0]

    cmd_collection_quarantine(**_quarantine_kwargs(), cache=cache)
    cmd_collection_unquarantine(**_it_identity(), by="tester", cache=cache)

    assert cache.load()["comics"][0] == before


# ---------------------------------------------------------------------------
# 5. The wrong-store guard (BUI-476/489)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command_name", ["quarantine", "unquarantine"])
def test_no_cache_and_no_locg_data_dir_is_refused(monkeypatch, command_name):
    """Both are MUTATORS. Run bare on the Mac Mini the default resolution lands
    on the repo's local ``data/locg`` — a different, possibly non-empty store."""
    import locg.commands as commands

    monkeypatch.delenv("LOCG_DATA_DIR", raising=False)
    fn = getattr(commands, f"cmd_collection_{command_name}")
    kwargs = _quarantine_kwargs() if command_name == "quarantine" else {
        **_it_identity(), "by": "tester"
    }

    assert fn(**kwargs)["status"] == "explicit_store_required"


# ---------------------------------------------------------------------------
# 6. Surfaces — a quarantined row is never re-pushed, and never silently
#    vanishes from a count it used to appear in.
# ---------------------------------------------------------------------------

def _pending_row(**kw: Any) -> dict[str, Any]:
    return _us_row(pushed_to_locg_at=None, local_added_at="2026-01-01T00:00:00.000000Z", **kw)


def test_pending_push_rows_partitions_quarantined_rows_out_of_ready():
    """The export writes ``ready`` to the LOCG bulk-import CSV. A quarantined
    row pushed back is the loop BUI-563 could not break: LOCG re-emits it on
    the next export, so it must never re-enter the CSV."""
    from locg.collection_io import _pending_push_rows

    payload = {"comics": [
        _pending_row(),
        _pending_row(full_title="The Amazing Spider-Man #239", quarantined=True),
        _pending_row(full_title="The Amazing Spider-Man #240", needs_manual_variant=True),
    ]}
    ready, manual_variant, manual_series, quarantined = _pending_push_rows(payload)

    assert [r["full_title"] for r in ready] == [TITLE]
    assert [r["full_title"] for r in quarantined] == ["The Amazing Spider-Man #239"]
    assert [r["full_title"] for r in manual_variant] == ["The Amazing Spider-Man #240"]
    assert manual_series == []


def test_a_quarantined_row_is_not_reported_as_a_manual_flag():
    """The buckets are exclusive and quarantine wins: a quarantined row that
    ALSO carries needs_manual_variant is not awaiting manual attention — it is
    never going to be pushed at all, and listing it under a manual flag invites
    someone to 'fix' it back into the export."""
    from locg.collection_io import _pending_push_rows

    payload = {"comics": [_pending_row(quarantined=True, needs_manual_variant=True)]}
    ready, manual_variant, manual_series, quarantined = _pending_push_rows(payload)

    assert (ready, manual_variant, manual_series) == ([], [], [])
    assert len(quarantined) == 1


def test_a_quarantined_row_never_reaches_the_bulk_import_csv(tmp_path, monkeypatch):
    """End-to-end through the real export, not just the partition: the CSV is
    what gets uploaded to LOCG, and re-pushing a quarantined row re-asserts the
    very ownership the quarantine exists to stop trusting (BUI-563's loop)."""
    from locg.commands import cmd_collection_export

    monkeypatch.setenv("LOCG_DATA_DIR", str(tmp_path))
    _cache(tmp_path, [
        _pending_row(),
        _pending_row(full_title="The Amazing Spider-Man #239", quarantined=True),
    ])

    out = tmp_path / "bulk.csv"
    result = cmd_collection_export(out_path=str(out))
    csv_text = out.read_text()

    assert result["ready_count"] == 1
    assert result["quarantined_pending_count"] == 1
    assert TITLE in csv_text
    assert "The Amazing Spider-Man #239" not in csv_text


def test_status_reports_the_quarantined_counts(tmp_path, monkeypatch):
    """``pending_push_count`` no longer includes a quarantined pending row, so
    the count that replaces it has to exist or the row silently vanishes."""
    from locg.commands import cmd_collection_status

    monkeypatch.setenv("LOCG_DATA_DIR", str(tmp_path))
    _cache(tmp_path, [
        _pending_row(),
        _pending_row(full_title="The Amazing Spider-Man #239", quarantined=True),
        _us_row(full_title="The Amazing Spider-Man #240", quarantined=True),
    ])

    result = cmd_collection_status(verbose=True)

    assert result["row_count"] == 3, "quarantined rows are still rows"
    assert result["pending_push_count"] == 1
    assert result["quarantined_count"] == 2
    assert result["quarantined_pending_count"] == 1


def test_import_summary_reports_the_quarantined_count(tmp_path):
    """After the one operation that rewrites every row, the marker's survival
    (BUI-647) has to be observable without re-reading the store by hand."""
    import openpyxl

    from locg.collection_io import LOCG_XLSX_HEADERS, import_xlsx

    cache = _cache(tmp_path, [_it_row(quarantined=True), _us_row()])

    xlsx = tmp_path / "reexport.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(LOCG_XLSX_HEADERS))
    ws.append([
        IT_PUBLISHER, IT_SERIES, TITLE, "1994-06-01",
        1, 0, 0, None, "Print", None, None,
        None, None, None, None, None, None, None, None, None, None,
    ])
    wb.save(xlsx)

    summary = import_xlsx(xlsx, cache)

    assert summary["quarantined_count"] == 1


# ---------------------------------------------------------------------------
# 7. Coverage reuses the matcher's own pool — asserted, not assumed
# ---------------------------------------------------------------------------

def test_coverage_reads_the_same_pool_cmd_collection_check_reads():
    """``_owned_rows_covering`` predicts what ``collection check`` will say
    AFTER the quarantine lands. If it drew its rows from anywhere other than
    ``_owned_series_issue_candidates`` — the pool the check itself reads — the
    two could disagree, and the direction that costs is the guard permitting a
    quarantine the check then reports as not-owned.
    """
    import inspect

    from locg.commands import _owned_rows_covering

    source = inspect.getsource(_owned_rows_covering)
    assert "_owned_series_issue_candidates" in source
    assert "owned_match_keys" in source, (
        "coverage must span the masthead-equivalence key set, not one literal "
        "series key — see test_a_cross_masthead_copy_covers_the_target"
    )


def test_the_write_path_goes_through_collection_cache_apply():
    """Never a bare rewrite of collection.json: ``apply`` is what supplies the
    exclusive flock, the .bak rotation and the atomic write. The DELETE API is
    unsafe on this store (alias + cross-volume ambiguity), which is why this
    is a hand-built identity mutation in the first place."""
    import locg.commands as commands

    tree = ast.parse(Path(commands.__file__).read_text())
    for name in ("cmd_collection_quarantine", "cmd_collection_unquarantine"):
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == name
        )
        calls = {
            n.func.attr for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert "apply" in calls, f"{name} must mutate through CollectionCache.apply"
        assert "append_audit" in calls, f"{name} must append an audit record"
