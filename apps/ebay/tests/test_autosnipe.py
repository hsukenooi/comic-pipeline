"""comic-autosnipe (BUI-1340): every gate skips, and only clean books are sniped."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import autosnipe
from autosnipe import Autosnipe, consensus, format_message, load_config

NOW = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)


def iso(hours):
    return (NOW + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def listing(iid, title, *, price=10.0, grade="VF 8.0", hours=48, **extra):
    return {"item_id": iid, "title": title, "listing_type": "Auction",
            "current_price": price, "end_date_iso": iso(hours), "grade": grade,
            "grade_source": "title" if grade else "missing", "condition_defects": [],
            "seller": "s1", "cover_year": 1985, "item_specifics": {"Publisher": "Marvel"},
            **extra}


class Fake:
    """Stands in for every console script; records each call."""

    def __init__(self, watch, listings, *, snipes=(), collection=None, fmv=None,
                 seats=None, add_status="added", fail=None):
        self.watch, self.listings = watch, listings
        self.snipes, self.collection = list(snipes), collection or {}
        self.fmv, self.seats = fmv or {}, seats or {}
        self.add_status, self.fail = add_status, fail or {}
        self.calls = []

    def fetch(self, ids):
        return {i: self.listings[i] for i in ids if i in self.listings}

    def __call__(self, argv, cwd=None, env=None, timeout=None):
        self.calls.append(argv)
        key = " ".join(argv[:2])
        if key in self.fail:
            return 1, "", self.fail[key]
        if argv[:2] == ["ebay-watchlist", "--type"]:
            return 0, json.dumps(self.watch), ""
        if argv[:2] == ["ebay-watchlist", "--record"]:
            return 0, "", ""
        if argv[:2] == ["gixen", "list"]:
            return 0, json.dumps([{"item_id": s} for s in self.snipes]), ""
        if argv[:3] == ["locg", "collection", "check-batch"]:
            items = json.loads(Path(argv[3]).read_text())["items"]
            res = [{"series": i["series"], "issue": i["issue"],
                    "match_status": "not_in_cache", "verdict": "❌ Not in collection",
                    "flags": [], **self.collection.get(i["issue"], {})} for i in items]
            return 0, json.dumps({"results": res}), ""
        if argv[0] == "grade-photos":
            wd = Path(argv[argv.index("--workdir") + 1])
            for n, iid in enumerate(argv[1:argv.index("--workdir")], 1):
                d = wd / f"comic-{n}"
                d.mkdir(parents=True, exist_ok=True)
                (d / "img-01.jpg").write_bytes(b"x")
            return 0, "", ""
        if argv[0] == "grade-seats":
            spec = json.loads(Path(argv[1]).read_text())
            books = [dict(self.seats[b["item_id"]], item_id=b["item_id"]) for b in spec["books"]]
            return 0, json.dumps({"books": books}), ""
        if argv[0] == "comic-fmv":
            rows = json.loads(Path(argv[2]).read_text())
            out = []
            for r in rows:
                base = {"item_id": r["item_id"], "comic_id": 1, "fmv_id": 2, "max_bid": 40,
                        "fmv_low": 40, "fmv_high": 50, "flag_reason": None, "source": "computed",
                        "_in": r}
                base.update(self.fmv.get(r["item_id"], {}))
                out.append(json.dumps(base))
            return 0, "table\n" + "\n".join(out), ""
        if argv[:2] == ["gixen", "build-batch"]:
            Path(argv[argv.index("--out") + 1]).write_text("[]")
            self.working_list = json.loads(Path(argv[3]).read_text())
            return 0, "", ""
        if argv[:2] == ["gixen", "add-batch"]:
            rows = [{"item_id": w["item_id"], "status": self.add_status} for w in self.working_list]
            Path(argv[argv.index("--json-out") + 1]).write_text(json.dumps({"rows": rows}))
            return 0 if self.add_status == "added" else 1, "", ""
        raise AssertionError(f"unexpected call {argv}")


def make(fake, tmp_path, **cfg):
    cfg.setdefault("AUTOSNIPE_GRADE_CACHE", tmp_path / "grades.json")
    conf = dict(autosnipe.DEFAULTS, **{k: str(v) for k, v in cfg.items()})
    return Autosnipe(conf, run=fake, fetch=fake.fetch, now=NOW, workdir=tmp_path)


def watch(*ids, hours=48):
    return [{"item_id": i, "title": i, "end_time": iso(hours), "url": f"u/{i}"} for i in ids]


def by_id(s):
    return {b.item_id: b for b in s.books}


def test_clean_book_is_sniped_and_recorded(tmp_path):
    fake = Fake(watch("1"), {"1": listing("1", "Amazing Spider-Man #300 (1988)")})
    s = make(fake, tmp_path)
    s.execute()
    assert [b.item_id for b in s.sniped()] == ["1"]
    assert ["ebay-watchlist", "--record", "1", "--outcome", "sniped"] in fake.calls
    assert fake.working_list[0]["seller_grade"] == "VF 8.0"


def test_dry_run_adds_and_records_nothing(tmp_path):
    fake = Fake(watch("1"), {"1": listing("1", "Amazing Spider-Man #300 (1988)")})
    s = make(fake, tmp_path, AUTOSNIPE_DRY_RUN=1)
    s.execute()
    assert [b.item_id for b in s.sniped()] == ["1"]
    assert not any(c[:2] in (["gixen", "add-batch"], ["ebay-watchlist", "--record"])
                   for c in fake.calls)
    assert "dry run" in format_message(s)


def test_already_sniped_and_ended_items_are_dropped(tmp_path):
    w = watch("1") + watch("2", hours=-1)
    fake = Fake(w, {}, snipes=["1"])
    s = make(fake, tmp_path)
    s.execute()
    assert s.books == []


def test_timing_skip_within_min_hours(tmp_path):
    fake = Fake(watch("1", hours=3), {})
    s = make(fake, tmp_path)
    s.execute()
    assert by_id(s)["1"].skip.startswith("Timing")


@pytest.mark.parametrize("title,extra,reason", [
    ("Spider-Man comic lot of 10", {}, "Identify – lot"),
    ("Batman comics", {}, "Identify – series or issue"),
    ("Batman #227 (1970)", {"listing_type": "BIN"}, "Identify – Buy It Now"),
    ("Batman #227 (1970)", {"condition_defects": [{"code": "rust", "phrase": "rusty staple"}]},
     "Grade – seller notes rust"),
])
def test_identify_and_defect_gates(tmp_path, title, extra, reason):
    fake = Fake(watch("1"), {"1": listing("1", title, **extra)})
    s = make(fake, tmp_path)
    s.execute()
    assert by_id(s)["1"].skip.startswith(reason)


def test_owned_and_flagged_books_are_skipped(tmp_path):
    fake = Fake(watch("1", "2"),
                {"1": listing("1", "X-Men #141 (1981)"), "2": listing("2", "X-Men #142 (1981)")},
                collection={"141": {"match_status": "in_collection"},
                            "142": {"flags": ["Pattern A"]}})
    s = make(fake, tmp_path)
    s.execute()
    b = by_id(s)
    assert b["1"].skip == "Collection – already owned" and b["1"].record == "skipped"
    assert b["2"].skip.startswith("Collection – check flagged")


def test_unreachable_collection_check_stops_the_run(tmp_path):
    fake = Fake(watch("1"), {"1": listing("1", "X-Men #141 (1981)")},
                fail={"locg collection": "server unreachable"})
    s = make(fake, tmp_path)
    with pytest.raises(autosnipe.RunError, match="Collection check"):
        s.execute()
    assert not any(c[:2] == ["gixen", "add-batch"] for c in fake.calls)


def test_failed_watchlist_read_is_never_an_empty_list(tmp_path):
    fake = Fake([], {}, fail={"ebay-watchlist --type": "auth expired"})
    with pytest.raises(autosnipe.RunError, match="ebay-watchlist"):
        make(fake, tmp_path).execute()


def test_duplicate_listing_keeps_first_ending(tmp_path):
    w = watch("1", hours=72) + watch("2", hours=24)
    fake = Fake(w, {"1": listing("1", "X-Men #141 (1981)", hours=72),
                    "2": listing("2", "X-Men #141 (1981)", hours=24)})
    s = make(fake, tmp_path)
    s.execute()
    assert by_id(s)["1"].skip.startswith("Duplicate") and not by_id(s)["2"].skip


def test_ungraded_book_is_photo_graded_and_priced_at_photo_grade(tmp_path):
    seats = {"1": {"seats": [], "policy": {"consensus": {"grade": 6.0, "confidence": "MEDIUM"}}}}
    fake = Fake(watch("1"), {"1": listing("1", "X-Men #141 (1981)", grade=None)}, seats=seats)
    s = make(fake, tmp_path)
    s.execute()
    b = by_id(s)["1"]
    assert not b.skip and b.grade == 6.0 and b.grade_confidence == "medium"
    fmv_in = json.loads((tmp_path / "fmv_in.json").read_text())[0]
    assert fmv_in["grade"] == 6.0 and fmv_in["grade_confidence"] == "medium"


def test_stated_grade_over_100_is_graded_anyway(tmp_path):
    seats = {"1": {"seats": [], "policy": {"consensus": {"grade": 7.0, "confidence": "LOW"}}}}
    fake = Fake(watch("1"), {"1": listing("1", "X-Men #141 (1981)", price=150)}, seats=seats)
    s = make(fake, tmp_path)
    s.execute()
    assert by_id(s)["1"].skip.startswith("Grade – photo grade 7.0 has low confidence")


@pytest.mark.parametrize("fmv,reason", [
    ({"flag_reason": "too_wide", "max_bid": None}, "FMV – needs manual"),
    ({"source": "ledger-advisory", "max_bid": None, "comic_id": None}, "FMV – live comps"),
    ({"source": "skipped_lookup_error", "max_bid": None}, "FMV – lookup failed"),
    ({"max_bid": 5}, "Price –"),
])
def test_fmv_and_price_gates(tmp_path, fmv, reason):
    fake = Fake(watch("1"), {"1": listing("1", "X-Men #141 (1981)")}, fmv={"1": fmv})
    s = make(fake, tmp_path)
    s.execute()
    assert by_id(s)["1"].skip.startswith(reason)


def test_spend_cap_keeps_soonest_ending(tmp_path):
    w = watch("1", hours=24) + watch("2", hours=48)
    fake = Fake(w, {"1": listing("1", "X-Men #141 (1981)", hours=24),
                    "2": listing("2", "X-Men #142 (1981)", hours=48)})
    s = make(fake, tmp_path, AUTOSNIPE_SPEND_CAP=50)
    s.execute()
    assert not by_id(s)["1"].skip and by_id(s)["2"].skip.startswith("Spend cap")


def test_failed_add_is_reported_and_not_recorded(tmp_path):
    fake = Fake(watch("1"), {"1": listing("1", "X-Men #141 (1981)")}, add_status="failed")
    s = make(fake, tmp_path)
    s.execute()
    assert s.failed_adds() and by_id(s)["1"].skip == "Snipe add – failed"
    assert not any(c[:2] == ["ebay-watchlist", "--record"] for c in fake.calls)


def test_consensus_rules():
    assert consensus(None) == "grading did not return a result"
    assert consensus({"adjudicator": {"grade": 5.5, "confidence": "Medium"}}) == (5.5, "MEDIUM")
    assert consensus({"split": 1.5, "adjudicator_failed": True}).startswith("graders split")
    seats = [{"grade": 6.0, "confidence": "HIGH"}, {"grade": 5.0, "confidence": "MEDIUM-LOW"},
             {"grade": 6.5, "confidence": "MEDIUM"}]
    assert consensus({"seats": seats}) == (6.0, "MEDIUM-LOW")
    assert consensus({"seats": [{"failed": True}, seats[0]]}).startswith("too few")


def test_message_escapes_and_lists_reasons(tmp_path):
    fake = Fake(watch("1", "2", hours=48), {
        "1": listing("1", "X-Men #141 (1981)"),
        "2": listing("2", "Tom & Jerry <b> lot of 5 comics")})
    s = make(fake, tmp_path)
    s.execute()
    msg = format_message(s)
    assert "Sniped (1)" in msg and "max bid $40" in msg and "FMV $40–$50" in msg
    assert "Tom &amp; Jerry &lt;b&gt;" in msg and "Identify – lot" in msg


def test_config_file_and_env_precedence(tmp_path):
    p = tmp_path / "env"
    p.write_text("# comment\nAUTOSNIPE_DRY_RUN=1\nAUTOSNIPE_SPEND_CAP=200\n")
    cfg = load_config(p, environ={"AUTOSNIPE_SPEND_CAP": "75"})
    assert cfg["AUTOSNIPE_DRY_RUN"] == "1" and cfg["AUTOSNIPE_SPEND_CAP"] == "75"


def test_main_reports_a_failed_run(tmp_path, monkeypatch):
    def boom(self):
        raise autosnipe.RunError("gixen list failed (exit 1): down")
    monkeypatch.setattr(Autosnipe, "execute", boom)
    out = tmp_path / "msg.txt"
    rc = autosnipe.main(["--config", str(tmp_path / "none"), "--message-out", str(out),
                         "--json-out", str(tmp_path / "s.json")])
    assert rc == 1 and "Run failed: gixen list failed" in out.read_text()
    assert json.loads((tmp_path / "s.json").read_text())["error"].startswith("gixen list")


GOOD = {"seats": [], "policy": {"consensus": {"grade": 6.0, "confidence": "MEDIUM"}}}


def ungraded(*ids, hours=48):
    return {i: listing(i, f"X-Men #{140 + n} (1981)", grade=None, hours=hours)
            for n, i in enumerate(ids, 1)}


def grade_calls(fake):
    return [c for c in fake.calls if c[0] in ("grade-photos", "grade-seats")]


def test_grade_is_cached_and_reused_next_night(tmp_path):
    cache = tmp_path / "grades.json"
    (tmp_path / "n1").mkdir()
    (tmp_path / "n2").mkdir()
    first = Fake(watch("1"), ungraded("1"), seats={"1": GOOD})
    s1 = make(first, tmp_path / "n1", AUTOSNIPE_GRADE_CACHE=cache)
    s1.execute()
    assert grade_calls(first) and not by_id(s1)["1"].grade_cached
    second = Fake(watch("1"), ungraded("1"))
    s2 = make(second, tmp_path / "n2", AUTOSNIPE_DRY_RUN=1, AUTOSNIPE_GRADE_CACHE=cache)
    s2.execute()
    b = by_id(s2)["1"]
    assert grade_calls(second) == [] and b.grade_cached and not b.skip
    assert b.grade == 6.0 and b.grade_confidence == "medium"
    assert "photo, medium, cached" in format_message(s2)
    assert s2.summary()["books"][0]["grade_cached"] is True


@pytest.mark.parametrize("seat,cached", [
    ({"split": True}, True),
    ({"seats": [{"grade": 5.0, "confidence": "HIGH"}]}, False),
])
def test_only_real_verdicts_are_cached(tmp_path, seat, cached):
    s = make(Fake(watch("1"), ungraded("1"), seats={"1": seat}), tmp_path)
    s.execute()
    assert by_id(s)["1"].skip.startswith("Grade")
    assert ("1" in json.loads((tmp_path / "grades.json").read_text())) is cached


def test_expired_cache_entries_are_pruned(tmp_path):
    (tmp_path / "grades.json").write_text(json.dumps(
        {"old": {"grade": 5.0, "confidence": "HIGH", "end": iso(-1)}}))
    s = make(Fake(watch("1"), ungraded("1"), seats={"1": GOOD}), tmp_path)
    s.execute()
    assert set(json.loads((tmp_path / "grades.json").read_text())) == {"1"}


def test_unreadable_cache_grades_again(tmp_path):
    (tmp_path / "grades.json").write_text("{not json")
    fake = Fake(watch("1"), ungraded("1"), seats={"1": GOOD})
    s = make(fake, tmp_path)
    s.execute()
    assert grade_calls(fake) and not by_id(s)["1"].skip
    assert any("grade cache unreadable" in n for n in s.notes)


def test_grading_cap_grades_soonest_ending_first(tmp_path):
    w = watch("1", hours=72) + watch("2", hours=24)
    books = {**ungraded("1", hours=72), **{"2": listing("2", "X-Men #150 (1981)", grade=None, hours=24)}}
    (tmp_path / "grades.json").write_text(json.dumps(
        {"3": {"grade": 7.0, "confidence": "HIGH", "end": iso(96)}}))
    w += watch("3", hours=96)
    books["3"] = listing("3", "X-Men #160 (1981)", grade=None, hours=96)
    fake = Fake(w, books, seats={"2": GOOD})
    s = make(fake, tmp_path, AUTOSNIPE_GRADE_MAX=1)
    s.execute()
    b = by_id(s)
    assert b["1"].skip == "Grade – over the 1-book grading cap for one run"
    assert not b["2"].skip and not b["3"].skip and b["3"].grade_cached
    assert "1" not in json.loads((tmp_path / "grades.json").read_text())
