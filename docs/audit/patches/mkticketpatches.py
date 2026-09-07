#!/usr/bin/env python3
"""Split the audit edits into one patch per Linear ticket, generated
cumulatively in merge order so applying them in sequence is conflict-free.

Ticket N's patch = diff(state after tickets 1..N-1, state after tickets 1..N).
Also verifies the sequence in a throwaway worktree.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mkpatch  # noqa: E402  (module-level E() calls populate mkpatch.EDITS)

REPO = mkpatch.REPO
OUT = Path(__file__).resolve().parent

# Merge order and the finding tags each ticket owns.
TICKETS = [
    ("BUI-890", {"H3", "M14"}),
    ("BUI-889", {"H1", "M4"}),
    ("BUI-894", {"H2", "H4", "H12-210", "M12"}),
    ("BUI-896", {"H5"}),
    ("BUI-893", {"M2", "H9", "M5", "M11"}),
    ("BUI-892", {"H6", "H7", "H10", "H12", "H13", "M7", "M10", "M13", "M15", "M16", "M18"}),
    ("BUI-891", {"H11"}),
    ("BUI-899", {"M6"}),
    ("BUI-895", {"H8"}),
    ("BUI-898", {"M8", "M9"}),
    ("BUI-897", {"M3"}),
]


def main() -> int:
    owned = set().union(*(t for _, t in TICKETS))
    tags = {e[0] for e in mkpatch.EDITS}
    unowned = tags - owned
    if unowned:
        print(f"FAIL: edits with no ticket: {sorted(unowned)}", file=sys.stderr)
        return 1

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    state: dict[str, str] = {}   # relpath -> current content (after applied tickets)

    def current(rel: str) -> str:
        if rel not in state:
            state[rel] = (REPO / rel).read_text()
        return state[rel]

    for ticket, ticket_tags in TICKETS:
        before = {rel: txt for rel, txt in state.items()}
        touched = set()
        for finding, rel, old, new, count in mkpatch.EDITS:
            if finding not in ticket_tags:
                continue
            s = current(rel)
            if rel not in before:
                before[rel] = s
            n = s.count(old)
            if n != count:
                print(f"FAIL [{ticket} {finding}] {rel}: expected {count}, found {n}", file=sys.stderr)
                return 1
            state[rel] = s.replace(old, new)
            touched.add(rel)
        chunks = []
        for rel in sorted(touched):
            a = OUT / f".a_{ticket}" / rel
            b = OUT / f".b_{ticket}" / rel
            a.parent.mkdir(parents=True, exist_ok=True)
            b.parent.mkdir(parents=True, exist_ok=True)
            a.write_text(before[rel])
            b.write_text(state[rel])
            r = subprocess.run(
                ["diff", "-u", "--label", f"a/{rel}", "--label", f"b/{rel}", str(a), str(b)],
                capture_output=True, text=True,
            )
            if r.returncode == 1:
                chunks.append(r.stdout)
        (OUT / f"{ticket}.patch").write_text("".join(chunks))
        shutil.rmtree(OUT / f".a_{ticket}")
        shutil.rmtree(OUT / f".b_{ticket}")
        print(f"{ticket}: {len(touched)} file(s), {sum(c.count(chr(10)+'@@') for c in chunks)} hunk(s)")

    # Verify: apply in order inside a throwaway worktree.
    wt = OUT / ".wt"
    if wt.exists():
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)], capture_output=True)
    subprocess.run(["git", "-C", str(REPO), "worktree", "add", "-q", str(wt), "HEAD"], check=True)
    ok = True
    try:
        for ticket, _ in TICKETS:
            r = subprocess.run(["git", "-C", str(wt), "apply", str(OUT / f"{ticket}.patch")], capture_output=True, text=True)
            if r.returncode != 0:
                print(f"SEQUENCE FAIL at {ticket}: {r.stderr}", file=sys.stderr)
                ok = False
                break
        if ok:
            # The sequential result must equal the combined patch's result.
            r = subprocess.run(["git", "-C", str(wt), "diff", "--stat"], capture_output=True, text=True)
            print("sequential apply: OK\n" + r.stdout.strip().splitlines()[-1])
    finally:
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)], capture_output=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
