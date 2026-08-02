"""Tests for sentinel_probe.py (BUI-603).

Every test here mocks subprocess.run (never spawns the real ebay-sold-comps
binary) and requests.post (never makes a real HTTP call) — zero network,
zero provider spend, matching the pattern test_fmv_runner.py already uses
for the same reason.
"""

import json

import pytest
import requests

import sentinel_probe


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_state_dir(monkeypatch, tmp_path):
    """Redirect the baseline file to a per-test tmp path — without this, a
    test that reaches a healthy read would write to the real
    ~/.cache/comic-fmv-sentinel/baseline.json."""
    monkeypatch.setenv("COMIC_FMV_SENTINEL_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def _no_real_heartbeat_post(monkeypatch):
    """Belt-and-suspenders: fail loud if a test forgets to mock requests.post
    and something tries a real network call."""
    def _boom(*a, **k):
        raise AssertionError("requests.post was called without being mocked")
    monkeypatch.setattr(sentinel_probe.requests, "post", _boom)


def _comp(price, grade=None):
    return {"product_id": "p", "title": "t", "price": price, "grade": grade,
            "sold_date": "", "buying_format": "", "link": ""}


def _result(req_id, comps=None, error=None):
    r = {"input": {"_req_id": req_id}, "comps": comps or [], "queries_used": [],
         "breaker_tripped": False}
    if error is not None:
        r["error"] = error
    return r


def _healthy_comps(target_grade, n=15):
    """A comp pool that safely clears SENTINEL_MIN_N and prices cleanly at
    target_grade with no gradeable-comp gap."""
    comps = [_comp(100.0, grade=target_grade) for _ in range(5)]
    comps += [_comp(3.0 + i, grade=None) for i in range(n - 5)]  # ungraded noise
    return comps


def _wire_subprocess(monkeypatch, results_by_req_id):
    """Patch shutil.which + subprocess.run so _fetch_batch() succeeds and
    returns exactly `results_by_req_id.values()` for whatever payload the
    real code sends (the fake ignores the request payload's own content and
    just answers by whatever req_ids it was asked for, mirroring how the
    real ebay-sold-comps binary echoes _req_id back)."""
    monkeypatch.setattr(sentinel_probe.shutil, "which", lambda _b: "/usr/bin/ebay")

    def fake_run(cmd, capture_output, text, timeout=None):
        in_path = cmd[cmd.index("--batch") + 1]
        out_path = cmd[cmd.index("--out") + 1]
        with open(in_path) as fh:
            payload = json.load(fh)
        out = [results_by_req_id[b["_req_id"]] for b in payload]
        with open(out_path, "w") as fh:
            json.dump(out, fh)
        return type("R", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(sentinel_probe.subprocess, "run", fake_run)


def _all_healthy_results():
    """A full, healthy 4-entry result set (3 sentinels + negative control)."""
    results = {}
    for i, book in enumerate(sentinel_probe.SENTINEL_BOOKS):
        results[i] = _result(i, comps=_healthy_comps(book["target_grade"]))
    results[len(sentinel_probe.SENTINEL_BOOKS)] = _result(
        len(sentinel_probe.SENTINEL_BOOKS), comps=[])
    return results


# ─── Happy path ─────────────────────────────────────────────────────────────

class TestHealthyRun:
    def test_all_pass_returns_zero_and_pings_heartbeat(self, monkeypatch):
        _wire_subprocess(monkeypatch, _all_healthy_results())
        pinged = []
        monkeypatch.setattr(
            sentinel_probe.requests, "post",
            lambda url, timeout=None: pinged.append(url) or type(
                "R", (), {"status_code": 200, "raise_for_status": lambda self: None})())

        code = sentinel_probe.run_sentinel_probe(server_url="http://x")

        assert code == 0
        assert pinged == ["http://x/api/heartbeat/sentinel-probe"]

    def test_first_run_seeds_baseline_without_failing(self, monkeypatch):
        """No prior baseline file exists — the price-jump check must not fire
        (nothing to compare against yet), and the run must still pass."""
        _wire_subprocess(monkeypatch, _all_healthy_results())
        monkeypatch.setattr(sentinel_probe.requests, "post",
                            lambda *a, **k: type(
                                "R", (), {"status_code": 200,
                                          "raise_for_status": lambda self: None})())

        assert not sentinel_probe._baseline_path().exists()
        code = sentinel_probe.run_sentinel_probe(server_url=None)
        assert code == 0
        assert sentinel_probe._baseline_path().exists()
        saved = json.loads(sentinel_probe._baseline_path().read_text())
        assert len(saved) == len(sentinel_probe.SENTINEL_BOOKS)

    def test_no_server_url_skips_heartbeat_but_still_passes(self, monkeypatch):
        _wire_subprocess(monkeypatch, _all_healthy_results())
        code = sentinel_probe.run_sentinel_probe(server_url=None)
        assert code == 0  # requests.post's autouse guard would raise if called


# ─── Sentinel failure modes ────────────────────────────────────────────────

class TestSentinelFailures:
    def test_zero_comps_alerts_and_exits_one(self, monkeypatch):
        results = _all_healthy_results()
        results[0] = _result(0, comps=[])  # first sentinel: n=0
        _wire_subprocess(monkeypatch, results)

        code = sentinel_probe.run_sentinel_probe(server_url=None)
        assert code == 1

    def test_fetch_error_alerts_and_exits_one(self, monkeypatch):
        results = _all_healthy_results()
        results[0] = _result(0, comps=[], error="boom")
        _wire_subprocess(monkeypatch, results)

        code = sentinel_probe.run_sentinel_probe(server_url=None)
        assert code == 1

    def test_below_floor_alerts_even_with_nonzero_n(self, monkeypatch):
        """n > 0 but below SENTINEL_MIN_N must still alert — n=0 is not the
        only degraded-instrument signal."""
        results = _all_healthy_results()
        book = sentinel_probe.SENTINEL_BOOKS[0]
        thin = [_comp(100.0, grade=book["target_grade"])
                for _ in range(sentinel_probe.SENTINEL_MIN_N - 1)]
        assert 0 < len(thin) < sentinel_probe.SENTINEL_MIN_N
        results[0] = _result(0, comps=thin)
        _wire_subprocess(monkeypatch, results)

        code = sentinel_probe.run_sentinel_probe(server_url=None)
        assert code == 1

    def test_no_gradeable_comps_alerts(self, monkeypatch):
        """Enough raw comps to clear the floor, but none carry a parsed
        grade — build_pool() would return an empty pool."""
        results = _all_healthy_results()
        results[0] = _result(0, comps=[_comp(5.0 + i, grade=None)
                                       for i in range(sentinel_probe.SENTINEL_MIN_N + 5)])
        _wire_subprocess(monkeypatch, results)

        code = sentinel_probe.run_sentinel_probe(server_url=None)
        assert code == 1

    def test_malformed_comp_does_not_crash_the_whole_batch(self, monkeypatch, capsys):
        """A comp missing an expected field (schema drift upstream) must
        surface as a failing report for THAT book, not an unhandled
        exception that kills the run before the other books are even
        printed."""
        results = _all_healthy_results()
        book = sentinel_probe.SENTINEL_BOOKS[0]
        # Missing "price" — _priced_median would otherwise raise KeyError.
        malformed = [{"grade": book["target_grade"]}
                    for _ in range(sentinel_probe.SENTINEL_MIN_N + 5)]
        results[0] = _result(0, comps=malformed)
        _wire_subprocess(monkeypatch, results)

        code = sentinel_probe.run_sentinel_probe(server_url=None)

        assert code == 1
        out = capsys.readouterr().out
        assert "evaluation error" in out
        # The OTHER sentinels and the negative control still got reported.
        assert "Batman #251" in out
        assert "Captain America #100" in out
        assert "negative control" in out

    def test_healthy_sentinels_do_not_mask_a_failing_one(self, monkeypatch):
        """Overall exit code must reflect ANY failure, not just an
        all-or-nothing aggregate that a single healthy book could hide."""
        results = _all_healthy_results()
        results[1] = _result(1, comps=[])  # second sentinel: n=0
        _wire_subprocess(monkeypatch, results)

        code = sentinel_probe.run_sentinel_probe(server_url=None)
        assert code == 1


# ─── Price-jump detection ──────────────────────────────────────────────────

class TestPriceJump:
    def _seed_baseline(self, book, median):
        key = sentinel_probe._book_key(book)
        existing = sentinel_probe._load_baselines()
        existing[key] = {"median": median, "pool_n": 10}
        sentinel_probe._save_baselines(existing)

    def test_upward_jump_alerts_and_leaves_baseline_untouched(self, monkeypatch):
        book = sentinel_probe.SENTINEL_BOOKS[0]
        self._seed_baseline(book, median=100.0)

        results = _all_healthy_results()
        # > 2.0x the $100 baseline
        results[0] = _result(0, comps=[_comp(500.0, grade=book["target_grade"])
                                       for _ in range(sentinel_probe.SENTINEL_MIN_N + 5)])
        _wire_subprocess(monkeypatch, results)

        code = sentinel_probe.run_sentinel_probe(server_url=None)
        assert code == 1

        saved = json.loads(sentinel_probe._baseline_path().read_text())
        assert saved[sentinel_probe._book_key(book)]["median"] == 100.0

    def test_downward_jump_alerts(self, monkeypatch):
        book = sentinel_probe.SENTINEL_BOOKS[0]
        self._seed_baseline(book, median=100.0)

        results = _all_healthy_results()
        # < 0.5x the $100 baseline
        results[0] = _result(0, comps=[_comp(10.0, grade=book["target_grade"])
                                       for _ in range(sentinel_probe.SENTINEL_MIN_N + 5)])
        _wire_subprocess(monkeypatch, results)

        code = sentinel_probe.run_sentinel_probe(server_url=None)
        assert code == 1

    def test_within_band_passes_and_rolls_baseline_forward(self, monkeypatch):
        book = sentinel_probe.SENTINEL_BOOKS[0]
        self._seed_baseline(book, median=100.0)

        results = _all_healthy_results()
        results[0] = _result(0, comps=[_comp(150.0, grade=book["target_grade"])
                                       for _ in range(sentinel_probe.SENTINEL_MIN_N + 5)])
        _wire_subprocess(monkeypatch, results)

        code = sentinel_probe.run_sentinel_probe(server_url=None)
        assert code == 0

        saved = json.loads(sentinel_probe._baseline_path().read_text())
        assert saved[sentinel_probe._book_key(book)]["median"] == 150.0

    def test_one_sentinels_jump_does_not_block_others_baseline_update(self, monkeypatch):
        """A failing book's baseline must not be updated, but a DIFFERENT,
        healthy sentinel's baseline in the SAME run must still roll forward —
        one bad instrument reading shouldn't freeze every other one too."""
        jumped_book = sentinel_probe.SENTINEL_BOOKS[0]
        healthy_book = sentinel_probe.SENTINEL_BOOKS[1]
        self._seed_baseline(jumped_book, median=100.0)
        self._seed_baseline(healthy_book, median=200.0)

        results = _all_healthy_results()
        results[0] = _result(0, comps=[_comp(1000.0, grade=jumped_book["target_grade"])
                                       for _ in range(sentinel_probe.SENTINEL_MIN_N + 5)])
        results[1] = _result(1, comps=[_comp(210.0, grade=healthy_book["target_grade"])
                                       for _ in range(sentinel_probe.SENTINEL_MIN_N + 5)])
        _wire_subprocess(monkeypatch, results)

        code = sentinel_probe.run_sentinel_probe(server_url=None)
        assert code == 1  # the jumped sentinel still fails the overall run

        saved = json.loads(sentinel_probe._baseline_path().read_text())
        assert saved[sentinel_probe._book_key(jumped_book)]["median"] == 100.0  # untouched
        assert saved[sentinel_probe._book_key(healthy_book)]["median"] == 210.0  # rolled forward


# ─── Negative control ───────────────────────────────────────────────────────

class TestNegativeControl:
    def test_matching_comps_alerts_even_if_all_sentinels_are_healthy(self, monkeypatch):
        results = _all_healthy_results()
        neg_id = len(sentinel_probe.SENTINEL_BOOKS)
        results[neg_id] = _result(neg_id, comps=[_comp(9.99, grade=None)])
        _wire_subprocess(monkeypatch, results)

        code = sentinel_probe.run_sentinel_probe(server_url=None)
        assert code == 1

    def test_zero_with_a_fetch_error_still_passes(self, monkeypatch):
        """The negative control's own expectation is just n==0 — a fetch
        error on IT specifically is not informative (0 is 0 either way) and
        must not fail a run whose sentinels are otherwise healthy."""
        results = _all_healthy_results()
        neg_id = len(sentinel_probe.SENTINEL_BOOKS)
        results[neg_id] = _result(neg_id, comps=[], error="transient")
        _wire_subprocess(monkeypatch, results)

        code = sentinel_probe.run_sentinel_probe(server_url=None)
        assert code == 0

    def test_negative_control_never_updates_any_sentinel_baseline(self, monkeypatch):
        results = _all_healthy_results()
        _wire_subprocess(monkeypatch, results)
        sentinel_probe.run_sentinel_probe(server_url=None)
        saved_before = json.loads(sentinel_probe._baseline_path().read_text())
        assert len(saved_before) == len(sentinel_probe.SENTINEL_BOOKS)
        assert all(sentinel_probe._book_key(sentinel_probe.NEGATIVE_CONTROL_BOOK)
                   not in k for k in saved_before)


# ─── Heartbeat robustness ────────────────────────────────────────────────────

class TestHeartbeat:
    def test_not_pinged_on_any_failure(self, monkeypatch):
        results = _all_healthy_results()
        results[0] = _result(0, comps=[])
        _wire_subprocess(monkeypatch, results)
        # autouse _no_real_heartbeat_post fixture raises if post() is called
        code = sentinel_probe.run_sentinel_probe(server_url="http://x")
        assert code == 1

    def test_404_is_logged_and_non_fatal(self, monkeypatch, capsys):
        _wire_subprocess(monkeypatch, _all_healthy_results())
        monkeypatch.setattr(
            sentinel_probe.requests, "post",
            lambda url, timeout=None: type(
                "R", (), {"status_code": 404,
                          "raise_for_status": lambda self: None})())

        code = sentinel_probe.run_sentinel_probe(server_url="http://x")
        assert code == 0
        assert "404" in capsys.readouterr().err

    def test_network_error_is_non_fatal(self, monkeypatch):
        _wire_subprocess(monkeypatch, _all_healthy_results())

        def _raise(url, timeout=None):
            raise requests.RequestException("down")
        monkeypatch.setattr(sentinel_probe.requests, "post", _raise)

        code = sentinel_probe.run_sentinel_probe(server_url="http://x")
        assert code == 0  # heartbeat failure must not flip a healthy result


# ─── Probe-level (not book-level) failures ─────────────────────────────────

class TestProbeCannotRun:
    def test_missing_binary_returns_two(self, monkeypatch):
        monkeypatch.setattr(sentinel_probe.shutil, "which", lambda _b: None)
        code = sentinel_probe.run_sentinel_probe(server_url=None)
        assert code == 2

    def test_timeout_returns_two(self, monkeypatch):
        monkeypatch.setattr(sentinel_probe.shutil, "which", lambda _b: "/usr/bin/ebay")

        def fake_run(cmd, capture_output, text, timeout=None):
            raise sentinel_probe.subprocess.TimeoutExpired(cmd, timeout)
        monkeypatch.setattr(sentinel_probe.subprocess, "run", fake_run)

        code = sentinel_probe.run_sentinel_probe(server_url=None)
        assert code == 2

    def test_nonzero_exit_returns_two(self, monkeypatch):
        monkeypatch.setattr(sentinel_probe.shutil, "which", lambda _b: "/usr/bin/ebay")

        def fake_run(cmd, capture_output, text, timeout=None):
            return type("R", (), {"returncode": 1, "stderr": "boom"})()
        monkeypatch.setattr(sentinel_probe.subprocess, "run", fake_run)

        code = sentinel_probe.run_sentinel_probe(server_url=None)
        assert code == 2

    def test_unparseable_output_returns_two(self, monkeypatch):
        monkeypatch.setattr(sentinel_probe.shutil, "which", lambda _b: "/usr/bin/ebay")

        def fake_run(cmd, capture_output, text, timeout=None):
            out_path = cmd[cmd.index("--out") + 1]
            with open(out_path, "w") as fh:
                fh.write("{not json")
            return type("R", (), {"returncode": 0, "stderr": ""})()
        monkeypatch.setattr(sentinel_probe.subprocess, "run", fake_run)

        code = sentinel_probe.run_sentinel_probe(server_url=None)
        assert code == 2

    def test_result_identity_mismatch_returns_two(self, monkeypatch):
        """A short/duplicate result set must never be mapped positionally —
        same BUI-174/187 discipline as fmv_runner._fail_mapping."""
        monkeypatch.setattr(sentinel_probe.shutil, "which", lambda _b: "/usr/bin/ebay")

        def fake_run(cmd, capture_output, text, timeout=None):
            out_path = cmd[cmd.index("--out") + 1]
            # Only one result for a 4-book batch.
            with open(out_path, "w") as fh:
                json.dump([_result(0, comps=[])], fh)
            return type("R", (), {"returncode": 0, "stderr": ""})()
        monkeypatch.setattr(sentinel_probe.subprocess, "run", fake_run)

        code = sentinel_probe.run_sentinel_probe(server_url=None)
        assert code == 2


# ─── Baseline persistence helpers ──────────────────────────────────────────

class TestBaselineHelpers:
    def test_missing_file_returns_empty_dict(self):
        assert sentinel_probe._load_baselines() == {}

    def test_corrupt_file_returns_empty_dict(self):
        path = sentinel_probe._baseline_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        assert sentinel_probe._load_baselines() == {}

    def test_round_trip(self):
        data = {"a|1|2000": {"median": 42.0, "pool_n": 7}}
        sentinel_probe._save_baselines(data)
        assert sentinel_probe._load_baselines() == data


# ─── Hard constraint: never touches the FMV write path (BUI-603) ──────────

def test_module_has_no_code_path_to_the_fmv_write_path():
    """sentinel_probe.py must be provably incapable of upserting FMV: it
    must never IMPORT fmv_runner (the only place /api/comics is POSTed from)
    and must never reference that endpoint's URL itself. AST-parsed (not a
    substring scan) so this doesn't false-positive on the module's own
    docstrings/comments explaining the constraint in prose."""
    import ast
    import pathlib

    import sentinel_probe as mod

    src = pathlib.Path(mod.__file__).read_text()
    tree = ast.parse(src)
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
    assert "fmv_runner" not in imported_names
    assert "fmv_math" in imported_names  # the only fmv-side import: pure math, no I/O

    # No string literal anywhere in the source builds the FMV-write endpoint.
    string_literals = [n.value for n in ast.walk(tree)
                       if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not any("/api/comics" in s and "heartbeat" not in s for s in string_literals)


def test_negative_control_has_no_year_and_absurd_issue():
    """Ground the 'guaranteed to stay zero forever' claim structurally, not
    just by assertion in the docstring."""
    nc = sentinel_probe.NEGATIVE_CONTROL_BOOK
    assert nc["year"] is None
    assert int(nc["issue"]) > 10_000  # no real comic is anywhere close


def test_sentinels_are_pre_speculative_era_keys():
    """Sentinels must be evergreen back-issue keys, not modern/speculative
    books whose price can legitimately swing on real news — that would
    make PRICE_JUMP fire on genuine market movement, not instrument
    failure."""
    for book in sentinel_probe.SENTINEL_BOOKS:
        assert book["year"] < 2000
        assert book["measured_pool_depth"] >= sentinel_probe.SENTINEL_MIN_N * 5
