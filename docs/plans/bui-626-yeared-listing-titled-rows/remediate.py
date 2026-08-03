"""BUI-626 remediation — the yeared listing-title signature.

Gates copied in intent from docs/plans/bui-600-listing-titled-comic-rows/remediate.py:
  * fingerprint gate   — refuse to act on any row that drifted since review
  * bid-link gate      — bid_fmvs / bids.fmv_id / bids.comic_id must all be 0
                         (bids.fmv_id is a bare INTEGER with NO foreign key, so
                          foreign_key_check cannot see a dangling ref — BUI-600)
  * price-signal gate  — no cascading fmv row may carry low/high/comps
  * twin gate          — every target must have a clean yeared twin
  * post-write diff    — only the intended rows/fields changed
Dry run by default; writes only with --apply.
"""
import argparse
import os
import sqlite3
import sys

DB = os.path.expanduser("~/.comics-server/db.sqlite")

# (title, issue, year, variant) as read during adjudication on 2026-08-03.
ADJUDICATED = {
    330: dict(fingerprint=("Thor FA/ .5 1st issue Hercules", "126", 1966, None),
              twin=295, src_bid=254,
              why="Yeared + partially normalized. Twin 295 'Thor' #126/1966 holds the "
                  "real fmv 317 (1.5/45-60/comps=2). Own fmv 356 is an empty shell. "
                  "Source bid 254 (REMOVED) already points fmv_id=317 at the twin. "
                  "Rewrite impossible: would hard-collide with 295 on idx_comics_tiyv."),
    332: dict(fingerprint=("Thor .5 Maddening Menace of Super-Beast! Jack Kirby Art", "135", 1966, None),
              twin=297, src_bid=256,
              why="Not named in BUI-626; identical signature, found by EM scan. Twin 297 "
                  "holds real fmv 319 (6.5/30-50/comps=2). Own fmv 358 empty shell. "
                  "Source bid 256 is WON but already points fmv_id=319 at the twin, so "
                  "the delete severs nothing."),
    334: dict(fingerprint=("Thor .0 Scourge Super Skrull! Jack Kirby", "142", 1967, None),
              twin=299, src_bid=259,
              why="Not named in BUI-626; identical signature, found by EM scan. Twin 299 "
                  "holds real fmv 322 (7.0/10-10/comps=1) + 417 (4.5/15-15/comps=5). "
                  "Own fmv 360 empty shell. Source bid 259 (LOST) points at 322."),
    335: dict(fingerprint=("Thor .5 2nd Wrecker! Origin Black Bolt! Inhumans", "149", 1968, None),
              twin=301, src_bid=261,
              why="Twin 301 'Thor' #149/1968 holds real fmv 324 (6.5/10-10/comps=3) + "
                  "399 (6.0/10-15/comps=4). Own fmv 361 empty shell. Source bid 261 "
                  "(LOST) already points fmv_id=324 at the twin."),
}


def snapshot(conn):
    return {
        "comics": {r["id"]: dict(r) for r in conn.execute("SELECT * FROM comics")},
        "fmv": {r["id"]: dict(r) for r in conn.execute("SELECT * FROM fmv")},
        "links": sorted(tuple(r) for r in conn.execute("SELECT bid_id, fmv_id, is_primary FROM bid_fmvs")),
        "bids": {r["id"]: dict(r) for r in conn.execute("SELECT * FROM bids")},
    }


def preflight(conn):
    """Return list of gate failures. Empty list == safe to write."""
    problems = []
    for cid, spec in sorted(ADJUDICATED.items()):
        r = conn.execute("SELECT * FROM comics WHERE id=?", (cid,)).fetchone()
        if r is None:
            problems.append(f"{cid}: row is GONE — plan is stale")
            continue
        got = (r["title"], r["issue"], r["year"], r["variant"])
        if got != spec["fingerprint"]:
            problems.append(f"{cid}: fingerprint DRIFTED {got!r} != {spec['fingerprint']!r}")

        tw = conn.execute("SELECT * FROM comics WHERE id=?", (spec["twin"],)).fetchone()
        if tw is None:
            problems.append(f"{cid}: twin {spec['twin']} is GONE")
        elif (tw["issue"], tw["year"]) != (r["issue"], r["year"]):
            problems.append(f"{cid}: twin {spec['twin']} no longer matches issue/year")

        fmvs = conn.execute("SELECT * FROM fmv WHERE comic_id=?", (cid,)).fetchall()
        for f in fmvs:
            if (f["comps"] or 0) > 0 or f["low"] is not None or f["high"] is not None:
                problems.append(f"{cid}: cascading fmv {f['id']} CARRIES PRICE DATA "
                                f"(low={f['low']} high={f['high']} comps={f['comps']})")
        fids = [f["id"] for f in fmvs]
        if fids:
            q = ",".join("?" * len(fids))
            for tbl, sql in (
                ("bid_fmvs", f"SELECT bid_id, fmv_id FROM bid_fmvs WHERE fmv_id IN ({q})"),
                ("bids.fmv_id", f"SELECT id, status FROM bids WHERE fmv_id IN ({q})"),
            ):
                hits = conn.execute(sql, fids).fetchall()
                if hits:
                    problems.append(f"{cid}: LIVE {tbl} refs {[tuple(h) for h in hits]}")
        bc = conn.execute("SELECT id, status FROM bids WHERE comic_id=?", (cid,)).fetchall()
        if bc:
            problems.append(f"{cid}: LIVE bids.comic_id refs {[tuple(h) for h in bc]}")
    return problems


def verify(before, after):
    dels = set(ADJUDICATED)
    problems = []
    gone = set(before["comics"]) - set(after["comics"])
    if gone != dels:
        problems.append(f"deleted comics {sorted(gone)} != planned {sorted(dels)}")
    if set(after["comics"]) - set(before["comics"]):
        problems.append("unexpected NEW comics rows")
    for cid, a in after["comics"].items():
        b = before["comics"].get(cid)
        if b and {k for k in b if b[k] != a[k]}:
            problems.append(f"comics {cid} unexpectedly CHANGED "
                            f"{ {k: (b[k], a[k]) for k in b if b[k] != a[k]} }")

    exp = {fid for fid, f in before["fmv"].items() if f["comic_id"] in dels}
    got = set(before["fmv"]) - set(after["fmv"])
    if got != exp:
        problems.append(f"fmv deleted {sorted(got)} != expected cascade {sorted(exp)}")
    if set(after["fmv"]) - set(before["fmv"]):
        problems.append("unexpected NEW fmv rows")
    for fid, a in after["fmv"].items():
        b = before["fmv"].get(fid)
        if b and {k for k in b if b[k] != a[k]}:
            problems.append(f"fmv {fid} unexpectedly CHANGED")

    if before["links"] != after["links"]:
        problems.append("bid_fmvs CHANGED — nothing was planned there")
    if before["bids"] != after["bids"]:
        ch = [i for i in before["bids"] if before["bids"].get(i) != after["bids"].get(i)]
        problems.append(f"bids CHANGED — nothing was planned there: {ch}")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")

    print(f"DB: {a.db}")
    print(f"Plan: DELETE {sorted(ADJUDICATED)} (4 rows), cascading their fmv shells.\n")

    problems = preflight(conn)
    if problems:
        print("PREFLIGHT GATES FAILED — refusing to write:")
        for p in problems:
            print("  ✗", p)
        return 1
    print("Preflight gates: PASS (fingerprints match, twins present, 0 bid links, 0 price signal)\n")

    before = snapshot(conn)
    print("before:", {k: len(v) for k, v in before.items()})

    if not a.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        for cid, spec in sorted(ADJUDICATED.items()):
            print(f"  would DELETE comics {cid}  (twin {spec['twin']}, src bid {spec['src_bid']})")
        return 0

    with conn:
        conn.executemany("DELETE FROM comics WHERE id=?", [(c,) for c in sorted(ADJUDICATED)])

    after = snapshot(conn)
    print("after: ", {k: len(v) for k, v in after.items()})

    problems = verify(before, after)
    print()
    if problems:
        print("POST-WRITE VERIFY FAILED:")
        for p in problems:
            print("  ✗", p)
        return 2
    print("POST-WRITE VERIFY: PASS — only the 4 planned comics rows and their "
          "4 empty-shell fmv rows changed; bids and bid_fmvs byte-identical.")

    dangling = conn.execute(
        "SELECT COUNT(*) FROM bids WHERE fmv_id IS NOT NULL "
        "AND fmv_id NOT IN (SELECT id FROM fmv)").fetchone()[0]
    print(f"dangling bids.fmv_id refs (the no-FK trap): {dangling}")
    print("integrity_check:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    print("foreign_key_check:", "clean" if not fk else fk)
    return 0 if dangling == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
