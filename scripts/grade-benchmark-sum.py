#!/usr/bin/env python3
"""Usage totals for a headless /comic:grade run (BUI-1107).

grade-benchmark-sum.py <outer envelope> <seat envelope dir>
    Prints orchestrator, per-seat, seat and whole-run tokens plus each consensus line.
grade-benchmark-sum.py --run-dir <outer envelope> <seat root>
    Prints the <seat root>/run-* work dir the orchestrator's result names (empty if none).
"""
import glob
import json
import re
import sys


def tot(m, k):
    return sum(v[k] for v in m.values())


def usage(m):
    return (tot(m, "outputTokens"), tot(m, "cacheReadInputTokens"), tot(m, "cacheCreationInputTokens"))


def main():
    if sys.argv[1] == "--run-dir":
        r = json.load(open(sys.argv[2])).get("result", "") or ""
        found = re.findall(re.escape(sys.argv[3]) + r"/run-[A-Za-z0-9._-]+", r)
        print(found[-1] if found else "")
        return
    outer = json.load(open(sys.argv[1]))
    o = usage(outer.get("modelUsage", {}))
    print("orchestrator  out %d  read %d  create %d  num_turns %s" % (*o, outer.get("num_turns")))
    s_tot, n = [0, 0, 0], 0
    for f in sorted(glob.glob(f"{sys.argv[2]}/seat-*.json")):
        s = usage(json.load(open(f)).get("modelUsage", {}))
        n += 1
        s_tot = [a + b for a, b in zip(s_tot, s)]
        print("  %-34s out %6d  read %7d  create %6d" % (f.split("/")[-1], *s))
    print("seats (%d)     out %d  read %d  create %d" % (n, *s_tot))
    print("WHOLE RUN     out %d  read %d  create %d" % tuple(a + b for a, b in zip(o, s_tot)))
    for ln in (outer.get("result", "") or "").splitlines():
        if re.match(r"^\|\s*\*\*Consensus", ln) or ln.startswith("###"):
            print(ln[:120])


if __name__ == "__main__":
    main()
