"""BUI-1257 -- the nightly selector skips tickets owned by unfinished attended queues.

The script name has hyphens, so it is loaded by path. Queue fixtures live under
a tmp dir passed as the state root; the real ~/.local/state is never touched.
"""

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "em-batch-nightly-select.py"
_spec = importlib.util.spec_from_file_location("em_batch_nightly_select", _SCRIPT)
sel = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sel
_spec.loader.exec_module(sel)


def queue(root, run_id, body):
    d = root / run_id
    d.mkdir(parents=True)
    (d / "queue.md").write_text(body, encoding="utf-8")


def chunk(n, status, tickets, dash="–"):
    return "## Chunk %d\n- **Status** %s %s\n- **Tickets** %s %s\n\n" % (
        n, dash, status, dash, tickets)


HEADER = "# em-batch Queue: x\n\n- **Tickets** – BUI-900 (top-level, ignored)\n\n"


def test_pending_running_done_held(tmp_path):
    queue(tmp_path, "a", HEADER + chunk(1, "pending", "BUI-1, BUI-2"))
    queue(tmp_path, "b", HEADER + chunk(1, "running", "BUI-3"))
    queue(tmp_path, "c", HEADER + chunk(1, "done", "BUI-4"))
    queue(tmp_path, "d", HEADER + chunk(1, "held", "BUI-5"))
    assert sel.attended_tickets(str(tmp_path)) == {"BUI-1", "BUI-2", "BUI-3", "BUI-5"}


def test_done_chunk_beside_pending_chunk(tmp_path):
    queue(tmp_path, "a", HEADER + chunk(1, "done", "BUI-1") + chunk(2, "pending", "BUI-2"))
    assert sel.attended_tickets(str(tmp_path)) == {"BUI-2"}


def test_id_prefix_does_not_collide(tmp_path):
    queue(tmp_path, "a", HEADER + chunk(1, "pending", "BUI-1160"))
    owned = sel.attended_tickets(str(tmp_path))
    assert "BUI-116" not in owned and "BUI-1160" in owned


def test_hyphen_dash_and_heading_fallback(tmp_path):
    queue(tmp_path, "a", HEADER + chunk(1, "pending", "BUI-7", dash="-"))
    queue(tmp_path, "b", HEADER + "## Chunk 1\n- **Status** – pending\n\n"
          "### BUI-8: title\n- **Outcome** – pending\n")
    assert sel.attended_tickets(str(tmp_path)) == {"BUI-7", "BUI-8"}


def test_bad_queues_are_skipped(tmp_path, capsys):
    queue(tmp_path, "a", "no chunks here\n")
    d = tmp_path / "b"
    d.mkdir()
    (d / "queue.md").write_bytes(b"\xff\xfe\x00bad")
    queue(tmp_path, "c", HEADER + chunk(1, "pending", "BUI-9"))
    assert sel.attended_tickets(str(tmp_path)) == {"BUI-9"}
    assert capsys.readouterr().err.count("skipping queue") == 2


def test_missing_root_is_empty(tmp_path):
    assert sel.attended_tickets(str(tmp_path / "nope")) == set()


def test_main_filters_attended(tmp_path, monkeypatch, capsys):
    queue(tmp_path, "a", HEADER + chunk(1, "pending", "BUI-1"))
    queue(tmp_path, "b", HEADER + chunk(1, "done", "BUI-2"))
    rows = [{"identifier": "BUI-%d" % n, "title": "t", "priority": 0,
             "createdAt": "2026-01-0%d" % n, "state": {"name": "Today"}} for n in (1, 2, 3)]
    monkeypatch.setattr(sel, "api_key", lambda _p: "k")
    monkeypatch.setattr(sel, "fetch_all", lambda _k: rows)
    monkeypatch.setattr(sys, "argv", ["x", "--state-root", str(tmp_path)])
    assert sel.main() == 0
    assert capsys.readouterr().out.split() == ["BUI-2", "BUI-3"]
