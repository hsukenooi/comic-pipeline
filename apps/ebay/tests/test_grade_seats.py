"""Tests for grade-seats (BUI-1095). A fake `claude` script stands in via GRADE_SEATS_CLAUDE."""

import json
import stat
import sys
from pathlib import Path

import pytest

import grade_seats

# Fake claude: reads the job on stdin, finds the seat from "crops-<seat>", counts its
# own calls, and acts out the seat's plan (FAKE_DIR/<seat>.json, one step per call).
FAKE = f"""#!{sys.executable}
import json, os, re, sys, time
job = sys.stdin.read()
d = os.environ["FAKE_DIR"]
seat = re.search(r"crops-([\\w-]+)", job).group(1)
n = len([f for f in os.listdir(d) if f.startswith(seat + ".job.")]) + 1
open(os.path.join(d, seat + ".job." + str(n)), "w").write(job)
open(os.path.join(d, seat + ".cwd." + str(n)), "w").write(os.getcwd())
plan = json.load(open(os.path.join(d, seat + ".json")))
step = plan[min(n, len(plan)) - 1]

def block(item, g, cap, rationale, conf="MEDIUM-LOW: 2 photos", rng=None):
    return (item + "\\nPHOTO MAP: img-01: front cover\\nGRADE: " + str(g) + " (VF)\\nGRADE RANGE: " + (rng or str(g)) + "\\n"
            "CONFIDENCE: " + conf + "\\nGRADE CAP: " + cap + "\\nRATIONALE: " + rationale + " More text.\\n"
            "PHOTO LIMITATIONS: no spine.\\n")

if step.get("exit"):
    sys.stderr.write("boom\\n")
    sys.exit(step["exit"])
if step.get("hang"):
    time.sleep(60)
if step.get("garbage"):
    print("not json")
    sys.exit(0)
if "raw" in step:
    print(json.dumps({{"result": step["raw"], "num_turns": 2, "modelUsage": {{}}}}))
    sys.exit(0)
result = "".join(block(i, g, step.get("cap", "none"), step.get("rationale", "Clean."),
                       step.get("conf", "MEDIUM-LOW: 2 photos"), step.get("range"))
                 for i, g in step.get("grades", {{}}).items())
usage = {{"m": {{"outputTokens": 10, "cacheReadInputTokens": 100, "cacheCreationInputTokens": 5}},
         "n": {{"outputTokens": 1, "cacheReadInputTokens": 2, "cacheCreationInputTokens": 3}}}}
print(json.dumps({{"result": result, "is_error": step.get("is_error", False), "num_turns": 3, "modelUsage": usage}}))
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = tmp_path / "fake-claude"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    fdir = tmp_path / "fake"
    fdir.mkdir()
    monkeypatch.setenv("GRADE_SEATS_CLAUDE", str(fake))
    monkeypatch.setenv("FAKE_DIR", str(fdir))
    monkeypatch.setattr(grade_seats, "SEAT_TIMEOUT", 3.0)
    (tmp_path / "agent.md").write_text("---\nname: x\n---\nBODY\n")
    monkeypatch.setattr(grade_seats, "shared_crops", lambda books: {})
    return tmp_path


def plan(env, seat, *steps):
    (env / "fake" / f"{seat}.json").write_text(json.dumps(list(steps)))


def spec(env, books, photos=1):
    work = env / "work"
    for b in books:
        (work / b["folder"]).mkdir(parents=True, exist_ok=True)
        for n in range(1, photos + 1):
            (work / b["folder"] / f"img-{n:02d}.jpg").write_bytes(b"x")
    p = env / "spec.json"
    p.write_text(json.dumps({"workdir": str(work), "books": books}))
    return p


def run(env, books, capsys, photos=1):
    p = spec(env, books, photos)
    code = grade_seats.main([str(p), "--grader-agent", str(env / "agent.md"), "--json"])
    return code, json.loads(capsys.readouterr().out)


def book(n=1, seats=("a", "b"), **kw):
    return {"item_id": f"10{n}", "comic": f"Comic {n} (1970)", "folder": f"comic-{n}",
            "seats": [f"g{n}-{s}" for s in seats], **kw}


def test_parse_block_and_scrub():
    text = ("GRADE: 5.5 (FN-)\nGRADE RANGE: 5.0-6.0 VG/FN-FN\nCONFIDENCE: MEDIUM-LOW: two photos\n"
            'GRADE CAP: spine split ~1/4" caps at 6.0 FN\nRATIONALE: Split sets the cap. Rest.\n'
            "PHOTO LIMITATIONS: no interior.\n")
    b = grade_seats.parse_block(text)
    assert b["grade"] == 5.5 and b["confidence"] == "MEDIUM-LOW" and b["range"].startswith("5.0")
    assert "6.0" not in b["defect"] and "[grade]" in b["defect"]
    assert grade_seats.parse_block("GRADE: 5.0\n") is None


def test_parse_envelope_usage_and_errors(tmp_path):
    good = tmp_path / "ok.json"
    usage = {"a": {"outputTokens": 7, "cacheReadInputTokens": 70, "cacheCreationInputTokens": 1},
             "b": {"outputTokens": 3, "cacheReadInputTokens": 30, "cacheCreationInputTokens": 2}}
    good.write_text(json.dumps({"result": "GRADE: 6.0 (FN)\nGRADE RANGE: 6.0\nCONFIDENCE: HIGH\n",
                                "num_turns": 4, "modelUsage": usage}))
    parsed, u, err = grade_seats.parse_envelope(good, ["1"])
    assert parsed["1"]["grade"] == 6.0 and err == ""
    assert u == {"turns": 4, "out": 10, "cache_read": 100, "cache_create": 3}
    bad = tmp_path / "bad.json"
    bad.write_text("nope")
    assert grade_seats.parse_envelope(bad, ["1"])[0] == {"1": None}
    assert grade_seats.parse_envelope(tmp_path / "missing.json", ["1"])[2].startswith("unreadable")
    iserr = tmp_path / "e.json"
    iserr.write_text(json.dumps({"is_error": True, "subtype": "error_max_turns", "result": ""}))
    assert grade_seats.parse_envelope(iserr, ["1"])[2] == "error_max_turns"


def test_batched_result_splits_by_item_id(tmp_path):
    result = ("111\nGRADE: 6.0 (FN)\nGRADE RANGE: 6.0\nCONFIDENCE: HIGH\n"
              "222\nGRADE: 3.0 (GD)\nGRADE RANGE: 3.0\nCONFIDENCE: LOW\n")
    env = tmp_path / "e.json"
    env.write_text(json.dumps({"result": result}))
    parsed, _, err = grade_seats.parse_envelope(env, ["111", "222", "333"])
    assert parsed["111"]["grade"] == 6.0 and parsed["222"]["grade"] == 3.0
    assert parsed["333"] is None and err


def test_agreeing_seats_no_second_pass(env, capsys):
    plan(env, "g1-a", {"grades": {"101": 5.0}})
    plan(env, "g1-b", {"grades": {"101": 5.5}})
    code, rep = run(env, [book()], capsys)
    assert code == 0 and rep["books"][0]["adjudicator"] is None
    assert len(rep["usage"]) == 2 and rep["usage"][0]["cache_read"] == 102
    job = (env / "work/comic-1/job-g1-a.txt").read_text()
    assert "CROP DIRECTORY: " in job and job.rstrip().endswith("write nothing after them.")
    assert (env / "work/grader-body.md").read_text().strip() == "BODY"


def test_every_seat_launches_in_workdir_not_repo(env, capsys):
    """BUI-1177: first-pass, retry, and adjudicator seats all run with cwd=workdir."""
    plan(env, "g1-a", {"grades": {"101": 5.0}})
    plan(env, "g1-b", {"exit": 1}, {"grades": {"101": 7.0}})  # retried, then splits 2.0
    plan(env, "adjudicator", {"raw": "RECONCILED BLOCK\n101\nGRADE: 6.0\nGRADE RANGE: 6.0\n"
                                      "CONFIDENCE: MEDIUM\nSEAT FINDINGS\n- g1-a: ok\n- g1-b: ok\n"})
    code, rep = run(env, [book()], capsys)
    work = (env / "work").resolve()
    cwds = sorted((env / "fake").glob("*.cwd.*"))
    assert len(cwds) == 4  # a, b, b-retry, adjudicator
    assert {Path(c.read_text()).resolve() for c in cwds} == {work}
    assert Path(grade_seats.__file__).resolve().parents[3] not in work.parents


def test_retry_after_failure_uses_retry_job_name(env, capsys):
    plan(env, "g1-a", {"exit": 3}, {"grades": {"101": 5.0}})
    plan(env, "g1-b", {"grades": {"101": 5.0}})
    code, rep = run(env, [book()], capsys)
    assert code == 0
    assert (env / "work/comic-1/job-g1-a-retry.txt").exists()
    assert (env / "work/comic-1/seat-g1-a.json").exists()  # the failed envelope survives
    rows = [u for u in rep["usage"] if u["seat"] == "g1-a"]
    assert [r["retry"] for r in rows] == [False, True] and "exit 3: boom" in rows[0]["error"]
    assert not rep["books"][0]["seats"][0].get("failed")


def test_double_failure_reports_failed_seat(env, capsys):
    plan(env, "g1-a", {"garbage": True}, {"is_error": True})
    plan(env, "g1-b", {"grades": {"101": 5.0}})
    code, rep = run(env, [book()], capsys)
    assert code == 0  # one seat returned
    assert rep["books"][0]["seats"][0]["failed"] is True
    for f in (env / "fake").glob("*.job.*"):
        f.unlink()
    code, rep = run(env, [book(seats=("a",))], capsys)  # the failing seat alone
    assert code == 1


def test_hung_seat_times_out_and_is_retried(env, capsys):
    plan(env, "g1-a", {"hang": True}, {"grades": {"101": 5.0}})
    plan(env, "g1-b", {"grades": {"101": 5.0}})
    code, rep = run(env, [book()], capsys)
    assert code == 0
    assert "timeout" in [u for u in rep["usage"] if u["seat"] == "g1-a"][0]["error"]


def adj_reply(grade, findings=("- g1-a: confirmed nothing; rejected the gloss read because glare",
                                "- g1-b: confirmed the spine split in xexam/g1-b-crop-01.jpg")):
    """An adjudicator reply that quotes a seat's block before its own (the ambiguity case)."""
    quoted = ("Seat g1-a said:\nGRADE: 6.0 (FN)\nGRADE RANGE: 6.0\nCONFIDENCE: HIGH\n\n")
    return (quoted + "RECONCILED BLOCK\nPHOTO MAP: img-01: front cover\nGRADE: " + str(grade)
            + " (VG)\nGRADE RANGE: 4.5-5.0\nCONFIDENCE: MEDIUM-LOW: 2 photos\n"
            "GRADE CAP: spine split caps at 5.0\nRATIONALE: Split confirmed. More.\n"
            "PHOTO LIMITATIONS: no interior.\nSEAT FINDINGS\n" + "\n".join(findings) + "\n")


def split_setup(env, adj_steps):
    plan(env, "g1-a", {"grades": {"101": 6.0}})
    plan(env, "g1-b", {"grades": {"101": 4.5}, "cap": 'spine split ~1/4" caps at 4.5',
                       "rationale": "Spine split seen."})
    plan(env, "adjudicator", *adj_steps)
    crops = env / "work/comic-1/crops-g1-b"
    crops.mkdir(parents=True)
    (crops / "crop-01.jpg").write_bytes(b"c")


def test_split_runs_one_adjudicator_with_every_block_and_crop(env, capsys):
    split_setup(env, [{"raw": adj_reply(5.0)}])
    code, rep = run(env, [book()], capsys)
    bk = rep["books"][0]
    assert code == 0 and bk["split"] == 1.5 and bk["adjudicator_failed"] is False
    adj = bk["adjudicator"]
    assert adj["grade"] == 5.0 and adj["range"] == "4.5-5.0"  # the reconciled block, not the quote
    assert len(adj["findings"]) == 2 and adj["findings"][1].startswith("g1-b: confirmed")
    assert [s["grade"] for s in bk["seats"]] == [6.0, 4.5]  # first-pass grades retained
    assert all("raw" not in s for s in bk["seats"]) and "raw" not in adj
    assert (env / "work/comic-1/xexam/g1-b-crop-01.jpg").exists()
    job = (env / "work/comic-1/job-adjudicator.txt").read_text()
    assert "ADJUDICATION:" in job and "CROP DIRECTORY: " in job and "crops-adjudicator" in job
    for seat, g in (("g1-a", "6.0"), ("g1-b", "4.5")):
        assert f"=== FIRST-PASS SEAT {seat} ===" in job and f"GRADE: {g} (VF)" in job
    assert 'GRADE CAP: spine split ~1/4" caps at 4.5' in job and "RATIONALE: Spine split seen." in job
    assert "xexam/g1-b-crop-01.jpg" in job and "none (this seat made no ad hoc crops)" in job
    assert job.index("ADJUDICATION:") < job.index("HARNESS:") and "SEAT FINDINGS" in job
    assert not list((env / "work/comic-1").glob("job-g1-*-p2*.txt"))  # no per-seat re-run
    rows = [u for u in rep["usage"] if u["pass"] == "adj"]
    assert len(rows) == 1 and rows[0]["seat"] == "adjudicator" and len(rep["usage"]) == 3


def test_adjudicator_text_output(env, capsys):
    split_setup(env, [{"raw": adj_reply(5.0)}])
    p = spec(env, [book()])
    assert grade_seats.main([str(p), "--grader-agent", str(env / "agent.md")]) == 0
    out = capsys.readouterr().out
    assert "A g1-a: 6.0 |" in out and "B g1-b: 4.5 |" in out and "ADJ adjudicator: 5.0 |" in out
    assert "  - g1-b: confirmed the spine split" in out
    assert "Adjudicated: split 1.5, adjudicator grade 5.0" in out
    assert "| Comic 1 (1970) | adjudicator | adj |" in out


def test_adjudicator_retry_then_double_failure_keeps_first_pass(env, capsys):
    split_setup(env, [{"exit": 2}, {"raw": adj_reply(5.0)}])
    code, rep = run(env, [book()], capsys)
    assert rep["books"][0]["adjudicator"]["grade"] == 5.0
    assert (env / "work/comic-1/job-adjudicator-retry.txt").exists()
    assert [u["retry"] for u in rep["usage"] if u["pass"] == "adj"] == [False, True]
    for f in (env / "fake").glob("*.job.*"):
        f.unlink()
    plan(env, "adjudicator", {"garbage": True}, {"is_error": True})
    p = spec(env, [book()])
    assert grade_seats.main([str(p), "--grader-agent", str(env / "agent.md")]) == 0
    out = capsys.readouterr().out
    assert "adjudicator FAILED twice, first-pass grades kept" in out
    assert "A g1-a: 6.0 |" in out and "ADJ " not in out


def test_parse_adjudication_ambiguity():
    assert grade_seats.parse_adjudication(adj_reply(3.0))["grade"] == 3.0
    # no marker but two GRADE: lines: ambiguous, fails (the retry then runs)
    two = "GRADE: 6.0\nGRADE RANGE: 6.0\nCONFIDENCE: HIGH\nGRADE: 3.0\nGRADE RANGE: 3.0\nCONFIDENCE: LOW\n"
    assert grade_seats.parse_adjudication(two) is None
    one = "GRADE: 3.0 (GD/VG)\nGRADE RANGE: 3.0\nCONFIDENCE: LOW\n"
    assert grade_seats.parse_adjudication(one)["findings"] == []
    # the LAST marker wins when the reply echoes the instruction's marker first
    echoed = "RECONCILED BLOCK\n<one block>\n" + adj_reply(2.5)
    assert grade_seats.parse_adjudication(echoed)["grade"] == 2.5
    md = adj_reply(3.5).replace("RECONCILED BLOCK", "**RECONCILED BLOCK**").replace(
        "SEAT FINDINGS", "## SEAT FINDINGS")
    assert grade_seats.parse_adjudication(md)["grade"] == 3.5


def test_split_threshold_boundary(env, capsys):
    plan(env, "g1-a", {"grades": {"101": 6.0}})
    plan(env, "g1-b", {"grades": {"101": 5.0}})
    plan(env, "adjudicator", {"raw": adj_reply(5.5)})
    assert run(env, [book()], capsys)[1]["books"][0]["split"] == 1.0  # exactly 1.0 triggers
    for f in (env / "fake").glob("*.job.*"):
        f.unlink()
    plan(env, "g1-b", {"grades": {"101": 5.5}})
    bk = run(env, [book()], capsys)[1]["books"][0]
    assert bk["split"] is None and bk["adjudicator"] is None
    assert not (env / "fake" / "adjudicator.job.2").exists()  # no adjudicator on 0.5


def test_single_seat_book_and_batch(env, capsys):
    plan(env, "g1-a", {"grades": {"101": 7.0}})
    plan(env, "gb", {"grades": {"102": 4.0, "103": 3.0}})
    books = [book(seats=("a",)),
             {"item_id": "102", "comic": "Two", "folder": "comic-2", "seats": ["gb"], "batch": "x"},
             {"item_id": "103", "comic": "Three", "folder": "comic-3", "seats": ["gb"], "batch": "x"}]
    code, rep = run(env, books, capsys)
    assert code == 0 and all(b["split"] is None and b["adjudicator"] is None for b in rep["books"])
    assert [b["seats"][0]["grade"] for b in rep["books"]] == [7.0, 4.0, 3.0]
    assert not list((env / "work").glob("*/job-adjudicator*.txt"))  # a batch is never adjudicated
    job = (env / "work/comic-2/job-gb.txt").read_text()
    assert "ITEM ID: 102" in job and "ITEM ID: 103" in job and "crops-gb" in job


def test_text_output_and_version(env, capsys):
    plan(env, "g1-a", {"grades": {"101": 6.0}})
    plan(env, "g1-b", {"grades": {"101": 6.0}})
    p = spec(env, [book()])
    assert grade_seats.main([str(p), "--grader-agent", str(env / "agent.md")]) == 0
    out = capsys.readouterr().out
    assert "### Comic 1 (1970) — 101" in out and "| **Total** |" in out and "Adjudicated: no" in out
    with pytest.raises(SystemExit):
        grade_seats.main(["--version"])
    assert grade_seats._version_string().startswith("grade-seats ")


def test_bad_spec_exit_2(env):
    assert grade_seats.main([str(env / "nope.json")]) == 2


# ---------- two-first policy (BUI-1098) ----------

def two_first(n=1, comic=None, **kw):
    b = book(n, seats=("a", "b", "c"), policy="two-first", **kw)
    if comic:
        b["comic"] = comic
    return b


def ran(env, seat):
    return (env / "fake" / f"{seat}.job.1").exists()


def test_two_first_agreed_skips_third_and_takes_lower_grade_and_union(env, capsys):
    plan(env, "g1-a", {"grades": {"101": 6.0}, "conf": "MEDIUM: 4 photos", "range": "5.5-6.5"})
    plan(env, "g1-b", {"grades": {"101": 6.0}, "conf": "HIGH", "range": "6.0-7.0"})
    plan(env, "g1-c", {"grades": {"101": 9.0}})
    code, rep = run(env, [two_first()], capsys)
    bk = rep["books"][0]
    assert code == 0 and not ran(env, "g1-c") and [s["seat"] for s in bk["seats"]] == ["g1-a", "g1-b"]
    pol = bk["policy"]
    assert pol["third"] is False and pol["trigger"] is None
    assert pol["consensus"] == {"grade": 6.0, "range": "5.5-7.0", "confidence": "MEDIUM"}
    assert len(rep["usage"]) == 2


def test_two_seat_consensus_lower_grade_when_apart():
    a = {"grade": 9.6, "range": "9.4-9.8 NM+", "confidence": "HIGH"}
    b = {"grade": 9.4, "range": "9.2", "confidence": "MEDIUM"}
    assert grade_seats.two_seat_consensus(a, b) == {"grade": 9.4, "range": "9.2-9.8", "confidence": "MEDIUM"}
    same = {"grade": 5.0, "range": "5.0", "confidence": "HIGH"}
    assert grade_seats.two_seat_consensus(same, dict(same))["range"] == "5.0"


def test_two_first_agreed_text_line(env, capsys):
    plan(env, "g1-a", {"grades": {"101": 6.0}, "conf": "HIGH"})
    plan(env, "g1-b", {"grades": {"101": 6.0}, "conf": "HIGH"})
    p = spec(env, [two_first()])
    assert grade_seats.main([str(p), "--grader-agent", str(env / "agent.md")]) == 0
    out = capsys.readouterr().out
    assert "Two-first: agreed, third seat skipped; two-seat consensus 6.0 | 6.0 | HIGH" in out
    assert "Seats: 2 of 2 returned" in out and "g1-c" not in out


@pytest.mark.parametrize("a, b, want", [
    ({"grades": {"101": 6.0}, "conf": "HIGH"}, {"grades": {"101": 5.5}, "conf": "HIGH"}, "split 0.5"),
    ({"grades": {"101": 6.0}, "conf": "HIGH", "cap": "spine split caps at 6.0"},
     {"grades": {"101": 6.0}, "conf": "HIGH"}, "grade cap (g1-a)"),
    ({"grades": {"101": 6.0}, "conf": "HIGH"}, {"grades": {"101": 6.0}, "conf": "Medium-Low: 2 photos"},
     "confidence MEDIUM-LOW (g1-b)"),
    ({"grades": {"101": 6.0}, "conf": "LOW"}, {"grades": {"101": 6.0}, "conf": "HIGH"}, "confidence LOW (g1-a)"),
    ({"grades": {"101": 6.0}, "conf": "MEDIUM LOW"}, {"grades": {"101": 6.0}, "conf": "HIGH"},
     "confidence MEDIUM-LOW (g1-a)"),
    ({"garbage": True}, {"grades": {"101": 6.0}, "conf": "HIGH"}, "seat g1-a failed"),
])
def test_two_first_each_trigger_dispatches_third(env, capsys, a, b, want):
    plan(env, "g1-a", a, a)
    plan(env, "g1-b", b)
    plan(env, "g1-c", {"grades": {"101": 6.0}, "conf": "HIGH"})
    code, rep = run(env, [two_first()], capsys)
    bk = rep["books"][0]
    assert code == 0 and ran(env, "g1-c") and len(bk["seats"]) == 3
    assert want in bk["policy"]["trigger"] and bk["policy"]["third"] and bk["policy"]["consensus"] is None
    assert bk["adjudicator"] is None  # no seat pair split by 1.0


def test_third_seat_then_adjudicator_on_a_one_point_split(env, capsys):
    plan(env, "g1-a", {"grades": {"101": 6.0}, "conf": "HIGH"})
    plan(env, "g1-b", {"grades": {"101": 5.5}, "conf": "HIGH"})
    plan(env, "g1-c", {"grades": {"101": 4.5}, "conf": "HIGH"})
    plan(env, "adjudicator", {"raw": adj_reply(5.0)})
    code, rep = run(env, [two_first()], capsys)
    bk = rep["books"][0]
    assert bk["policy"]["trigger"] == "split 0.5" and bk["split"] == 1.5
    assert bk["adjudicator"]["grade"] == 5.0
    job = (env / "work/comic-1/job-adjudicator.txt").read_text()
    assert all(f"=== FIRST-PASS SEAT g1-{s} ===" in job for s in "abc")


def test_pre_1980_with_eight_photos_dispatches_three_up_front(env, capsys):
    for s in "abc":
        plan(env, f"g1-{s}", {"grades": {"101": 6.0}, "conf": "HIGH"})
    code, rep = run(env, [two_first(comic="Hulk #180 (1974)")], capsys, photos=8)
    pol = rep["books"][0]["policy"]
    assert pol["upfront"] and pol["third"] and pol["trigger"] == "pre-1980 (1974) with 8 photos"
    assert ran(env, "g1-c") and len(rep["usage"]) == 3


def test_upfront_needs_both_age_and_photos(env, capsys):
    for n, comic, photos in ((1, "Hulk #180 (1974)", 7), (2, "Spawn #1 (1992)", 12)):
        b = two_first(n, comic=comic)
        b["folder"] = f"t-{n}"
        for f in (env / "fake").glob("*"):
            f.unlink()
        for s in "abc":
            plan(env, f"g{n}-{s}", {"grades": {f"10{n}": 6.0}, "conf": "HIGH"})
        rep = run(env, [b], capsys, photos=photos)[1]
        assert rep["books"][0]["policy"]["third"] is False and not ran(env, f"g{n}-c")
    assert grade_seats.book_year({"comic": "Fantastic Four #29, August 1964, Ungraded"}) == 1964
    assert grade_seats.book_year({"comic": "No year", "year": 1975}) == 1975
    assert grade_seats.book_year({"comic": "No year"}) is None


def test_batched_cheap_books_untouched_beside_two_first(env, capsys):
    plan(env, "g1-a", {"grades": {"101": 6.0}, "conf": "HIGH"})
    plan(env, "g1-b", {"grades": {"101": 6.0}, "conf": "HIGH"})
    plan(env, "gb", {"grades": {"102": 4.0, "103": 3.0}})
    books = [two_first(),
             {"item_id": "102", "comic": "Two (1970)", "folder": "comic-2", "seats": ["gb"], "batch": "x"},
             {"item_id": "103", "comic": "Three (1970)", "folder": "comic-3", "seats": ["gb"], "batch": "x"}]
    code, rep = run(env, books, capsys, photos=9)
    assert code == 0 and [b["policy"] for b in rep["books"][1:]] == [None, None]
    assert [b["seats"][0]["grade"] for b in rep["books"][1:]] == [4.0, 3.0]
    assert rep["books"][0]["policy"]["upfront"]  # 1970 + 9 photos: three at once


def test_two_first_spec_validation(env):
    bad = [dict(two_first(2), seats=["x", "y"]), dict(two_first(3), batch="b"),
           dict(book(4), policy="three-always")]
    for b in bad:
        p = spec(env, [b])
        assert grade_seats.main([str(p), "--grader-agent", str(env / "agent.md")]) == 2


def test_conf_label_and_has_cap():
    assert grade_seats.conf_label("**MEDIUM–LOW**: two photos") == "MEDIUM-LOW"
    assert grade_seats.conf_label("Medium") == "MEDIUM" and grade_seats.conf_label("unsure") == ""
    assert grade_seats.conf_label("MEDIUMLOW") == "MEDIUM-LOW" and grade_seats.conf_label("Medium / High") == "MEDIUM-HIGH"
    assert not grade_seats.has_cap({"cap": "None (no capping defect seen)"})
    assert grade_seats.has_cap({"cap": "detached cover caps at 2.0"})
    assert "confidence unreadable (s)" in grade_seats.third_seat_triggers(
        {"s": {"grade": 5.0, "confidence": "unsure", "cap": "none"}, "t": None})
