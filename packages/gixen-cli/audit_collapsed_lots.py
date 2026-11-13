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

Only a definite ``not_in_cache`` verdict counts as missing (see
``fetch_verdicts``: fresh cache, not wish-listed, exact match, no fuzzy series
resolution). Anything else that is not ``in_collection`` is "unsure" and is
never restored: a false "missing" would duplicate-record an
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
STATUS_PATH = "/api/comics/collection/status"
RESOLVE_PATH = "/api/comics/collection/series-names/resolve"
STALE_CACHE_DAYS = 14  # mirrors locg.check_batch.STALE_CACHE_DAYS
KNOWN_STATUSES = frozenset({"in_collection", "not_in_cache", "ambiguous_cross_volume"})
COMMIT = "/api/comics/collection/record-win/commit"
REQUIRED_COMMIT = "4a1a651"  # BUI-1384 collision-refusal commit
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


def _get_json(session, url: str, timeout: float) -> dict:
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"unparseable response from {url}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected response shape from {url}")
    return data


def _post_json(session, url: str, body: dict, timeout: float) -> dict:
    resp = session.post(url, json=body, timeout=timeout)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"unparseable response from {url}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected response shape from {url}")
    return data


def fetch_verdicts(
    server_url: str, lots: list[dict], *, session=requests, timeout: float = 60.0
) -> dict[tuple[str, str], str]:
    """Collection check for every (series, issue), reduced to IN / MISSING / UNSURE.

    Ports ``locg.check_batch``'s trust rules (gixen-cli cannot import locg).
    A raw ``not_in_cache`` is only MISSING when it is a definite not-owned:
    the store was imported and is fresh, the book is not wish-listed, the
    match is not a non-exact one, and the series name does not resolve to a
    different catalog spelling (Pattern C). Otherwise it is UNSURE. Hard-fails
    (R11) on any non-200, malformed row, or unknown ``match_status``."""
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
    if not items:
        return {}
    _get_json(session, f"{server_url}/health", timeout)
    status = _get_json(session, f"{server_url}{STATUS_PATH}", timeout)
    if status.get("last_full_import") is None:
        raise RuntimeError("collection is empty on the comics server (never imported)")
    age = status.get("cache_age_days")
    stale = isinstance(age, int) and age > STALE_CACHE_DAYS

    rows: list[tuple[dict, dict]] = []
    for i in range(0, len(items), CHECK_CHUNK):
        chunk = items[i : i + CHECK_CHUNK]
        data = _post_json(session, f"{server_url}{CHECK_BATCH}", {"items": chunk}, timeout)
        results = data.get("results") or []
        if len(results) != len(chunk):
            raise RuntimeError(f"check/batch returned {len(results)} results for {len(chunk)} items")
        for item, res in zip(chunk, results):
            if not isinstance(res, dict):
                raise RuntimeError("check/batch row was not an object")
            if res.get("match_status") not in KNOWN_STATUSES:
                raise RuntimeError(
                    f"check/batch row for {item['series']} #{item['issue']} has an "
                    f"unknown match_status {res.get('match_status')!r}"
                )
            rows.append((item, res))

    names = sorted({it["series"] for it, r in rows if r["match_status"] == MISSING})
    fuzzy: set[str] = set()
    if names:
        data = _post_json(session, f"{server_url}{RESOLVE_PATH}", {"names": names}, timeout)
        results = data.get("results")
        if not isinstance(results, list):
            raise RuntimeError("series-name resolve response had a non-list 'results'")
        for row in results:
            if isinstance(row, dict) and row.get("match_kind") == "fuzzy" and row.get("resolved"):
                fuzzy.add(str(row.get("query")))

    out: dict[tuple[str, str], str] = {}
    for item, res in rows:
        verdict = res["match_status"]
        if verdict == MISSING:
            untrusted = (
                stale
                or res.get("in_wish_list")
                or item["series"] in fuzzy
                or res.get("match_kind") not in (None, "", "exact")
            )
            verdict = UNSURE if untrusted else MISSING
        elif verdict != IN:
            verdict = UNSURE
        out[(item["series"], item["issue"])] = verdict
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
    """True only when the server's build commit descends from the BUI-1384 fix.

    Fails closed: a bad sha, a sha unknown to the local clone, a git error, or
    a timeout all return False."""
    if not git_sha or not re.fullmatch(r"[0-9a-f]{7,40}", git_sha):
        return False
    try:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", REQUIRED_COMMIT, git_sha],
            cwd=repo or Path(__file__).resolve().parent,
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


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


def recheck_missing(
    result: dict, server_url: str, *, session=requests, verdicts_fn=fetch_verdicts
) -> dict:
    """Fresh ownership check of the to-restore issues; keep only those still
    definitely missing (a book owned since the audit must never be recorded)."""
    lots = []
    for lot in result["lots"]:
        if lot["missing"]:
            lots.append({**lot["_lot"], "constituents": list(lot["missing"])})
    verdicts = verdicts_fn(server_url, lots, session=session) if lots else {}
    fresh = []
    for lot in result["lots"]:
        keep = [i for i in lot["missing"] if verdicts.get((lot["series"], i)) == MISSING]
        fresh.append(dict(lot, missing=keep))
    return dict(result, lots=fresh)


def apply_restore(
    result: dict, server_url: str, *, session=requests, health_sha: str | None = None,
    fetch_era=fetch_era_evidence, repo: Path | None = None, verdicts_fn=fetch_verdicts,
) -> dict:
    """Guard on the server build, re-check ownership, then POST only the
    still-missing issues."""
    if health_sha is None:
        health_sha = _get_json(session, f"{server_url}/health", 30).get("git_sha")
    if not server_has_fix(health_sha, repo=repo):
        raise RuntimeError(
            f"refusing --apply: server git_sha {health_sha!r} is not a descendant of "
            f"{REQUIRED_COMMIT} (BUI-1384); deploy it first or the write overwrites rows"
        )
    result = recheck_missing(result, server_url, session=session, verdicts_fn=verdicts_fn)
    entries, notes = build_apply_entries(result, server_url, fetch_era=fetch_era)
    if not entries:
        return {"posted": 0, "notes": notes, "response": None}
    resp = session.post(f"{server_url}{COMMIT}", json={"wins": entries}, timeout=600)
    resp.raise_for_status()
    return {"posted": len(entries), "notes": notes, "response": resp.json()}
