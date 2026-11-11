"""BUI-1282: PriceCharting CGC sales appended to the slab pool.

Every page here is SYNTHETIC (shaped like a firecrawl scrape of a real PC
product page, with made-up titles, ids, and prices). The scrape and the
`ebay-sold-comps --classify-titles` shell-out are mocked: no test touches the
network.
"""

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

import fmv_math
import fmv_runner
import pricecharting as pc

URL = "https://www.pricecharting.com/game/comic-books-x-men/the-x-men-96-1975"
LOCK = ("![](https://www.pricecharting.com/images/lock.gif)<br>Time Warp shows "
        "photos of completed sales.<br>[>Subscribe](https://www.pricecharting.com/"
        "pricecharting-pro?f=salesPhotos)<br>($6/month) to see photos.<br>"
        f"[OK]({URL}#)")
REPORT = f'[Report It]({URL}# "Click if inaccurate.  Tells admin to look at sale")'


def _row(day, title, item, prices, host="www.ebay.com/itm/"):
    cell = "<br>".join(f"${p:,.2f}" for p in prices)
    return (f"| {day} | {LOCK} | [{title}](https://{host}{item}?nordt=true)<br> <br> "
            f"\\[eBay\\] | {cell} |  | {REPORT} |")


def synthetic_page(h1="The X-Men \\#96 (1975)", rows=None):
    rows = rows if rows is not None else [
        _row("2026-09-20", "X-Men #96 CGC 9.4 White Pages 1975", "1000000001", [900.0]),
        # best offer: accepted $700, list $800
        _row("2026-09-10", "X-Men #96 CGC 9.4 1975", "1000000002", [700.0, 800.0]),
        _row("2026-09-05", "X-Men #96 VF 1975 Marvel", "1000000003", [95.0]),
        _row("2026-09-01", "X-Men #96 CGC 9.4 1975", "1000000001", [900.0]),  # dup id
        _row("2026-08-30", "X-Men 96 CGC 9.0 Heritage", "lot-77", [500.0],
             host="comics.ha.com/"),
    ]
    return ("Some nav\n\n"
            f"# {h1}   [X-Men](https://www.pricecharting.com/console/comic-books-x-men)\n\n"
            "| Sale Date | TW | Title | Price | | |\n| --- | --- | --- | --- | --- | --- |\n"
            + "\n".join(rows) + "\n")


def _verdict(title, issue="96"):
    """A stand-in for `ebay-sold-comps --classify-titles` on these titles."""
    import re
    g = re.search(r"CGC (\d+\.\d)", title)
    if not g:
        return {"grade": 8.0, "certifier": None, "label": None,
                "page_quality": None, "exclude_code": "not_graded"}
    return {"grade": float(g.group(1)), "certifier": "cgc", "label": "universal",
            "page_quality": "white" if "White" in title else "unknown",
            "exclude_code": None}


@pytest.fixture
def pc_on(monkeypatch):
    monkeypatch.setenv("FMV_PRICECHARTING", "1")
    monkeypatch.setattr(pc, "classify",
                        lambda rows, issue: [_verdict(r["title"]) for r in rows])


# ─── parse ──────────────────────────────────────────────────────────────────

def test_parse_rows_reads_graded_rows_prices_and_sources():
    rows = pc.parse_rows(synthetic_page())
    assert [r["product_id"] for r in rows] == ["1000000001", "1000000002",
                                               rows[2]["product_id"]]
    assert rows[0]["price"] == 900.0 and rows[0]["ask"] is None
    assert rows[1]["price"] == 700.0 and rows[1]["ask"] == 800.0
    assert rows[2]["source"] == "heritage" and rows[2]["product_id"].startswith("pc:")
    assert all(r["source"] == "ebay" for r in rows[:2])


def test_build_sales_marks_universal_and_issue_mismatch():
    rows = pc.parse_rows(synthetic_page(rows=[
        _row("2026-09-20", "X-Men 105 CGC 9.4 1975 #96", "1", [900.0]),
        _row("2026-09-21", "X-Men #96 CGC 9.4 1975", "2", [1200.0, 1500.0]),
    ]))
    sales = pc.build_sales(rows, [_verdict(r["title"]) for r in rows],
                           series="X-Men", issue="96")
    assert sales[0]["exclude_code"] == "issue_mismatch" and not sales[0]["universal"]
    assert sales[1]["universal"] and sales[1]["best_offer"] is True


# ─── title verification and slug resolution ─────────────────────────────────

def test_verify_title_accepts_the_book():
    title = pc.page_title(synthetic_page())
    assert title == "The X-Men #96 (1975) | console: X-Men"
    assert pc.verify_title(title, "X-Men", "96", 1975) is None


@pytest.mark.parametrize("h1,why", [
    ("The X-Men \\#97 (1975)", "issue:97"),
    ("The X-Men \\#96 (1991)", "year:1991"),
    ("X-Force \\#96 (1975)", "series:X-Force"),
    ("The X-Men \\#96 (1975) Facsimile Edition", "edition:"),
])
def test_verify_title_rejects_another_book(h1, why):
    title = pc.page_title(synthetic_page(h1=h1))
    assert pc.verify_title(title, "X-Men", "96", 1975).startswith(why)


def test_verify_title_rejects_a_page_with_no_h1():
    assert pc.verify_title(pc.page_title("no heading here"), "X-Men", "96", 1975) == "no_page_or_h1"


def test_candidate_urls_strip_the_article_and_try_the_prefixed_product():
    urls = pc.candidate_urls("The Amazing Spider-Man", "194", 1979)
    assert urls == [
        pc.BASE_URL + "comic-books-amazing-spider-man/amazing-spider-man-194-1979",
        pc.BASE_URL + "comic-books-amazing-spider-man/the-amazing-spider-man-194-1979"]
    assert pc.series_name("The Amazing Spider-Man (1963)") == "The Amazing Spider-Man"
    assert pc.candidate_urls("Captain America Annual", "8", 1986)[0].endswith(
        "comic-books-captain-america-annual/captain-america-annual-8-1986")


# ─── dedup ──────────────────────────────────────────────────────────────────

PC_SALE = {"product_id": "1000000002", "grade": 9.4, "sold_date": "2026-09-10",
           "price": 700.0, "ask": 800.0, "universal": True, "exclude_code": None,
           "title": "X-Men #96 CGC 9.4", "page_quality": "unknown"}


def test_same_sale_matches_the_best_offer_list_price_twin():
    ledger = {"product_id": "sc-1", "grade": 9.4, "price": 800.0,
              "sold_date": "2026-09-11T00:00:00Z"}
    assert pc.same_sale(PC_SALE, ledger)


def test_same_sale_matches_on_item_id_and_rejects_other_sales():
    assert pc.same_sale(PC_SALE, {"product_id": "1000000002", "grade": 9.0,
                                  "price": 1.0, "sold_date": None})
    base = {"product_id": "x", "grade": 9.4, "price": 700.0, "sold_date": "2026-09-10"}
    assert pc.same_sale(PC_SALE, base)
    assert not pc.same_sale(PC_SALE, {**base, "grade": 9.2})
    assert not pc.same_sale(PC_SALE, {**base, "sold_date": "2026-09-13"})
    assert not pc.same_sale(PC_SALE, {**base, "price": 760.0})


def test_append_rows_drops_ledger_twins_and_non_universal_rows():
    other = {**PC_SALE, "product_id": "9", "price": 950.0, "ask": None,
             "sold_date": "2026-08-01"}
    signed = {**other, "product_id": "10", "universal": False}
    pool = [{"product_id": "sc-1", "grade": 9.4, "price": 800.0,
             "sold_date": "2026-09-10"}]
    out = pc.append_rows([PC_SALE, other, signed], pool,
                         certifier="cgc", label="universal")
    assert [r["product_id"] for r in out] == ["9"]
    assert out[0]["provider"] == "pricecharting" and out[0]["best_offer"] is False
    assert pc.append_rows([other], [], certifier="cgc", label="signature_series") == []
    assert pc.append_rows([other], [], certifier="cbcs", label="universal") == []


# ─── refusal guard ──────────────────────────────────────────────────────────

REFUSED = {"fmv_low": None, "fmv_high": None, "flag_reason": "too_sparse"}


@pytest.mark.parametrize("base,appended,keep", [
    (REFUSED, {"fmv_low": 1, "fmv_high": 2, "pricing_basis": "ladder"}, False),
    (REFUSED, {"fmv_low": 1, "fmv_high": 2, "pricing_basis": "lone_sale"}, False),
    (REFUSED, {"fmv_low": 1, "fmv_high": 2, "pricing_basis": "direct"}, True),
    ({"fmv_low": 1, "fmv_high": 2}, {"fmv_low": 3, "fmv_high": 4, "pricing_basis": "ladder"}, True),
    ({"fmv_low": 1, "fmv_high": 2}, REFUSED, False),
])
def test_keep_appended_guard(base, appended, keep):
    assert pc.keep_appended(base, appended) is keep


# ─── fetch, cache, failures, kill switch ────────────────────────────────────

def test_kill_switch_never_scrapes(monkeypatch):
    monkeypatch.setenv("FMV_PRICECHARTING", "0")
    got = pc.fetch_book_sales("X-Men", "96", 1975)
    assert got["status"] == pc.DISABLED and got["sales"] == []


def test_no_year_means_no_pc_data(pc_on):
    assert pc.fetch_book_sales("X-Men", "96", None)["status"] == pc.NO_IDENTITY


def test_resolves_the_prefixed_slug_then_serves_the_cache(pc_on, monkeypatch, tmp_path):
    calls = []

    def fake_scrape(url):
        calls.append(url)
        if url.endswith("/x-men-96-1975"):
            return "# Search results\n"  # PC's search page: no matching H1
        return synthetic_page()

    monkeypatch.setattr(pc, "scrape", fake_scrape)
    got = pc.fetch_book_sales("X-Men", "96", 1975, now=1_000_000.0)
    assert got["status"] == pc.OK and got["url"] == URL and got["cache"] == "miss"
    assert len(got["sales"]) == 3 and sum(s["universal"] for s in got["sales"]) == 3
    assert len(calls) == 2
    cached = list((tmp_path / "pc-cache").glob("*.json"))
    assert len(cached) == 1

    again = pc.fetch_book_sales("X-Men", "96", 1975, now=1_000_000.0 + 6 * 86400)
    assert again["cache"] == "hit" and len(calls) == 2
    assert again["sales"] == got["sales"]

    stale = pc.fetch_book_sales("X-Men", "96", 1975, now=1_000_000.0 + 8 * 86400)
    assert stale["cache"] == "miss" and len(calls) == 4


def test_unresolved_book_is_negative_cached_for_a_day(pc_on, monkeypatch):
    calls = []
    monkeypatch.setattr(pc, "scrape", lambda url: calls.append(url) or "# Search\n")
    got = pc.fetch_book_sales("X-Men", "96", 1975, now=0.0)
    assert got["status"] == pc.UNRESOLVED and got["warning"] is None and len(calls) == 2
    pc.fetch_book_sales("X-Men", "96", 1975, now=3600.0)
    assert len(calls) == 2
    pc.fetch_book_sales("X-Men", "96", 1975, now=2 * 86400.0)
    assert len(calls) == 4


def test_scrape_failure_warns_falls_back_and_is_not_cached(pc_on, monkeypatch, tmp_path):
    calls = []

    def boom(url):
        calls.append(url)
        raise RuntimeError("firecrawl timed out after 120s")

    monkeypatch.setattr(pc, "scrape", boom)
    got = pc.fetch_book_sales("X-Men", "96", 1975)
    assert got["status"] == "scrape_failed" and got["sales"] == []
    assert "timed out" in got["warning"]
    assert not list((tmp_path / "pc-cache").glob("*.json"))
    # a second grade of the same book in the same run does not spend again
    pc.fetch_book_sales("X-Men", "96", 1975)
    assert len(calls) == 2


def test_missing_firecrawl_is_a_reported_failure(pc_on, monkeypatch):
    monkeypatch.undo()  # restore the real scrape; keep the env below
    monkeypatch.setenv("FMV_PRICECHARTING", "1")
    monkeypatch.setattr(pc, "_FAILED_THIS_PROCESS", {})
    monkeypatch.setattr(pc.shutil, "which", lambda name: None)
    got = pc.fetch_book_sales("X-Men", "96", 1975)
    assert got["status"] == "scrape_failed" and "not on PATH" in got["warning"]


def test_classify_failure_is_reported(pc_on, monkeypatch):
    monkeypatch.setattr(pc, "scrape", lambda url: synthetic_page())

    def bad(rows, issue):
        raise RuntimeError("ebay-sold-comps --classify-titles exited 2")

    monkeypatch.setattr(pc, "classify", bad)
    got = pc.fetch_book_sales("X-Men", "96", 1975)
    assert got["status"] == "classify_failed" and got["sales"] == []
    assert "exited 2" in got["warning"]


def test_classify_rejects_a_short_answer(monkeypatch):
    monkeypatch.setattr(pc.shutil, "which", lambda name: "/bin/true")
    proc = MagicMock(returncode=0, stdout="[]", stderr="")
    monkeypatch.setattr(pc.subprocess, "run", lambda *a, **k: proc)
    with pytest.raises(RuntimeError, match="0 results for 1 titles"):
        pc.classify([{"title": "X CGC 9.4"}], "96")


# ─── the graded path ────────────────────────────────────────────────────────

def _slab(price, grade, pid, day="2026-09-01"):
    return {"product_id": pid, "title": f"CGC {grade} comic {price}",
            "price": price, "grade": grade, "sold_date": day, "buying_format": "",
            "certifier": "cgc", "label": "universal", "page_quality": "unknown"}


def _pc_sales(*specs):
    return [{"product_id": pid, "title": f"X-Men #96 CGC {g}", "price": p,
             "ask": None, "grade": g, "sold_date": d, "page_quality": "unknown",
             "certifier": "cgc", "label": "universal", "universal": True,
             "exclude_code": None, "best_offer": False, "url": "", "source": "ebay"}
            for pid, g, p, d in specs]


DIRECT_PC = _pc_sales(("p1", 4.5, 1000.0, "2026-09-02"), ("p2", 4.5, 1050.0, "2026-09-03"),
                      ("p3", 4.5, 980.0, "2026-09-04"))


def test_apply_pricecharting_guard_keeps_a_ledger_refusal_refused():
    base = fmv_math.graded_fmv([], 4.5, certifier="cgc", label="universal",
                               as_of=date(2026, 9, 15))
    ladder_only = _pc_sales(("p0", 4.5, 700.0, "2026-09-01"),
                            ("p1", 4.0, 900.0, "2026-08-20"),
                            ("p2", 5.5, 1400.0, "2026-08-10"),
                            ("p3", 6.0, 1500.0, "2026-08-05"))
    # The PC rungs alone DO price it (a ladder), so the guard is what refuses.
    opened = fmv_math.graded_fmv(
        pc.append_rows(ladder_only, [], certifier="cgc", label="universal"), 4.5,
        certifier="cgc", label="universal", as_of=date(2026, 9, 15))
    assert opened["fmv_low"] is not None and opened["pricing_basis"] != "direct"
    with patch.object(pc, "fetch_book_sales",
                      return_value={"status": "ok", "sales": ladder_only, "warning": None}):
        fmv, info = fmv_runner._apply_pricecharting(
            base, [], {"title": "X-Men", "issue": "96", "year": 1975}, 4.5,
            certifier="cgc", label="universal", page_quality=None,
            as_of=date(2026, 9, 15))
    assert fmv is base and fmv["fmv_low"] is None and fmv["pc_slab_count"] == 0
    assert info["guard_kept_base"] is True


def test_apply_pricecharting_takes_a_direct_appended_price():
    base = fmv_math.graded_fmv([], 4.5, certifier="cgc", label="universal",
                               as_of=date(2026, 9, 15))
    with patch.object(pc, "fetch_book_sales",
                      return_value={"status": "ok", "sales": DIRECT_PC, "warning": None}):
        fmv, info = fmv_runner._apply_pricecharting(
            base, [], {"title": "X-Men", "issue": "96", "year": 1975}, 4.5,
            certifier="cgc", label="universal", page_quality=None,
            as_of=date(2026, 9, 15))
    assert fmv["pricing_basis"] == "direct" and fmv["pc_slab_count"] == 3
    assert not info["guard_kept_base"]


class TestGradedRunWithPriceCharting:
    def _run(self, tmp_path, pc_result):
        book = {"item_id": "1", "title": "X-Men", "issue": "96", "year": 1975,
                "grade": 4.5, "certifier": "cgc"}
        result = {"input": {"_req_id": 0, **book}, "comps": [],
                  "slab_comps": [_slab(700, 4.5, "e0"), _slab(900, 4.0, "e1"),
                                 _slab(1400, 5.5, "e2"), _slab(1500, 6.0, "e3")],
                  "queries_used": [{"tier": "base", "cached": False}]}
        (tmp_path / "b.json").write_text(json.dumps([book]))
        upsert = MagicMock(return_value={"comic_id": 42, "fmv_id": 7, "certifier": "cgc"})
        post_comps = MagicMock(return_value=True)
        with patch("fmv_runner._fetch_comps", return_value=[result]), \
             patch("fmv_runner._upsert_fmv", upsert), \
             patch("fmv_runner._post_comps", post_comps), \
             patch("fmv_runner._fetch_ledger_comps", return_value=[]), \
             patch("fmv_runner._fetch_first_party_outcomes", return_value=[]), \
             patch("fmv_runner._db_lookup_by_identity", return_value=[]), \
             patch("fmv_runner._fetch_active_asks", return_value=None), \
             patch("fmv_runner._probe_certifier_support", return_value=True), \
             patch("fmv_runner._graded_as_of", return_value=date(2026, 9, 15)), \
             patch.object(pc, "fetch_book_sales", return_value=pc_result):
            fmv_runner.run(batch_path=str(tmp_path / "b.json"),
                           out_path=str(tmp_path / "o.json"), max_age_days=7,
                           force=False, quiet=True, server_url="http://test-server:8080")
        return json.loads((tmp_path / "o.json").read_text())[0], upsert, post_comps

    def test_pc_rows_price_the_row_but_never_reach_the_server(self, tmp_path, capsys):
        row, upsert, post_comps = self._run(
            tmp_path, {"status": "ok", "sales": DIRECT_PC, "warning": None})
        assert row["fmv"]["pricing_basis"] == "direct"
        assert row["fmv"]["pc_slab_count"] == 3
        posted = json.dumps([c.args for c in post_comps.call_args_list], default=str)
        assert "p1" not in posted and "pricecharting" not in posted
        assert all(c["product_id"] not in ("p1", "p2", "p3") for c in row["slab_comps"])
        notes = fmv_runner._build_notes(upsert.call_args.args[2])
        assert "pc=3)" in notes and "pricecharting" not in notes
        assert "PriceCharting: 1 slab row(s) priced" in capsys.readouterr().err
        # BUI-928: render the stitched row through the real table, not --brief.
        fmv_runner._print_table([row])
        assert "pc 3" in capsys.readouterr().out

    def test_a_failed_scrape_prices_ledger_only_and_says_so(self, tmp_path, capsys):
        row, _, _ = self._run(tmp_path, {"status": "scrape_failed", "sales": [],
                                         "warning": "PriceCharting X-Men #96 (1975): "
                                                    "scrape_failed (timeout)"})
        assert row["fmv"]["pricing_basis"] == "ladder"
        assert row["fmv"]["pc_slab_count"] == 0
        err = capsys.readouterr().err
        assert "scrape_failed (timeout)" in err
        assert "1 FAILED" in err
        fmv_runner._print_table([row])


def test_volume_year_in_the_title_still_resolves(pc_on, monkeypatch):
    calls = []
    monkeypatch.setattr(pc, "scrape", lambda url: calls.append(url) or synthetic_page())
    got = pc.fetch_book_sales("The X-Men (1963)", "96", 1975)
    assert got["status"] == pc.OK
    assert calls[0].endswith("comic-books-x-men/x-men-96-1975")


def test_an_unexpected_error_prices_ledger_only_and_is_reported(capsys):
    base = fmv_math.graded_fmv([_slab(900, 4.5, "e1"), _slab(950, 4.5, "e2")], 4.5,
                               certifier="cgc", label="universal",
                               as_of=date(2026, 9, 15))
    with patch.object(pc, "fetch_book_sales", side_effect=OSError("disk full")):
        fmv, info = fmv_runner._apply_pricecharting(
            base, [], {"title": "X-Men", "issue": "96", "year": 1975}, 4.5,
            certifier="cgc", label="universal", page_quality=None,
            as_of=date(2026, 9, 15))
    assert fmv is base and info["status"] == "error"
    assert "error" in pc.FAILED_STATUSES
    assert "disk full" in capsys.readouterr().err
