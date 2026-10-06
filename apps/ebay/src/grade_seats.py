"""grade-seats: run every /comic:grade headless seat in one process (BUI-1095).

Reads a seat spec, runs ``grade-crops`` once per book, writes each seat's job
text, launches every seat as a parallel ``claude -p`` process, retries a failed
seat once, dispatches a two-first book's third seat only when a trigger fires
(BUI-1098), runs one adjudicator seat (BUI-1100) for each book whose seats split
by 1.0 or more, and prints one compact block per book plus the usage table.

Spec JSON::

    {"workdir": "/tmp/comic-grading/run-x",
     "books": [{"item_id": "123", "comic": "Fantastic Four #48 (1966)",
                "folder": "comic-1",              # relative to workdir, or absolute
                "seats": ["grader-c1-a", "grader-c1-b", "grader-c1-c"],
                "seller_grade": null,             # or "VF 8.0"
                "policy": "two-first",            # optional; needs exactly 3 seats
                "batch": null}]}                  # same string = one shared seat

Books that share a ``batch`` value are graded by one seat (the first book's
first seat name); a batch is first-pass only, with no adjudicator.

A ``two-first`` book (BUI-1098) runs its first two seats, then its third only
when a trigger fires: the two differ by 0.5 or more, either names a GRADE CAP,
either reports confidence at or below MEDIUM-LOW, or a seat failed. A pre-1980
book with eight or more photos is known up front and gets all three at once.
Two seats that agree give the lower grade and the union of their ranges.
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
ADJ_HARNESS = (
    "HARNESS: SendMessage is unavailable in this run. Your final message is your "
    "report: the RECONCILED BLOCK line, one OUTPUT FORMAT block, the SEAT FINDINGS "
    "line and its seat lines, and nothing after them."
)
ADJUDICATOR = "adjudicator"
SPLIT_THRESHOLD = 1.0
SEAT_TIMEOUT = float(os.environ.get("GRADE_SEATS_TIMEOUT", "900"))
REQUIRED = ("GRADE:", "GRADE RANGE:", "CONFIDENCE:")
TWO_FIRST = "two-first"
AGREE_BAND = 0.5        # two seats this far apart or more dispatch the third
UPFRONT_BEFORE = 1980   # a book older than this ...
UPFRONT_PHOTOS = 8      # ... with this many photos or more gets three seats at once
CONF_RANK = {"HIGH": 4, "MEDIUM-HIGH": 3.5, "MEDIUM": 3, "MEDIUM-LOW": 2, "LOW": 1}


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


def conf_label(text):
    """Normalize a CONFIDENCE field to a CONF_RANK key, or "" when unrecognized."""
    t = re.sub(r"[*_`]", "", text or "").upper()
    m = re.match(r"\s*(HIGH|MEDIUM[\s\-\u2010-\u2015/]*HIGH|MEDIUM[\s\-\u2010-\u2015/]*LOW|MEDIUM|LOW)\b", t)
    if not m:
        return ""
    return "-".join(re.findall(r"HIGH|MEDIUM|LOW", m.group(1)))


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
        "confidence": conf_label(conf) or (re.split(r"[:\s]", conf, maxsplit=1)[0] if conf else ""),
        "cap": cap,
        "defect": _scrub("; ".join(parts))[:240],
        "limits": _field(text, "PHOTO LIMITATIONS")[:240],
        "raw": text.strip(),
    }


def parse_adjudication(text):
    """Parse the adjudicator's reply: its reconciled block plus per-seat findings.

    The reply may quote the seats' blocks, so the reconciled block is the text after
    the LAST ``RECONCILED BLOCK`` marker, up to ``SEAT FINDINGS``. Without a marker
    the reply must hold exactly one GRADE: line, or it is ambiguous and fails.
    """
    marks = list(re.finditer(r"^[ \t*#]*RECONCILED BLOCK\b.*$", text, re.M))
    body = text[marks[-1].end():] if marks else text
    if not marks and len(re.findall(r"^GRADE:", text, re.M)) != 1:
        return None
    parts = re.split(r"^[ \t*#]*SEAT FINDINGS\b.*$", body, maxsplit=1, flags=re.M)
    block = parse_block(parts[0])
    if not block:
        return None
    tail = parts[1] if len(parts) > 1 else ""
    block["findings"] = [ln.strip().lstrip("-* ").strip()[:400]
                         for ln in tail.splitlines() if ln.strip().startswith(("-", "*"))]
    return block


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


def parse_envelope(path, item_ids, parse=parse_block):
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
    parsed = {i: parse(texts[i]) for i in item_ids}
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


def build_job(books, seat, shared):
    text = "\n\n".join(book_block(b, seat, shared.get(b["item_id"])) for b in books) + "\n\n"
    return text + HARNESS + "\n"


def adjudication_job(book, firsts, xexam, shared):
    """One adjudicator job: every first-pass seat's full block and crops (BUI-1100)."""
    seats = ", ".join(firsts)
    out = [book_block(book, ADJUDICATOR, shared.get(book["item_id"])), "",
           f"ADJUDICATION: the first-pass graders of this book ({seats}) split by "
           f"{SPLIT_THRESHOLD:.1f} or more. You are the adjudicator, not another independent "
           "seat. Their full OUTPUT FORMAT blocks and the ad hoc crops each one made follow. "
           "Do not re-survey every photo: in one parallel turn Read every seat crop listed "
           "below plus the shared crops and photos that show the disputed defects, then at "
           "most one ad hoc crop round into your CROP DIRECTORY. Test each defect claim that "
           "moves a seat's grade or cap. Confirm a claim only when you can see it in a named "
           "photo or crop; printed art, glare, and lint are not defects. A claim nobody can "
           "see either way is coverage: widen GRADE RANGE and lower CONFIDENCE, never "
           "average the grades.",
           "Reply in exactly this shape, nothing before or after:",
           "RECONCILED BLOCK",
           "<one full OUTPUT FORMAT block for this book>",
           "SEAT FINDINGS",
           "- <seat name>: confirmed <claims, each with its photo or crop>; rejected "
           "<claims> because <why>",
           f"(one line per seat: {seats})", ""]
    for seat, parsed in firsts.items():
        crops = xexam.get(seat) or []
        out.append(f"=== FIRST-PASS SEAT {seat} ===")
        out.append(f"SEAT CROPS ({seat}):")
        out += [f"  {c}" for c in crops] or ["  none (this seat made no ad hoc crops)"]
        out.append(f"SEAT BLOCK ({seat}):")
        out.append(parsed.get("raw") or "(block text unavailable)")
        out.append(f"=== END SEAT {seat} ===")
        out.append("")
    return "\n".join(out) + "\n" + ADJ_HARNESS + "\n"


def copy_crops(folder, seat):
    """Copy a seat's ad hoc crops to xexam/ so the adjudicator reads a stable copy."""
    dest = Path(folder) / "xexam"
    dest.mkdir(exist_ok=True)
    out = []
    for p in sorted((Path(folder) / f"crops-{seat}").glob("crop-*.jpg")):
        target = dest / f"{seat}-{p.name}"
        shutil.copy2(p, target)
        out.append(str(target))
    return out


# ---------- two-first policy (BUI-1098) ----------

def book_year(book):
    """The book's year: the spec's ``year``, else the last 19xx/20xx in its comic line."""
    if book.get("year"):
        return int(book["year"])
    years = re.findall(r"\b(1[89]\d\d|20\d\d)\b", book.get("comic") or "")
    return int(years[-1]) if years else None


def photo_count(book):
    return len(list(Path(book["folder"]).glob("img-*.jpg")))


def upfront_trigger(book):
    """The trigger known before any seat runs: pre-1980 with 8+ photos, else None."""
    year, n = book_year(book), photo_count(book)
    if year is not None and year < UPFRONT_BEFORE and n >= UPFRONT_PHOTOS:
        return f"pre-{UPFRONT_BEFORE} ({year}) with {n} photos"
    return None


def has_cap(parsed):
    cap = (parsed.get("cap") or "").strip().strip("*_` ").lower()
    return bool(cap) and not re.match(r"(none|n/?a)\b", cap)


def third_seat_triggers(pair):
    """Every trigger that dispatches the third seat, given {seat: parsed or None}."""
    out = [f"seat {s} failed" for s, p in pair.items() if not p]
    got = {s: p for s, p in pair.items() if p}
    if len(got) == 2:
        grades = [p["grade"] for p in got.values()]
        if abs(grades[0] - grades[1]) >= AGREE_BAND:
            out.append(f"split {abs(grades[0] - grades[1]):.1f}")
    for s, p in got.items():
        if has_cap(p):
            out.append(f"grade cap ({s})")
        rank = CONF_RANK.get(p.get("confidence") or "")
        if rank is None:
            out.append(f"confidence unreadable ({s})")
        elif rank <= CONF_RANK["MEDIUM-LOW"]:
            out.append(f"confidence {p['confidence']} ({s})")
    return out


def _range_bounds(text):
    return [float(x) for x in re.findall(r"\b(\d{1,2}\.\d)\b", text or "")]


def two_seat_consensus(a, b):
    """Two agreeing seats: the lower grade, the union of ranges, the lower confidence."""
    nums = _range_bounds(a["range"]) + _range_bounds(b["range"]) + [a["grade"], b["grade"]]
    lo, hi = min(nums), max(nums)
    conf = min((a["confidence"], b["confidence"]), key=lambda c: CONF_RANK.get(c, 0))
    return {"grade": min(a["grade"], b["grade"]),
            "range": f"{lo:.1f}" if lo == hi else f"{lo:.1f}-{hi:.1f}",
            "confidence": conf}


# ---------- process running ----------

def claude_cmd(workdir):
    workdir = Path(workdir).resolve()
    exe = os.environ.get("GRADE_SEATS_CLAUDE", "claude")
    if os.sep in exe:
        exe = str(Path(exe).resolve())  # a relative path must survive the cwd change
    return [
        exe, "-p", "--model", MODEL,
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
            # cwd=workdir (outside the repo, BUI-1177): a seat launched from the repo
            # root auto-loads CLAUDE.md + MEMORY.md, ~17k extra tokens per API call.
            proc = subprocess.Popen(claude_cmd(workdir), stdin=fin, stdout=fout, stderr=ferr,
                                    cwd=Path(workdir).resolve())
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


def run_units(workdir, units, suffix, log, parse=parse_block):
    """Run units (seat, books, job) in parallel, retrying each failure once.

    Returns {(seat, first folder): {item_id: parsed or None}}. Every attempt,
    including a failed one, appends (unit, suffix, usage, error) to ``log``.
    """
    def attempt(unit, sfx):
        seat, books, job = unit
        env, run_err = run_seat(workdir, books[0]["folder"], seat, sfx, job)
        parsed, usage, perr = parse_envelope(env, [b["item_id"] for b in books], parse)
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
        if b.get("policy") not in (None, TWO_FIRST):
            raise ValueError(f"book {b['item_id']}: unknown policy {b['policy']!r}")
        if b.get("policy") == TWO_FIRST and (b.get("batch") or len(b["seats"]) != 3):
            raise ValueError(f"book {b['item_id']}: {TWO_FIRST} needs exactly 3 seats and no batch")
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


def first_seats(book):
    """The seats of the first dispatch: a two-first book runs two unless a trigger is known up front."""
    if book.get("policy") == TWO_FIRST and not upfront_trigger(book):
        return book["seats"][:2]
    return book["seats"]


def make_units(books, shared):
    units, batches = [], {}
    for b in books:
        if b.get("batch"):
            batches.setdefault(b["batch"], []).append(b)
            continue
        units += [(seat, [b], build_job([b], seat, shared)) for seat in first_seats(b)]
    for group in batches.values():
        seat = group[0]["seats"][0]
        units.append((seat, group, build_job(group, seat, shared)))
    return units


def adjudicate(workdir, book, good, shared, log):
    """Run one adjudicator seat for a split book; its parsed block, or None (BUI-1100)."""
    xexam = {s: copy_crops(book["folder"], s) for s in good}
    unit = (ADJUDICATOR, [book], adjudication_job(book, good, xexam, shared))
    res = run_units(workdir, [unit], "", log, parse=parse_adjudication)
    return res[(ADJUDICATOR, book["folder"])][book["item_id"]]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="grade-seats",
        description="Run every /comic:grade headless grader seat in one call: shared crops, "
        "parallel claude -p seats, one retry per failed seat, a two-first third seat only on a trigger, "
        "one adjudicator seat on a 1.0+ split. "
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

    # two-first (BUI-1098): read the first two seats, dispatch the third where a trigger fires
    policy, third = {}, []
    for b in books:
        if b.get("policy") != TWO_FIRST:
            continue
        up = upfront_trigger(b)
        if up:
            policy[b["item_id"]] = {"name": TWO_FIRST, "upfront": True, "trigger": up,
                                    "third": True, "consensus": None}
            continue
        pair = {s: first.get((s, b["folder"]), {}).get(b["item_id"]) for s in b["seats"][:2]}
        trig = third_seat_triggers(pair)
        policy[b["item_id"]] = {"name": TWO_FIRST, "upfront": False, "trigger": "; ".join(trig) or None,
                                "third": bool(trig),
                                "consensus": None if trig else two_seat_consensus(*pair.values())}
        if trig:
            third.append((b["seats"][2], [b], build_job([b], b["seats"][2], shared)))
    if third:
        first.update(run_units(workdir, third, "", log))

    report = {"books": [], "usage": []}
    for b in books:
        iid = b["item_id"]
        pol = policy.get(iid)
        seat_names = (b["seats"][:1] if b.get("batch")
                      else b["seats"][:2] if pol and not pol["third"] else b["seats"])
        # a batched book's seat ran under the batch's first book folder
        home = next(x["folder"] for x in books if x.get("batch") == b["batch"]) if b.get("batch") else b["folder"]
        res = {s: first[(s, home)][iid] for s in seat_names
               if (s, home) in first and first[(s, home)].get(iid)}
        info = {"split": None, "adjudicator": None, "adjudicator_failed": False, "policy": pol}
        grades = [v["grade"] for v in res.values()]
        if not b.get("batch") and len(grades) >= 2 and max(grades) - min(grades) >= SPLIT_THRESHOLD:
            info["split"] = max(grades) - min(grades)
            adj = adjudicate(workdir, b, res, shared, log)
            if adj:
                info["adjudicator"] = {k: v for k, v in adj.items() if k != "raw"}
            else:
                info["adjudicator_failed"] = True
        seats = [dict({k: v for k, v in res[s].items() if k != "raw"}, seat=s)
                 if s in res else {"seat": s, "failed": True} for s in seat_names]
        report["books"].append({"item_id": iid, "comic": b["comic"], "seats": seats, **info})
    for unit, sfx, usage, err in log:
        report["usage"].append({"book": unit[1][0]["comic"], "seat": unit[0],
                                "pass": "adj" if unit[0] == ADJUDICATOR else 1,
                                "retry": sfx.endswith("-retry"),
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
            out.append(f"{tag} {s['seat']}: {s['grade']} | {s['range']} | {s['confidence']} | "
                       f"cap: {s['cap'] or 'none'} | defect: {s['defect']}")
        adj = bk["adjudicator"]
        if adj:
            out.append(f"ADJ {ADJUDICATOR}: {adj['grade']} | {adj['range']} | {adj['confidence']} | "
                       f"cap: {adj['cap'] or 'none'} | defect: {adj['defect']}")
            out += [f"  - {f}" for f in adj["findings"]] or ["  (no seat findings returned)"]
        returned = sum(1 for s in bk["seats"] if not s.get("failed"))
        out.append(f"Seats: {returned} of {len(bk['seats'])} returned")
        pol = bk.get("policy")
        if pol and pol["third"]:
            when = "dispatched up front" if pol["upfront"] else "third seat dispatched"
            out.append(f"Two-first: {when} (trigger: {pol['trigger']})")
        elif pol:
            c = pol["consensus"]
            out.append("Two-first: agreed, third seat skipped; two-seat consensus "
                       f"{c['grade']} | {c['range']} | {c['confidence']}")
        if adj:
            out.append(f"Adjudicated: split {bk['split']:.1f}, adjudicator grade {adj['grade']}")
        elif bk["adjudicator_failed"]:
            out.append(f"Adjudicated: split {bk['split']:.1f}, adjudicator FAILED twice, "
                       "first-pass grades kept")
        else:
            out.append("Adjudicated: no")
        src = ([adj] if adj else []) + bk["seats"]
        limits = next((s["limits"] for s in src if s.get("limits")), "")
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
