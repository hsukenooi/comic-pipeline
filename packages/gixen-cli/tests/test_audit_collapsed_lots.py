"""Unit tests for audit_collapsed_lots.py (BUI-1386)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import audit_collapsed_lots as acl

PF = "800765031857"


def _snipe(item_id, title):
    return {"item_id": item_id, "title": title, "status": "WON", "time_to_end": "ENDED",
            "current_bid": 10.0, "end_date_iso": "2026-11-13T00:00:00Z"}


def _lot_identity(series, issues, year=2020):
    return {"series": series, "issue": None, "year": year, "is_lot": True,
            "constituent_issues": issues, "edition": "single-issue", "variant_text": ""}


def _run(snipes, identities, verdicts, rows=None):
    return acl.audit(
        snipes, "http://x", rows=rows,
        identify=lambda titles: identities,
        verdicts_fn=lambda url, lots: verdicts,
    )


PF_ISSUES = [str(i) for i in range(1, 17)]


def _pf():
    return [_snipe(PF, "Power Fantasy 1-16")], [_lot_identity("The Power Fantasy", PF_ISSUES)]


def test_lot_fully_present():
    s, i = _pf()
    v = {("The Power Fantasy", n): acl.IN for n in PF_ISSUES}
    lot = _run(s, i, v)["lots"][0]
    assert lot["missing"] == [] and lot["present"] == PF_ISSUES


def test_collapsed_lot_only_last_issue_present():
    s, i = _pf()
    v = {("The Power Fantasy", n): acl.MISSING for n in PF_ISSUES}
    v[("The Power Fantasy", "16")] = acl.IN
    lot = _run(s, i, v)["lots"][0]
    assert lot["present"] == ["16"]
    assert lot["missing"] == PF_ISSUES[:15]


def test_workaround_rows_without_item_id_are_not_missing():
    # #1-15 re-recorded with no item id: series+issue says owned, row keys empty.
    s, i = _pf()
    v = {("The Power Fantasy", n): acl.IN for n in PF_ISSUES}
    rows = [{"gixen_item_id": None, "full_title": f"The Power Fantasy #{n}"} for n in PF_ISSUES[:15]]
    rows.append({"gixen_item_id": PF, "full_title": "The Power Fantasy #16"})
    assert _run(s, i, v, rows)["lots"][0]["missing"] == []


def test_item_id_row_match_beats_missing_verdict():
    s, i = _pf()
    v = {("The Power Fantasy", n): acl.MISSING for n in PF_ISSUES}
    rows = [{"gixen_item_id": PF, "full_title": "The Power Fantasy #016"}]
    lot = _run(s, i, v, rows)["lots"][0]
    assert "16" in lot["present"] and "16" not in lot["missing"]


def test_unsure_verdict_is_never_missing():
    s, i = _pf()
    v = {("The Power Fantasy", n): acl.IN for n in PF_ISSUES}
    v[("The Power Fantasy", "3")] = "ambiguous_cross_volume"
    del v[("The Power Fantasy", "4")]
    lot = _run(s, i, v)["lots"][0]
    assert lot["missing"] == [] and lot["unsure"] == ["3", "4"]


def test_non_lot_win_is_ignored():
    ident = {"series": "X-Men", "issue": "104", "year": 1977, "is_lot": False}
    r = _run([_snipe("1", "X-Men 104")], [ident], {})
    assert r["lots"] == [] and r["unparseable"] == []


def test_unparseable_lot_is_reported_not_classified():
    ident = _lot_identity("Foo", ["1", None, "3"])
    r = _run([_snipe("2", "Foo lot")], [ident], {})
    assert r["lots"] == [] and r["unparseable"][0]["item_id"] == "2"


def test_range_title_reported_as_candidate_not_lot():
    ident = {"series": "Image The Power Fantasy image Comics", "issue": "1", "year": None,
             "is_lot": False, "constituent_issues": []}
    snipes = [_snipe(PF, "Image The Power Fantasy image Comics 1-16, 1st Prints"),
              _snipe("5", "X-Men 104 - 1st brief Starjammers")]
    r = _run(snipes, [ident, dict(ident, issue="104")], {})
    assert r["lots"] == []
    assert [c["range"] for c in r["range_candidates"]] == ["1-16"]
    assert "POSSIBLE RANGE LOT" in acl.format_report(r)


def test_duplicate_constituents_deduped():
    s = [_snipe("3", "Foo 100,100,101")]
    i = [_lot_identity("Foo", ["100", "100", "101"])]
    lot = _run(s, i, {("Foo", "100"): acl.IN, ("Foo", "101"): acl.MISSING})["lots"][0]
    assert lot["constituents"] == ["100", "101"] and lot["missing"] == ["101"]


def test_format_report_summary_line():
    s, i = _pf()
    v = {("The Power Fantasy", n): acl.IN for n in PF_ISSUES}
    v[("The Power Fantasy", "2")] = acl.MISSING
    out = acl.format_report(_run(s, i, v))
    assert "missing:      2" in out
    assert "1 lot(s), 1 with missing issues, 1 missing issue(s)" in out


class _Resp:
    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


class _Session:
    def __init__(self, health=None):
        self.posts, self.health = [], health or {"git_sha": "abc1234"}

    def get(self, url, timeout):
        return _Resp(self.health)

    def post(self, url, json, timeout):
        self.posts.append((url, json))
        return _Resp({"ok": True})


def test_apply_refuses_without_bui_1384(monkeypatch):
    s, i = _pf()
    result = _run(s, i, {("The Power Fantasy", n): acl.MISSING for n in PF_ISSUES})
    sess = _Session()
    monkeypatch.setattr(acl, "server_has_fix", lambda sha, repo=None: False)
    with pytest.raises(RuntimeError, match="BUI-1384"):
        acl.apply_restore(result, "http://x", session=sess)
    assert sess.posts == []


def test_apply_posts_only_missing_issues(monkeypatch):
    s, i = _pf()
    v = {("The Power Fantasy", n): acl.IN for n in PF_ISSUES}
    v[("The Power Fantasy", "5")] = acl.MISSING
    result = _run(s, i, v)
    sess = _Session()
    monkeypatch.setattr(acl, "server_has_fix", lambda sha, repo=None: True)
    out = acl.apply_restore(result, "http://x", session=sess)
    assert out["posted"] == 1
    (url, body), = sess.posts
    assert url.endswith(acl.COMMIT)
    assert [w["identify_data"]["issue"] for w in body["wins"]] == ["5"]
    assert body["wins"][0]["item_id"] == PF


def test_apply_null_year_lot_skips_unconfirmed(monkeypatch):
    s = [_snipe("9", "Foo 1-2")]
    result = _run(s, [_lot_identity("Foo", ["1", "2"], year=None)],
                  {("Foo", "1"): acl.MISSING, ("Foo", "2"): acl.MISSING})
    monkeypatch.setattr(acl, "server_has_fix", lambda sha, repo=None: True)
    sess = _Session()
    era = lambda url, reqs: {r["item_id"]: r["issue"] == "1" for r in reqs}
    out = acl.apply_restore(result, "http://x", session=sess, fetch_era=era)
    assert [w["identify_data"]["issue"] for w in sess.posts[0][1]["wins"]] == ["1"]
    assert "not restored: 2" in out["notes"][0]


@pytest.mark.parametrize("sha", [None, "", "not-a-sha", "08148e8"])
def test_server_has_fix_rejects_bad_or_old_sha(sha):
    assert acl.server_has_fix(sha) is False


def test_server_has_fix_accepts_fixed_commit():
    # 30a3bc8 is the BUI-1384 commit itself; skip if history is shallow.
    import subprocess
    ok = subprocess.run(["git", "cat-file", "-e", "30a3bc8"], capture_output=True).returncode == 0
    if not ok:
        pytest.skip("BUI-1384 commit not in local history")
    assert acl.server_has_fix("30a3bc8") is True
