"""BUI-600: the plan module. Importable by remediate.py. READ-ONLY helpers.

Single source of truth for the adjudicated row list, the per-row action and the
end-state simulation. `remediate.py` imports `build_plan()` so the reviewed plan
and the executed plan cannot drift apart. Same contract as BUI-596's `plan.py`.

WHY THIS IS A HARDCODED LIST AND NOT A RULE
-------------------------------------------
BUI-596's rule was *generative*: `series_prefix(title, issue)` split the title on
the `#<issue>` token, so the rule both SELECTED the row and DERIVED its repair.

BUI-600's rows have no `#<issue>` token. There is nothing to split on. Every
proposed title here was recovered by joining the row to the eBay listing that
created it (`bid_fmvs` -> `bids.ebay_title`, or `bids.fmv_id` for rows whose link
had already been re-pointed to a clean twin) and then choosing the spelling this
table already uses. Two of the four rewrites are not derivable from the stored
title by any rule:

  * 328 stores 'Hulk Annual ...' but the book is 'Incredible Hulk Annual' — the
    correct token is absent from the row.
  * 315 stores 'Avengers Silver age Black Panther Joins'; knowing that
    'Black Panther Joins' is a cover blurb and not part of a series name is not
    a lexical property of the string.

So a classifier, however precise, would leave 100% of the repair as hand work.
12 rows do not warrant machinery that does none of the job. The list below is
hand-adjudicated, and the vocabulary scan survives only as a DRIFT TRIPWIRE
(`vocabulary_scan`) — it selects nothing.
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
    connect. (Found and fixed during BUI-596; kept identical here.)
    """
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        c.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


# --- THE DRIFT TRIPWIRE (not a selector) ------------------------------------
# Measured on the live table 2026-08-02: this expression flags exactly the 12
# adjudicated ids, 0 false positives, 0 misses. That is a property of THIS TABLE
# TODAY, not a proof — real series names contain these tokens (DC's 2000 event
# one-shots `Silver Age: Showcase` etc.). It is used only to detect that the
# table has moved since adjudication, never to choose a row.
AGE_MARKER = re.compile(r'\b(?:bronze|silver|golden|gold|copper|modern|atom)\s+age\b',
                        re.IGNORECASE)


def vocabulary_scan(rows) -> set:
    """Ids whose title carries a listing-derived 'age' marker."""
    return {r["id"] for r in rows if AGE_MARKER.search(r["title"])}


# --- THE ADJUDICATED LIST ---------------------------------------------------
# `fingerprint` is the exact (title, issue, year, variant) the row had when a
# human read it. remediate.py aborts if any row no longer matches — the plan is
# only valid for the rows that were actually adjudicated.
#
# `src_bid` / `ebay_title` are the provenance: the listing whose title was stored
# as row identity. `twin` is the clean row holding the same book.
ADJUDICATED = {
    201: dict(
        fingerprint=("Avengers Bronze age 1st Squadron Supreme", "85", None, None),
        action="rewrite", new_title="Avengers", src_bid=152, twin=172,
        ebay_title="Avengers #85 Bronze age 1st Squadron Supreme Key VF- Beauty Wow  Z",
        why="Carries the REAL fmv (212: 7.5/40-45/comps=2) and the bid_fmvs link "
            "for bid 152, and bids.fmv_id=212 points at it. Deleting would sever "
            "both. Twin 172 'Avengers' #85/1971 holds an identical fmv (184), so "
            "the rewrite hands this to the existing yearless-orphan merge.",
    ),
    202: dict(
        fingerprint=("Avengers Bronze age Daredevil Fine+", "82", None, None),
        action="delete", new_title=None, src_bid=153, twin=173,
        ebay_title="Avengers  82 Bronze age Daredevil Fine+ Beauty Wow Z",
        why="Inert shell: 0 fmv rows, 0 bid links. bid 153 already re-points to "
            "fmv 185 on twin 173 'Avengers' #82/1970, which carries real comps.",
    ),
    203: dict(
        fingerprint=("Avengers Bronze age 1st Lethal Legion Story FVF", "79", None, None),
        action="delete", new_title=None, src_bid=154, twin=205,
        ebay_title="Avengers #79 Bronze age 1st Lethal Legion Story FVF Beauty Wow Z",
        why="Inert shell: 0 fmv rows, 0 bid links. bid 154 already re-points to "
            "fmv 214 on twin 205 'The Avengers' #79/1970. (Note 174 'Avengers' "
            "#79/1970 also exists — a pre-existing article-variant duplicate pair, "
            "out of scope here.)",
    ),
    204: dict(
        fingerprint=("Avengers Bronze age Vision Declares his love for Wanda", "81", None, None),
        action="rewrite", new_title="Avengers", src_bid=155, twin=175,
        ebay_title="Avengers  81 Bronze age Vision Declares his love for Wanda VF- beauty Wow Z",
        why="fmv 213 has comps=0 (so it fails BUI-596's 'real data' predicate) but "
            "low/high ARE populated (15/25, 'using prior DB value'), it holds the "
            "bid_fmvs link for bid 155, and bids.fmv_id=213. Delete would sever a "
            "live link, so this is a rewrite. Twin 175 has the strictly better fmv "
            "187 (same 7.5/15-25, comps=1).",
    ),
    314: dict(
        fingerprint=("Avengers Silver age", "10", None, None),
        action="delete", new_title=None, src_bid=166, twin=187,
        ebay_title="Avengers #10 Silver age 1st Immortus Key Nice VGF Wow",
        why="Pure orphan: 0 fmv rows, 0 bid links, and bid 166 has fmv_id NULL. "
            "Twin 187 'Avengers' #10/1964 carries the real fmv.",
    ),
    315: dict(
        fingerprint=("Avengers Silver age Black Panther Joins", "52", None, None),
        action="rewrite", new_title="Avengers", src_bid=168, twin=232,
        ebay_title="Avengers #52 Silver age Black Panther Joins VG+ Wow Z",
        why="fmv 341 is an empty shell BUT holds the bid_fmvs link for bid 168, "
            "which is WON, and bids.fmv_id=341. Deleting would sever a won "
            "auction from its FMV. Rewriting lets the merge reparent that link "
            "onto twin 232's real fmv 250 (4.5/40-60/comps=4) — a strict upgrade.",
    ),
    316: dict(
        fingerprint=("Avengers Silver age", "54", None, None),
        action="delete", new_title=None, src_bid=169, twin=233,
        ebay_title="Avengers #54 Silver age 1st Ultron Cameo Key Fine- Wow Z",
        why="Pure orphan: 0 fmv rows, 0 bid links, bid 169 has fmv_id NULL. "
            "Twin 233 'Avengers' #54/1968 carries the real fmv.",
    ),
    328: dict(
        fingerprint=("Hulk Annual Silver age Steranko Inhumans", "1", None, None),
        action="rewrite", new_title="Incredible Hulk Annual", src_bid=244, twin=312,
        ebay_title="Incredible Hulk Annual #1 Silver age Steranko Key Inhumans VF- Beauty Wow z",
        why="fmv 350 is an empty shell but holds the bid_fmvs link for bid 244 and "
            "bids.fmv_id=350, so delete is unsafe. The correct masthead is "
            "'Incredible Hulk Annual' — a token the stored title does NOT contain; "
            "it comes from the source listing and matches twin 312's spelling. "
            "(308 'Incredible Hulk King Size Special' #1/1968 is the same physical "
            "book under its cover name — a pre-existing duplicate, out of scope.)",
    ),
    339: dict(
        fingerprint=("Iron Man Bronze age Layton Sub-mariner Fine", "120", None, None),
        action="delete", new_title=None, src_bid=270, twin=266,
        ebay_title="Iron Man  120 Bronze age Layton Sub-mariner Fine- Wow z",
        why="Inert shell: 0 fmv rows, 0 bid links. bid 270 re-points to fmv 334 on "
            "twin 266 'Iron Man' #120/1979.",
    ),
    340: dict(
        fingerprint=("Iron Man Bronze age", "122", None, None),
        action="delete", new_title=None, src_bid=271, twin=309,
        ebay_title="Iron Man  122 Bronze age VF+ Beauty Wow z",
        why="Carries fmv 363 but it is a pure empty shell (low/high/comps all "
            "NULL) with 0 bid_fmvs links and 0 bids.fmv_id references. bid 271 "
            "re-points to fmv 335 on twin 309 'Iron Man' #122/1979.",
    ),
    341: dict(
        fingerprint=("Iron Man Bronze age Layton", "125", None, None),
        action="delete", new_title=None, src_bid=272, twin=310,
        ebay_title="Iron Man  125 Bronze age Layton VF Beauty Wow z",
        why="Carries fmv 364 but it is a pure empty shell with 0 bid_fmvs links "
            "and 0 bids.fmv_id references. bid 272 re-points to fmv 336 on twin "
            "310 'Iron Man' #125/1979.",
    ),
    342: dict(
        fingerprint=("Iron Man Bronze age Layton", "126", None, None),
        action="delete", new_title=None, src_bid=273, twin=311,
        ebay_title="Iron Man  126 Bronze age Layton 1st Hammer Fine Wow z",
        why="Inert shell: 0 fmv rows, 0 bid links. bid 273 re-points to fmv 337 on "
            "twin 311 'Iron Man' #126/1979.",
    ),
}

# Same root cause, DELIBERATELY NOT IN THE PLAN. Surfaced by the wider vocabulary
# scan (ordinal + grade tokens) during this analysis; they are yeared, carry no
# age marker, and were never adjudicated by the ticket. Widening a data
# migration's blast radius past its reviewed set is exactly what this ritual
# exists to prevent. Recommend a follow-up ticket. See README.
OUT_OF_SCOPE = {
    330: "Thor FA/ .5 1st issue Hercules (issue=126, year=1966)",
    335: "Thor .5 2nd Wrecker! Origin Black Bolt! Inhumans (issue=149, year=1968)",
}


def build_plan(db_path: str = DEFAULT_DB):
    """Return (plan, stats). Opens the DB read-only. Keyed on comics.id ONLY."""
    conn = connect_read(db_path)
    rows = conn.execute(
        "SELECT id, title, issue, year, variant, created_at FROM comics ORDER BY id"
    ).fetchall()
    by_id = {r["id"]: r for r in rows}

    # per-row FMV / linkage facts.
    #   fmv_rows            : attached fmv rows
    #   fmv_rows_with_value : fmv rows carrying ANY price signal. Deliberately
    #                         WIDER than BUI-596's `comps > 0` predicate — row 204
    #                         has comps=0 but low/high populated from a prior DB
    #                         value, and treating that as "no data" would have
    #                         green-lit deleting it.
    #   bid_links           : bid_fmvs junction rows (BUI-596's gate)
    #   bid_fmv_id_refs     : bids.fmv_id references. NEW GATE — `bids.fmv_id` is a
    #                         bare INTEGER with NO foreign key, so an ON DELETE
    #                         CASCADE through `fmv` does NOT null it: it would be
    #                         left DANGLING. BUI-596 never needed this because all
    #                         173 of its rows had zero links; 4 of these 12 do not.
    facts = {}
    for r in rows:
        cid = r["id"]
        fmvs = conn.execute(
            "SELECT id, grade, low, high, comps FROM fmv WHERE comic_id=?", (cid,)
        ).fetchall()
        links = refs = 0
        valued = []
        for f in fmvs:
            links += conn.execute(
                "SELECT COUNT(*) FROM bid_fmvs WHERE fmv_id=?", (f["id"],)).fetchone()[0]
            refs += conn.execute(
                "SELECT COUNT(*) FROM bids WHERE fmv_id=?", (f["id"],)).fetchone()[0]
            if (f["comps"] or 0) > 0 or f["low"] is not None or f["high"] is not None:
                valued.append(f["id"])
        facts[cid] = {"fmv_rows": len(fmvs), "fmv_ids": [f["id"] for f in fmvs],
                      "fmv_rows_with_value": len(valued), "valued_fmv_ids": valued,
                      "bid_links": links, "bid_fmv_id_refs": refs}
    conn.close()

    # --- integrity of the adjudication itself -------------------------------
    fingerprint_problems = []
    for cid, spec in sorted(ADJUDICATED.items()):
        r = by_id.get(cid)
        if r is None:
            fingerprint_problems.append(f"comics {cid} no longer exists")
            continue
        got = (r["title"], r["issue"], r["year"], r["variant"])
        if got != spec["fingerprint"]:
            fingerprint_problems.append(
                f"comics {cid} fingerprint drifted: {got!r} != {spec['fingerprint']!r}")

    scanned = vocabulary_scan(rows)
    tripwire_extra = sorted(scanned - set(ADJUDICATED))
    tripwire_missing = sorted(set(ADJUDICATED) - scanned)

    # --- index occupancy, exactly as the two partial UNIQUE indexes define it -
    yeared, yearless = defaultdict(list), defaultdict(list)
    for r in rows:
        v = r["variant"] or ""
        if r["year"] is not None:
            yeared[(r["title"].lower(), r["issue"], r["year"], v)].append(r["id"])
        else:
            yearless[(r["title"].lower(), r["issue"], v)].append(r["id"])

    plan = []
    for cid, spec in sorted(ADJUDICATED.items()):
        r = by_id.get(cid)
        if r is None:
            continue
        f = facts[cid]
        occ = []
        if spec["action"] == "rewrite":
            v = r["variant"] or ""
            nt = spec["new_title"].lower()
            occ = ([i for i in yeared.get((nt, r["issue"], r["year"], v), []) if i != cid]
                   if r["year"] is not None
                   else [i for i in yearless.get((nt, r["issue"], v), []) if i != cid])
        plan.append({
            "id": cid, "title": r["title"], "issue": r["issue"], "year": r["year"],
            "variant": r["variant"], "action": spec["action"],
            "proposed_new_title": spec["new_title"] or "",
            "index_collision": "YES" if occ else "no",
            "collides_with": ",".join(map(str, occ)),
            "twin": spec["twin"], "src_bid": spec["src_bid"],
            "ebay_title": spec["ebay_title"], "why": spec["why"],
            "created_at": r["created_at"], **f,
        })

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

    # Residual yearless/yeared orphan pairs — NOT index violations. These are the
    # shape `_merge_yearless_into_yeared` (plugins/gixen-overlay/db.py) exists to
    # collapse, losslessly: it reparents fmv, bid_fmvs AND bids.fmv_id. The four
    # rewrites deliberately create one each; see README.
    orphan_pairs = []
    for r in rows:
        if r["id"] in deleted or r["year"] is not None:
            continue
        t = renamed.get(r["id"], r["title"])
        sib = [x["id"] for x in rows
               if x["id"] not in deleted and x["year"] is not None
               and renamed.get(x["id"], x["title"]).lower() == t.lower()
               and x["issue"] == r["issue"]]
        if sib:
            orphan_pairs.append((r["id"], t, r["issue"], sib))

    dels = [p for p in plan if p["action"] == "delete"]
    stats = {
        "total_comics": len(rows),
        "adjudicated": len(ADJUDICATED),
        "fingerprint_problems": fingerprint_problems,
        "vocabulary_scan_size": len(scanned),
        "tripwire_extra": tripwire_extra,
        "tripwire_missing": tripwire_missing,
        "actions": _tally(p["action"] for p in plan),
        "collisions": sum(1 for p in plan if p["index_collision"] == "YES"),
        "delete_bid_links": sum(p["bid_links"] for p in dels),
        "delete_bid_fmv_id_refs": sum(p["bid_fmv_id_refs"] for p in dels),
        "delete_valued_fmv": sum(p["fmv_rows_with_value"] for p in dels),
        "cascade_fmv_rows": sum(p["fmv_rows"] for p in dels),
        "end_state_violations": violations,
        "orphan_pairs_after": len(orphan_pairs),
        "orphan_pairs_new": sorted(renamed),
        "delete_ids": sorted(deleted),
        "rewrite_ids": sorted(renamed),
    }
    return plan, stats


def _tally(it):
    d = defaultdict(int)
    for x in it:
        d[x] += 1
    return dict(sorted(d.items()))


TSV_COLUMNS = [
    "id", "title", "issue", "year", "variant", "action", "proposed_new_title",
    "index_collision", "collides_with", "twin", "src_bid", "ebay_title",
    "fmv_rows", "fmv_rows_with_value", "bid_links", "bid_fmv_id_refs",
    "created_at", "why",
]


def emit_tsv(plan) -> str:
    """Regenerate rows.tsv from the plan, so the table cannot drift from the code."""
    out = ["\t".join(TSV_COLUMNS)]
    for p in plan:
        out.append("\t".join(
            str(p.get(c, "")).replace("\t", " ").replace("\n", " ") for c in TSV_COLUMNS))
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    import sys
    plan, st = build_plan()
    if "--tsv" in sys.argv:
        sys.stdout.write(emit_tsv(plan))
        raise SystemExit(0)
    for k, v in st.items():
        print(f"{k}: {v}")
    print()
    for p in plan:
        print("%-4d %-8s %-56r -> %-24r coll=%-4s fmv=%d valued=%d links=%d refs=%d" % (
            p["id"], p["action"], p["title"], p["proposed_new_title"],
            p["index_collision"], p["fmv_rows"], p["fmv_rows_with_value"],
            p["bid_links"], p["bid_fmv_id_refs"]))
