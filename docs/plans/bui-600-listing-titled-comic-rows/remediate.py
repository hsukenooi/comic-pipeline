#!/usr/bin/env python3
"""BUI-600 remediation: clean the 12 listing-titled `comics` rows that carry no `#`.

DRY RUN BY DEFAULT. Writes only with --apply.

Order of operations (BUI-514 ritual, unchanged from BUI-596):
  1. independent `sqlite3 .backup` of the live DB   -> refuses to continue if it fails
  2. rebuild the plan FROM THE BACKUP (frozen input, so the plan cannot shift
     under a concurrent writer between planning and applying)
  3. apply inside ONE transaction against the live DB
  4. diff live-vs-backup, proving only the intended rows/fields changed
  5. row-count check

Every statement is keyed on `comics.id` — never on a field assumed unique
(the BUI-500 trap: `gixen_item_id` was not unique and a keyed fix went wide).

TWO GATES BUI-596 DID NOT HAVE, both because these rows are load-bearing where
its 173 were not:

  * `bids.fmv_id` references. That column is a bare INTEGER with NO foreign key,
    so an ON DELETE CASCADE through `fmv` does not null it — it leaves a DANGLING
    reference. 4 of these 12 rows are referenced that way. Any row scheduled for
    delete must have zero.
  * A fingerprint check on all 12 rows. The plan is a hand-adjudicated id list,
    so it is valid ONLY for the exact rows a human read. If any row's
    (title, issue, year, variant) has drifted, the adjudication no longer applies
    and the script aborts rather than acting on an id it cannot vouch for.

Usage:
    python3 remediate.py                 # dry run, prints the plan + a simulated diff
    python3 remediate.py --apply         # takes the backup, then writes
    python3 remediate.py --db /path/to/db.sqlite
"""
import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plan import OUT_OF_SCOPE, build_plan, connect_read  # noqa: E402

DEFAULT_DB = os.path.expanduser("~/.comics-server/db.sqlite")


def take_backup(db_path: str, dest: str) -> None:
    """`sqlite3 <db> ".backup <dest>"` — the ONLY correct way to copy a WAL DB.

    A plain `cp` captures the main file without the -wal, i.e. a stale snapshot.
    Raises on any failure; the caller must abort.
    """
    exe = shutil.which("sqlite3")
    if not exe:
        raise RuntimeError("sqlite3 CLI not on PATH — cannot take the required backup")
    r = subprocess.run([exe, db_path, f".backup '{dest}'"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"sqlite3 .backup failed (rc={r.returncode}): {r.stderr.strip()}")
    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        raise RuntimeError(f"backup file missing or empty: {dest}")
    c = connect_read(dest)
    n = c.execute("SELECT COUNT(*) FROM comics").fetchone()[0]
    ok = c.execute("PRAGMA integrity_check").fetchone()[0]
    c.close()
    if ok != "ok":
        raise RuntimeError(f"backup failed integrity_check: {ok}")
    print(f"  backup OK: {dest} ({os.path.getsize(dest)} bytes, comics={n}, integrity=ok)")


def snapshot(db_path: str):
    c = connect_read(db_path)
    comics = {r["id"]: dict(r) for r in c.execute(
        "SELECT id, title, issue, year, variant, locg_id, locg_variant_id, created_at FROM comics")}
    fmv = {r["id"]: dict(r) for r in c.execute(
        "SELECT id, comic_id, grade, low, high, comps, confidence, notes, updated_at,"
        " flag_reason FROM fmv")}
    links = {(r["bid_id"], r["fmv_id"]) for r in c.execute("SELECT bid_id, fmv_id FROM bid_fmvs")}
    # bids.fmv_id / bids.comic_id have NO foreign key — capture them so the diff
    # can prove no reference was left dangling.
    bidrefs = {r["id"]: (r["comic_id"], r["fmv_id"])
               for r in c.execute("SELECT id, comic_id, fmv_id FROM bids")}
    c.close()
    return {"comics": comics, "fmv": fmv, "links": links, "bidrefs": bidrefs}


def verify(before, after, plan) -> int:
    """Diff before/after and prove only the intended rows/fields changed."""
    dels = {p["id"] for p in plan if p["action"] == "delete"}
    rews = {p["id"]: p["proposed_new_title"] for p in plan if p["action"] == "rewrite"}
    problems = []

    # --- comics ---------------------------------------------------------
    gone = set(before["comics"]) - set(after["comics"])
    added = set(after["comics"]) - set(before["comics"])
    if gone != dels:
        problems.append(f"deleted comics {sorted(gone)} != planned {sorted(dels)}")
    if added:
        problems.append(f"unexpected NEW comics rows: {sorted(added)}")
    for cid, a in after["comics"].items():
        b = before["comics"].get(cid)
        if b is None:
            continue        # already reported as an unexpected NEW row above;
                            # indexing blindly here would raise KeyError and
                            # crash verify() AFTER the transaction committed,
                            # losing the diff report exactly when it matters.
        changed = {k for k in b if b[k] != a[k]}
        if not changed:
            continue
        if cid in rews and changed == {"title"} and a["title"] == rews[cid]:
            continue
        problems.append(f"comics {cid} unexpected field change {changed}: "
                        f"{ {k: (b[k], a[k]) for k in changed} }")
    for cid, want in rews.items():
        if cid not in after["comics"]:
            problems.append(f"rewrite target comics {cid} vanished")
        elif after["comics"][cid]["title"] != want:
            problems.append(f"comics {cid} title is {after['comics'][cid]['title']!r},"
                            f" expected {want!r}")

    # --- fmv: only rows cascading off a deleted comic may disappear -------
    exp_fmv_gone = {fid for fid, f in before["fmv"].items() if f["comic_id"] in dels}
    fmv_gone = set(before["fmv"]) - set(after["fmv"])
    if fmv_gone != exp_fmv_gone:
        problems.append(f"fmv deleted {sorted(fmv_gone)} != expected cascade {sorted(exp_fmv_gone)}")
    if set(after["fmv"]) - set(before["fmv"]):
        problems.append(f"unexpected NEW fmv rows: {sorted(set(after['fmv']) - set(before['fmv']))}")
    for fid, a in after["fmv"].items():
        b = before["fmv"].get(fid)
        if b is None:
            continue        # reported as an unexpected NEW fmv row above
        ch = {k for k in b if b[k] != a[k]}
        if ch:
            problems.append(f"fmv {fid} changed {ch} — no fmv field change was planned")
    # nothing carrying ANY price signal may be destroyed (wider than BUI-596's
    # comps-only test: row 204 has comps=0 with populated low/high)
    lost_valued = [fid for fid in exp_fmv_gone
                   if (before["fmv"][fid]["comps"] or 0) > 0
                   or before["fmv"][fid]["low"] is not None
                   or before["fmv"][fid]["high"] is not None]
    if lost_valued:
        problems.append(f"CASCADE would destroy fmv rows carrying price data: {lost_valued}")

    # --- bid_fmvs + bids must be untouched --------------------------------
    if before["links"] != after["links"]:
        problems.append(f"bid_fmvs changed: -{sorted(before['links']-after['links'])} "
                        f"+{sorted(after['links']-before['links'])}")
    if before["bidrefs"] != after["bidrefs"]:
        diff = {k for k in before["bidrefs"] if before["bidrefs"].get(k) != after["bidrefs"].get(k)}
        problems.append(f"bids rows changed (count or comic_id/fmv_id): {sorted(diff)}")

    # --- no dangling reference left behind (no FK protects these) ---------
    dangling_fmv = sorted(bid for bid, (_c, f) in after["bidrefs"].items()
                          if f is not None and f not in after["fmv"])
    dangling_comic = sorted(bid for bid, (c, _f) in after["bidrefs"].items()
                            if c is not None and c not in after["comics"])
    if dangling_fmv:
        problems.append(f"bids.fmv_id left DANGLING for bids {dangling_fmv}")
    if dangling_comic:
        problems.append(f"bids.comic_id left DANGLING for bids {dangling_comic}")
    orphan_fmv = sorted(f for f, r in after["fmv"].items() if r["comic_id"] not in after["comics"])
    orphan_link = sorted(l for l in after["links"] if l[1] not in after["fmv"])
    if orphan_fmv:
        problems.append(f"orphaned fmv rows (comic_id gone): {orphan_fmv}")
    if orphan_link:
        problems.append(f"orphaned bid_fmvs rows (fmv_id gone): {orphan_link}")

    # --- row-count arithmetic --------------------------------------------
    print("\n  row-count check:")
    for tbl, key in (("comics", "comics"), ("fmv", "fmv")):
        exp = len(before[key]) - (len(dels) if tbl == "comics" else len(exp_fmv_gone))
        got = len(after[key])
        mark = "OK " if exp == got else "FAIL"
        print(f"    [{mark}] {tbl}: {len(before[key])} -> {got} (expected {exp})")
        if exp != got:
            problems.append(f"{tbl} row count {got} != expected {exp}")
    # These two must be byte-identical, not merely equal in count — print the
    # real verdict rather than a hardcoded OK.
    lm = "OK " if before["links"] == after["links"] else "FAIL"
    bm = "OK " if before["bidrefs"] == after["bidrefs"] else "FAIL"
    print(f"    [{lm}] bid_fmvs: {len(before['links'])} -> {len(after['links'])} (must be identical)")
    print(f"    [{bm}] bids: {len(before['bidrefs'])} -> {len(after['bidrefs'])} "
          f"(rows + comic_id/fmv_id must be identical)")

    print("\n  field-level diff:")
    if problems:
        for p in problems:
            print(f"    FAIL {p}")
    else:
        print(f"    [OK ] exactly {len(dels)} comics deleted, "
              f"{len(rews)} comics.title rewritten, no other field touched")
        print(f"    [OK ] {len(exp_fmv_gone)} fmv rows removed by ON DELETE CASCADE, "
              f"0 of them carrying any price data")
        print("    [OK ] bid_fmvs and bids untouched; 0 dangling bids.fmv_id / bids.comic_id")
    return len(problems)


def main() -> int:
    ap = argparse.ArgumentParser(description="BUI-600 listing-titled comics.title remediation")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--backup-dir", default=os.path.expanduser("~/.comics-server/backups"))
    args = ap.parse_args()

    db = os.path.abspath(os.path.expanduser(args.db))
    if not os.path.exists(db):
        print(f"FATAL: no such DB: {db}")
        return 2
    print(f"DB      : {db}")
    print(f"mode    : {'APPLY (WILL WRITE)' if args.apply else 'DRY RUN (no writes)'}")

    # ---------------- 1. backup ------------------------------------------
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = os.path.abspath(os.path.expanduser(args.backup_dir))
    backup = os.path.join(backup_dir, f"db.sqlite.bui-600.{stamp}.bak")
    if args.apply:
        os.makedirs(backup_dir, exist_ok=True)
        print(f"\n[1] taking sqlite3 .backup -> {backup}")
        try:
            take_backup(db, backup)
        except Exception as e:                     # noqa: BLE001 - must abort loudly
            print(f"FATAL: backup failed, refusing to write: {e}")
            return 3
        plan_src = backup       # plan from the frozen snapshot
    else:
        print("\n[1] backup: SKIPPED (dry run) — --apply would write it to "
              f"{args.backup_dir}")
        plan_src = db

    # ---------------- 2. plan --------------------------------------------
    print(f"\n[2] building plan from {plan_src}")
    plan, st = build_plan(plan_src)
    print(f"    comics total           : {st['total_comics']}")
    print(f"    adjudicated ids        : {st['adjudicated']} (hand-curated list)")
    print(f"    fingerprint problems   : {len(st['fingerprint_problems'])}")
    print(f"    vocabulary tripwire    : scan={st['vocabulary_scan_size']} "
          f"extra={st['tripwire_extra']} missing={st['tripwire_missing']}")
    print(f"    actions                : {st['actions']}")
    print(f"    index collisions       : {st['collisions']}")
    print(f"    delete bid_links       : {st['delete_bid_links']} (must be 0)")
    print(f"    delete bids.fmv_id refs: {st['delete_bid_fmv_id_refs']} (must be 0)")
    print(f"    delete valued fmv rows : {st['delete_valued_fmv']} (must be 0)")
    print(f"    fmv rows cascading     : {st['cascade_fmv_rows']}")
    print(f"    end-state violations   : {len(st['end_state_violations'])}")
    print(f"    yearless orphan pairs  : {st['orphan_pairs_after']} after "
          f"(+4 intentional: {st['orphan_pairs_new']})")
    print(f"    known out-of-scope     : {sorted(OUT_OF_SCOPE)} (NOT touched)")

    # ---------------- preflight gates ------------------------------------
    gates = []
    if st["fingerprint_problems"]:
        gates.append("the adjudicated rows have drifted since review: "
                     + "; ".join(st["fingerprint_problems"]))
    if st["tripwire_extra"]:
        gates.append(f"vocabulary tripwire found UNADJUDICATED rows {st['tripwire_extra']} "
                     "— a new listing-titled row appeared; re-adjudicate before writing")
    if st["tripwire_missing"]:
        gates.append(f"vocabulary tripwire no longer matches adjudicated rows "
                     f"{st['tripwire_missing']} — the table moved")
    if st["delete_bid_links"] != 0:
        gates.append(f"a row scheduled for delete has {st['delete_bid_links']} bid_fmvs links")
    if st["delete_bid_fmv_id_refs"] != 0:
        gates.append(f"a row scheduled for delete is referenced by "
                     f"{st['delete_bid_fmv_id_refs']} bids.fmv_id (would dangle — no FK)")
    if st["delete_valued_fmv"] != 0:
        gates.append("a row scheduled for delete carries fmv price data")
    if st["end_state_violations"]:
        gates.append(f"plan would violate a unique index: {st['end_state_violations']}")
    if st["collisions"]:
        gates.append(f"a rewrite target collides with an existing row ({st['collisions']})")
    if gates:
        print("\nFATAL preflight:")
        for g in gates:
            print(f"    {g}")
        return 4
    print("    preflight              : OK")

    dels = [p for p in plan if p["action"] == "delete"]
    rews = [p for p in plan if p["action"] == "rewrite"]
    print(f"\n    -> DELETE {len(dels)} comics rows "
          f"(cascading {sum(p['fmv_rows'] for p in dels)} empty fmv rows)")
    for p in dels:
        print(f"         {p['id']:<4} {p['title']!r} (issue {p['issue']}) "
              f"-> twin {p['twin']}")
    print(f"    -> REWRITE comics.title on {len(rews)} rows")
    for p in rews:
        print(f"         {p['id']:<4} {p['title']!r} -> {p['proposed_new_title']!r} "
              f"(issue {p['issue']}, links={p['bid_links']}, twin {p['twin']})")

    if not args.apply:
        print("\n[3] DRY RUN — no transaction opened, nothing written.")
        print("    Re-run with --apply to take the backup and write.")
        print(f"\n    expected: comics {st['total_comics']} -> "
              f"{st['total_comics'] - len(dels)}, "
              f"fmv -{st['cascade_fmv_rows']}, bid_fmvs and bids unchanged")
        return 0

    # ---------------- 3. apply -------------------------------------------
    before = snapshot(db)
    print(f"\n[3] applying in ONE transaction against {db}")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")       # required for ON DELETE CASCADE
    try:
        with conn:                                  # commits on success, rolls back on raise
            conn.execute("BEGIN IMMEDIATE")
            for p in rews:
                cur = conn.execute(
                    "UPDATE comics SET title=? WHERE id=?",
                    (p["proposed_new_title"], p["id"]),
                )
                if cur.rowcount != 1:               # assert exactly-one-match (BUI-500)
                    raise RuntimeError(
                        f"UPDATE comics id={p['id']} matched {cur.rowcount} rows, expected 1")
            for p in dels:
                cur = conn.execute("DELETE FROM comics WHERE id=?", (p["id"],))
                if cur.rowcount != 1:
                    raise RuntimeError(
                        f"DELETE comics id={p['id']} matched {cur.rowcount} rows, expected 1")
        print("    transaction committed")
    except Exception as e:                          # noqa: BLE001
        print(f"FATAL: transaction rolled back: {e}")
        print(f"       the DB is unchanged; backup is at {backup}")
        return 5
    finally:
        conn.close()

    # ---------------- 4+5. diff vs backup + row counts --------------------
    print(f"\n[4] diffing live DB against the backup {backup}")
    after = snapshot(db)
    base = snapshot(backup)
    if base["comics"].keys() != before["comics"].keys():
        print("    WARNING: the DB changed between backup and apply (concurrent writer)")
    nprob = verify(base, after, plan)
    if nprob:
        print(f"\nVERIFICATION FAILED ({nprob} problems). Restore with:")
        print("    launchctl bootout gui/$(id -u)/com.comics.server")
        print(f"    cp {backup} {db} && rm -f {db}-wal {db}-shm")
        print("    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.comics.server.plist")
        return 6
    print("\nDONE — remediation applied and verified.")
    print(f"Backup retained at {backup}")
    print("\nOptional follow-up (NOT run here): the 4 rewritten rows now sit beside")
    print("their yeared twin as yearless orphans. `POST /api/sweep-orphans?dry_run=false`")
    print("collapses them losslessly — but it would ALSO merge the 13 pre-existing")
    print("pairs left by BUI-596. Preview with dry_run=true first.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
