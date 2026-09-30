#!/usr/bin/env python3
"""CGC cert harvest scaffold (BUI-1016).

STOPPED BY POLICY: CGC's Terms of Website Use forbid "any robot, spider, or
other automatic device, process, or means to access the Website for any
purpose, including monitoring or copying any of the material" (see the .md
next to this file). So `lookup` refuses to run unless CGC_WRITTEN_CONSENT=1
is set by a human who holds CGC's written consent. The zero-CGC-contact
subcommands (`enumerate`, `extract`, `report`) ran and produced the counts
in the doc. No Browse calls, no ledger writes.

    uv run --project plugins/gixen-overlay python docs/audit/2026-09-30-cgc-cert-harvest.py enumerate
    ... extract     # certs from ledger titles + the 50 holdout slabs' listing text (on disk)
    ... report      # counts -> stdout
    ... lookup <cert>...   # refuses without CGC_WRITTEN_CONSENT=1

Fixture: ~/comic-grader-fixtures/cgc-certs/ (no binaries in the repo).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "fmv" / "src"))
import fmv_math  # noqa: E402

DB = os.path.expanduser("~/.comics-server/db.sqlite")
FIX = Path(os.path.expanduser("~/comic-grader-fixtures/cgc-certs"))
HOLDOUT = Path(os.path.expanduser("~/comic-grader-fixtures/bui-1006-holdout"))
PHOTO_WINDOW_DAYS = 90
LOOKUP_PACE_S = 20
MAX_LOOKUPS = 300
BLOCK_BANNER = "exceeded our limits"

# CGC certs are 10 digits, printed NNNNNNN-NNN on the label. Bare runs must not
# touch other digits or dashes, so 12-digit eBay item ids and phone numbers
# do not match. 'ctx' records whether a cert keyword sits within 25 chars.
CERT_RE = re.compile(r"(?<![\d-])(\d{7})-?(\d{3})(?![\d-])")
CTX_RE = re.compile(r"cert|cgc|#", re.I)


def find_certs(text: str) -> list[dict]:
    out, seen = [], set()
    for m in CERT_RE.finditer(text or ""):
        cert = m.group(1) + m.group(2)
        if cert in seen:
            continue
        seen.add(cert)
        near = text[max(0, m.start() - 25):m.start()]
        out.append({"cert": cert, "dashed": "-" in m.group(0), "ctx": bool(CTX_RE.search(near))})
    return out


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def slab_comps() -> list[dict]:
    rows = _conn().execute(
        "SELECT provider, product_id, title, sold_date, grade, certifier, label FROM comps "
        "WHERE excluded_at IS NULL AND certifier != 'none' ORDER BY id"
    ).fetchall()
    out = []
    for r in rows:
        d = fmv_math._parse_sold_date(r["sold_date"])
        out.append({**dict(r), "d": d.isoformat() if d else None})
    return out


def cmd_enumerate() -> dict:
    cutoff = (date.today() - timedelta(days=PHOTO_WINDOW_DAYS)).isoformat()
    rows = slab_comps()
    pids = {r["product_id"] for r in rows if r["product_id"]}
    win = [r for r in rows if r["d"] and r["d"] >= cutoff]
    win_pids = {r["product_id"] for r in win if r["product_id"]}
    return {"slab_rows": len(rows), "distinct_listings": len(pids), "undated": sum(r["d"] is None for r in rows),
            "in_photo_window_rows": len(win), "in_photo_window_listings": len(win_pids), "cutoff": cutoff}


def cmd_extract() -> dict:
    """Certs from (a) ledger slab titles, (b) holdout slab listing text on disk."""
    res: dict = {"ledger_title": {}, "holdout_text": {}}
    for r in slab_comps():
        for c in find_certs(r["title"]):
            res["ledger_title"][c["cert"]] = {"product_id": r["product_id"], **c}
    man = json.loads((HOLDOUT / "manifest.json").read_text())
    n_slab = 0
    for c in man["comps"]:
        if c.get("pool") != "slab":
            continue
        n_slab += 1
        p = HOLDOUT / "full" / str(c["product_id"]) / "item.json"
        if not p.exists():
            continue
        j = json.loads(p.read_text())
        text = " ".join(str(j.get(k) or "") for k in ("title", "conditionDescription", "description"))
        for f in find_certs(text):
            res["holdout_text"][f["cert"]] = {"product_id": c["product_id"], "grade_listing": c["grade"], **f}
    res["holdout_slabs"] = n_slab
    FIX.mkdir(parents=True, exist_ok=True)
    (FIX / "extracted.json").write_text(json.dumps(res, indent=1))
    return res


def parse_cert_page(md: str) -> dict | None:
    """Parse a CGC certlookup markdown page. The 'exceeded our limits' banner
    also appears on pages that DO carry data, so a block is 'no Title field',
    never 'banner present'. Returns None when the page has no cert data."""
    m = re.search(r"Title(.+?)\s*\nIssue(\S*)Issue Date(\S*)Issue Year(\d*)Publisher(.*?)\n", md)
    g = re.search(r"Grade\s*\n\s*([\d.]+)\s*Page Quality(.*?)Grade Date(\S+)Label Category(\S+)", md, re.S)
    if not (m and g):
        return None
    notes = re.search(r"\*\*Grader Notes\*\*(.*?)\n- \[Total Graded", md, re.S)
    lines = [ln.strip() for ln in notes.group(1).splitlines() if ln.strip()] if notes else []
    return {"title": m.group(1).strip(), "issue": m.group(2), "issue_year": m.group(4),
            "publisher": m.group(5).strip(), "grade": float(g.group(1)), "page_quality": g.group(2).strip(),
            "grade_date": g.group(3), "label": g.group(4), "grader_notes": lines}


def is_blocked(md: str) -> bool:
    return BLOCK_BANNER in md and parse_cert_page(md) is None


def cmd_lookup(certs: list[str]) -> None:
    if os.environ.get("CGC_WRITTEN_CONSENT") != "1":
        sys.exit("refused: CGC Terms of Use forbid automated access (see 2026-09-30-cgc-cert-harvest.md). "
                 "Set CGC_WRITTEN_CONSENT=1 only with CGC's written consent.")
    FIX.mkdir(parents=True, exist_ok=True)
    blocks, done = 0, 0
    for cert in certs:
        if (FIX / f"{cert}.json").exists():  # resume
            continue
        if done >= MAX_LOOKUPS:
            break
        tmp = FIX / f".{cert}.md"
        subprocess.run(["firecrawl", "scrape", f"https://www.cgccomics.com/certlookup/{cert}/",
                        "--only-main-content", "--wait-for", "4000", "-o", str(tmp)], check=False,
                       capture_output=True)
        md = tmp.read_text() if tmp.exists() else ""
        tmp.unlink(missing_ok=True)
        done += 1
        page = parse_cert_page(md)
        if page is None and is_blocked(md):
            blocks += 1
            if blocks >= 2:
                print("second block, ending the day", flush=True)
                return
            print("blocked, backing off 10 min", flush=True)
            time.sleep(600)
            continue
        (FIX / f"{cert}.json").write_text(json.dumps({"cert": cert, "cgc": page}, indent=1))
        time.sleep(LOOKUP_PACE_S)


def _counts(res: dict) -> dict:
    return {k: (len(v) if isinstance(v, dict) else v) for k, v in res.items()}


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "enumerate":
        print(json.dumps(cmd_enumerate(), indent=1))
    elif cmd == "extract":
        print(json.dumps(_counts(cmd_extract()), indent=1))
    elif cmd == "report":
        print(json.dumps({"enumerate": cmd_enumerate(), "extract": _counts(cmd_extract())}, indent=1))
    elif cmd == "lookup":
        cmd_lookup(sys.argv[2:])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
