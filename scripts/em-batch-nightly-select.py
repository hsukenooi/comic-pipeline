#!/usr/bin/env python3
"""Pick the tickets for tonight's autonomous em-batch run (BUI-972).

Prints one Linear identifier per line, highest-value first, capped. Reads the
Linear API key the way linear-method's add-link.sh does (credentials.toml, or
LINEAR_API_KEY), and never prints it.

Selection (all must hold):
  team BUI, label `comics`, unassigned, state in Today / Soon / Someday.
In Progress and Blocked are excluded on purpose: In Progress means another
agent (or a previous night) owns it; Blocked is Hsu Ken's own state (waiting on
something he cannot do right now). A run that needs him does NOT set Blocked: it
assigns the ticket to him (`linear issue update ID -a hsukenooi`), and the
unassigned filter then keeps it out of every later night.

Also skipped (BUI-1257): every ticket listed in a chunk of any
`~/.local/state/em-batch/*/queue.md` whose status is not `done` (pending,
running, held). An attended queue leaves its tickets unassigned until the chunk
spawns, so without this the nightly run merges work the queue still owns. A
ticket in a `done` chunk stays eligible. A queue that cannot be read or parsed
is skipped with a note on stderr. Override the root with EM_BATCH_STATE_ROOT or
--state-root (tests do).

Order: Today, then Soon, then Someday; within a state by Linear priority
(urgent first; "no priority" sorts last); then oldest first.

Stdlib only -- launchd's /usr/bin/python3 is 3.9 with no third-party packages.
"""
import argparse
import glob
import json
import os
import re
import sys
import urllib.request

API = "https://api.linear.app/graphql"
DEFAULT_STATE_ROOT = "~/.local/state/em-batch"
CHUNK_RE = re.compile(r"^##\s+Chunk\s+\d+")
H2_RE = re.compile(r"^##\s")
STATUS_RE = re.compile(r"\*\*Status\*\*\s*[\u2013\u2014-]\s*(\w+)", re.I)
TICKETS_RE = re.compile(r"\*\*Tickets\*\*\s*[\u2013\u2014-]\s*(.*)", re.I)
HEADING_ID_RE = re.compile(r"^###\s+([A-Z][A-Z0-9]*-\d+)\s*:")
ID_RE = re.compile(r"(?<![A-Za-z0-9-])[A-Z][A-Z0-9]*-\d+(?!\d)")
STATE_RANK = {"Today": 0, "Soon": 1, "Someday": 2}

QUERY = """
query($after: String) {
  issues(first: 100, after: $after, filter: {
    team: { key: { eq: "BUI" } },
    labels: { name: { eq: "comics" } },
    state: { name: { in: ["Today", "Soon", "Someday"] } },
    assignee: { null: true }
  }) {
    nodes { identifier title priority createdAt state { name } }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def queue_tickets(text):
    """Ticket ids in chunks of one queue.md whose status is not `done`.

    Raises ValueError when the file has no `## Chunk <n>` heading.
    """
    chunks, cur = [], None
    for line in text.splitlines():
        if CHUNK_RE.match(line):
            cur = {"status": None, "listed": set(), "headings": set()}
            chunks.append(cur)
        elif H2_RE.match(line):
            cur = None
        elif cur is not None:
            m = STATUS_RE.search(line)
            if m and cur["status"] is None:
                cur["status"] = m.group(1).lower()
            m = TICKETS_RE.search(line)
            if m:
                cur["listed"].update(ID_RE.findall(m.group(1)))
            m = HEADING_ID_RE.match(line)
            if m:
                cur["headings"].add(m.group(1))
    if not chunks:
        raise ValueError("no '## Chunk <n>' heading")
    found = set()
    for c in chunks:
        if c["status"] != "done":       # a missing status counts as not done
            found |= c["listed"] or c["headings"]
    return found


def attended_tickets(state_root):
    """Tickets owned by unfinished chunks of any attended queue under state_root."""
    owned = set()
    root = os.path.expanduser(state_root)
    for path in sorted(glob.glob(os.path.join(glob.escape(root), "*", "queue.md"))):
        try:
            with open(path, encoding="utf-8") as fh:
                owned |= queue_tickets(fh.read())
        except (OSError, ValueError) as exc:    # UnicodeDecodeError is a ValueError
            print("skipping queue %s: %s" % (path, exc), file=sys.stderr)
    return owned


def api_key(creds_path):
    env = os.environ.get("LINEAR_API_KEY")
    if env:
        return env
    want, best, section = None, None, ""
    with open(creds_path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^\[(.+)\]$", line)
            if m:
                section = m.group(1).strip().strip('"\'').split(".")[-1]
                continue
            if "=" not in line:
                continue
            k, v = (s.strip() for s in line.split("=", 1))
            v = v.split(" #")[0].strip().strip('"\'')
            if section == "" and k == "default":
                want = v
            elif v.startswith(("lin_api_", "lin_oauth_")):
                if section == want or best is None:
                    best = v
    if not best:
        sys.exit("no Linear API key in %s" % creds_path)
    return best


def fetch_all(key):
    nodes, after = [], None
    while True:
        body = json.dumps({"query": QUERY, "variables": {"after": after}}).encode()
        req = urllib.request.Request(API, data=body, headers={
            "Content-Type": "application/json",
            # A personal API key goes bare; only OAuth tokens take "Bearer".
            "Authorization": key if key.startswith("lin_api_") else "Bearer " + key,
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            doc = json.load(resp)
        if doc.get("errors"):
            sys.exit("Linear API error: %s" % doc["errors"][0].get("message"))
        page = doc["data"]["issues"]
        nodes.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        after = page["pageInfo"]["endCursor"]


def rank(issue):
    prio = issue["priority"] or 5          # 0 = none in Linear; sort it last
    return (STATE_RANK.get(issue["state"]["name"], 9), prio, issue["createdAt"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=8)
    ap.add_argument("--exclude", action="append", default=[],
                    help="identifier to leave out (repeatable)")
    ap.add_argument("--state-root",
                    default=os.environ.get("EM_BATCH_STATE_ROOT", DEFAULT_STATE_ROOT),
                    help="em-batch state dir holding <run-id>/queue.md files")
    ap.add_argument("--json", action="store_true", help="full rows, not just ids")
    ap.add_argument("--creds", default=os.path.expanduser("~/.config/linear/credentials.toml"))
    args = ap.parse_args()

    skip = set(args.exclude) | attended_tickets(args.state_root)
    issues = [i for i in fetch_all(api_key(args.creds)) if i["identifier"] not in skip]
    issues.sort(key=rank)
    picked = issues[: args.cap]
    if args.json:
        json.dump({"picked": picked, "candidates": len(issues)}, sys.stdout, indent=1)
        print()
    else:
        for i in picked:
            print(i["identifier"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
