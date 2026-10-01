"""grade-seats: run every /comic:grade headless seat in one process (BUI-1095).

Reads a seat spec, runs ``grade-crops`` once per book, writes each seat's job
text, launches every seat as a parallel ``claude -p`` process, retries a failed
seat once, runs the BUI-1090 second pass for books whose seats split by 1.0 or
more, and prints one compact block per book plus the per-seat usage table.

Spec JSON::

    {"workdir": "/tmp/comic-grading/run-x",
     "books": [{"item_id": "123", "comic": "Fantastic Four #48 (1966)",
                "folder": "comic-1",              # relative to workdir, or absolute
                "seats": ["grader-c1-a", "grader-c1-b", "grader-c1-c"],
                "seller_grade": null,             # or "VF 8.0"
                "batch": null}]}                  # same string = one shared seat

Books that share a ``batch`` value are graded by one seat (the first book's
first seat name); a batch is first-pass only, with no second pass.
"""

import argparse
import concurrent.futures
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

MODEL = "claude-fable-5-1"
HARNESS = (
    "HARNESS: SendMessage is unavailable in this run. Your final message is your "
    "report: end with the OUTPUT FORMAT block(s) and write nothing after them."
)
SPLIT_THRESHOLD = 1.0
SEAT_TIMEOUT = float(os.environ.get("GRADE_SEATS_TIMEOUT", "900"))
REQUIRED = ("GRADE:", "GRADE RANGE:", "CONFIDENCE:")


def _version_string() -> str:
    try:
        pkg_version = importlib.metadata.version("ebay-tools")
    except importlib.metadata.PackageNotFoundError:
        pkg_version = "unknown"
    try:
        from _ebay_build_stamp import GIT_DATE, GIT_SHA
    except ImportError:
        GIT_SHA, GIT_DATE = "unknown", "unknown"
    return f"grade-seats {pkg_version} (git {GIT_SHA}, {GIT_DATE})"


# ---------- envelope parsing ----------

def _field(text, name):
    m = re.search(rf"^{re.escape(name)}:[ \t]*(.*)$", text, re.M)
    return m.group(1).strip() if m else ""


def _scrub(line):
    """Mask decimal grade numbers so a defect line never anchors another seat."""
    return re.sub(r"\b\d{1,2}\.\d\b", "[grade]", line)


def parse_block(text):
    """Parse one OUTPUT FORMAT block; None when a required field is missing."""
    if not all(re.search(rf"^{re.escape(f)}", text, re.M) for f in REQUIRED):
        return None
    m = re.search(r"^GRADE:[ \t]*([0-9]+(?:\.[0-9]+)?)", text, re.M)
    if not m:
        return None
    rationale = _field(text, "RATIONALE")
    sentence = re.split(r"(?<=[.!?])\s", rationale, maxsplit=1)[0]
    cap = _field(text, "GRADE CAP")
    parts = [x for x in (cap if cap.lower() != "none" else "", sentence) if x]
    conf = _field(text, "CONFIDENCE")
    return {
        "grade": float(m.group(1)),
        "range": _field(text, "GRADE RANGE"),
        "confidence": re.split(r"[:\s]", conf, maxsplit=1)[0] if conf else "",
        "cap": cap,
        "defect": _scrub("; ".join(parts))[:240],
        "limits": _field(text, "PHOTO LIMITATIONS")[:240],
    }


def split_books(result, item_ids):
    """Split a batched result into per-book text by each item id's first mention."""
    if len(item_ids) == 1:
        return {item_ids[0]: result}
    found = sorted((result.find(i), i) for i in item_ids if i in result)
    out = {i: "" for i in item_ids}
    for n, (p, i) in enumerate(found):
        end = found[n + 1][0] if n + 1 < len(found) else len(result)
        out[i] = result[p:end]
    return out


def parse_envelope(path, item_ids):
    """Return ({item_id: parsed or None}, usage dict, error string)."""
    usage = {"turns": 0, "out": 0, "cache_read": 0, "cache_create": 0}
    none = {i: None for i in item_ids}
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        return none, usage, f"unreadable envelope ({e})"
    if not isinstance(d, dict):
        return none, usage, "envelope is not an object"
    models = [m for m in (d.get("modelUsage") or {}).values() if isinstance(m, dict)]
    usage = {
        "turns": d.get("num_turns") or 0,
        "out": sum(m.get("outputTokens", 0) for m in models),
        "cache_read": sum(m.get("cacheReadInputTokens", 0) for m in models),
        "cache_create": sum(m.get("cacheCreationInputTokens", 0) for m in models),
    }
    if d.get("is_error"):
        return none, usage, str(d.get("subtype") or "is_error")
    texts = split_books(d.get("result") or "", item_ids)
    parsed = {i: parse_block(texts[i]) for i in item_ids}
    err = "" if all(parsed.values()) else "no OUTPUT FORMAT block"
    return parsed, usage, err


# ---------- job text ----------

def book_block(book, seat, crops_shared):
    folder = book["folder"]
    imgs = sorted(p.name for p in Path(folder).glob("img-*.jpg"))
    lines = [
        f"ITEM ID: {book['item_id']}",
        f"COMIC: {book['comic']}",
        f"IMAGE FOLDER: {folder}",
        f"CROP DIRECTORY: {folder}/crops-{seat}",
    ]
    if crops_shared:
        lines.append("SHARED CROPS:")
        lines += [f"  {p}" for p in crops_shared]
    lines.append(f"IMAGES: {imgs[0]} through {imgs[-1]} ({len(imgs)} photos)" if imgs else "IMAGES: none")
    lines.append(f"SELLER-STATED GRADE: {book.get('seller_grade') or 'none stated'}")
    return "\n".join(lines)


def build_job(books, seat, shared, cross=None):
    text = "\n\n".join(book_block(b, seat, shared.get(b["item_id"])) for b in books) + "\n\n"
    if cross:
        text += (
            "CROSS-EXAMINATION (second pass): other graders of this book saw the defects "
            "below. Read their crops, then re-grade from all the evidence. Change your grade "
            "only for a defect you can confirm in a photo or crop; printed art, glare, and "
            "lint are not defects. Return a full OUTPUT FORMAT block.\n" + cross + "\n\n"
        )
    return text + HARNESS + "\n"


def cross_text(seat, firsts, xexam):
    """One line per other seat: its defect line (no grade number) plus crop paths."""
    out = []
    for other, parsed in firsts.items():
        if other != seat:
            crops = ", ".join(xexam.get(other, [])) or "none"
            out.append(f"- {other}: {parsed['defect'] or 'no named defect'}. Crops: {crops}")
    return "\n".join(out)


def copy_crops(folder, seat):
    """Copy a seat's ad hoc crops to xexam/ so the second pass cannot overwrite them."""
    dest = Path(folder) / "xexam"
    dest.mkdir(exist_ok=True)
    out = []
    for p in sorted((Path(folder) / f"crops-{seat}").glob("crop-*.jpg")):
        target = dest / f"{seat}-{p.name}"
        shutil.copy2(p, target)
        out.append(str(target))
    return out


# ---------- process running ----------

def claude_cmd(workdir):
    return [
        os.environ.get("GRADE_SEATS_CLAUDE", "claude"), "-p", "--model", MODEL,
        "--system-prompt-file", str(workdir / "grader-body.md"),
        "--tools", "Read,Bash", "--strict-mcp-config",
        "--mcp-config", str(workdir / "empty-mcp.json"),
        "--max-turns", "8", "--output-format", "json",
    ]


def run_seat(workdir, folder, seat, suffix, job):
    """Run one seat process. Returns (envelope path, error string)."""
    folder = Path(folder)
    job_path = folder / f"job-{seat}{suffix}.txt"
    env_path = folder / f"seat-{seat}{suffix}.json"
    err_path = folder / f"seat-{seat}{suffix}.stderr"
    job_path.write_text(job)
    err = ""
    try:
        with open(job_path) as fin, open(env_path, "w") as fout, open(err_path, "w") as ferr:
            proc = subprocess.Popen(claude_cmd(workdir), stdin=fin, stdout=fout, stderr=ferr)
            try:
                code = proc.wait(timeout=SEAT_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                code, err = None, f"timeout after {SEAT_TIMEOUT:.0f}s"
    except OSError as e:
        return str(env_path), f"launch failed ({e})"
    if code:
        tail = err_path.read_text().strip().splitlines()
        err = f"exit {code}" + (f": {tail[-1][:160]}" if tail else "")
    return str(env_path), err


def run_units(workdir, units, suffix, log):
    """Run units (seat, books, job) in parallel, retrying each failure once.

    Returns {(seat, first folder): {item_id: parsed or None}}. Every attempt,
    including a failed one, appends (unit, suffix, usage, error) to ``log``.
    """
    def attempt(unit, sfx):
        seat, books, job = unit
        env, run_err = run_seat(workdir, books[0]["folder"], seat, sfx, job)
        parsed, usage, perr = parse_envelope(env, [b["item_id"] for b in books])
        return unit, sfx, parsed, usage, run_err or perr

    def key(unit):
        return (unit[0], unit[1][0]["folder"])

    def run_all(todo, sfx):
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(todo))) as ex:
            return list(ex.map(lambda u: attempt(u, sfx), todo))

    results, failed = {}, []
    for unit, sfx, parsed, usage, err in run_all(units, suffix):
        log.append((unit, sfx, usage, err))
        results[key(unit)] = parsed
        if not all(parsed.values()):
            failed.append(unit)
    if failed:
        for unit, sfx, parsed, usage, err in run_all(failed, suffix + "-retry"):
            log.append((unit, sfx, usage, err))
            for iid, v in parsed.items():
                if v:
                    results[key(unit)][iid] = v
    return results


# ---------- orchestration ----------

def load_spec(path):
    spec = json.loads(Path(path).read_text())
    workdir = Path(spec["workdir"]).resolve()
    books = []
    for b in spec["books"]:
        b = dict(b)
        folder = Path(b["folder"])
        b["folder"] = str(folder if folder.is_absolute() else workdir / folder)
        b["item_id"] = str(b["item_id"])
        if not b.get("seats"):
            raise ValueError(f"book {b['item_id']}: no seats")
        books.append(b)
    return workdir, books


def shared_crops(books):
    shared = {}
    for b in books:
        try:
            r = subprocess.run(["grade-crops", b["folder"], str(Path(b["folder"]) / "crops-shared")],
                               capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired):
            continue
        paths = [ln for ln in r.stdout.splitlines() if ln.strip()]
        if r.returncode == 0 and paths:
            shared[b["item_id"]] = paths
    return shared


def make_units(books, shared):
    units, batches = [], {}
    for b in books:
        if b.get("batch"):
            batches.setdefault(b["batch"], []).append(b)
            continue
        units += [(seat, [b], build_job([b], seat, shared)) for seat in b["seats"]]
    for group in batches.values():
        seat = group[0]["seats"][0]
        units.append((seat, group, build_job(group, seat, shared)))
    return units


def second_pass(workdir, book, good, shared, log):
    """Cross-examine one split book. Returns ({seat: parsed+p1}, failed seats)."""
    xexam = {s: copy_crops(book["folder"], s) for s in good}
    units = [(s, [book], build_job([book], s, shared, cross_text(s, good, xexam))) for s in good]
    p2, failed = {}, []
    for (s, _), parsed in run_units(workdir, units, "-p2", log).items():
        v = parsed[book["item_id"]]
        if v:
            p2[s] = dict(v, p1=good[s]["grade"])
        else:
            failed.append(s)
    return p2, failed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="grade-seats",
        description="Run every /comic:grade headless grader seat in one call: shared crops, "
        "parallel claude -p seats, one retry per failed seat, a second pass on a 1.0+ split. "
        "Prints one compact block per book and the per-seat usage table.",
    )
    parser.add_argument("spec", type=Path, help="seat spec JSON (see the module docstring)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--grader-agent", type=Path, default=Path(".claude/agents/comic-grader.md"),
                        help="grader agent definition (default: run from the repo root)")
    parser.add_argument("--version", action="version", version=_version_string())
    args = parser.parse_args(argv)

    try:
        workdir, books = load_spec(args.spec)
        lines = args.grader_agent.read_text().splitlines()
    except (OSError, ValueError, KeyError) as e:
        print(f"grade-seats: {e}", file=sys.stderr)
        return 2
    for b in books:
        if not Path(b["folder"]).is_dir():
            print(f"grade-seats: not a directory: {b['folder']}", file=sys.stderr)
            return 2
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "empty-mcp.json").write_text('{"mcpServers":{}}\n')
    if lines and lines[0].strip() == "---":
        end = next((n for n in range(1, len(lines)) if lines[n].strip() == "---"), 0)
        lines = lines[end + 1:]
    (workdir / "grader-body.md").write_text("\n".join(lines) + "\n")

    shared = shared_crops(books)
    log = []  # (unit, suffix, usage, error), one entry per process attempt
    first = run_units(workdir, make_units(books, shared), "", log)

    report = {"books": [], "usage": []}
    for b in books:
        iid = b["item_id"]
        seat_names = b["seats"][:1] if b.get("batch") else b["seats"]
        # a batched book's seat ran under the batch's first book folder
        home = next(x["folder"] for x in books if x.get("batch") == b["batch"]) if b.get("batch") else b["folder"]
        res = {s: first[(s, home)][iid] for s in seat_names
               if (s, home) in first and first[(s, home)].get(iid)}
        info = {"p2": False, "split_before": None, "split_after": None, "p2_failed": []}
        grades = [v["grade"] for v in res.values()]
        if not b.get("batch") and len(grades) >= 2 and max(grades) - min(grades) >= SPLIT_THRESHOLD:
            info["split_before"] = max(grades) - min(grades)
            p2, info["p2_failed"] = second_pass(workdir, b, res, shared, log)
            info["p2"] = True
            res.update(p2)
            g2 = [v["grade"] for v in res.values()]
            info["split_after"] = max(g2) - min(g2)
        seats = [dict(res[s], seat=s) if s in res else {"seat": s, "failed": True} for s in seat_names]
        report["books"].append({"item_id": iid, "comic": b["comic"], "seats": seats, **info})
    for unit, sfx, usage, err in log:
        report["usage"].append({"book": unit[1][0]["comic"], "seat": unit[0],
                                "pass": 2 if "-p2" in sfx else 1, "retry": sfx.endswith("-retry"),
                                "error": err, **usage})

    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0 if all(any(not s.get("failed") for s in bk["seats"]) for bk in report["books"]) else 1


def render(report):
    out = []
    for bk in report["books"]:
        out.append(f"### {bk['comic']} — {bk['item_id']}")
        for i, s in enumerate(bk["seats"]):
            tag = chr(ord("A") + i)
            if s.get("failed"):
                out.append(f"{tag} {s['seat']}: FAILED (no valid block after retry)")
                continue
            p1 = f" (p1 {s['p1']})" if "p1" in s else ""
            out.append(f"{tag} {s['seat']}: {s['grade']}{p1} | {s['range']} | {s['confidence']} | "
                       f"cap: {s['cap'] or 'none'} | defect: {s['defect']}")
        returned = sum(1 for s in bk["seats"] if not s.get("failed"))
        out.append(f"Seats: {returned} of {len(bk['seats'])} returned")
        if bk["p2"]:
            note = f"Cross-examined: split {bk['split_before']:.1f} → {bk['split_after']:.1f}"
            if bk["p2_failed"]:
                note += f" (second pass failed, first-pass grade kept: {', '.join(bk['p2_failed'])})"
            out.append(note)
        else:
            out.append("Cross-examined: no")
        limits = next((s["limits"] for s in bk["seats"] if s.get("limits")), "")
        if limits:
            out.append(f"Caveats: {limits}")
        out.append("")
    out += ["| Book | Seat | Pass | Turns | Output | Cache-read | Cache-create |",
            "|------|------|------|-------|--------|------------|--------------|"]
    tot = {"turns": 0, "out": 0, "cache_read": 0, "cache_create": 0}
    for u in report["usage"]:
        label = f"{u['pass']}" + (" retry" if u["retry"] else "") + (f" ({u['error']})" if u["error"] else "")
        out.append(f"| {u['book']} | {u['seat']} | {label} | {u['turns']} | {u['out']:,} | "
                   f"{u['cache_read']:,} | {u['cache_create']:,} |")
        for k in tot:
            tot[k] += u[k]
    out.append(f"| **Total** | | | **{tot['turns']}** | **{tot['out']:,}** | "
               f"**{tot['cache_read']:,}** | **{tot['cache_create']:,}** |")
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
