"""BUI-596 phase 1f: the plan module. Importable by remediate.py. READ-ONLY helpers.

Single source of truth for the classification rule, the per-row action, and the
end-state simulation. `remediate.py` imports `build_plan()` so the reviewed plan
and the executed plan cannot drift apart.
"""
import os
import re
import sqlite3
from collections import defaultdict

DEFAULT_DB = os.path.expanduser("~/.comics-server/db.sqlite")


def connect_read(path: str) -> sqlite3.Connection:
    """Open *path* for reading.

    Prefers `mode=ro`, but falls back to a plain connect: a database that is in
    WAL mode and has no `-shm` sidecar yet — exactly the case for a file just
    produced by `sqlite3 .backup` — cannot be opened read-only, because SQLite
    needs to create the shared-memory index and `mode=ro` forbids it. The
    symptom is a bare `unable to open database file` on the first query, not on
    connect.
    """
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        c.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c

# --- THE RULE ---------------------------------------------------------------
# A `comics` row is MALFORMED iff its own issue number appears in its own title
# as a `#<issue>` token. This is strictly tighter than `title LIKE '%#%'`:
# it requires the `#` to be a duplication of the `issue` column, not any `#`.
ISSUE_TOKEN = r'#\s*{issue}\b'

ART = re.compile(r'^(?:the|a|an)\s+', re.IGNORECASE)


def is_malformed(title: str, issue: str) -> bool:
    return bool(re.search(ISSUE_TOKEN.format(issue=re.escape(str(issue))), title, re.IGNORECASE))


def series_prefix(title: str, issue: str):
    """Everything before the `#<issue>` token — the series name as the row meant it."""
    m = re.match(rf'^(.*?)\s*#\s*{re.escape(str(issue))}\b', title, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', m.group(1)).strip() if m and m.group(1).strip() else None


def remainder(title: str, issue: str) -> str:
    m = re.match(rf'^(.*?)\s*#\s*{re.escape(str(issue))}\b(.*)$', title, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', m.group(2)).strip() if m else ""


def skey(t: str) -> str:
    """Series identity key: leading article folded, case folded, whitespace folded."""
    return ART.sub('', re.sub(r'\s+', ' ', t or '').strip()).lower()


# --- sub-classes ------------------------------------------------------------
_GRADE = re.compile(r'\((?:VG|FN|VF|NM|GD|FR|PR|FINE)[^)]*\)'
                    r'|\b(?:VG|FN|VF|NM|GD|FVF|VGF)\s*[-+](?:/\w+)?\b'
                    r'|\b(?:VF|FN|NM|VG)/(?:VF|FN|NM|VG)\b|\bcondition\b', re.IGNORECASE)
_PUBDATE = re.compile(r'\b(?:Marvel|DC)\s+Comics\b|\b(?:January|February|March|April|May|June'
                      r'|July|August|September|October|November|December)\b', re.IGNORECASE)
_LOT = re.compile(r'\blot of \d+\b|^\s*(?:,\s*\d+)+', re.IGNORECASE)
_COVERVAR = re.compile(r'\b(?:cover|variant|virgin|newsstand)\b', re.IGNORECASE)
_KEYNOTE = re.compile(r'\b(?:1st|2nd|3rd|\d+th|key|app\b|bronze age|silver age|rare|beauty'
                      r'|gem|wow|huge)\b', re.IGNORECASE)


def subclass(rem: str) -> str:
    if rem == "":
        return "A-doubled-only"
    if _LOT.search(rem):
        return "D-multi-issue-lot"
    if _GRADE.search(rem) or _PUBDATE.search(rem) or _KEYNOTE.search(rem):
        return "B-full-listing-title"
    if _COVERVAR.search(rem):
        return "C-variant-designation"
    return "E-other-remainder"


def build_plan(db_path: str = DEFAULT_DB):
    """Return (plan, stats). Opens the DB read-only. Keyed on comics.id ONLY."""
    conn = connect_read(db_path)
    rows = conn.execute(
        "SELECT id, title, issue, year, variant, created_at FROM comics ORDER BY id"
    ).fetchall()
    stat = {r["id"]: r for r in conn.execute(
        """SELECT c.id,
                  (SELECT COUNT(*) FROM fmv f WHERE f.comic_id=c.id) fn,
                  (SELECT COUNT(*) FROM fmv f WHERE f.comic_id=c.id
                    AND NOT (f.comps IS NULL OR f.comps=0)) fr,
                  (SELECT COUNT(*) FROM fmv f JOIN bid_fmvs b ON b.fmv_id=f.id
                    WHERE f.comic_id=c.id) ln
             FROM comics c""")}
    conn.close()

    targets = [r for r in rows if is_malformed(r["title"], r["issue"])]
    tset = {r["id"] for r in targets}

    # index occupancy, exactly as the two partial UNIQUE indexes define it
    yeared, yearless = defaultdict(list), defaultdict(list)
    for r in rows:
        v = r["variant"] or ""
        if r["year"] is not None:
            yeared[(r["title"].lower(), r["issue"], r["year"], v)].append(r["id"])
        else:
            yearless[(r["title"].lower(), r["issue"], v)].append(r["id"])

    # clean rows (rule-negative) for the same logical book
    clean_at = defaultdict(list)
    for r in rows:
        if r["id"] not in tset:
            clean_at[(skey(r["title"]), r["issue"])].append(r["id"])

    plan = []
    for r in targets:
        new_title = series_prefix(r["title"], r["issue"])
        v = r["variant"] or ""
        occ = ([i for i in yeared.get((new_title.lower(), r["issue"], r["year"], v), []) if i != r["id"]]
               if r["year"] is not None
               else [i for i in yearless.get((new_title.lower(), r["issue"], v), []) if i != r["id"]])
        peers = [i for i in clean_at.get((skey(new_title), r["issue"])) or [] if i != r["id"]]
        s = stat[r["id"]]

        if s["fr"] > 0:
            # carries real FMV data -> never delete it
            action = "rewrite" if not occ else "merge-into"
        elif occ or peers:
            # a clean row already holds this book; this row is an inert placeholder
            action = "delete"
        else:
            action = "rewrite"

        plan.append({
            "id": r["id"], "title": r["title"], "issue": r["issue"], "year": r["year"],
            "variant": r["variant"], "class": subclass(remainder(r["title"], r["issue"])),
            "action": action if action != "merge-into" else f"merge-into-{occ[0]}",
            "proposed_new_title": new_title if action == "rewrite" else "",
            "index_collision": "YES" if occ else "no",
            "collides_with": ",".join(map(str, occ)),
            "clean_peer_ids": ",".join(map(str, peers)),
            "fmv_rows": s["fn"], "fmv_rows_with_data": s["fr"], "bid_links": s["ln"],
            "created_at": r["created_at"],
        })

    # --- CONVERGENCE RESOLUTION ---------------------------------------------
    # Two malformed rows can rewrite onto the SAME identity (e.g. 426/432 are
    # `THE MIGHTY THOR # 128` at two different grades — one book, two rows; the
    # `comics` table is an identity table, grade belongs in `fmv`). Keep exactly
    # one per group and delete the rest, or the rewrite violates the unique index.
    #
    # Tiebreaker, in order: (1) the row carrying real FMV data, (2) lowest
    # comics.id. NOTE the premise correction: `last_seen_in_export_at` is a
    # column on the LOCG collection JSON store, NOT on `comics`; `comics` has
    # only `created_at`, which is identical to the second across each of these
    # bulk-written groups and so cannot break the tie. Lowest id is used because
    # it is deterministic and stable. Every row in every convergent group here
    # has zero real FMV, so nothing is lost either way.
    conv = defaultdict(list)
    for p in plan:
        if p["action"] == "rewrite":
            conv[(p["proposed_new_title"].lower(), p["issue"], p["year"],
                  p["variant"] or "")].append(p)
    for _k, group in conv.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda p: (-p["fmv_rows_with_data"], p["id"]))
        for loser in group[1:]:
            loser["action"] = "delete"
            loser["proposed_new_title"] = ""
            loser["convergence_loser_to"] = str(group[0]["id"])
    for p in plan:
        p.setdefault("convergence_loser_to", "")

    # --- END-STATE SIMULATION: prove no unique-index violation results -------
    deleted = {p["id"] for p in plan if p["action"] == "delete"}
    renamed = {p["id"]: p["proposed_new_title"] for p in plan if p["action"] == "rewrite"}
    end_y, end_n = defaultdict(list), defaultdict(list)
    for r in rows:
        if r["id"] in deleted:
            continue
        t = renamed.get(r["id"], r["title"])
        v = r["variant"] or ""
        if r["year"] is not None:
            end_y[(t.lower(), r["issue"], r["year"], v)].append(r["id"])
        else:
            end_n[(t.lower(), r["issue"], v)].append(r["id"])
    violations = ([(k, ids) for k, ids in end_y.items() if len(ids) > 1]
                  + [(k, ids) for k, ids in end_n.items() if len(ids) > 1])

    # residual yearless/yeared logical dups (NOT index violations; informational)
    logical = defaultdict(lambda: {"y": [], "n": []})
    for r in rows:
        if r["id"] in deleted:
            continue
        t = renamed.get(r["id"], r["title"])
        v = r["variant"] or ""
        logical[(skey(t), r["issue"], v)]["y" if r["year"] is not None else "n"].append(r["id"])
    logical_dups = [(k, s) for k, s in logical.items() if s["y"] and s["n"]]

    stats = {
        "total_comics": len(rows),
        "heuristic_hash": sum(1 for r in rows if "#" in r["title"]),
        "rule_matched": len(targets),
        "classes": _tally(p["class"] for p in plan),
        "actions": _tally(p["action"].split("-into-")[0] for p in plan),
        "collisions": sum(1 for p in plan if p["index_collision"] == "YES"),
        "bid_links_total": sum(p["bid_links"] for p in plan),
        "fmv_rows_total": sum(p["fmv_rows"] for p in plan),
        "fmv_rows_with_data": sum(p["fmv_rows_with_data"] for p in plan),
        "end_state_violations": violations,
        "residual_logical_dups": len(logical_dups),
        "delete_ids": sorted(deleted),
        "rewrite_ids": sorted(renamed),
    }
    return plan, stats


def _tally(it):
    d = defaultdict(int)
    for x in it:
        d[x] += 1
    return dict(sorted(d.items()))


if __name__ == "__main__":
    plan, st = build_plan()
    for k, v in st.items():
        if k in ("delete_ids", "rewrite_ids"):
            print(f"{k}: n={len(v)}")
        elif k == "end_state_violations":
            print(f"{k}: n={len(v)}")
            for kk, ids in v:
                print(f"    VIOLATION {kk} <- {ids}")
        else:
            print(f"{k}: {v}")
