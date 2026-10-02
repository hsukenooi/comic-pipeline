"""BUI-1080: `comic-fmv --unpriced-rerun`, HTTP and the pricing run faked."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import fmv_cli
import fmv_runner

SERVER = "http://comics.test"


def _row(cid, title, issue, *, updated="2026-01-01T00:00:00+00:00",
         certifier="none", grade=8.0, high=None, flag="too_sparse", cap=None,
         fmv_id=None):
    return {"id": cid, "title": title, "issue": issue, "year": 1990,
            "locg_id": None, "locg_variant_id": None, "variant": None,
            "fmv_id": fmv_id if fmv_id is not None else cid * 10,
            "grade": grade, "fmv_high": high, "fmv_flag_reason": flag,
            "fmv_ceiling_cap": cap, "fmv_updated_at": updated,
            "certifier": certifier, "label": "universal"}


def _selection(by_certifier):
    def fake_get(url, *, params, warn, default, **kw):
        assert url == f"{SERVER}/api/comics"
        return by_certifier.get(params.get("certifier", "none"), [])
    return fake_get


def _fresh(row_fmv_id=1, *, fmv=None, **extra):
    res = {"source": "fresh", "fmv_id": row_fmv_id, "comps_posted": True,
           "queries_used": [{"tier": "base"}], "breaker_tripped": False,
           "comp_count_total": 5,
           "fmv": fmv or {"fmv_high": 120.0, "flag_reason": None}}
    res.update(extra)
    return res


def _go(by_certifier, run_results, *, max_requests=None):
    """Run the driver; returns (code, run mock, post mock)."""
    if isinstance(run_results, BaseException):
        run = MagicMock(side_effect=run_results)
    else:
        run = MagicMock(return_value=run_results)
    with patch("fmv_runner._get_json_or_warn", _selection(by_certifier)), \
         patch("fmv_runner.run", run), \
         patch("fmv_runner.requests.post") as post:
        code = fmv_runner.run_unpriced_rerun(
            server_url=SERVER, max_requests=max_requests)
    return code, run, post


def _urls(post):
    return [c.args[0] for c in post.call_args_list]


class TestSelection:
    def test_oldest_first_and_cap(self, capsys):
        rows = [_row(1, "New", "1", updated="2026-09-01T00:00:00+00:00"),
                _row(2, "Old", "1", updated="2026-01-01T00:00:00+00:00"),
                _row(3, "Mid", "1", updated="2026-05-01T00:00:00+00:00")]
        code, run, post = _go({"none": rows},
                              [_fresh(), _fresh()], max_requests=2)
        assert code == 0
        sent = run.call_args.kwargs["books"]
        assert [b["title"] for b in sent] == ["Old", "Mid"]
        assert run.call_args.kwargs["force"] is False
        assert run.call_args.kwargs["max_age_days"] == 0.0
        assert "cap (2) reached; 1 row(s) left" in capsys.readouterr().err
        # a capped run that attempted everything cleanly still pings
        assert _urls(post) == [f"{SERVER}/api/heartbeat/unpriced-rerun"]

    def test_raw_and_graded_selected_priced_and_norow_dropped(self):
        by = {
            "none": [_row(1, "Raw", "1"),
                     _row(2, "Priced", "1", high=50.0, flag=None),
                     {**_row(3, "NoRow", "1"), "fmv_id": None, "grade": None}],
            "cgc": [_row(4, "Slab", "1", certifier="cgc", grade=9.6)],
        }
        code, run, _ = _go(by, [_fresh(), _fresh()])
        books = run.call_args.kwargs["books"]
        assert sorted(b["title"] for b in books) == ["Raw", "Slab"]
        slab = next(b for b in books if b["title"] == "Slab")
        assert slab["certifier"] == "cgc" and slab["label"] == "universal"
        assert "certifier" not in next(b for b in books if b["title"] == "Raw")

    def test_malformed_identity_skipped_and_counted(self, capsys):
        by = {"none": [_row(1, "", "1"), _row(2, "Ok", "")] +
              [_row(3, "Fine", "2")]}
        code, run, _ = _go(by, [_fresh()])
        assert [b["title"] for b in run.call_args.kwargs["books"]] == ["Fine"]
        assert "2 malformed skipped" in capsys.readouterr().out

    def test_old_server_ignoring_certifier_refuses_to_start(self):
        by = {"cgc": [_row(1, "Raw twin", "1", certifier="none")]}
        code, run, post = _go(by, [])
        assert code == 2
        run.assert_not_called()

    def test_selection_read_failure_returns_2_with_failure_ping(self):
        run = MagicMock()
        with patch("fmv_runner._get_json_or_warn",
                   return_value=fmv_runner._LOOKUP_FAILED), \
             patch("fmv_runner.run", run), \
             patch("fmv_runner.requests.post") as post:
            code = fmv_runner.run_unpriced_rerun(server_url=SERVER)
        assert code == 2
        run.assert_not_called()
        assert _urls(post) == [f"{SERVER}/api/heartbeat/unpriced-rerun/failure"]

    def test_no_server_url(self):
        with patch("fmv_runner.requests.post") as post:
            assert fmv_runner.run_unpriced_rerun(server_url=None) == 2
        post.assert_not_called()

    def test_empty_set_is_a_success(self):
        code, run, post = _go({"none": []}, [])
        assert code == 0
        run.assert_not_called()
        assert _urls(post) == [f"{SERVER}/api/heartbeat/unpriced-rerun"]


class TestCountsAndHeartbeat:
    def test_before_after_counts_and_capped(self, capsys):
        by = {"none": [
            _row(1, "A", "1", flag="too_sparse"),
            _row(2, "B", "1", flag="too_wide"),
            _row(3, "C", "1", flag="too_wide", cap=40),
            _row(4, "D", "1", flag="one_sided")]}
        results = [
            _fresh(fmv={"fmv_high": 90.0, "flag_reason": None}),
            _fresh(fmv={"fmv_high": None, "flag_reason": "too_wide",
                        "pricing_basis": "ceiling", "ceiling_cap": 35}),
            _fresh(fmv={"fmv_high": None, "flag_reason": "too_wide",
                        "pricing_basis": "ceiling", "ceiling_cap": 40}),
            _fresh(fmv={"fmv_high": None, "flag_reason": "one_sided"}),
        ]
        code, _, post = _go(by, results)
        out = capsys.readouterr().out
        assert code == 0
        assert ("BEFORE 4 unpriced row(s): capped=1, flagged:one_sided=1, "
                "flagged:too_sparse=1, flagged:too_wide=1") in out
        assert ("AFTER 3 unpriced row(s): capped=2, flagged:one_sided=1"
                ) in out
        assert "1 newly priced" in out

    def test_hand_priced_skip_is_not_a_failure(self, capsys):
        by = {"none": [_row(1, "Hand", "1")]}
        hand = {"source": "skipped_hand_priced", "fmv": {"fmv_high": None}}
        code, _, post = _go(by, [hand])
        assert code == 0
        assert "1 hand-priced skipped" in capsys.readouterr().out
        assert _urls(post) == [f"{SERVER}/api/heartbeat/unpriced-rerun"]

    @pytest.mark.parametrize("bad", [
        {"source": "error"},
        {"source": "skipped_lookup_error"},
        {"source": "skipped_rejected"},
        {"source": "fresh", "fmv_id": None},
        {"source": "fresh", "comps_posted": False},
        {"source": "fresh", "fetch_error": True},
        {"source": "fresh", "breaker_tripped": True},
    ])
    def test_any_failure_sends_failure_ping_only(self, bad):
        by = {"none": [_row(1, "A", "1"), _row(2, "B", "1")]}
        res = [_fresh(), {**_fresh(), **bad}]
        code, _, post = _go(by, res)
        assert code == 1
        assert _urls(post) == [f"{SERVER}/api/heartbeat/unpriced-rerun/failure"]

    def test_aborted_pricing_run_is_a_failure(self):
        by = {"none": [_row(1, "A", "1")]}
        code, _, post = _go(by, SystemExit(1))
        assert code == 1
        assert _urls(post) == [f"{SERVER}/api/heartbeat/unpriced-rerun/failure"]

    def test_failed_ping_never_changes_exit_code(self):
        import requests
        by = {"none": [_row(1, "A", "1")]}
        with patch("fmv_runner._get_json_or_warn", _selection(by)), \
             patch("fmv_runner.run", return_value=[_fresh()]), \
             patch("fmv_runner.requests.post",
                   side_effect=requests.ConnectionError("down")):
            assert fmv_runner.run_unpriced_rerun(server_url=SERVER) == 0


class TestHandPricedGuardThroughRealRun:
    def test_hand_priced_row_is_bucketed_skipped_hand_and_never_fetched(self):
        hand_row = {"fmv_provenance": "hand", "fmv_notes": "hand §",
                    "fmv_low": None, "fmv_high": None}
        book = {"title": "Hand Book", "issue": "1", "grade": 8.0}
        fetch = MagicMock()
        with patch("fmv_runner._hand_price_candidates",
                   return_value=[hand_row]), \
             patch("fmv_runner._fetch_comps", fetch), \
             patch("fmv_runner.requests.post"):
            final = fmv_runner.run(
                batch_path=None, out_path=None, max_age_days=0.0,
                force=False, quiet=True, server_url=SERVER, books=[book])
        assert final[0]["source"] == "skipped_hand_priced"
        fetch.assert_not_called()


class TestCli:
    def test_option_dispatches_and_exits_with_code(self):
        with patch("fmv_runner.run_unpriced_rerun", return_value=1) as drv:
            res = CliRunner().invoke(
                fmv_cli.cli, ["--unpriced-rerun", "--server-url", SERVER])
        assert res.exit_code == 1
        drv.assert_called_once_with(server_url=SERVER)

    @pytest.mark.parametrize("other", ["--probe", "--sentinel-probe",
                                       "--slab-watch-collect"])
    def test_mutually_exclusive(self, other):
        res = CliRunner().invoke(
            fmv_cli.cli, ["--unpriced-rerun", other, "--server-url", SERVER])
        assert res.exit_code == 2


class TestMaxRequests:
    def test_default_env_and_bad_env(self, monkeypatch, capsys):
        monkeypatch.delenv("UNPRICED_RERUN_MAX_REQUESTS", raising=False)
        assert (fmv_runner._unpriced_rerun_max_requests(None)
                == fmv_runner._UNPRICED_RERUN_DEFAULT_MAX_REQUESTS)
        monkeypatch.setenv("UNPRICED_RERUN_MAX_REQUESTS", "5")
        assert fmv_runner._unpriced_rerun_max_requests(None) == 5
        assert fmv_runner._unpriced_rerun_max_requests(9) == 9
        monkeypatch.setenv("UNPRICED_RERUN_MAX_REQUESTS", "x")
        assert (fmv_runner._unpriced_rerun_max_requests(None)
                == fmv_runner._UNPRICED_RERUN_DEFAULT_MAX_REQUESTS)
        assert "does not parse" in capsys.readouterr().err
