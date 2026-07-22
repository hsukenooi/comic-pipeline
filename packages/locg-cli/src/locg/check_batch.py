"""Mechanized collection-check executor (BUI-504).

Backs the ``locg collection check-batch items.json --table`` command. This is
the deterministic replacement for the ~450-line ``collection-check.md`` prose
program a sub-agent used to execute on every ``/comic:buy`` run: resolve the
comics server, health-gate it, read collection status, POST the batch check,
apply the stale-cache verdict downgrade, and compute the advisory
false-match flags (Patterns A / C / D / D2 / D3 / E) the skill labels
"Mechanized".

R11 (in code, not prose): ANY failure — an unreachable server, a non-200, a
timeout, the never-imported 409, an unparseable body, or a correlation
mismatch — raises :class:`CheckBatchError`. The caller (cli.py) turns that into
a hard non-zero exit and renders NO verdicts. A false "not owned" verdict from
a failed call buys a duplicate comic, so a failed/partial call must never
produce a table or a per-item verdict.

The flags are ADVISORY. They FLAG a suspect verdict; they never DECIDE, flip,
or invent ownership. The user resolves each at the skill's Step 4 decision gate.

This module talks to the comics server over HTTP (locg-cli's local
``CollectionCache`` on the MacBook is never seeded and always returns
``not_in_cache`` — see the root CLAUDE.md). It deliberately does NOT import
gixen-cli; it mirrors ``record_win_prep.py``'s fail-closed server-interaction
style with locg-cli's own ``requests`` dependency so locg-cli stays standalone.
"""

from __future__ import annotations

import os
import re
import socket
import sys
from typing import Any, Optional, Protocol

import requests

# --- Constants ---------------------------------------------------------------

# Endpoints on the comics server (provider-neutral, BUI-87/BUI-220).
_HEALTH_PATH = "/health"
_STATUS_PATH = "/api/comics/collection/status"
_BATCH_PATH = "/api/comics/collection/check/batch"
_RESOLVE_PATH = "/api/comics/collection/series-names/resolve"

# Stale-cache downgrade threshold (collection-check.md Step 2).
STALE_CACHE_DAYS = 14

# Pattern A: distinct lines that share a masthead with a base/annual series and
# can be conflated by the matcher (collection-check.md Step 2.5 Pattern A).
# The [\s-]? tolerates both "Giant-Size" and "Giant Size" spellings.
_PATTERN_A_RE = re.compile(
    r"\b(giant[\s-]?size|annual|king[\s-]?size|special)\b", re.IGNORECASE
)

# The only match statuses cmd_collection_check ever returns. Any other value in
# a batch row is a malformed response — validating it (R11) stops a garbage /
# missing status from silently rendering as "❌ Not in collection" (a buy).
_KNOWN_MATCH_STATUSES = frozenset(
    {"in_collection", "not_in_cache", "ambiguous_cross_volume"}
)

# Pattern D3: long-running rebootable mastheads where a no-year exact match can
# silently land on the wrong volume (collection-check.md Step 2.5 Pattern D3).
# Matched as normalized substrings; advisory only, so a broad list is safe.
_REBOOTABLE_MASTHEADS = (
    "fantastic four",
    "spider-man",
    "spiderman",
    "x-men",
    "avengers",
    "thor",
    "iron man",
    "hulk",
    "captain america",
    "batman",
    "superman",
    "wonder woman",
)


class CheckBatchError(Exception):
    """Hard-stop failure (R11). The batch check could not be completed, so NO
    verdicts may be rendered and the process must exit non-zero."""


# --- HTTP session typing (for test injection) --------------------------------


class _HTTPSession(Protocol):  # pragma: no cover - typing only
    def get(self, url: str, **kwargs: Any) -> requests.Response: ...
    def post(self, url: str, **kwargs: Any) -> requests.Response: ...


# --- Server resolution -------------------------------------------------------


def resolve_server_url(env: Optional[dict] = None) -> str:
    """Resolve the comics server URL, mirroring ``scripts/comics-server.sh``'s
    ``comics_resolve_server``.

    Precedence: ``COMICS_SERVER_URL`` (the canonical var, BUI-220) → the
    deprecated ``GIXEN_SERVER_URL`` alias (with a warning) → hostname inference
    (the Mac Mini hosts the server; the MacBook reaches it via Tailscale). An
    unrecognized machine with no preset is a hard failure (R11 — never guess a
    URL a failed call could be mistaken for "not owned").
    """
    env = env if env is not None else os.environ
    url = (env.get("COMICS_SERVER_URL") or "").strip()
    if not url:
        alias = (env.get("GIXEN_SERVER_URL") or "").strip()
        if alias:
            print(
                "warning: GIXEN_SERVER_URL is deprecated; use COMICS_SERVER_URL",
                file=sys.stderr,
            )
            url = alias
    if not url:
        host = (socket.gethostname() or "").lower()
        if "mac-mini" in host or "macmini" in host:
            url = "http://localhost:8080"
        elif "macbook" in host:
            url = "http://mac-mini.tail9b7fa5.ts.net:8080"
        else:
            raise CheckBatchError(
                f"COMICS_SERVER_URL is not set and the machine ('{host}') is "
                "unrecognised. Set COMICS_SERVER_URL and confirm the comics "
                "server is running before checking the collection (R11 — a "
                "guessed/failed check must never render 'not owned')."
            )
    return url.rstrip("/")


# --- HTTP helpers (every one fail-closes to CheckBatchError) -----------------


def _request(
    session: _HTTPSession,
    method: str,
    url: str,
    *,
    timeout: float,
    json_body: Optional[dict] = None,
) -> requests.Response:
    """Issue one request, translating every connectivity-class failure into a
    :class:`CheckBatchError` (R11 hard stop). Mirrors ``record_win_prep``'s
    exception discipline: a ConnectionError/Timeout/any RequestException is a
    hard stop, never a swallow-to-empty."""
    try:
        if method == "GET":
            return session.get(url, timeout=timeout)
        return session.post(url, json=json_body, timeout=timeout)
    except requests.ConnectionError as exc:
        raise CheckBatchError(
            f"cannot reach the comics server at {url} ({exc}) — HARD STOP: "
            "will not render collection verdicts from an unreachable server "
            "(R11)."
        ) from exc
    except requests.Timeout as exc:
        raise CheckBatchError(
            f"the comics server at {url} timed out ({exc}) — HARD STOP: will "
            "not render collection verdicts from a timed-out call (R11)."
        ) from exc
    except requests.RequestException as exc:
        raise CheckBatchError(
            f"error contacting the comics server at {url} ({exc}) — HARD STOP "
            "(R11)."
        ) from exc


def _health_gate(server_url: str, session: _HTTPSession, timeout: float) -> None:
    """/health must answer 200. Proves the process is up (the store health is
    verified by the status call that follows)."""
    resp = _request(session, "GET", f"{server_url}{_HEALTH_PATH}", timeout=timeout)
    if resp.status_code != 200:
        raise CheckBatchError(
            f"the comics server at {server_url} returned HTTP {resp.status_code} "
            f"for {_HEALTH_PATH} — HARD STOP: it is not healthy (R11)."
        )


def _get_status(server_url: str, session: _HTTPSession, timeout: float) -> dict:
    """Read collection status; hard-fail on any non-200 or unparseable body,
    and on a never-imported store (``last_full_import is None``)."""
    resp = _request(session, "GET", f"{server_url}{_STATUS_PATH}", timeout=timeout)
    if resp.status_code != 200:
        raise CheckBatchError(
            f"collection status call to {server_url} returned HTTP "
            f"{resp.status_code} — HARD STOP (R11)."
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise CheckBatchError(
            f"unparseable collection-status response from {server_url}: {exc} "
            "— HARD STOP (R11)."
        ) from exc
    if not isinstance(data, dict):
        raise CheckBatchError(
            f"unexpected collection-status shape from {server_url} — HARD STOP "
            "(R11)."
        )
    if data.get("last_full_import") is None:
        raise CheckBatchError(
            "collection is empty on the comics server (never imported) — run a "
            "full LOCG import before checking. HARD STOP: an empty store would "
            "report every comic 'not owned' (R11)."
        )
    return data


def _post_batch(
    server_url: str, items: list[dict], session: _HTTPSession, timeout: float
) -> list[dict]:
    """POST the batch check. A 409 (never-imported store) is the whole-batch R11
    refusal lifted to the batch boundary; every other non-200 is equally a hard
    stop. Returns the ``results`` list."""
    resp = _request(
        session, "POST", f"{server_url}{_BATCH_PATH}", timeout=timeout,
        json_body={"items": items},
    )
    if resp.status_code == 409:
        raise CheckBatchError(
            f"the comics server at {server_url} refused the batch with HTTP 409 "
            "— the collection store was never imported, so it declines to answer "
            "for EVERY item. HARD STOP: no verdicts rendered (R11)."
        )
    if resp.status_code != 200:
        raise CheckBatchError(
            f"batch collection check to {server_url} returned HTTP "
            f"{resp.status_code} — HARD STOP: no verdicts rendered (R11)."
        )
    try:
        data = resp.json()
        results = data["results"]
    except (ValueError, KeyError, TypeError) as exc:
        raise CheckBatchError(
            f"unparseable batch-check response from {server_url}: {exc} — HARD "
            "STOP (R11)."
        ) from exc
    if not isinstance(results, list):
        raise CheckBatchError(
            f"batch-check response from {server_url} had a non-list 'results' — "
            "HARD STOP (R11)."
        )
    return results


def _resolve_series_names(
    server_url: str, names: list[str], session: _HTTPSession, timeout: float
) -> dict[str, dict]:
    """POST the suspect ``not_in_cache`` series names for catalog reconciliation
    (Pattern C). Returns ``{query: {resolved, match_kind}}``.

    R11 applies to this re-query too: a failed/non-200 resolve call is a hard
    STOP, not a fallback that silently drops the flag (collection-check.md
    Step 2.5 callout — a re-query failure aborts the whole check)."""
    if not names:
        return {}
    resp = _request(
        session, "POST", f"{server_url}{_RESOLVE_PATH}", timeout=timeout,
        json_body={"names": names},
    )
    if resp.status_code != 200:
        raise CheckBatchError(
            f"series-name resolve call to {server_url} returned HTTP "
            f"{resp.status_code} — HARD STOP: a Pattern-C re-query failure "
            "aborts the whole check (R11)."
        )
    try:
        data = resp.json()
        results = data["results"]
    except (ValueError, KeyError, TypeError) as exc:
        raise CheckBatchError(
            f"unparseable series-name resolve response from {server_url}: {exc} "
            "— HARD STOP (R11)."
        ) from exc
    if not isinstance(results, list):
        raise CheckBatchError(
            f"series-name resolve response from {server_url} had a non-list "
            "'results' — HARD STOP (R11)."
        )
    out: dict[str, dict] = {}
    for row in results:
        if isinstance(row, dict) and "query" in row:
            out[row["query"]] = row
    return out


# --- Flag + verdict computation (pure) ---------------------------------------


def _is_rebootable_masthead(series: str) -> bool:
    s = (series or "").lower()
    return any(m in s for m in _REBOOTABLE_MASTHEADS)


def _candidate_series_names(candidates: Any) -> str:
    if not isinstance(candidates, list):
        return ""
    names = []
    for c in candidates:
        if isinstance(c, dict):
            name = c.get("series_name") or c.get("full_title")
            if name:
                names.append(str(name))
    return ", ".join(names)


def _ordinal_label(ordinal: Any) -> str:
    if ordinal == 1:
        return "base"
    if not isinstance(ordinal, int):
        return "reprint"
    suffix = {2: "nd", 3: "rd"}.get(ordinal, "th")
    return f"{ordinal}{suffix} printing"


def _printing_candidate_summary(candidates: Any) -> str:
    """Render printing_candidates as ``ordinal: owned/wish/untracked`` so the
    flag describes the listing's printing landscape without re-parsing titles."""
    if not isinstance(candidates, list):
        return ""
    parts = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        label = _ordinal_label(c.get("printing_ordinal"))
        if c.get("in_collection"):
            state = "owned"
        elif c.get("in_wish_list"):
            state = "wishlisted"
        else:
            state = "untracked"
        parts.append(f"{label}: {state}")
    return "; ".join(parts)


def compute_flags(
    result: dict, item: dict, resolve_map: dict[str, dict], cache_age_days: Optional[int]
) -> list[dict]:
    """Compute the advisory false-match flags for one verdict row.

    Each flag is ``{"pattern": <code>, "message": <text>}``. Flags never change
    ``match_status`` — they annotate a suspect verdict for the user to resolve.
    """
    flags: list[dict] = []
    match_status = result.get("match_status")
    match_kind = result.get("match_kind")
    series = item.get("series") or result.get("series") or ""
    issue = item.get("issue") or result.get("issue") or ""
    year = item.get("year")

    if match_status == "in_collection":
        # Pattern A — Giant-Size / Annual / King-Size conflation (false positive).
        if _PATTERN_A_RE.search(series):
            flags.append({
                "pattern": "A",
                "message": (
                    f'possible false positive — "{series}" is a Giant-Size/Annual/'
                    "King-Size line that may be conflated with the base/annual "
                    "series; confirm before skipping"
                ),
            })
        # Pattern D — masthead-alias match, unconfirmed volume (false positive).
        if match_kind == "alias":
            flags.append({
                "pattern": "D",
                "message": (
                    f'alias match — matched "{result.get("matched_series_name")}" '
                    f'({result.get("matched_release_date")}); confirm this is the '
                    "same volume as the listing before skipping"
                ),
            })
        # Pattern D3 — single-owned-wrong-volume residual (no year, exact match).
        elif match_kind == "exact" and not year and _is_rebootable_masthead(series):
            flags.append({
                "pattern": "D3",
                "message": (
                    f'possible wrong-volume (no year) — "{series}" is a rebootable '
                    f'masthead; the no-year match to "{result.get("matched_series_name")}" '
                    "could be the wrong volume. Confirm Matched Volume against the "
                    "listing's era, or re-check with the cover year"
                ),
            })
        # Pattern E — printing conflict (false positive).
        if result.get("printing_conflict") is True:
            summary = _printing_candidate_summary(result.get("printing_candidates"))
            flags.append({
                "pattern": "E",
                "message": (
                    f'printing conflict — matched "{result.get("full_title_matched")}", '
                    "a different printing than the listing"
                    + (f" (printings — {summary})" if summary else "")
                    + "; confirm before skipping"
                ),
            })

    elif match_status == "ambiguous_cross_volume":
        # Pattern D2 — cross-volume ambiguity, no year given.
        names = _candidate_series_names(result.get("candidates"))
        flags.append({
            "pattern": "D2",
            "message": (
                f'cross-volume ambiguity — "{series} #{issue}" is owned in multiple '
                f"volumes ({names}); re-check with the listing's cover year before "
                "deciding"
            ),
        })

    elif match_status == "not_in_cache":
        # Pattern C — ambiguous / unrecognized series (spelling-drift false
        # negative, BUI-129/171). Keyed on a FUZZY catalog resolution: the
        # single-item matcher gates ownership on an exact normalized key, so a
        # fuzzy-only catalog hit is exactly the owned row the check could have
        # silently missed. (An `exact` resolve means the same key the check
        # already used, and every catalog name carries year/volume decoration,
        # so flagging on raw string inequality would fire on nearly every
        # not_in_cache — noise that defeats the guard.)
        resolved = resolve_map.get(series)
        if resolved and resolved.get("match_kind") == "fuzzy" and resolved.get("resolved"):
            flags.append({
                "pattern": "C",
                "message": (
                    f'ambiguous/unrecognized series — "{series}" is not the catalog '
                    f'spelling; did you mean "{resolved.get("resolved")}"? Re-check '
                    "under the catalog name before trusting this verdict"
                ),
            })
        # Stale-cache note (Step 2) — a wishlisted-not-owned row keeps its own
        # verdict but still carries the staleness note.
        if _is_stale(cache_age_days) and result.get("in_wish_list"):
            flags.append({
                "pattern": "stale",
                "message": (
                    f"cache {cache_age_days} days stale — manual LOCG check "
                    "recommended before bidding"
                ),
            })

    return flags


def _is_stale(cache_age_days: Optional[int]) -> bool:
    return isinstance(cache_age_days, int) and cache_age_days > STALE_CACHE_DAYS


def compute_verdict(result: dict, cache_age_days: Optional[int]) -> str:
    """The 'In Cache?' cell text, including the Step 2 stale downgrade of a
    confident 'Not in collection' verdict."""
    match_status = result.get("match_status")
    if match_status == "in_collection":
        return "✅ In collection"
    if match_status == "ambiguous_cross_volume":
        return "⚠️ Ambiguous (cross-volume)"
    # not_in_cache
    if result.get("in_wish_list"):
        return "\U0001f4cb Wishlisted (not owned)"
    if _is_stale(cache_age_days):
        return (
            f"⚠️ Not in cache (cache {cache_age_days} days stale — "
            "manual LOCG check recommended before bidding)"
        )
    return "❌ Not in collection"


# --- Orchestration -----------------------------------------------------------


def run_check_batch(
    items: list[dict],
    *,
    server_url: Optional[str] = None,
    session: Optional[_HTTPSession] = None,
    health_timeout: float = 30.0,
    status_timeout: float = 30.0,
    batch_timeout: float = 60.0,
    resolve_timeout: float = 30.0,
) -> dict:
    """Run the full mechanized collection check and return a structured result.

    Raises :class:`CheckBatchError` on ANY failure (R11): the caller renders no
    verdicts and exits non-zero.
    """
    if not items:
        raise CheckBatchError("no items to check — items list is empty.")
    session = session if session is not None else requests
    if server_url is None:
        server_url = resolve_server_url()
    else:
        server_url = server_url.rstrip("/")

    # Step 0: health + status.
    _health_gate(server_url, session, health_timeout)
    status = _get_status(server_url, session, status_timeout)
    cache_age_days = status.get("cache_age_days")
    pending_push_count = status.get("pending_push_count")
    oldest_pending_days = status.get("oldest_pending_days")

    # Step 1: batch check.
    results = _post_batch(server_url, items, session, batch_timeout)

    # Correlate by index (the endpoint returns one result per item, in order,
    # echoing series/issue verbatim). Verify length + echo so an ordering
    # surprise is a loud STOP, never a silent mis-attribution (R11).
    if len(results) != len(items):
        raise CheckBatchError(
            f"batch returned {len(results)} results for {len(items)} items — "
            "cannot correlate verdicts to inputs. HARD STOP (R11)."
        )
    for item, result in zip(items, results):
        if not isinstance(result, dict):
            raise CheckBatchError(
                "batch result row was not an object — cannot read a verdict. "
                "HARD STOP (R11)."
            )
        echoed_series = result.get("series")
        echoed_issue = result.get("issue")
        if (
            str(echoed_series).strip() != str(item.get("series")).strip()
            or str(echoed_issue).strip() != str(item.get("issue")).strip()
        ):
            raise CheckBatchError(
                "batch result order does not match the request "
                f"(expected {item.get('series')} #{item.get('issue')}, got "
                f"{echoed_series} #{echoed_issue}) — cannot correlate. HARD "
                "STOP (R11)."
            )
        if result.get("match_status") not in _KNOWN_MATCH_STATUSES:
            raise CheckBatchError(
                f"batch row for {item.get('series')} #{item.get('issue')} has an "
                f"unknown match_status {result.get('match_status')!r} — a "
                "malformed verdict must never render as 'not owned'. HARD STOP "
                "(R11)."
            )

    # Pattern C: one resolve call for every distinct not_in_cache series name.
    suspect_names = sorted({
        str(r.get("series"))
        for r in results
        if r.get("match_status") == "not_in_cache" and r.get("series")
    })
    resolve_map = _resolve_series_names(server_url, suspect_names, session, resolve_timeout)

    enriched: list[dict] = []
    for item, result in zip(items, results):
        flags = compute_flags(result, item, resolve_map, cache_age_days)
        verdict = compute_verdict(result, cache_age_days)
        enriched.append({
            **result,
            "year": item.get("year"),
            "variant": item.get("variant"),
            "verdict": verdict,
            "flags": flags,
        })

    banners = _build_banners(cache_age_days, pending_push_count, oldest_pending_days)

    return {
        "server_url": server_url,
        "last_full_import": status.get("last_full_import"),
        "cache_age_days": cache_age_days,
        "pending_push_count": pending_push_count,
        "oldest_pending_days": oldest_pending_days,
        "count": len(enriched),
        "results": enriched,
        "banners": banners,
    }


def _build_banners(
    cache_age_days: Optional[int],
    pending_push_count: Optional[int],
    oldest_pending_days: Optional[int],
) -> list[str]:
    banners: list[str] = []
    if isinstance(cache_age_days, int) and cache_age_days > STALE_CACHE_DAYS:
        banners.append(
            f"⚠️ Cache is {cache_age_days} days old — consider "
            "re-importing from LOCG (leagueofcomicgeeks.com → My Comics "
            "→ Export)."
        )
    if isinstance(pending_push_count, int) and pending_push_count > 0:
        oldest = oldest_pending_days if oldest_pending_days is not None else "?"
        line = f"{pending_push_count} rows pending push to LOCG; oldest pending = {oldest} days."
        escalate = (
            (isinstance(oldest_pending_days, int) and oldest_pending_days > 21)
            or pending_push_count > 25
        )
        if escalate:
            line = "⚠️ " + line + " Consider running /comic:collection-sync."
        banners.append(line)
    return banners


# --- Table rendering ---------------------------------------------------------


def render_table(payload: dict) -> str:
    """Render the Step 3 markdown table + status banners (human presentation)."""
    lines = [
        "| # | Comic | In Cache? | Full Title Matched | Matched Volume | Cache Age | Notes |",
        "|---|---|---|---|---|---|---|",
    ]
    cache_age_days = payload.get("cache_age_days")
    cache_age_cell = f"{cache_age_days} days" if cache_age_days is not None else "—"
    for i, row in enumerate(payload.get("results", []), start=1):
        series = row.get("series") or ""
        issue = row.get("issue") or ""
        variant = row.get("variant")
        comic = f"{series} #{issue}" + (f" ({variant})" if variant else "")
        full_title = row.get("full_title_matched") or "—"
        matched_volume = row.get("matched_series_name") or "—"
        notes = "; ".join(f.get("message", "") for f in row.get("flags", []))
        lines.append(
            f"| {i} | {comic} | {row.get('verdict', '')} | {full_title} | "
            f"{matched_volume} | {cache_age_cell} | {notes} |"
        )
    out = "\n".join(lines)
    banners = payload.get("banners") or []
    if banners:
        out += "\n\n" + "\n".join(banners)
    return out


def parse_items_file(raw: str) -> list[dict]:
    """Parse an items.json body into the list of ``{series, issue, year?,
    variant?}`` items. Accepts either ``{"items": [...]}`` (the shape the
    batch endpoint wants) or a bare ``[...]`` list."""
    import json

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CheckBatchError(f"failed to parse items JSON: {exc}") from exc
    if isinstance(data, dict):
        items = data.get("items")
    elif isinstance(data, list):
        items = data
    else:
        raise CheckBatchError(
            "items JSON must be a list or an object with an 'items' list."
        )
    if not isinstance(items, list) or not items:
        raise CheckBatchError("items JSON has no items to check.")
    normalized: list[dict] = []
    for entry in items:
        if not isinstance(entry, dict):
            raise CheckBatchError("each item must be an object with series + issue.")
        series = entry.get("series")
        issue = entry.get("issue")
        if not series or not str(series).strip() or issue is None or not str(issue).strip():
            raise CheckBatchError("each item requires a non-empty series and issue.")
        norm: dict[str, Any] = {"series": str(series), "issue": str(issue)}
        if entry.get("year") not in (None, ""):
            norm["year"] = str(entry["year"])
        if entry.get("variant") not in (None, ""):
            norm["variant"] = str(entry["variant"])
        normalized.append(norm)
    return normalized
