"""BUI-1085: `comic-fmv --probe` is a no-write mode.

The contract under test is that a probe run makes NO HTTP write of any kind.
The guard is deliberately independent of fmv_runner's own helpers: it patches
every `requests` write entry point (post/put/patch/delete/request and
Session.request) so ANY write, including one added by a future code path that
bypasses `_post_json`, fails the test. GETs are allowed (cache lookup,
hand-priced provenance, certifier schema probe).
"""

import json

import pytest
import requests
from click.testing import CliRunner

import fmv_cli
import fmv_runner

SERVER = "http://test-server:8080"


class _Resp:
    status_code = 200
    text = "[]"

    def raise_for_status(self):
        pass

    def json(self):
        return []


@pytest.fixture
def write_calls(monkeypatch):
    """Record every HTTP write attempt; raise so the run cannot proceed past
    one. Reads return an empty 200."""
    calls = []

    def _forbid(name):
        def _f(*a, **k):
            calls.append((name, a, k))
            raise AssertionError(f"HTTP write attempted: {name} {a}")
        return _f

    for name in ("post", "put", "patch", "delete", "request"):
        monkeypatch.setattr(requests, name, _forbid(name))
    monkeypatch.setattr(requests.Session, "request", _forbid("Session.request"))
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(fmv_runner, "_PROBE_MODE", False)
    return calls


def _comp(i, price, grade=9.0):
    return {"product_id": f"p{i}", "title": f"comic {price}", "price": price,
            "grade": grade, "sold_date": "2026-09-01", "buying_format": ""}


def _fetch(books, **_):
    comps = [_comp(i, p) for i, p in enumerate([50, 55, 60, 65, 70])]
    slab = [dict(_comp(100 + i, 300 + i), certifier="cgc", label="universal",
                 page_quality="unknown") for i in range(3)]
    return [{"input": dict(b, _req_id=b["_idx"]), "comps": comps,
             "slab_comps": slab,
             "queries_used": [{"tier": "base", "cached": False}]}
            for b in books]


def _invoke(tmp_path, monkeypatch, *flags):
    batch = tmp_path / "b.json"
    batch.write_text(json.dumps([{"item_id": "1", "title": "Book", "issue": "1",
                                  "year": 1990, "grade": 9.0}]))
    monkeypatch.setenv("COMICS_SERVER_URL", SERVER)
    monkeypatch.setattr(fmv_runner, "_fetch_comps", _fetch)
    monkeypatch.setattr(fmv_runner, "_fetch_first_party_outcomes",
                        lambda *a, **k: [])
    return CliRunner().invoke(
        fmv_cli.cli, ["--batch", str(batch), "--brief", *flags])


def test_probe_run_makes_no_http_write_and_still_prices(
        tmp_path, monkeypatch, write_calls):
    result = _invoke(tmp_path, monkeypatch, "--probe")
    assert result.exit_code == 0, result.output
    assert write_calls == []
    assert "PROBE" in result.output
    brief = [json.loads(ln) for ln in result.output.splitlines()
             if ln.startswith("{")]
    # Priced exactly as a normal run (a real band), but nothing stored.
    assert brief and brief[0].get("fmv_low") and brief[0].get("fmv_high")
    assert all(not r.get("comic_id") and not r.get("fmv_id") for r in brief)


def test_guard_is_load_bearing_normal_run_does_write(
        tmp_path, monkeypatch, write_calls):
    """The same harness WITHOUT --probe must trip the write guard; otherwise
    the probe test above proves nothing."""
    result = _invoke(tmp_path, monkeypatch)
    assert write_calls, "a normal run should have attempted a POST"
    assert result.exit_code != 0


def test_probe_flag_reaches_run(tmp_path, monkeypatch, write_calls):
    seen = {}
    monkeypatch.setattr(fmv_cli.fmv_runner, "run", lambda **kw: seen.update(kw))
    batch = tmp_path / "b.json"
    batch.write_text("[]")
    monkeypatch.setenv("COMICS_SERVER_URL", SERVER)
    CliRunner().invoke(fmv_cli.cli, ["--batch", str(batch), "--probe"])
    assert seen["probe"] is True


@pytest.mark.parametrize("other", ["--sentinel-probe", "--slab-watch-collect"])
def test_probe_rejects_other_write_modes(other):
    r = CliRunner().invoke(fmv_cli.cli, ["--probe", other])
    assert r.exit_code == 2


def test_direct_write_helpers_are_silent_in_probe(monkeypatch, write_calls):
    monkeypatch.setattr(fmv_runner, "_PROBE_MODE", True)
    fmv_runner._ping_fmv_heartbeat(SERVER, persisted=3)
    fmv_runner._ping_slab_watch_collect_heartbeat(SERVER, detail="x")
    assert fmv_runner._post_json(f"{SERVER}/api/comics", {"a": 1},
                                 what="t") == {"a": 1}
    rows = [{"product_id": "p1", "comic_id": 7}]
    assert fmv_runner._post_comps_exclusions(
        SERVER, rows, [{"product_id": "p1", "code": "x"}]) == 1
    assert write_calls == []
