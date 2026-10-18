"""BUI-1292: the Stop hook reports only docs newer than session start, once per session."""

import json
import os
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "hooks" / "stop-untracked-docs.sh"


def _run(repo, tmp, sid, transcript, start=None):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo), "TMPDIR": str(tmp)}
    if start is not None:
        env["STOP_NAG_SESSION_START"] = str(start)
    payload = json.dumps({"session_id": sid, "transcript_path": str(transcript)})
    r = subprocess.run([str(SCRIPT)], input=payload, text=True, capture_output=True, env=env, cwd=repo)
    assert r.returncode == 0
    return r.stdout


def _setup(tmp_path):
    repo = tmp_path / "my repo"
    (repo / "docs").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    old = repo / "docs" / "old doc.md"
    old.write_text("x")
    past = time.time() - 3600
    os.utime(old, (past, past))
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    return repo, tmp, transcript


def test_stale_doc_silent_new_doc_nags_once(tmp_path):
    repo, tmp, transcript = _setup(tmp_path)
    start = int(time.time())  # explicit start: CI filesystems have no birth time
    assert _run(repo, tmp, "s1", transcript, start) == ""
    time.sleep(1.1)
    (repo / "docs" / "new doc.md").write_text("y")
    out = json.loads(_run(repo, tmp, "s1", transcript, start))
    assert "docs/new doc.md" in out["systemMessage"]
    assert "old doc.md" not in out["systemMessage"]
    assert _run(repo, tmp, "s1", transcript, start) == ""


def test_unknown_birth_time_is_silent(tmp_path):
    repo, tmp, transcript = _setup(tmp_path)
    (repo / "docs" / "new doc.md").write_text("y")
    for start in ("0", "-"):
        assert _run(repo, tmp, "s2", transcript, start) == ""


def test_bad_input_is_silent(tmp_path):
    repo, tmp, _ = _setup(tmp_path)
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo), "TMPDIR": str(tmp)}
    for stdin in ("", "not json", "{}"):
        r = subprocess.run([str(SCRIPT)], input=stdin, text=True, capture_output=True, env=env)
        assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
