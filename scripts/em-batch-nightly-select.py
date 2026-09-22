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

Order: Today, then Soon, then Someday; within a state by Linear priority
(urgent first; "no priority" sorts last); then oldest first.

Stdlib only -- launchd's /usr/bin/python3 is 3.9 with no third-party packages.
"""
import argparse
import json
import os
import re
import sys
import urllib.request

API = "https://api.linear.app/graphql"
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
    ap.add_argument("--json", action="store_true", help="full rows, not just ids")
    ap.add_argument("--creds", default=os.path.expanduser("~/.config/linear/credentials.toml"))
    args = ap.parse_args()

    issues = [i for i in fetch_all(api_key(args.creds)) if i["identifier"] not in args.exclude]
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
