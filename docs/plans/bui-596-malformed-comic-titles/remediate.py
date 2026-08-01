#!/usr/bin/env python3
"""BUI-596 remediation: clean the malformed `comics.title` rows.

DRY RUN BY DEFAULT. Writes only with --apply.

Order of operations (BUI-514 ritual):
  1. independent `sqlite3 .backup` of the live DB   -> refuses to continue if it fails
  2. rebuild the plan FROM THE BACKUP (frozen input, so the plan cannot shift
     under a concurrent writer between planning and applying)
  3. apply inside ONE transaction against the live DB
  4. diff live-vs-backup, proving only the intended rows/fields changed
  5. row-count check

Every statement is keyed on `comics.id` — never on a field assumed unique
(the BUI-500 trap: `gixen_item_id` was not unique and a keyed fix went wide).

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
from plan import build_plan, connect_read  # noqa: E402

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
    # prove the backup is readable and complete
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
    bids = c.execute("SELECT COUNT(*) FROM bids").fetchone()[0]
    c.close()
    return {"comics": comics, "fmv": fmv, "links": links, "bids": bids}


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
        b = before["comics"][cid]
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
        b = before["fmv"][fid]
        ch = {k for k in b if b[k] != a[k]}
        if ch:
            problems.append(f"fmv {fid} changed {ch} — no fmv field change was planned")
    # nothing with real comps may be destroyed
    lost_real = [fid for fid in exp_fmv_gone if (before["fmv"][fid]["comps"] or 0) > 0]
    if lost_real:
        problems.append(f"CASCADE would destroy fmv rows carrying real comps: {lost_real}")

    # --- bid_fmvs + bids must be untouched --------------------------------
    if before["links"] != after["links"]:
        problems.append(f"bid_fmvs changed: -{sorted(before['links']-after['links'])} "
                        f"+{sorted(after['links']-before['links'])}")
    if before["bids"] != after["bids"]:
        problems.append(f"bids row count changed {before['bids']} -> {after['bids']}")

    # --- row-count arithmetic --------------------------------------------
    print("\n  row-count check:")
    for tbl, key in (("comics", "comics"), ("fmv", "fmv")):
        exp = len(before[key]) - (len(dels) if tbl == "comics" else len(exp_fmv_gone))
        got = len(after[key])
        mark = "OK " if exp == got else "FAIL"
        print(f"    [{mark}] {tbl}: {len(before[key])} -> {got} (expected {exp})")
        if exp != got:
            problems.append(f"{tbl} row count {got} != expected {exp}")
    print(f"    [OK ] bid_fmvs: {len(before['links'])} (unchanged)")
    print(f"    [OK ] bids: {before['bids']} (unchanged)")

    print("\n  field-level diff:")
    if problems:
        for p in problems:
            print(f"    FAIL {p}")
    else:
        print(f"    [OK ] exactly {len(dels)} comics deleted, "
              f"{len(rews)} comics.title rewritten, no other field touched")
        print(f"    [OK ] {len(exp_fmv_gone)} fmv rows removed by ON DELETE CASCADE, "
              f"0 of them carrying real comps")
        print("    [OK ] bid_fmvs and bids untouched")
    return len(problems)


def main() -> int:
    ap = argparse.ArgumentParser(description="BUI-596 malformed comics.title remediation")
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
    # absolute: a relative path breaks the `file:...?mode=ro` URI used to verify it
    backup_dir = os.path.abspath(os.path.expanduser(args.backup_dir))
    backup = os.path.join(backup_dir, f"db.sqlite.bui-596.{stamp}.bak")
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
    print(f"    comics total          : {st['total_comics']}")
    print(f"    heuristic (# in title): {st['heuristic_hash']}")
    print(f"    RULE (#<own issue>)   : {st['rule_matched']}")
    print(f"    classes               : {st['classes']}")
    print(f"    actions               : {st['actions']}")
    print(f"    index collisions      : {st['collisions']}")
    print(f"    bid_links (must be 0) : {st['bid_links_total']}")
    print(f"    fmv rows w/ real data : {st['fmv_rows_with_data']}")
    print(f"    end-state violations  : {len(st['end_state_violations'])}")

    # ---------------- preflight gates ------------------------------------
    gates = []
    if st["bid_links_total"] != 0:
        gates.append(f"bid_links is {st['bid_links_total']}, expected 0 — a row is load-bearing")
    if st["end_state_violations"]:
        gates.append(f"plan would violate a unique index: {st['end_state_violations']}")
    dels = [p for p in plan if p["action"] == "delete"]
    if any(p["fmv_rows_with_data"] for p in dels):
        gates.append("a row scheduled for delete carries real FMV data")
    if gates:
        print("\nFATAL preflight:")
        for g in gates:
            print(f"    {g}")
        return 4
    print("    preflight             : OK")

    rews = [p for p in plan if p["action"] == "rewrite"]
    print(f"\n    -> DELETE {len(dels)} comics rows "
          f"(cascading {sum(p['fmv_rows'] for p in dels)} empty fmv rows)")
    print(f"    -> REWRITE comics.title on {len(rews)} rows")

    if not args.apply:
        print("\n[3] DRY RUN — no transaction opened, nothing written.")
        print("    Re-run with --apply to take the backup and write.")
        print("\n    first 10 deletes :", [p["id"] for p in dels[:10]])
        print("    first 10 rewrites:",
              [(p["id"], p["proposed_new_title"]) for p in rews[:10]])
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
        print(f"    launchctl bootout gui/$(id -u)/com.comics.server")
        print(f"    cp {backup} {db} && rm -f {db}-wal {db}-shm")
        print(f"    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.comics.server.plist")
        return 6
    print("\nDONE — remediation applied and verified.")
    print(f"Backup retained at {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
