"""comic-autosnipe: snipe watched eBay auctions unattended (BUI-1340).

The nightly routine in ~/.claude runs this once a night. It is /comic:buy with
every user gate replaced by a fixed skip rule, so a book is sniped only when
every check passes. Anything uncertain is skipped and listed with its reason.

Flow (each step shells out to the same console script /comic:buy uses):

  1. ebay-watchlist --type auction --json   live watched auctions
  2. gixen list --json                      drop items that already have a snipe
  3. timing                                 skip auctions ending within 6 hours
  4. ebay_fetch (import)                    fetch each listing fresh
  5. identify, BIN, defect, duplicate gates
  6. locg collection check-batch            skip owned, flagged, or stale rows (R11)
  7. grade-photos + grade-seats             photo-grade raw books with no stated
                                            grade or a price above $100
  8. comic-fmv --batch --brief              skip needs-manual, ledger, lookup errors
  9. price and spend-cap gates
 10. gixen build-batch + add-batch --verify (skipped in a dry run)
 11. ebay-watchlist --record                sniped, owned, and defect rows only

Config: KEY=VALUE lines in ~/.config/comic-autosnipe/env (override the path with
AUTOSNIPE_CONFIG). An environment variable wins over the file.

  AUTOSNIPE_DRY_RUN=1       report the snipes it would add and add none
  AUTOSNIPE_SPEND_CAP=300   total max bid for one run, in dollars
  AUTOSNIPE_MIN_HOURS=6     skip auctions ending sooner than this
  AUTOSNIPE_GRADE_OVER=100  photo-grade a stated-grade raw book above this price
  AUTOSNIPE_GRADE_MAX=5     photo-grade at most this many new books a run (BUI-1350)
  AUTOSNIPE_GRADE_CACHE     grade verdicts kept by item ID until the auction ends
                            (default ~/.local/state/comic-autosnipe/grades.json)
  AUTOSNIPE_REPO            repo root for grade-seats (default ~/Projects/comic-pipeline)
  AUTOSNIPE_DASHBOARD_URL   link printed in the message

Output: the plain-text message on stdout (or --message-out), the full JSON
summary at --json-out. Exit 0 ok, 2 needs attention (a row failed to land),
1 failed (the run could not complete; the message says why).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFIG_DEFAULT = Path.home() / ".config" / "comic-autosnipe" / "env"
DEFAULTS = {
    "AUTOSNIPE_DRY_RUN": "0",
    "AUTOSNIPE_SPEND_CAP": "300",
    "AUTOSNIPE_MIN_HOURS": "6",
    "AUTOSNIPE_GRADE_OVER": "100",
    "AUTOSNIPE_GRADE_MAX": "5",
    "AUTOSNIPE_GRADE_CACHE": str(Path.home() / ".local" / "state" / "comic-autosnipe" / "grades.json"),
    "AUTOSNIPE_REPO": str(Path.home() / "Projects" / "comic-pipeline"),
    "AUTOSNIPE_DASHBOARD_URL": "http://mac-mini.tail9b7fa5.ts.net:8080/comics",
    "COMICS_SERVER_URL": "http://localhost:8080",
}
ALLOWED_EDITIONS = {"single-issue", "annual", "giant-size", "king-size"}
SLAB_REASON = re.compile(r"slab|cgc|cbcs", re.I)
CONF_RANK = {"HIGH": 4, "MEDIUM-HIGH": 3.5, "MEDIUM": 3, "MEDIUM-LOW": 2, "LOW": 1}
CONF_MAP = {"HIGH": "high", "MEDIUM-HIGH": "medium", "MEDIUM": "medium",
            "MEDIUM-LOW": "medium-low", "LOW": "low"}
LANDED = {"added", "updated"}
SPLIT = "graders split by a full grade or more"


class RunError(Exception):
    """The run cannot continue. Nothing after this point is trusted."""


@dataclass
class Book:
    item_id: str
    url: str
    title: str
    end: datetime | None
    listing: dict = field(default_factory=dict)
    series: str | None = None
    issue: str | None = None
    year: int | None = None
    grade: object = None          # what comic-fmv prices (seller or photo grade)
    seller_grade: object = None
    photo_grade: float | None = None
    grade_confidence: str | None = None
    grade_cached: bool = False    # the photo grade came from an earlier run
    fmv: dict = field(default_factory=dict)
    skip: str | None = None       # reason, once a gate drops the book
    record: str | None = None     # watchlist outcome to record (live runs only)
    status: str | None = None     # add-batch row status

    @property
    def name(self):
        if self.series and self.issue:
            return f"{self.series} #{self.issue}" + (f" ({self.year})" if self.year else "")
        return self.title

    @property
    def price(self):
        p = self.listing.get("current_price")
        try:
            return float(p) if p is not None else None
        except (TypeError, ValueError):
            return None


# ---------- config and process helpers ----------

def load_config(path=None, environ=None):
    environ = os.environ if environ is None else environ
    path = Path(path or environ.get("AUTOSNIPE_CONFIG") or CONFIG_DEFAULT)
    cfg = dict(DEFAULTS)
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in list(cfg):
        if environ.get(k):
            cfg[k] = environ[k]
    return cfg


def truthy(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def run_cmd(argv, *, cwd=None, env=None, timeout=1800):
    """Run a console script. Returns (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, cwd=cwd, env=env,
                           timeout=timeout)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not on PATH (run ./scripts/install.sh)"
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]}: timed out after {timeout}s"
    return p.returncode, p.stdout, p.stderr


def parse_time(text):
    if not text:
        return None
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def json_lines(text):
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            rows.append(json.loads(line))
    return rows


def last_error_line(stderr):
    lines = [x for x in (stderr or "").strip().splitlines() if x.strip()]
    return lines[-1][:200] if lines else "no error text"


# ---------- the pipeline ----------

class Autosnipe:
    def __init__(self, cfg, *, run=run_cmd, fetch=None, now=None, workdir=None):
        self.cfg = cfg
        self.run = run
        self.fetch = fetch or fetch_listings
        self.now = now or datetime.now(timezone.utc)
        self.workdir = Path(workdir or tempfile.mkdtemp(prefix="autosnipe-"))
        self.dry_run = truthy(cfg["AUTOSNIPE_DRY_RUN"])
        self.books: list[Book] = []
        self.notes: list[str] = []
        self.env = dict(os.environ, COMICS_SERVER_URL=cfg["COMICS_SERVER_URL"])

    # helpers
    def live(self):
        return [b for b in self.books if not b.skip]

    def call(self, argv, what, **kw):
        rc, out, err = self.run(argv, env=self.env, **kw)
        if rc != 0:
            raise RunError(f"{what} failed (exit {rc}): {last_error_line(err)}")
        return out

    def write(self, name, data):
        path = self.workdir / name
        path.write_text(json.dumps(data, indent=2))
        return str(path)

    # steps
    def read_watchlist(self):
        out = self.call(["ebay-watchlist", "--type", "auction", "--json"], "ebay-watchlist")
        try:
            items = json.loads(out)
        except json.JSONDecodeError as e:
            raise RunError(f"ebay-watchlist returned unreadable JSON ({e})") from e
        out = self.call(["gixen", "list", "--json"], "gixen list")
        try:
            sniped = {str(r.get("item_id")) for r in json.loads(out)}
        except (json.JSONDecodeError, AttributeError) as e:
            raise RunError(f"gixen list returned unreadable JSON ({e})") from e
        min_end = self.now + timedelta(hours=float(self.cfg["AUTOSNIPE_MIN_HOURS"]))
        for it in items:
            iid = str(it.get("item_id"))
            if iid in sniped:
                continue
            b = Book(iid, it.get("url") or f"https://www.ebay.com/itm/{iid}",
                     it.get("title") or iid, parse_time(it.get("end_time")))
            if b.end and b.end <= self.now:
                continue
            if b.end is None:
                b.skip = "Timing – no end time on the watchlist"
            elif b.end < min_end:
                b.skip = f"Timing – ends within {self.cfg['AUTOSNIPE_MIN_HOURS']} hours"
            self.books.append(b)

    def fetch_and_identify(self):
        from comic_identity import identify_comic
        todo = self.live()
        if not todo:
            return
        got = self.fetch([b.item_id for b in todo])
        for b in todo:
            item = got.get(b.item_id)
            if not item:
                b.skip = "Identify – listing fetch failed"
                continue
            b.listing = item
            b.title = item.get("title") or b.title
            b.end = parse_time(item.get("end_date_iso")) or b.end
            if item.get("listing_type") == "BIN":
                b.skip = "Identify – Buy It Now, not an auction"
                continue
            ident = identify_comic(b.title)
            reasons = [r for r in (ident.reject_reasons or [])
                       if not (item.get("certifier") and SLAB_REASON.search(r))]
            if ident.is_lot:
                b.skip = "Identify – lot listing"
            elif not ident.series or not ident.issue or (ident.confidence or 0) < 0.8:
                b.skip = "Identify – series or issue uncertain"
            elif ident.edition not in ALLOWED_EDITIONS:
                b.skip = f"Identify – {ident.edition} edition"
            elif reasons:
                b.skip = f"Identify – {reasons[0]}"
            if b.skip:
                b.record = "non_comic" if "trading card" in b.skip.lower() else None
                continue
            b.series = f"{ident.series} Annual" if ident.edition == "annual" else ident.series
            b.issue = str(ident.issue)
            b.year = item.get("cover_year")
            b.grade = item.get("grade") or item.get("grade_from_description")
            b.seller_grade = b.grade
            defects = item.get("condition_defects") or []
            if defects:
                d = defects[0]
                b.skip = f"Grade – seller notes {d.get('code', 'a defect')}: \"{d.get('phrase', '')}\""
                b.record = "skipped"

    def drop_duplicates(self):
        seen = {}
        for b in sorted(self.live(), key=lambda x: x.end or self.now):
            key = (b.series.lower(), b.issue, b.year)
            if key in seen:
                b.skip = f"Duplicate – another listing of this book ends first ({seen[key]})"
            else:
                seen[key] = b.item_id

    def check_collection(self):
        todo = self.live()
        if not todo:
            return
        items = [{"series": b.series, "issue": b.issue,
                  **({"year": b.year} if b.year else {}),
                  **({"variant": b.listing["variant"]} if b.listing.get("variant") else {})}
                 for b in todo]
        path = self.write("items.json", {"items": items})
        out = self.call(["locg", "collection", "check-batch", path],
                        "Collection check (comics server unreachable?)")
        payload = json.loads(out)
        results = payload.get("results") or []
        if len(results) != len(todo):
            raise RunError("Collection check returned a different row count; no verdicts trusted")
        for b, r in zip(todo, results):
            status = r.get("match_status")
            if status == "in_collection":
                b.skip, b.record = "Collection – already owned", "skipped"
            elif status != "not_in_cache":
                b.skip = f"Collection – {r.get('verdict') or status}"
            elif r.get("flags"):
                b.skip = "Collection – check flagged a possible false match"
            elif "stale" in str(r.get("verdict", "")):
                b.skip = "Collection – collection cache is stale"

    def needs_grade(self, b):
        if b.listing.get("certifier"):
            return False
        price = b.price or 0
        return not b.grade or price > float(self.cfg["AUTOSNIPE_GRADE_OVER"])

    def load_grade_cache(self):
        """Unexpired verdicts by item ID. An unreadable cache means grade again."""
        path = Path(self.cfg["AUTOSNIPE_GRADE_CACHE"])
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as e:
            self.notes.append(f"grade cache unreadable, grading again ({e})")
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items()
                if isinstance(v, dict) and (parse_time(v.get("end")) or self.now) > self.now}

    def save_grade_cache(self, cache):
        path = Path(self.cfg["AUTOSNIPE_GRADE_CACHE"])
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache, indent=2, sort_keys=True))
            tmp.replace(path)
        except OSError as e:
            self.notes.append(f"could not save the grade cache: {e}")

    def apply_grade(self, b, entry):
        if "skip" in entry:
            b.skip = f"Grade – {entry['skip']}"
        elif entry["confidence"] == "LOW":
            b.skip = f"Grade – photo grade {entry['grade']} has low confidence"
        else:
            b.photo_grade = b.grade = entry["grade"]
            b.grade_confidence = CONF_MAP.get(entry["confidence"], "medium-low")

    def grade(self):
        todo = [b for b in self.live() if self.needs_grade(b)]
        if not todo:
            return
        cache = self.load_grade_cache()
        fresh = []
        for b in todo:
            if b.item_id in cache:
                b.grade_cached = True
                self.apply_grade(b, cache[b.item_id])
            else:
                fresh.append(b)
        fresh.sort(key=lambda b: b.end or self.now)
        cap = int(self.cfg["AUTOSNIPE_GRADE_MAX"])
        for b in fresh[cap:]:
            b.skip = f"Grade – over the {cap}-book grading cap for one run"
        self.grade_fresh(fresh[:cap], cache)
        self.save_grade_cache(cache)

    def grade_fresh(self, todo, cache):
        """Photo-grade books with no cached verdict, and cache each real verdict."""
        if not todo:
            return
        gdir = self.workdir / "grading"
        rc, _, err = self.run(["grade-photos", *[b.item_id for b in todo], "--workdir", str(gdir)],
                              env=self.env)
        books, spec_books = [], []
        for n, b in enumerate(todo, 1):
            folder = gdir / f"comic-{n}"
            if not any(folder.glob("img-*.jpg")):
                b.skip = "Grade – listing photos could not be downloaded"
                continue
            books.append(b)
            spec_books.append({"item_id": b.item_id, "comic": b.name, "folder": str(folder),
                               "seats": [f"grader-c{n}-a", f"grader-c{n}-b", f"grader-c{n}-c"],
                               "seller_grade": b.seller_grade, "policy": "two-first"})
        if not books:
            if rc != 0:
                self.notes.append(f"grade-photos failed: {last_error_line(err)}")
            return
        spec = self.write("seats.json", {"workdir": str(gdir), "books": spec_books})
        rc, out, err = self.run(["grade-seats", spec, "--json"], cwd=self.cfg["AUTOSNIPE_REPO"],
                                env=self.env, timeout=3600)
        try:
            report = {bk["item_id"]: bk for bk in json.loads(out).get("books", [])}
        except (json.JSONDecodeError, AttributeError, KeyError):
            report = {}
            self.notes.append(f"grade-seats failed (exit {rc}): {last_error_line(err)}")
        for b in books:
            verdict = consensus(report.get(b.item_id))
            if isinstance(verdict, str):
                entry = {"skip": verdict}
                cacheable = verdict == SPLIT   # a failed grader run is retried next night
            else:
                entry = {"grade": verdict[0], "confidence": verdict[1]}
                cacheable = True
            if cacheable and b.end:
                cache[b.item_id] = {**entry, "end": b.end.isoformat(),
                                    "graded_at": self.now.isoformat()}
            self.apply_grade(b, entry)

    def price(self):
        todo = self.live()
        if not todo:
            return
        rows = []
        for b in todo:
            it = b.listing
            row = {"item_id": b.item_id, "title": b.series, "issue": b.issue, "grade": b.grade}
            if b.year:
                row["year"] = b.year
            pub = (it.get("item_specifics") or {}).get("Publisher")
            if pub:
                row["publisher"] = pub
            if it.get("variant"):
                row["variant"] = it["variant"]
            if b.grade_confidence:
                row["grade_confidence"] = b.grade_confidence
            for k in ("certifier", "label", "page_quality", "cert_number"):
                if it.get(k):
                    row[k] = it[k]
            if it.get("certifier"):
                row["listing_type"] = it.get("listing_type")
            rows.append(row)
        path = self.write("fmv_in.json", rows)
        out = self.call(["comic-fmv", "--batch", path, "--out", str(self.workdir / "fmv_out.json"),
                         "--brief", "--quiet"], "comic-fmv", timeout=3600)
        (self.workdir / "fmv_brief.json").write_text(out)
        brief = {str(r.get("item_id")): r for r in json_lines(out)}
        for b in todo:
            r = brief.get(b.item_id)
            b.fmv = r or {}
            if not r:
                b.skip = "FMV – no result row"
            elif r.get("source") == "ledger-advisory":
                b.skip = "FMV – live comps fetch failed (ledger advisory only)"
            elif str(r.get("source") or "").startswith("skipped_"):
                b.skip = f"FMV – lookup failed ({r['source']})"
            elif r.get("flag_reason"):
                b.skip = f"FMV – needs manual pricing ({r['flag_reason']})"
            elif not r.get("max_bid") or not r.get("comic_id"):
                b.skip = "FMV – no max bid computed"
            elif b.price is not None and b.price >= float(r["max_bid"]):
                b.skip = f"Price – current bid ${b.price:g} is at or above max bid ${r['max_bid']:g}"
                b.record = "over_cap"

    def apply_spend_cap(self):
        cap = float(self.cfg["AUTOSNIPE_SPEND_CAP"])
        total = 0.0
        for b in sorted(self.live(), key=lambda x: x.end or self.now):
            bid = float(b.fmv["max_bid"])
            if total + bid > cap:
                b.skip = f"Spend cap – ${bid:g} would pass the ${cap:g} run cap"
            else:
                total += bid

    def add_snipes(self):
        todo = self.live()
        if not todo or self.dry_run:
            return
        brief = str(self.workdir / "fmv_brief.json")
        wl = self.write("working_list.json", [{
            "item_id": b.item_id, "title": b.name, "grade": b.grade,
            "listing_type": "Auction", "seller": b.listing.get("seller"),
            **({"seller_grade": b.seller_grade} if b.seller_grade and not b.listing.get("certifier") else {}),
            **({"photo_grade": b.photo_grade} if b.photo_grade is not None else {}),
            **({"grade_confidence": b.grade_confidence} if b.grade_confidence else {}),
            **{k: b.listing[k] for k in ("certifier", "cert_number", "label") if b.listing.get(k)},
            "end_date_iso": b.listing.get("end_date_iso"),
        } for b in todo])
        rows = str(self.workdir / "rows.json")
        self.call(["gixen", "build-batch", brief, wl, "--out", rows], "gixen build-batch")
        results = str(self.workdir / "add_results.json")
        rc, _, err = self.run(["gixen", "add-batch", rows, "--verify", "--json-out", results],
                              env=self.env)
        try:
            landed = {str(r.get("item_id")): r for r in json.loads(Path(results).read_text())["rows"]}
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
            raise RunError(f"gixen add-batch gave no readable result (exit {rc}): "
                           f"{last_error_line(err)}") from e
        for b in todo:
            r = landed.get(b.item_id) or {}
            b.status = r.get("status") or "not_attempted"
            if b.status in LANDED:
                b.record = "sniped"
            else:
                b.skip = f"Snipe add – {b.status}" + (f": {r['error']}" if r.get("error") else "")

    def record_outcomes(self):
        if self.dry_run:
            return
        for b in self.books:
            if not b.record:
                continue
            rc, _, err = self.run(["ebay-watchlist", "--record", b.item_id, "--outcome", b.record],
                                  env=self.env)
            if rc != 0:
                self.notes.append(f"could not record {b.item_id}: {last_error_line(err)}")

    def execute(self):
        self.read_watchlist()
        self.fetch_and_identify()
        self.drop_duplicates()
        self.check_collection()
        self.grade()
        self.price()
        self.apply_spend_cap()
        self.add_snipes()
        self.record_outcomes()

    # output
    def sniped(self):
        return [b for b in self.books if not b.skip]

    def failed_adds(self):
        return [b for b in self.books if b.status and b.status not in LANDED]

    def summary(self):
        return {"dry_run": self.dry_run, "workdir": str(self.workdir), "notes": self.notes,
                "books": [{"item_id": b.item_id, "name": b.name, "url": b.url,
                           "end": b.end.isoformat() if b.end else None, "grade": b.grade,
                           "grade_confidence": b.grade_confidence,
                           "grade_cached": b.grade_cached, "fmv": b.fmv,
                           "skip": b.skip, "status": b.status} for b in self.books]}


def consensus(book):
    """(grade, CONFIDENCE) from one grade-seats book, or a skip reason string."""
    if not book:
        return "grading did not return a result"
    adj = book.get("adjudicator")
    if adj:
        return float(adj["grade"]), str(adj.get("confidence", "")).upper()
    if book.get("adjudicator_failed") or book.get("split"):
        return SPLIT
    pol = book.get("policy") or {}
    if pol.get("consensus"):
        c = pol["consensus"]
        return float(c["grade"]), str(c.get("confidence", "")).upper()
    seats = [s for s in book.get("seats", []) if not s.get("failed")]
    if len(seats) < 2:
        return "too few grader seats returned a grade"
    grade = statistics.median_low([float(s["grade"]) for s in seats])
    conf = min((str(s.get("confidence", "")).upper() for s in seats),
               key=lambda c: CONF_RANK.get(c, 0))
    return grade, conf


def fetch_listings(item_ids):
    """Fetch and parse each listing through ebay_fetch. Missing ids failed."""
    import ebay_fetch
    client_id, client_secret, base_url = ebay_fetch.load_config()
    token = ebay_fetch.get_token(client_id, client_secret, base_url)
    out = {}
    for iid in item_ids:
        data = ebay_fetch.fetch_item(iid, token, base_url)
        if data:
            out[iid] = ebay_fetch.parse_item(data)
    return out


def money(v):
    return f"${float(v):,.0f}" if v is not None else "?"


def format_message(snipe, error=None):
    """The Telegram text (HTML parse mode, so every field is escaped)."""
    e = html.escape
    sniped = snipe.sniped()
    skipped = [b for b in snipe.books if b.skip]
    head = "Autosnipe (dry run, nothing added)" if snipe.dry_run else "Autosnipe"
    lines = [f"<b>{e(head)}</b>"]
    if error:
        lines.append(f"Run failed: {e(error)}")
        lines.append("No snipes were added after this point.")
    verb = "Would snipe" if snipe.dry_run else "Sniped"
    if sniped:
        lines.append(f"\n<b>{verb} ({len(sniped)})</b>")
        for b in sniped:
            f = b.fmv
            grade = f"{b.grade}" + (f" photo, {b.grade_confidence}" if b.photo_grade is not None else "")
            grade += ", cached" if b.grade_cached else ""
            ends = b.end.strftime("%a %d %b %H:%M UTC") if b.end else "?"
            lines.append(f"• <a href=\"{e(b.url)}\">{e(b.name)}</a> – grade {e(str(grade))}, "
                         f"FMV {money(f.get('fmv_low'))}–{money(f.get('fmv_high'))}, "
                         f"max bid {money(f.get('max_bid'))}, now {money(b.price)}, ends {e(ends)}")
    elif not error:
        lines.append(f"\n{verb}: none.")
    if skipped:
        lines.append(f"\n<b>Skipped ({len(skipped)})</b>")
        for b in skipped:
            lines.append(f"• <a href=\"{e(b.url)}\">{e(b.name)}</a> – {e(b.skip)}")
    for n in snipe.notes:
        lines.append(f"Note: {e(n)}")
    lines.append(f"\n<a href=\"{e(snipe.cfg['AUTOSNIPE_DASHBOARD_URL'])}\">Dashboard</a>")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="comic-autosnipe", description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="add no snipes and record nothing (overrides the config)")
    parser.add_argument("--message-out", help="write the message here instead of stdout")
    parser.add_argument("--json-out", help="write the JSON summary here")
    parser.add_argument("--config", help="config file (default ~/.config/comic-autosnipe/env)")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.dry_run:
        cfg["AUTOSNIPE_DRY_RUN"] = "1"
    snipe = Autosnipe(cfg)
    error, code = None, 0
    try:
        snipe.execute()
    except RunError as e:
        error, code = str(e), 1
    except Exception as e:  # noqa: BLE001 -- an unattended run must still report
        error, code = f"{type(e).__name__}: {e}", 1
    if code == 0 and snipe.failed_adds():
        code = 2
    message = format_message(snipe, error)
    if args.message_out:
        Path(args.message_out).write_text(message)
    else:
        print(message)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps({**snipe.summary(), "error": error}, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
