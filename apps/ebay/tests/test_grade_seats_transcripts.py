"""Transcript cleanup for grade-seats (BUI-1285). Every test runs under a fake HOME."""

import json
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import pytest

import grade_seats

SID_A = "11111111-2222-3333-4444-555555555555"
SID_B = "66666666-7777-8888-9999-000000000000"


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """A scratch workdir under /tmp with an underscore-free path, plus a fake HOME."""
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GRADE_SEATS_KEEP_TRANSCRIPTS", raising=False)
    wd = Path(tempfile.mkdtemp(prefix="gsx", dir="/tmp")).resolve()
    wd = wd.rename(wd.with_name("gsx" + uuid.uuid4().hex[:8]))
    (wd / "grader-body.md").write_text("x")
    (wd / "empty-mcp.json").write_text("{}")
    (wd / "comic-1").mkdir()
    yield wd, home / ".claude" / "projects"
    shutil.rmtree(wd, ignore_errors=True)


def _transcripts(wd, projects, *sids):
    proj = projects / grade_seats.transcript_slug(wd)
    proj.mkdir()
    for i, sid in enumerate(sids):
        (proj / f"{sid}.jsonl").write_text("{}")
        (proj / sid).mkdir()
        (proj / sid / "tool.txt").write_text("x")
        (wd / "comic-1" / f"seat-s{i}.json").write_text(json.dumps({"session_id": sid}))
    return proj


def test_transcript_slug_matches_claude_code_rule():
    assert grade_seats.transcript_slug("/private/tmp/comic-grading/run-3px3wsic") == \
        "-private-tmp-comic-grading-run-3px3wsic"
    assert grade_seats.transcript_slug("/Users/x/.claude/wt") == "-Users-x--claude-wt"
    assert grade_seats.transcript_slug("/tmp/has_underscore") is None
    assert grade_seats.transcript_slug("/tmp/has space") is None
    assert grade_seats.transcript_slug("/" + "a" * 250) is None


def test_cleanup_removes_this_runs_sessions_then_the_empty_dir(scratch):
    wd, projects = scratch
    proj = _transcripts(wd, projects, SID_A)
    assert grade_seats.cleanup_transcripts(wd, [wd / "comic-1"], 0) == 1
    assert not proj.exists()


def test_cleanup_leaves_other_sessions_and_keeps_nonempty_dir(scratch):
    wd, projects = scratch
    proj = _transcripts(wd, projects, SID_A, SID_B)
    (wd / "comic-1" / "seat-s1.json").unlink()     # SID_B is not this run's
    assert grade_seats.cleanup_transcripts(wd, [wd / "comic-1"], 0) == 1
    assert not (proj / f"{SID_A}.jsonl").exists() and not (proj / SID_A).exists()
    assert (proj / f"{SID_B}.jsonl").exists() and (proj / SID_B / "tool.txt").exists()


def test_cleanup_ignores_stale_envelopes(scratch):
    wd, projects = scratch
    proj = _transcripts(wd, projects, SID_A)
    assert grade_seats.cleanup_transcripts(wd, [wd / "comic-1"], time.time() + 100) == 0
    assert (proj / f"{SID_A}.jsonl").exists()


def test_keep_env_var_keeps_everything(scratch, monkeypatch):
    wd, projects = scratch
    proj = _transcripts(wd, projects, SID_A)
    monkeypatch.setenv("GRADE_SEATS_KEEP_TRANSCRIPTS", "1")
    assert grade_seats.cleanup_transcripts(wd, [wd / "comic-1"], 0) == 0
    assert (proj / f"{SID_A}.jsonl").exists()


def test_cleanup_refuses_non_scratch_workdirs(scratch):
    wd, projects = scratch
    proj = _transcripts(wd, projects, SID_A)
    (wd / "grader-body.md").unlink()               # not a dir this tool prepared
    assert grade_seats.cleanup_transcripts(wd, [wd / "comic-1"], 0) == 0
    assert (proj / f"{SID_A}.jsonl").exists()
    home = Path.home()
    assert not grade_seats._is_scratch_workdir(home)
    assert not grade_seats._is_scratch_workdir(home.parent)
    assert not grade_seats._is_scratch_workdir(Path(grade_seats.__file__).resolve().parents[3])


def test_cleanup_never_follows_symlinks(scratch, tmp_path):
    wd, projects = scratch
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    slug = grade_seats.transcript_slug(wd)
    (projects / slug).symlink_to(victim)           # project dir is a symlink
    (wd / "comic-1" / "seat-s0.json").write_text(json.dumps({"session_id": SID_A}))
    assert grade_seats.cleanup_transcripts(wd, [wd / "comic-1"], 0) == 0
    assert (victim / "keep.txt").exists()
    (projects / slug).unlink()
    proj = projects / slug
    proj.mkdir()
    (proj / SID_A).symlink_to(victim)              # session dir is a symlink
    grade_seats.cleanup_transcripts(wd, [wd / "comic-1"], 0)
    assert (victim / "keep.txt").exists()


def test_cleanup_rejects_malformed_session_ids_and_logs(scratch):
    wd, projects = scratch
    proj = projects / grade_seats.transcript_slug(wd)
    proj.mkdir()
    (proj / "keep.jsonl").write_text("x")
    for sid in ("../..", "*", "keep", ""):
        (wd / "comic-1" / "seat-bad.json").write_text(json.dumps({"session_id": sid}))
        assert grade_seats.cleanup_transcripts(wd, [wd / "comic-1"], 0) == 0
    assert (proj / "keep.jsonl").exists()
    msgs = []
    grade_seats.cleanup_transcripts(wd / "missing", [], 0, msgs.append)
    assert msgs and "kept" in msgs[0]


def test_main_removes_transcripts_after_run(scratch, monkeypatch):
    wd, projects = scratch
    proj = projects / grade_seats.transcript_slug(wd)
    proj.mkdir()
    (proj / f"{SID_A}.jsonl").write_text("{}")
    monkeypatch.setattr(grade_seats, "shared_crops", lambda books: {})
    monkeypatch.setattr(grade_seats, "fetch_seller_description", lambda item_id: None)
    result = "x\nGRADE: 5.0 (FN)\nGRADE RANGE: 5.0\nCONFIDENCE: HIGH\n"
    fake = wd.parent / (wd.name + "-claude")
    fake.write_text("#!/usr/bin/env python3\nimport json\n"
                    f"print(json.dumps({{'session_id': {SID_A!r}, 'result': {result!r}, "
                    "'num_turns': 1, 'modelUsage': {}}))\n")
    fake.chmod(0o755)
    monkeypatch.setenv("GRADE_SEATS_CLAUDE", str(fake))
    (wd / "agent.md").write_text("---\nname: x\n---\nBODY\n")
    (wd / "comic-1" / "img-01.jpg").write_bytes(b"x")
    book = {"item_id": "101", "comic": "Comic 1 (1970)", "folder": "comic-1", "seats": ["g1-a"]}
    (wd / "spec.json").write_text(json.dumps({"workdir": str(wd), "books": [book]}))
    try:
        argv = [str(wd / "spec.json"), "--grader-agent", str(wd / "agent.md"), "--json"]
        assert grade_seats.main(argv) == 0
    finally:
        fake.unlink()
    assert not proj.exists()
