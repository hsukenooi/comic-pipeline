"""audit_collapsed_lots.py — BUI-1386: find won lots collapsed to one issue.

Before BUI-1384 a won lot's collection rows shared one ``gixen_item_id`` and
each issue overwrote the previous, so only the last issue survived. This audit
re-derives every Gixen WON's lot constituents the same way ``record-win-prep``
does (``comic-identify --batch`` is an offline title parser: no provider call),
asks the comics server whether each constituent is owned, and reports the
missing ones.

Read-only by default. ``--apply`` restores only the missing issues through
``POST /api/comics/collection/record-win/commit`` and refuses to run unless the
server build contains BUI-1384 (an older server would overwrite again).

Ownership signals, in order:
  1. ``rows``: collection rows carrying ``gixen_item_id`` + ``full_title``
     (matched with the store's own item_id + issue-key rule). No HTTP endpoint
     exposes these today, so the live run passes none.
  2. series + issue via ``POST /api/comics/collection/check/batch``. This is
     the match that also covers the BUI-1384 workaround rows (re-recorded
     without an item ID).

Only a definite ``not_in_cache`` verdict counts as missing. Anything else that
is not ``in_collection`` (``ambiguous_cross_volume``, a missing verdict) is
"unsure" and is never restored: a false "missing" would duplicate-record an
owned book.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable

import requests

from record_win_prep import (
    _build_win_entry,
    _issue_key,
    _lot_constituents,
    _lot_era_requests,
    fetch_era_evidence,
    filter_ended_won,
    identify_titles,
)

CHECK_BATCH = "/api/comics/collection/check/batch"
COMMIT = "/api/comics/collection/record-win/commit"
REQUIRED_TOKEN = "BUI-1384"
CHECK_CHUNK = 50
_ISSUE_RE = re.compile(r"#\s*([^\s,;:)\]]+)")

# comic-identify only parses comma-style lots; a title like "Power Fantasy
# 1-16" comes back as a single issue. Report those for a human, never restore.
_RANGE_RE = re.compile(r"(?<![\w#.-])#?(\d{1,4})\s*-\s*#?(\d{1,4})(?![\w-])")

IN, MISSING, UNSURE = "in_collection", "not_in_cache", "unsure"


def find_lots(wins: list[dict], identities: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split identified wins into (lots, unparseable_lots). Non-lot wins drop."""
    lots, unparseable = [], []
    for win, identity in zip(wins, identities):
        if identity.get("error") or not identity.get("is_lot"):
            continue
        constituents = _lot_constituents(identity)
        rec = {
            "item_id": win.get("item_id"),
            "title": win.get("title"),
            "win": win,
            "identity": identity,
            "constituents": constituents,
        }
        (unparseable if constituents is None else lots).append(rec)
    return lots, unparseable


def find_range_candidates(wins: list[dict], identities: list[dict]) -> list[dict]:
    """Non-lot wins whose title carries an ``a-b`` issue range (report only)."""
    out = []
    for win, identity in zip(wins, identities):
        if identity.get("is_lot"):
            continue
        for m in _RANGE_RE.finditer(win.get("title") or ""):
            if int(m.group(2)) > int(m.group(1)):
                out.append({"item_id": win.get("item_id"), "title": win.get("title"),
                            "range": m.group(0)})
                break
    return out


def _row_keys(rows: list[dict]) -> set[tuple[str, str]]:
    keys = set()
    for row in rows or []:
        item_id = row.get("gixen_item_id")
        m = _ISSUE_RE.search(str(row.get("full_title") or ""))
        if item_id and m:
            keys.add((str(item_id), _issue_key(m.group(1))))
    return keys


def classify_lot(
    lot: dict,
    verdicts: dict[tuple[str, str], str],
    row_keys: set[tuple[str, str]] = frozenset(),
) -> dict:
    """Per-constituent present / missing / unsure for one lot.

    ``verdicts`` maps (series, issue) to a check ``match_status``. A row match
    on item_id + issue key wins over the series+issue verdict.
    """
    present, missing, unsure = [], [], []
    series = lot["identity"]["series"]
    for issue in lot["constituents"]:
        if (str(lot["item_id"]), _issue_key(str(issue))) in row_keys:
            present.append(issue)
            continue
        status = verdicts.get((series, issue))
        if status == IN:
            present.append(issue)
        elif status == MISSING:
            missing.append(issue)
        else:
            unsure.append(issue)
    return {
        "item_id": lot["item_id"],
        "title": lot["title"],
        "series": series,
        "year": lot["identity"].get("year"),
        "constituents": list(lot["constituents"]),
        "present": present,
        "missing": missing,
        "unsure": unsure,
    }


def fetch_verdicts(
    server_url: str, lots: list[dict], *, session=requests, timeout: float = 60.0
) -> dict[tuple[str, str], str]:
    """One check/batch round trip per CHECK_CHUNK items. Hard-fails on any
    non-200 (R11: never read a failed call as "not owned")."""
    items, seen = [], set()
    for lot in lots:
        for issue in lot["constituents"]:
            key = (lot["identity"]["series"], issue)
            if key in seen:
                continue
            seen.add(key)
            item = {"series": key[0], "issue": issue}
            if lot["identity"].get("year") is not None:
                item["year"] = lot["identity"]["year"]
            items.append(item)
    out: dict[tuple[str, str], str] = {}
    for i in range(0, len(items), CHECK_CHUNK):
        chunk = items[i : i + CHECK_CHUNK]
        resp = session.post(f"{server_url}{CHECK_BATCH}", json={"items": chunk}, timeout=timeout)
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if len(results) != len(chunk):
            raise RuntimeError(f"check/batch returned {len(results)} results for {len(chunk)} items")
        for item, res in zip(chunk, results):
            out[(item["series"], item["issue"])] = res.get("match_status") or UNSURE
    return out


def audit(
    snipes: list[dict],
    server_url: str,
    *,
    rows: list[dict] | None = None,
    identify: Callable[[list[str]], list[dict]] = identify_titles,
    verdicts_fn: Callable[[str, list[dict]], dict] = fetch_verdicts,
) -> dict:
    wins = filter_ended_won(snipes)
    identities = identify([w.get("title") or "" for w in wins])
    lots, unparseable = find_lots(wins, identities)
    verdicts = verdicts_fn(server_url, lots) if lots else {}
    keys = _row_keys(rows or [])
    return {
        "total_wins": len(wins),
        "lots": [dict(classify_lot(lot, verdicts, keys), _lot=lot) for lot in lots],
        "range_candidates": find_range_candidates(wins, identities),
        "unparseable": [{"item_id": u["item_id"], "title": u["title"]} for u in unparseable],
    }


def format_report(result: dict) -> str:
    lines = []
    for lot in result["lots"]:
        lines += [
            f"item_id:      {lot['item_id']}",
            f"title:        {lot['title']}",
            f"constituents: {', '.join(lot['constituents'])}",
            f"present:      {', '.join(lot['present']) or '-'}",
            f"missing:      {', '.join(lot['missing']) or '-'}",
        ]
        if lot["unsure"]:
            lines.append(f"unsure:       {', '.join(lot['unsure'])} (not restored)")
        lines.append("")
    for u in result["unparseable"]:
        lines.append(f"UNPARSEABLE LOT (manual review): {u['item_id']}  {u['title']}")
    for r in result.get("range_candidates", []):
        lines.append(
            f"POSSIBLE RANGE LOT (not parsed as a lot, not restored; check by hand): "
            f"{r['item_id']}  {r['range']}  {r['title']}"
        )
    lots = result["lots"]
    with_missing = [lot for lot in lots if lot["missing"]]
    lines.append(
        f"SUMMARY: {result['total_wins']} WON, {len(lots)} lot(s), "
        f"{len(with_missing)} with missing issues, "
        f"{sum(len(lot['missing']) for lot in with_missing)} missing issue(s), "
        f"{sum(len(lot['unsure']) for lot in lots)} unsure, "
        f"{len(result['unparseable'])} unparseable, "
        f"{len(result.get('range_candidates', []))} possible range lot(s)"
    )
    return "\n".join(lines)


def server_has_fix(git_sha: str | None, *, repo: Path | None = None) -> bool:
    """True only when the server's build commit has BUI-1384 in its history."""
    if not git_sha or not re.fullmatch(r"[0-9a-f]{7,40}", git_sha):
        return False
    try:
        proc = subprocess.run(
            ["git", "log", git_sha, f"--grep={REQUIRED_TOKEN}", "-1", "--format=%H"],
            cwd=repo or Path(__file__).resolve().parent,
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def build_apply_entries(
    result: dict, server_url: str, *, fetch_era=fetch_era_evidence
) -> tuple[list[dict], list[str]]:
    """Entries for ONLY the missing issues (``_build_win_entry`` shape).

    A null-year lot gets the same per-issue era check as record-win-prep; an
    unconfirmed issue is skipped and named in the notes, never recorded."""
    entries, notes = [], []
    for lot in result["lots"]:
        if not lot["missing"]:
            continue
        src = lot["_lot"]
        win, identity = src["win"], src["identity"]
        confirmed: set[str] = set()
        null_year = identity.get("year") is None
        if null_year:
            reqs = [r for r in _lot_era_requests(win, identity) if r["issue"] in lot["missing"]]
            era = fetch_era(server_url, reqs) if reqs else {}
            confirmed = {r["issue"] for r in reqs if era.get(r["item_id"], False)}
            skipped = [i for i in lot["missing"] if i not in confirmed]
            if skipped:
                notes.append(f"{lot['item_id']}: era unconfirmed, not restored: {', '.join(skipped)}")
        for issue in lot["missing"]:
            if null_year and issue not in confirmed:
                continue
            entries.append(_build_win_entry(win, identity, issue=issue))
    return entries, notes


def apply_restore(
    result: dict, server_url: str, *, session=requests, health_sha: str | None = None,
    fetch_era=fetch_era_evidence, repo: Path | None = None,
) -> dict:
    """Guard on the server build, then POST only the missing issues."""
    if health_sha is None:
        h = session.get(f"{server_url}/health", timeout=30)
        h.raise_for_status()
        health_sha = h.json().get("git_sha")
    if not server_has_fix(health_sha, repo=repo):
        raise RuntimeError(
            f"refusing --apply: server git_sha {health_sha!r} does not contain "
            f"{REQUIRED_TOKEN}; deploy it first or the write overwrites rows"
        )
    entries, notes = build_apply_entries(result, server_url, fetch_era=fetch_era)
    if not entries:
        return {"posted": 0, "notes": notes, "response": None}
    resp = session.post(f"{server_url}{COMMIT}", json={"wins": entries}, timeout=600)
    resp.raise_for_status()
    return {"posted": len(entries), "notes": notes, "response": resp.json()}
