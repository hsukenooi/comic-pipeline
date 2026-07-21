"""record_win_prep.py — BUI-353: one-call gixen-list -> filter -> dedup ->
subtract-seen -> identify -> build-payload for /comic:collection-add.

Centralizes logic the skill previously re-authored as ~40 lines of inline
Python every run: filtering `gixen list --json` to ENDED+WON, deduplicating
by item_id, subtracting the BUI-121 seen-set, running `comic-identify
--batch` on the new wins' titles, and positionally mapping the results back
onto those wins to build the `{"wins": [...]}` record-win payload. The
positional mapping was a foot-gun when hand-authored inline (an off-by-one
silently mis-attributes an identity to the wrong won auction) — this module
owns that join in one tested place instead.

Also owns the BUI-352 seen-fetch hardening: a local/connectivity failure
(connection refused, DNS failure, timeout) is treated very differently from
"the server answered but had an internal error" — this raises
RecordWinPrepError (a hard stop) for the former, and reserves the silent
"treat as an empty seen-set" fallback for a genuine 5xx from the server
(BUI-34's already-owned dedup on the server is the second net for that one
case). Anything else unexpected (4xx, unparseable body) is also a hard stop:
falling back there could mask a real bug rather than ride out a transient
server hiccup.

BUI-354: the automated "ask the user" gates are a null series/issue and an
unparseable lot — comic-identify's baseline confidence of 0.5 on every
cleanly-parsed title made a numeric confidence threshold fire on nearly every
title, so this deliberately never looks at `confidence` as a gate. BUI-422
added a price-gated version of one more: a null `year` on a win priced at/above
a threshold gated to needs_review, on the theory that a null year correlates
with the ALL-CAPS/vintage-key titles most prone to mis-resolving to the wrong
volume downstream (see `resolve_series_for_win`, BUI-421). BUI-475 replaces
that price gate: the original plan was a server-side era-evidence endpoint
(Option A) to auto-resolve which volume a null-year win belongs to, but that
endpoint fails open — a null-year win with no competing same-title volume in
the collection auto-records under the sole owned (and possibly wrong-era)
volume, reproducing the BUI-421 mis-file BUI-422 was meant to prevent. Since
a win's era genuinely cannot be confirmed without a year, and price is not a
reliable proxy for that (a cheap null-year win is exactly as unconfirmable as
an expensive one), every null-year win now gates to needs_review
unconditionally, regardless of price. See BUI-475 and the redesign follow-up
BUI-498.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Callable, Protocol

import requests


class _HTTPGetter(Protocol):
    """Structural type for `fetch_seen_ids`'s `session` param — anything with
    a `requests`-shaped `get(url, timeout=...)` (the `requests` module itself,
    or a `requests.Session`, or a test double)."""

    def get(self, url: str, *, timeout: float) -> requests.Response: ...

SEEN_ENDPOINT = "/api/comics/collection/record-win/seen"
DEFAULT_IDENTIFY_CMD = ["comic-identify", "--batch"]

# BUI-354 needs_review reasons — named so source and tests can't silently
# drift on a typo in the literal.
REASON_NULL_SERIES_OR_ISSUE = "series or issue is null"
REASON_UNPARSEABLE_LOT = "lot with unparseable contents"
# BUI-475: a null `year` from comic-identify means the win's era can't be
# confirmed at all — the ALL-CAPS/vintage-key titles most prone to
# mis-resolving to the wrong Metron volume (see BUI-421 /
# resolve_series_for_win) are exactly the ones comic-identify can't date, and
# a server-side attempt to auto-resolve the era from collection evidence
# (Option A) was proven to fail open (see BUI-475). So every null-year win
# gates to needs_review unconditionally — there is no price threshold below
# which it's safe to auto-record without a year.
REASON_MISSING_YEAR = "year is null (vintage-key volume-mis-resolution risk)"


class RecordWinPrepError(Exception):
    """A hard-stop condition. The caller should exit non-zero and must not
    proceed to POST anything — the payload this run would build is not
    trustworthy."""


def filter_ended_won(snipes: list[dict]) -> list[dict]:
    """`gixen list --json` dumps every snipe, including still-live (PENDING)
    ones. Keep only genuine wins: `time_to_end == "ENDED"` and `status`
    contains "WON" (case-insensitive), deduplicated by item_id (first
    occurrence kept)."""
    out: list[dict] = []
    seen_ids: set[str] = set()
    for s in snipes:
        if (s.get("time_to_end") or "").upper() != "ENDED":
            continue
        if "WON" not in (s.get("status") or "").upper():
            continue
        item_id = s.get("item_id")
        if not item_id or item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        out.append(s)
    return out


def fetch_seen_ids(
    server_url: str, *, timeout: float = 30.0, session: _HTTPGetter = requests
) -> set[str]:
    """GET the BUI-121 seen-set. Raises RecordWinPrepError (hard stop) on a
    connectivity failure or any non-2xx response other than a genuine 5xx,
    which falls back to an empty seen-set (logging a warning first)."""
    url = f"{server_url.rstrip('/')}{SEEN_ENDPOINT}"
    try:
        resp = session.get(url, timeout=timeout)
    except requests.ConnectionError as exc:
        raise RecordWinPrepError(
            f"cannot reach comics server at {server_url} ({exc}) — HARD STOP: "
            "will not process wins without a real seen-set (BUI-352)"
        ) from exc
    except requests.Timeout as exc:
        raise RecordWinPrepError(
            f"comics server at {server_url} timed out fetching the seen-set "
            f"({exc}) — HARD STOP: will not process wins without a real "
            "seen-set (BUI-352)"
        ) from exc
    except requests.RequestException as exc:
        # Anything else requests-shaped (SSL errors, a malformed URL, too many
        # redirects, ...) is still a local/connectivity-class failure, not a
        # server-side 5xx — BUI-352's hard-stop applies here too, not just to
        # the two most common exception types.
        raise RecordWinPrepError(
            f"error contacting comics server at {server_url} ({exc}) — "
            "HARD STOP: will not process wins without a real seen-set (BUI-352)"
        ) from exc

    if 500 <= resp.status_code < 600:
        print(
            f"warning: comics server returned HTTP {resp.status_code} for the "
            "seen-set fetch — falling back to an empty seen-set this run "
            "(BUI-34's already-owned dedup on the server is the safety net)",
            file=sys.stderr,
        )
        return set()

    if resp.status_code != 200:
        raise RecordWinPrepError(
            f"unexpected HTTP {resp.status_code} fetching the seen-set from "
            f"{server_url} — HARD STOP (not a connectivity issue and not a "
            "5xx, so falling back here could mask a real bug)"
        )

    try:
        data = resp.json()
        return set(data["item_ids"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RecordWinPrepError(
            f"unparseable seen-set response from {server_url}: {exc}"
        ) from exc


def subtract_seen(ended_won: list[dict], seen_ids: set[str]) -> list[dict]:
    """New wins = ended_won minus anything already recorded in a prior run."""
    return [s for s in ended_won if s.get("item_id") not in seen_ids]


def identify_titles(
    titles: list[str], *, cmd: list[str] | None = None, timeout: float = 120.0
) -> list[dict]:
    """Shell out to `comic-identify --batch` ONCE for all titles. Its --batch
    contract guarantees exactly one JSONL row per input line, in order (a
    blank/unparseable title yields a null-series or "error" row rather than
    being dropped) — this still verifies the line count rather than trusting
    it blindly, because a mismatch here is exactly the silent-misattribution
    failure BUI-353 exists to eliminate."""
    if not titles:
        return []
    argv = cmd or DEFAULT_IDENTIFY_CMD
    try:
        proc = subprocess.run(
            argv,
            input="\n".join(titles) + "\n",
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except OSError as exc:
        # Covers a missing binary (FileNotFoundError) as well as any other
        # launch failure (PermissionError, etc.) — all mean the subprocess
        # never ran, so none of it produced identities to trust.
        raise RecordWinPrepError(
            f"could not launch comic-identify ({exc}) — run ./scripts/install.sh"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RecordWinPrepError(f"comic-identify --batch timed out: {exc}") from exc

    if proc.returncode != 0:
        raise RecordWinPrepError(
            f"comic-identify --batch exited {proc.returncode}: "
            f"{proc.stderr.strip()}"
        )

    lines = proc.stdout.splitlines()
    if len(lines) != len(titles):
        raise RecordWinPrepError(
            f"comic-identify --batch returned {len(lines)} line(s) for "
            f"{len(titles)} input title(s) — refusing to map positionally "
            "(this alignment check is the whole point of BUI-353)"
        )

    results = []
    for line in lines:
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RecordWinPrepError(
                f"unparseable comic-identify JSONL line {line!r}: {exc}"
            ) from exc
    return results


def _build_review_entry(win: dict, identity: dict, reason: str) -> dict:
    """Self-contained: carries everything needed to hand-build a wins entry
    once a human supplies the missing series/issue, without going back to the
    raw snipe list."""
    return {
        "item_id": win.get("item_id"),
        "title": win.get("title"),
        "current_bid": win.get("current_bid"),
        "end_date_iso": win.get("end_date_iso"),
        "reason": reason,
        "identity": {
            "series": identity.get("series"),
            "issue": identity.get("issue"),
            "year": identity.get("year"),
            "edition": identity.get("edition"),  # BUI-426: annual/etc. qualifier
            "is_lot": identity.get("is_lot"),
            "constituent_issues": identity.get("constituent_issues"),
            "error": identity.get("error"),
        },
    }


def _build_win_entry(win: dict, identity: dict, *, issue: str) -> dict:
    identify_data: dict = {
        "series": identity["series"],
        "issue": issue,
    }
    if identity.get("year") is not None:
        identify_data["year"] = identity["year"]
    variant_text = identity.get("variant_text") or ""
    if variant_text:
        identify_data["variant_text"] = variant_text
    # BUI-426: forward the edition qualifier (annual / giant-size / king-size /
    # treasury) so the downstream resolver files an annual as its DISTINCT
    # "<Series> Annual #N" identity instead of the same-numbered REGULAR issue
    # in the wrong volume. comic-identify strips "Annual"/"Treasury" out of the
    # extracted series text (those nest in the parent series' full_title),
    # recording the fact only in `edition`; dropping `edition` here is exactly
    # what let "Uncanny X-Men Annual 6" resolve to bare series "Uncanny X-Men" +
    # issue "6" and get filed as the Silver-Age "The X-Men #6" — a different,
    # valuable book falsely claimed as owned. Only non-default kinds carry
    # information; "single-issue" is the default and is omitted.
    edition = identity.get("edition") or ""
    if edition and edition != "single-issue":
        identify_data["edition"] = edition
    return {
        "item_id": win.get("item_id"),
        "current_bid": win.get("current_bid"),
        "end_date_iso": win.get("end_date_iso"),
        "identify_data": identify_data,
    }


def entries_for_win(win: dict, identity: dict) -> tuple[list[dict], dict | None]:
    """Turn one (win, comic-identify result) pair into either ready wins
    entries or a single needs_review entry. BUI-354: a null series/issue (or,
    for a lot, unparseable/empty constituent_issues) gates — no confidence
    check. BUI-475: a null `year` also gates, always — its era can't be
    confirmed without a year, regardless of price (this replaces BUI-422's
    price-gated version, after the server-side era-evidence redesign, Option
    A, was proven to fail open; see BUI-475 and the follow-up BUI-498)."""
    if identity.get("error"):
        return [], _build_review_entry(win, identity, f"comic-identify error: {identity['error']}")

    if identity.get("is_lot"):
        series = identity.get("series")
        raw_constituents = identity.get("constituent_issues") or []
        # Every element must be a genuine issue value — a partially-parsed lot
        # (e.g. ["1", None, "3"]) is exactly as untrustworthy as an entirely
        # empty one and must not silently produce a win entry with a null
        # issue (the blank-issue outcome BUI-354's gate exists to prevent).
        valid_constituents = [c for c in raw_constituents if c]
        if not series or not raw_constituents or len(valid_constituents) != len(raw_constituents):
            return [], _build_review_entry(win, identity, REASON_UNPARSEABLE_LOT)
        # BUI-475: a null year on a lot is the same volume-mis-resolution risk
        # as the non-lot case — its era can't be confirmed without a year, so
        # gate it unconditionally (no price threshold) before expanding into
        # per-issue win entries.
        if identity.get("year") is None:
            return [], _build_review_entry(win, identity, REASON_MISSING_YEAR)
        # De-duplicate while preserving order: a title with a literal repeated
        # issue number (e.g. a seller typo like "100, 100, 101") must not
        # produce two win entries sharing the same item_id + issue — that
        # would silently double-record the same physical book downstream.
        deduped_constituents = list(dict.fromkeys(valid_constituents))
        return [_build_win_entry(win, identity, issue=issue) for issue in deduped_constituents], None

    if not identity.get("series") or not identity.get("issue"):
        return [], _build_review_entry(win, identity, REASON_NULL_SERIES_OR_ISSUE)

    # BUI-475: a null year correlates with the ALL-CAPS/vintage-key titles
    # most prone to mis-resolving to the wrong volume downstream (BUI-421),
    # and its era can't be confirmed without a year — gate unconditionally
    # (no price threshold) for a human check instead of auto-recording.
    if identity.get("year") is None:
        return [], _build_review_entry(win, identity, REASON_MISSING_YEAR)

    return [_build_win_entry(win, identity, issue=identity["issue"])], None


def build_payload(
    snipes: list[dict],
    server_url: str,
    *,
    fetch_seen: Callable[[str], set[str]] = fetch_seen_ids,
    identify: Callable[[list[str]], list[dict]] = identify_titles,
) -> dict:
    """The single entry point: gixen-list (already fetched by the caller as
    `snipes`) -> filter ENDED+WON+dedup -> subtract seen -> identify -> build.

    Returns a dict with:
      - wins: POST-ready entries for /api/comics/collection/record-win
      - needs_review: entries a human must resolve before they can be added
      - total_ended_won: count after the ENDED+WON filter/dedup, before
        subtracting the seen-set (lets the caller distinguish "no wins at
        all" from "all wins already processed")
      - new_win_count: count after subtracting the seen-set
    """
    ended_won = filter_ended_won(snipes)
    seen_ids = fetch_seen(server_url)
    new_wins = subtract_seen(ended_won, seen_ids)

    result = {
        "wins": [],
        "needs_review": [],
        "total_ended_won": len(ended_won),
        "new_win_count": len(new_wins),
    }
    if not new_wins:
        return result

    titles = [w.get("title") or "" for w in new_wins]
    identities = identify(titles)

    for win, identity in zip(new_wins, identities):
        entries, review = entries_for_win(win, identity)
        result["wins"].extend(entries)
        if review is not None:
            result["needs_review"].append(review)

    return result
