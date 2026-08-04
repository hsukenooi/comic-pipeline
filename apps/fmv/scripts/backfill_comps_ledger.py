#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["requests"]
# ///
"""BUI-661 (plan unit U6) — one-shot backfill of the comps ledger from the
already-paid-for raw provider responses on disk.

WHAT THIS IS
------------
A one-shot, re-runnable importer that reads the two raw-response corpora we
already have and posts what they contain into the comps ledger
(``POST /api/comics/comps``, BUI-656):

  * ``~/.cache/ebay-sold-comps`` — the digest-keyed, 7-day-TTL response cache
    (``provenance='backfill-cache'``). It overwrites on re-query and evicts on
    age, so it is a *window*, not an archive: what is in it today is gone in a
    week. That is the whole reason this ticket is worth running.
  * the BUI-614 tier-0 capture (``provenance='backfill-capture'``) —
    append-only, never pruned, and carrying each response's REAL fetch time.
    Since BUI-628 this is a set of SEGMENTS in ``CAPTURE_DIR``, not one file:
    a live ``raw_responses.jsonl`` plus rotated ``raw_responses.<stamp>.jsonl``
    segments, usually gzipped. Reading only the live file would silently miss
    all rotated history — silently, because a short read is indistinguishable
    from a small corpus — so this globs the directory (``read_capture``).

Both are optional; either being absent is reported, not fatal.

    ./apps/fmv/scripts/backfill_comps_ledger.py --verify-shape   # prove the shape
    ./apps/fmv/scripts/backfill_comps_ledger.py --dry-run        # read-only plan
    ./apps/fmv/scripts/backfill_comps_ledger.py                  # apply

Sibling of ``fmv_high_calibration.py`` / ``bid_factor_demotion_measurement.py``
in shape (a one-shot ops script under ``apps/fmv/scripts/``), but unlike those
two this one WRITES. Run it only under the repo's backup → apply → diff ritual:

  1. The server must already be running BUI-656's schema and endpoint — check
     ``comics-api GET /health``'s ``git_sha``. Against an older server every
     POST 404s; the run aborts after 5 consecutive failures rather than
     printing a wall of them.
  2. No ``comic-fmv`` batch in flight. Live comps posted during the run would
     be inside the backup window, so a rollback would discard them too.
  3. ``sqlite3 .backup`` first — never ``cp``, the DB is WAL-mode and a ``cp``
     copies a stale snapshot.
  4. ``--verify-shape``, then ``--dry-run``, then optionally ``--limit`` for a
     smoke import, then the full run.
  5. Snapshot ``SELECT COUNT(*) FROM comps`` before and after and diff it
     against the ``inserted`` total printed below. Rollback is restoring the
     backup from step 3; nothing else writes ``comps`` while (2) holds.

Run it ON the machine that owns the corpus. ``observed_at`` for a cache-sourced
comp is the cache file's mtime, so importing from a directory that was copied
without ``-p`` would silently restamp the whole corpus as fetched today — the
summary prints the observed_at span per corpus so that is visible, not implied.

--------------------------------------------------------------------------
THE IMPORT-BOUNDARY DECISION (read this before changing how comps are parsed)
--------------------------------------------------------------------------
KTD1 requires a backfilled row to be the SAME OBJECT as a live pool row — same
``parse_comp``/``parse_comp_sold_comps``, same ``hard_exclude``, same dedupe —
so a future reader never has to ask which filters had run when a given row was
written. But ``apps/fmv`` is not a uv workspace member and cannot import
``apps/ebay``: ``comic-fmv`` shells out to the ``ebay-sold-comps`` console
script precisely so the two stay decoupled (see CLAUDE.md, "The FMV pipeline
shells out across package boundaries").

This script resolves that tension by **loading the live ``sold_comps`` module
from the repo source tree by path** (``_load_live_sold_comps`` below) rather
than duplicating any of it. Why, and what was rejected:

  * **Duplicating the parser — rejected.** The repo does carry deliberate
    apps/ebay↔apps/fmv duplications (``_SLAB_TITLE_RE``,
    ``_strip_leading_article``), each a self-contained handful of lines with a
    comment saying so. This is not that: ``hard_exclude`` delegates to
    ``comic_identity.is_comp_excluded``, eight sub-checks accumulated across
    BUI-269/598/637/645/668 and still changing weekly (BUI-645 and BUI-668 both
    landed 2026-08-03). A copy would drift within days, and a ledger whose rows
    were filtered by a stale copy of the exclusion rules is exactly the "which
    filters had run?" question KTD1 exists to abolish.
  * **Shelling out to ``ebay-sold-comps`` — rejected.** There is no offline
    "parse this response I already have" entry point; every entry point
    fetches. Re-fetching 500+ responses means re-billing the providers for data
    we already own, and most of these cache entries are past their TTL so a
    re-run would not even serve them from cache.
  * **Moving the script into ``apps/ebay`` — rejected.** The corpus is eBay
    data but the destination is the comics server, which ``apps/ebay`` has no
    business talking to; ``apps/fmv`` is already the side that owns the
    server round-trip.

The path-load is honest about its cost: it is a ``sys.path`` insert, and it
only works from a repo checkout. It is confined to this one-shot script —
``scripts/`` is NOT in ``comic-fmv``'s wheel (see ``pyproject.toml``'s
``[tool.hatch.build.targets.wheel] include``), so the shipped package boundary
is untouched and ``comic-fmv`` still never imports ``apps/ebay``. The load
fails LOUDLY if the source tree is not where it expects; it never falls back to
a copy, because a silent fallback is the drift this decision exists to prevent.

--------------------------------------------------------------------------
HOW SHAPE-IDENTITY IS VERIFIED (not asserted)
--------------------------------------------------------------------------
``--verify-shape`` replays the corpus through the LIVE ``fetch_book_comps``
with the network stubbed out (KTD11's method — the same replay BUI-657 used to
prove byte-identical output over 543 responses) and diffs, comp for comp, what
the live pool contains against what ``extract_comps`` below produces from the
same bytes. It compares every field the backfill claims to reproduce AND the
raw/slab pool split, and it fails on an unexpected extra key — so if someone
adds a sixth stamped field to the live path, this stops being green. See
``verify_shape`` for the field contract and the three fields that are
deliberately NOT reproducible.

--------------------------------------------------------------------------
WHAT IS AND IS NOT RECOVERABLE FROM A RAW RESPONSE
--------------------------------------------------------------------------
* ``comic_id`` — recovered by quoted-phrase lookup against ``GET /api/comics``
  (KTD10), with a masthead-alias fallback. **Never inferred** (KTD3): an
  ambiguous or unmatched phrase posts ``comic_id: null`` and the row lands with
  no identity rather than attached to a plausible book.
* ``observed_at`` — the capture record's own ``timestamp`` (the real live-fetch
  time) or the cache file's mtime. **Never now** (KTD6/KTD7): stamping the run
  time would restate a two-month-old comp as freshly observed, which is exactly
  the input the recency weighting consumes.
* ``tier`` — NOT recoverable. A response does not echo which tier of the ladder
  issued it, and inferring it from query shape is the guess KTD3's posture
  forbids. Posted NULL.
* ``from_cache`` — the corpus this response was read from: True for a cache
  file, False for a capture record (``_capture_raw_response`` fires only on a
  live fetch, right beside ``_cache_put``).
* a capture record whose BUI-628 ``validation`` field is anything other than
  ``"ok"`` is **not imported at all** — see ``read_capture``.
* ``pool`` — see ``extract_comps``.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import os
import re
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import requests

# --------------------------------------------------------------------------
# Live-module bootstrap
# --------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_EBAY_SRC = _REPO_ROOT / "apps" / "ebay" / "src"


def _load_live_sold_comps() -> types.ModuleType:
    """Import ``apps/ebay/src/sold_comps.py`` — the LIVE module, not a copy.

    See the module docstring's import-boundary section for why this is a path
    load rather than a duplication or a subprocess. Fails loudly when the
    source tree is not where it expects: a silent fallback to a vendored copy
    of the parser is the exact drift this whole decision exists to prevent, so
    there is deliberately no fallback at all.
    """
    target = _EBAY_SRC / "sold_comps.py"
    if not target.is_file():
        raise SystemExit(
            f"backfill: cannot find the live parser at {target}.\n"
            "This script must run from a comic-pipeline checkout — it imports "
            "apps/ebay's sold_comps directly (see the import-boundary section "
            "of this file's docstring) and will not fall back to a copy."
        )
    if str(_EBAY_SRC) not in sys.path:
        sys.path.insert(0, str(_EBAY_SRC))
    import sold_comps  # noqa: PLC0415  # deliberately late — see above

    return sold_comps


# --------------------------------------------------------------------------
# Corpus readers
# --------------------------------------------------------------------------

# `provenance` values, mirrored from gixen_overlay.db.COMPS_PROVENANCES. An
# unrecognized value 422s the WHOLE batch, so they are spelled once here.
PROVENANCE_CACHE = "backfill-cache"
PROVENANCE_CAPTURE = "backfill-capture"

# The four keyword excludes build_query appends unless exclude_graded=False.
# Their ABSENCE from the echoed query is what marks a response as having been
# fetched graded-inclusive — see extract_comps.
_GRADED_EXCLUDE_TOKENS = ("-cgc", "-cbcs", "-graded", "-slab")

# Consecutive POST failures before the run gives up. Five is the same bar
# apps/ebay's CIRCUIT_BREAKER_THRESHOLD uses, for the same reason: past it,
# the next attempt is not independent evidence.
_ABORT_AFTER_CONSECUTIVE_FAILURES = 5


class RawResponse:
    """One provider response recovered from disk, with everything the ledger
    needs that is not inside the response body itself."""

    __slots__ = ("source", "ident", "query", "provider", "data",
                 "observed_at", "from_cache", "provenance", "captured_url")

    def __init__(self, *, source: str, ident: str, query: str, provider: str,
                 data: dict, observed_at: float, from_cache: bool,
                 provenance: str, captured_url: str | None = None) -> None:
        self.source = source
        self.ident = ident
        self.query = query
        self.provider = provider
        self.data = data
        self.observed_at = observed_at
        self.from_cache = from_cache
        self.provenance = provenance
        # capture records only: the URL the response was actually fetched from,
        # recorded verbatim by `_capture_raw_response`. Used by the query-echo
        # verification; never needed for the import itself.
        self.captured_url = captured_url

    @property
    def graded_inclusive(self) -> bool:
        """True when the query carried NONE of build_query's graded excludes.

        `not any`, not `not all`: `build_query` emits the four tokens as one
        block or not at all, so "none present" is the exact reconstruction of
        `exclude_graded=False`. A hypothetical partial set is not a tier this
        codebase can produce, and treating it as raw is the failing-safe
        reading — raw is where every tier but one puts what it keeps.
        (Measured 2026-08-03: 521 of 543 responses carry all four, 22 carry
        none, 0 carry a partial set, so the two forms agree on the corpus; the
        difference is which one is right by construction.)
        """
        return not any(t in self.query for t in _GRADED_EXCLUDE_TOKENS)


def _provider_and_query(sc: types.ModuleType, data: dict) -> tuple[str, str] | None:
    """Recover (provider, query) from a raw response body.

    Both providers echo the query they were asked (that is the whole reason
    this backfill is possible): sold-comps.com as ``keyword``, SerpApi as
    ``search_parameters._nkw``. The presence of that echo also identifies the
    provider, so no filename convention or side-channel is needed. Returns None
    for a body that is neither shape — the caller counts it as malformed.
    """
    if isinstance(data.get("items"), list):
        keyword = data.get("keyword")
        if isinstance(keyword, str) and keyword:
            return sc.PROVIDER_SOLD_COMPS, keyword
        return None
    params = data.get("search_parameters")
    if isinstance(params, dict):
        nkw = params.get("_nkw")
        if isinstance(nkw, str) and nkw:
            return sc.PROVIDER_SERPAPI, nkw
    return None


def capture_segments(capture_dir: Path) -> list[Path]:
    """Every tier-0 capture segment in *capture_dir*, retired ones then live.

    BUI-628 turned the capture from one file into a rotating set: the live
    ``raw_responses.jsonl``, plus retired ``raw_responses.<stamp>-<token>
    .jsonl.gz`` segments that are never pruned — and, when compression could
    not finish without risking a line, an intact PLAINTEXT retired segment with
    no ``.gz`` suffix. A reader must therefore tolerate both extensions, and
    must glob rather than open one path: reading only the live segment after a
    rotation silently drops all the history, and a short read looks exactly
    like a small corpus.

    WHAT THE NAME SORT DOES AND DOES NOT GUARANTEE (BUI-680). Retired segments
    come before the live one, and among themselves they are ordered by name.
    That order is DETERMINISTIC — a re-run reads the corpus the same way — but
    it is only APPROXIMATELY chronological. ``_rotate_capture_if_needed`` mints
    ``<UTC stamp to the second>-<8 random hex>``, and the random token exists
    precisely so two rotations in one second cannot collide; within a shared
    second the sort therefore compares two random tokens, in an order
    uncorrelated with which segment was actually retired first. Do not build a
    first-observation rule on this order. KTD4's earliest-observation-wins is
    enforced one level down, in ``read_capture``, which sorts the RECORDS by
    the timestamp each one carries.

    When a retired segment exists in BOTH forms, the plaintext one is read and
    the ``.gz`` skipped: that pair can only mean compression is still in
    flight, and the plaintext copy is the one known to be complete.
    """
    if not capture_dir.is_dir():
        return []
    plain = {p for p in capture_dir.glob("raw_responses*.jsonl")}
    gzipped = [p for p in capture_dir.glob("raw_responses*.jsonl.gz")
               if p.with_suffix("") not in plain]
    live = capture_dir / "raw_responses.jsonl"
    # Name order only: deterministic, but NOT chronological within a shared
    # second (BUI-680 — see the docstring). Ordering observations is
    # `read_capture`'s job, not this one's.
    retired = sorted((plain | set(gzipped)) - {live}, key=lambda p: p.name)
    return retired + ([live] if live.is_file() else [])


def _open_segment(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open(encoding="utf-8")


def read_capture(sc: types.ModuleType, capture_dir: Path,
                 stats: collections.Counter) -> list[RawResponse]:
    """Read every BUI-614 tier-0 capture segment under *capture_dir*.

    The capture record carries its own ``timestamp`` — the real moment of a
    LIVE fetch (``_capture_raw_response`` is called right beside ``_cache_put``
    on the live-fetch path only), which is strictly better provenance than the
    cache's mtime. That is why the caller imports this corpus FIRST: on a
    response present in both, ``upsert_comps`` keeps the first answer (KTD4),
    so the capture's real timestamp and ``backfill-capture`` provenance win.

    SHAPE-INVALID RESPONSES ARE SKIPPED. BUI-628 moved the capture call AHEAD
    of ``_verify_sold_comps_shape`` / the ``LH_Sold=1`` assertion and tags each
    record with ``validation`` — ``"ok"``, or the error string. A body that
    failed validation was never cached and never produced a single comp: the
    pipeline treated it as an error, not as a comp source. Importing one would
    manufacture ledger rows the live path deliberately refused, and because
    ``upsert_comps`` keeps the FIRST answer (KTD4) those rows would then WIN
    over the good ones a later real fetch produced. They stay in tier 0, which
    is exactly the split KTD1 draws. Counted and reported, never silent.

    A record with NO ``validation`` key predates BUI-628, when the capture call
    ran only AFTER validation passed — so its absence positively means "valid",
    and it is imported. That is a fact about the old call site, not an
    optimistic default.

    RECORDS ARE RETURNED OLDEST FIRST, sorted by that same ``timestamp``, and
    that sort — not the segment order — is what makes KTD4's earliest-wins
    true (BUI-680). ``capture_segments`` orders segments by name, which is only
    second-resolution and so arbitrary between two segments rotated in the same
    second; sorting the records instead sidesteps the segment order entirely,
    at record granularity, for no extra I/O — every timestamp is already read
    in this same pass. It needs no fallback value, because a record whose
    ``timestamp`` is missing or non-numeric raises in the parse block below and
    is counted malformed: a ``RawResponse`` on this path always carries a real
    captured time. The sort is STABLE, so records sharing a timestamp keep
    segment-then-line order and a re-run stays byte-identical.

    The clock is the writer's ``time.time()``, so a backwards NTP step could
    still misorder two records. That is the same wall clock the segment names
    were minted from, so it is no worse than what it replaces — and the blast
    radius shrinks from a whole segment to a single fetch.

    Scoped to THIS corpus on purpose. ``main`` concatenates capture BEFORE
    cache so the capture's real fetch time beats the cache's mtime fallback on
    a response present in both; a sort spanning both corpora would silently
    undo that.
    """
    out: list[RawResponse] = []
    segments = capture_segments(capture_dir)
    if not segments:
        stats["capture_absent"] += 1
        return out
    stats["capture_segments"] += len(segments)
    for segment in segments:
        lineno = 0
        try:
            with _open_segment(segment) as fh:
                for lineno, line in enumerate(fh, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    stats["capture_records"] += 1
                    ident = f"{segment.name}:{lineno}"
                    try:
                        record = json.loads(line)
                        data = record["response"]
                        query = record["query"]
                        provider = record["provider"]
                        observed_at = float(record["timestamp"])
                    except Exception as exc:  # noqa: BLE001 — one bad line never aborts the import
                        stats["malformed"] += 1
                        print(f"  skip: capture {ident} unreadable ({exc})",
                              file=sys.stderr)
                        continue
                    if not isinstance(data, dict) or not query or not provider:
                        stats["malformed"] += 1
                        print(f"  skip: capture {ident} missing "
                              "response/query/provider", file=sys.stderr)
                        continue
                    validation = record.get("validation", "ok")
                    if validation != "ok":
                        stats["capture_invalid"] += 1
                        continue
                    out.append(RawResponse(
                        source="capture", ident=ident, query=query,
                        provider=provider, data=data, observed_at=observed_at,
                        from_cache=False, provenance=PROVENANCE_CAPTURE,
                        captured_url=record.get("canonical_url"),
                    ))
        except Exception as exc:  # noqa: BLE001 — a torn segment costs its tail, never the import
            stats["malformed"] += 1
            print(f"  skip: capture segment {segment.name} unreadable after "
                  f"line {lineno} ({exc}) — records already read are kept",
                  file=sys.stderr)
    # BUI-680: the ordering KTD4 actually relies on. Stable, so equal
    # timestamps keep segment-then-line order. See the docstring for why this
    # is done here and not by ordering the segments.
    out.sort(key=lambda r: r.observed_at)
    return out


def read_cache(sc: types.ModuleType, cache_dir: Path,
               stats: collections.Counter) -> list[RawResponse]:
    """Read the digest-keyed response cache.

    The filename is a SHA-256 of the canonical URL and carries no recoverable
    metadata, so both the query and the provider come out of the response body
    itself (``_provider_and_query``) and ``observed_at`` falls back to the
    file's mtime — which IS the response's fetch time, since ``_cache_put``
    writes the file at the moment of the live fetch. Sorted for a deterministic
    run order.
    """
    out: list[RawResponse] = []
    if not cache_dir.is_dir():
        stats["cache_absent"] += 1
        return out
    for path in sorted(cache_dir.glob("*.json")):
        stats["cache_files"] += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — a torn/partial cache file is skipped, never fatal
            stats["malformed"] += 1
            print(f"  skip: {path.name} unreadable ({exc})", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            stats["malformed"] += 1
            print(f"  skip: {path.name} is not a JSON object", file=sys.stderr)
            continue
        found = _provider_and_query(sc, data)
        if found is None:
            stats["malformed"] += 1
            print(f"  skip: {path.name} echoes no query (neither provider shape)",
                  file=sys.stderr)
            continue
        provider, query = found
        out.append(RawResponse(
            source="cache", ident=path.name, query=query, provider=provider,
            data=data, observed_at=path.stat().st_mtime, from_cache=True,
            provenance=PROVENANCE_CACHE,
        ))
    return out


# --------------------------------------------------------------------------
# Comp extraction — the KTD1 shape
# --------------------------------------------------------------------------

def _iso(epoch: float) -> str:
    """Unix epoch → ISO-8601 UTC.

    ``CompItem.observed_at`` is typed ``str | None``, and the ledger's own
    ``first_seen_at``/``last_seen_at`` are ``datetime.now(timezone.utc)
    .isoformat()`` — so ISO is the column's established encoding, and it is
    what makes ``idx_comps_observed`` orderable. The live path currently
    carries ``observed_at`` as a raw ``time.time()`` float (BUI-657), so
    whichever unit posts it must stringify it the same way; see this ticket's
    report for the note raised against U3.
    """
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def extract_comps(sc: types.ModuleType, raw: RawResponse) -> list[dict]:
    """Parse one raw response into ledger-shaped comps, using the LIVE parser.

    Mirrors ``fetch_book_comps._run``'s pooling loop exactly: pick the serving
    provider's extractor, drop comps with no ``product_id``, dedupe within the
    response by ``product_id``, drop ``hard_exclude`` titles, then stamp
    provenance at the point the comp joins the pool.

    POOL ROUTING. ``pool='slab'`` exists in the live ledger only via
    ``fetch_book_comps``'s BUI-524 tier-4 "inclusive" pass, the only caller
    that passes ``route_slabs=True``; every other tier puts everything it keeps
    into the raw pool, including a comp that happens to satisfy
    ``_is_slab_comp``. That tier is also the only one that drops the four
    graded excludes from its query — so the ABSENCE of those tokens from the
    echoed query is a faithful, purely textual reconstruction of the routing
    decision, with no tier inference involved.

    One acknowledged imprecision: ``comic-fmv``'s CGC-proxy second pass sets
    ``include_graded`` on the book, which also drops the excludes, on every
    tier, with ``route_slabs=False``. Its query text is indistinguishable from
    the inclusive tier's. It does not matter here: per plan unit U3 the proxy
    pass does not post comps at all, so there is no live row for a backfilled
    one to disagree with — and a genuine CGC/CBCS sale filed under ``slab`` is
    the semantically right home for it either way.
    """
    if raw.provider == sc.PROVIDER_SOLD_COMPS:
        raw_results = raw.data.get("items") or []
        parse = sc.parse_comp_sold_comps
    else:
        raw_results = raw.data.get("organic_results") or []
        parse = sc.parse_comp

    route_slabs = raw.graded_inclusive
    observed_at = _iso(raw.observed_at)
    seen_ids: set[str] = set()
    comps: list[dict] = []
    for result in raw_results:
        comp = parse(result)
        if comp is None or not comp["product_id"]:
            continue
        if comp["product_id"] in seen_ids:
            continue
        if sc.hard_exclude(comp["title"]):
            continue
        seen_ids.add(comp["product_id"])
        comp["provider"] = raw.provider
        comp["tier"] = None  # not recoverable from a response — see the module docstring
        comp["query"] = raw.query
        comp["from_cache"] = raw.from_cache
        comp["observed_at"] = observed_at
        comp["pool"] = "slab" if (route_slabs and sc._is_slab_comp(comp)) else "raw"
        comp["provenance"] = raw.provenance
        comps.append(comp)
    return comps


# --------------------------------------------------------------------------
# Identity resolution (KTD10) — quoted-phrase recovery
# --------------------------------------------------------------------------

_PHRASE_RE = re.compile(r'"([^"]+)"')


def _phrase_key(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


class ComicResolver:
    """Resolve a query back to the ``comics`` row it was issued for.

    KTD10 settled the method and falsified the obvious alternative: recovering
    the quoted phrase ``build_query`` puts at the front of every query resolves
    412 of 493 cached responses uniquely (83.6%), where re-generating queries
    forward from each ``comics`` row and matching exactly resolves only 270
    (54.8%) — tiers 2-4 mutate the query (drop the year, add a grade label,
    swap the masthead), so the forward direction loses most of the corpus.

    Two lookups, in order, both exact string matches against a precomputed
    index — never a fuzzy match, never a best candidate:

      1. The phrase as ``build_query`` would have written it for that row:
         ``"{article-and-issue-stripped title} {issue}"``, using apps/ebay's
         own ``_strip_leading_article``/``_strip_embedded_issue`` so the two
         normalizations cannot drift.
      2. The same phrase under the row's OTHER masthead
         (``_alias_masthead_title``, BUI-581) — this recovers the responses the
         masthead-swap tier fetched under a name the ``comics`` row does not
         carry. Measured +19 uniquely-resolved responses on the 2026-08-03
         corpus, and only ever consulted when the primary index missed.

    Anything matching more than one ``comics`` row is AMBIGUOUS and resolves to
    None; anything matching none is UNRESOLVED and resolves to None. Both post
    ``comic_id: null`` (KTD3) — identity is recorded or absent, never attached
    to a plausible book. Deliberately NOT narrowing an ambiguous match by a
    year token in the query: measured, that converts only 4 more responses on
    the same corpus, and it buys them with a new way to attach a comp to the
    wrong book (a ``comics`` row whose own ``year`` is NULL drops out of the
    narrowed set, leaving a wrong single survivor).
    """

    def __init__(self, sc: types.ModuleType, comics: list[dict]) -> None:
        self._primary: dict[str, set[int]] = collections.defaultdict(set)
        self._alias: dict[str, set[int]] = collections.defaultdict(set)
        for row in comics:
            comic_id = row.get("id")
            if comic_id is None:
                continue
            title = row.get("title") or ""
            issue = str(row.get("issue") or "")
            norm = sc._strip_embedded_issue(sc._strip_leading_article(title), issue)
            self._primary[_phrase_key(f"{norm} {issue}")].add(comic_id)
            alias = sc._alias_masthead_title(norm)
            if alias:
                alias = sc._strip_embedded_issue(alias, issue)
                self._alias[_phrase_key(f"{alias} {issue}")].add(comic_id)

    def resolve(self, query: str) -> tuple[int | None, str]:
        """→ (comic_id or None, verdict) where verdict is
        ``resolved`` / ``ambiguous`` / ``unresolved``."""
        match = _PHRASE_RE.search(query or "")
        if not match:
            return None, "unresolved"
        key = _phrase_key(match.group(1))
        ids = self._primary.get(key) or self._alias.get(key)
        if not ids:
            return None, "unresolved"
        if len(ids) > 1:
            return None, "ambiguous"
        return next(iter(ids)), "resolved"


# --------------------------------------------------------------------------
# HTTP (monkeypatched wholesale in tests)
# --------------------------------------------------------------------------

def _http_get_json(url: str, timeout: float = 60.0):
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _http_post_json(url: str, payload: dict, timeout: float = 60.0):
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _resolve_server_url(explicit: str | None) -> str:
    url = (explicit
           or os.environ.get("COMICS_SERVER_URL")
           or os.environ.get("GIXEN_SERVER_URL"))
    if not url:
        raise SystemExit(
            "backfill: COMICS_SERVER_URL must be set (or pass --server-url). "
            "A bare unset var would silently address an empty host — the "
            "BUI-352 trap — so this refuses rather than guessing."
        )
    return url.rstrip("/")


# --------------------------------------------------------------------------
# Shape verification (KTD11 replay)
# --------------------------------------------------------------------------

# Fields the backfill reproduces byte-for-byte from the live pool.
#
# `query` is NOT in this list and is not exempt either — it is verified by a
# stronger check that the replay cannot express. The replay drives a synthetic
# book, so the live path necessarily stamps that synthetic book's `nkw`; the
# real corpus's queries are unreachable from it. Instead `verify_shape` proves
# the two halves of the equality directly: `_replay_live` asserts the live path
# stamps exactly the nkw it was served under, and `_verify_query_echo` asserts
# the response's echoed query rebuilds the very URL the response was fetched
# from (the cache filename's digest / the capture record's canonical_url).
# Composed, those give live `query` == echoed `query` on real data.
VERIFIED_FIELDS = ("product_id", "title", "price", "grade", "sold_date",
                   "buying_format", "link", "provider")
# Fields the backfill deliberately does NOT reproduce, each with its reason in
# the module docstring. Listed so `verify_shape` can prove the live comp has
# exactly VERIFIED_FIELDS + these and nothing else: a NEW stamped field on the
# live path lands in neither set and fails the replay loudly, which is the
# anti-drift property this whole check exists for.
UNREPRODUCED_FIELDS = ("tier", "from_cache", "observed_at", "query")


def _verify_query_echo(sc: types.ModuleType, raw: RawResponse) -> bool:
    """Prove the response's echoed query IS the query it was fetched under.

    The whole backfill rests on the echo (`keyword` / `search_parameters._nkw`)
    being the real `nkw`, and that is checkable rather than assumed: rebuild
    the canonical URL from the echo with apps/ebay's own builder, and compare
    it to where the response actually came from —

      * cache: the filename is `sha256(canonical_url).hexdigest() + ".json"`
        (`_cache_path`), so the digest of the rebuilt URL must be the filename.
      * capture: the record stores its `canonical_url` verbatim.

    Returns True when the echo re-derives its own origin, False when it does
    not. **False is reported as UNVERIFIED, not as a failure**, because it has
    two indistinguishable causes and only one is a problem: an unfaithful echo,
    or a cache-key format that changed after the response was written. The
    second is real and benign — BUI-557 pinned `includeCompleteListing` into
    `canonical_sold_comps_url` on 2026-07-28, and every sold-comps.com entry
    written before that re-derives a different digest today with a byte-perfect
    echo. Calling those failures would make this check cry wolf.

    It keeps its teeth anyway: the risk it exists to catch — a provider echoing
    a normalized or truncated keyword instead of ours — would show up as
    essentially the WHOLE corpus going unverified, not a handful of it. Read
    the two counts together, not the failure count alone.
    """
    if raw.provider == sc.PROVIDER_SOLD_COMPS:
        # sold-comps.com is never paginated by us (BUI-523's page-2 gate is
        # SerpApi-shaped), so there is exactly one canonical URL per query.
        candidates = [sc.canonical_sold_comps_url(raw.query)]
    else:
        # BUI-523: a page-2 fetch of the SAME nkw caches under its own key
        # (`_pgn`), so both pages are legitimate origins for one echoed query.
        candidates = [sc.canonical_serpapi_url(raw.query, page=p) for p in (1, 2)]
    if raw.source == "capture":
        return bool(raw.captured_url) and raw.captured_url in candidates
    return raw.ident in [sc._cache_path(url).name for url in candidates]


def _replay_live(sc: types.ModuleType, raw: RawResponse) -> tuple[list[dict], list[dict]]:
    """Drive one raw response through the LIVE ``fetch_book_comps`` with the
    network stubbed, and return its (raw pool, slab pool).

    Zero network calls: ``_fetch_with_fallback`` — the single seam every tier
    funnels through — is swapped for a stub that serves these bytes. Reaching
    for a private function is deliberate; it is the narrowest possible seam
    that leaves the whole parse → hard_exclude → dedupe → stamp → route chain
    running as production runs it.

    Two replay modes, because ``route_slabs`` is only reachable from one tier:

      * exclusion query → the stub serves the response to every tier. Tier 1
        pools it; later tiers re-see the same product_ids and add nothing.
      * graded-inclusive query → the stub serves EMPTY to any query still
        carrying ``-cgc`` and the real bytes otherwise, and the book is dated
        vintage. Tiers 1-3 therefore find nothing, which is exactly the gate
        the BUI-524 tier-4 inclusive pass waits for, and it fires with
        ``route_slabs=True`` against the real bytes.

    The synthetic book uses a non-rebootable masthead and no variant so the
    BUI-581 masthead-swap and BUI-588 variant-drop tiers stay out of the way.
    """
    inclusive = raw.graded_inclusive
    empty = ({"items": []} if raw.provider == sc.PROVIDER_SOLD_COMPS
             else {"organic_results": []})
    served: list[str] = []

    def _stub(nkw, api_key, **kwargs):
        if inclusive and "-cgc" in nkw:
            return empty, False, raw.provider, raw.observed_at
        served.append(nkw)
        return raw.data, False, raw.provider, raw.observed_at

    original = sc._fetch_with_fallback
    sc._fetch_with_fallback = _stub
    try:
        result = sc.fetch_book_comps(
            {
                "title": "Backfill Verification Title",
                "issue": "1",
                "year": 1970 if inclusive else 2024,
                "grade": 9.0,
            },
            api_key="replay-no-network",
        )
    finally:
        sc._fetch_with_fallback = original
    if result.get("error"):
        raise RuntimeError(f"live replay errored: {result['error']}")
    pooled = result["comps"] + result["slab_comps"]
    # The `query` half of the contract (see VERIFIED_FIELDS): every comp the
    # live path pooled carries the nkw of the query that actually served it —
    # so "live stamps the nkw it ran" is checked here rather than assumed, and
    # `_verify_query_echo` supplies the other half on real data.
    if pooled and any(c.get("query") not in served for c in pooled):
        raise RuntimeError(
            "live path stamped a query that was never served: "
            f"{sorted({c.get('query') for c in pooled} - set(served))}")
    return result["comps"], result["slab_comps"]


def verify_shape(sc: types.ModuleType, responses: list[RawResponse],
                 limit: int | None = None) -> int:
    """Replay every response through the live path and diff. → mismatch count.

    This is the ticket's shape-identity *verification*, not its assertion: the
    live pool is recomputed from the same bytes by the same code the pipeline
    runs, and every field the backfill claims is compared against it.
    """
    subset = responses[:limit] if limit else responses
    print(f"\nSHAPE REPLAY — {len(subset)} response(s) through the live "
          f"fetch_book_comps, zero network calls")
    mismatches = 0
    compared = 0
    echo_verified = 0
    for raw in subset:
        if _verify_query_echo(sc, raw):
            echo_verified += 1
        try:
            live_raw, live_slab = _replay_live(sc, raw)
        except Exception as exc:  # noqa: BLE001 — a replay failure is a finding, not a crash
            mismatches += 1
            print(f"  MISMATCH {raw.ident}: replay failed ({exc})")
            continue
        # Keyed on product_id, never on list position: the live path emits two
        # pools and the backfill one list, so the same comps legitimately come
        # out in a different ORDER whenever the inclusive tier splits slabs off.
        # (product_id is unique on both sides — both dedupe on it.)
        live = {c["product_id"]: (c, "raw") for c in live_raw}
        live.update({c["product_id"]: (c, "slab") for c in live_slab})
        mine = {c["product_id"]: c for c in extract_comps(sc, raw)}
        if set(live) != set(mine):
            mismatches += 1
            print(f"  MISMATCH {raw.ident}: live pooled {len(live)} comps, "
                  f"backfill produced {len(mine)}; "
                  f"live-only={sorted(set(live) - set(mine))[:5]} "
                  f"backfill-only={sorted(set(mine) - set(live))[:5]}")
            continue
        for product_id, (live_comp, live_pool) in live.items():
            compared += 1
            my_comp = mine[product_id]
            extra = set(live_comp) - set(VERIFIED_FIELDS) - set(UNREPRODUCED_FIELDS)
            if extra:
                mismatches += 1
                print(f"  MISMATCH {raw.ident}: live comp carries unclassified "
                      f"field(s) {sorted(extra)} — the backfill neither "
                      f"reproduces nor exempts them")
                break
            diffs = [f for f in VERIFIED_FIELDS
                     if live_comp.get(f) != my_comp.get(f)]
            if live_pool != my_comp["pool"]:
                diffs.append("pool")
            if diffs:
                mismatches += 1
                field = diffs[0]
                live_value = live_pool if field == "pool" else live_comp.get(field)
                mine_value = my_comp.get("pool" if field == "pool" else field)
                print(f"  MISMATCH {raw.ident} product_id={product_id}: "
                      f"{field} live={live_value!r} backfill={mine_value!r}")
                break
    print(f"  compared {compared} comps across {len(subset)} responses — "
          f"{mismatches} mismatch(es)")
    print(f"  query echo re-derived its own fetch URL for {echo_verified} of "
          f"{len(subset)} responses "
          f"({len(subset) - echo_verified} unverifiable — see _verify_query_echo)")
    return mismatches


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="BUI-661: backfill the comps ledger from the cached and "
                    "captured raw provider responses.")
    parser.add_argument("--server-url", default=None,
                        help="comics server base URL (default: $COMICS_SERVER_URL)")
    parser.add_argument("--cache-dir", default=None,
                        help="override the response cache dir (default: "
                             "apps/ebay's own CACHE_DIR)")
    parser.add_argument("--capture-dir", default=None,
                        help="override the tier-0 capture dir, whose "
                             "raw_responses*.jsonl[.gz] segments are all read "
                             "(default: apps/ebay's own CAPTURE_DIR)")
    parser.add_argument("--dry-run", action="store_true",
                        help="read and resolve everything, print exactly what "
                             "would be posted, write nothing")
    parser.add_argument("--verify-shape", action="store_true",
                        help="replay the corpus through the live "
                             "fetch_book_comps and diff; writes nothing")
    parser.add_argument("--limit", type=int, default=None,
                        help="process at most N responses (per corpus) — for a "
                             "smoke import before the full run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sc = _load_live_sold_comps()

    cache_dir = Path(args.cache_dir) if args.cache_dir else sc.CACHE_DIR
    capture_dir = Path(args.capture_dir) if args.capture_dir else sc.CAPTURE_DIR

    stats: collections.Counter = collections.Counter()
    # Capture FIRST, deliberately. `upsert_comps` keeps the first answer
    # (KTD4), so on a response present in both corpora the capture's REAL
    # fetch timestamp and `backfill-capture` provenance are the ones that
    # survive, instead of the cache's mtime fallback.
    responses = read_capture(sc, capture_dir, stats)
    if args.limit:
        responses = responses[:args.limit]
    cache_responses = read_cache(sc, cache_dir, stats)
    if args.limit:
        cache_responses = cache_responses[:args.limit]
    responses.extend(cache_responses)

    print(f"corpus: capture={stats['capture_records']} record(s) across "
          f"{stats['capture_segments']} segment(s) "
          f"({'absent' if stats['capture_absent'] else capture_dir}), "
          f"cache={stats['cache_files']} file(s) "
          f"({'absent' if stats['cache_absent'] else cache_dir}), "
          f"shape-invalid capture records skipped={stats['capture_invalid']}, "
          f"malformed/skipped={stats['malformed']}, "
          f"usable={len(responses)}")
    if not responses:
        print("nothing to import.")
        return 0

    if args.verify_shape:
        return 1 if verify_shape(sc, responses) else 0

    server_url = _resolve_server_url(args.server_url)
    health = _http_get_json(f"{server_url}/health")
    print(f"server: {server_url} ({health})")
    resolver = ComicResolver(sc, _http_get_json(f"{server_url}/api/comics"))

    totals: collections.Counter = collections.Counter()
    spans: dict[str, list[str]] = {}
    consecutive_failures = 0
    aborted = False
    for raw in responses:
        comic_id, verdict = resolver.resolve(raw.query)
        totals[verdict] += 1
        comps = extract_comps(sc, raw)
        totals["comps"] += len(comps)
        totals[f"comps_{raw.source}"] += len(comps)
        if comps:
            observed = comps[0]["observed_at"]
            span = spans.setdefault(raw.source, [observed, observed])
            span[0] = min(span[0], observed)
            span[1] = max(span[1], observed)
        if not comps:
            # Never post an empty batch: `CompsIngestRequest` requires a
            # non-empty `comps` list, so it would 422 the whole call and land
            # in `rejected_writes` looking like a defect. A response whose
            # every comp was hard-excluded is a normal outcome, not a failure.
            totals["responses_empty"] += 1
            continue
        if args.dry_run:
            totals["would_post"] += 1
            continue
        try:
            result = _http_post_json(
                f"{server_url}/api/comics/comps",
                {"comic_id": comic_id, "comps": comps},
            )
        except Exception as exc:  # noqa: BLE001 — one failed batch never aborts the import
            totals["post_failed"] += 1
            consecutive_failures += 1
            print(f"  POST FAILED for {raw.ident} (comic_id={comic_id}): {exc}",
                  file=sys.stderr)
            if consecutive_failures >= _ABORT_AFTER_CONSECUTIVE_FAILURES:
                # Nothing is landing — almost always a server without BUI-656's
                # endpoint, or one that is down. Stop rather than emit one error
                # line per response: 500 identical failures bury the signal, and
                # there is nothing to lose by stopping since a re-run is a no-op
                # for everything already imported.
                aborted = True
                print(f"  ABORTING after {consecutive_failures} consecutive "
                      "POST failures — is the server running BUI-656's "
                      "/api/comics/comps? Re-running is safe.", file=sys.stderr)
                break
            continue
        consecutive_failures = 0
        totals["posted"] += 1
        totals["inserted"] += int(result.get("inserted") or 0)
        totals["updated"] += int(result.get("updated") or 0)
        totals["conflicts"] += int(result.get("conflicts") or 0)

    resolved = totals["resolved"]
    considered = resolved + totals["ambiguous"] + totals["unresolved"]
    rate = (100.0 * resolved / considered) if considered else 0.0
    print("\n" + "=" * 64)
    print(f"responses           : {considered}")
    print(f"  resolved          : {resolved} ({rate:.1f}%)  [KTD10 floor: 412 of 493 = 83.6%]")
    print(f"  ambiguous         : {totals['ambiguous']}  -> comic_id NULL (KTD3)")
    print(f"  unresolved        : {totals['unresolved']}  -> comic_id NULL (KTD3)")
    print(f"  malformed/skipped : {stats['malformed']}")
    print(f"  shape-invalid     : {stats['capture_invalid']}  "
          "(BUI-628 capture records the live path refused — left in tier 0)")
    print(f"comps extracted     : {totals['comps']} "
          f"(capture {totals['comps_capture']}, cache {totals['comps_cache']})")
    for source, (lo, hi) in sorted(spans.items()):
        # A corpus copied without preserving mtimes shows up here as a span of
        # "today .. today" instead of the weeks it should cover (KTD6/KTD7).
        print(f"  observed_at {source:<8}: {lo} .. {hi}")
    if args.dry_run:
        print(f"DRY RUN — would post {totals['would_post']} batch(es); "
              "nothing was written.")
        return 0
    print(f"batches posted      : {totals['posted']}")
    print(f"  inserted          : {totals['inserted']}")
    print(f"  updated           : {totals['updated']}  "
          "(a re-run lands here: seen_count bumped, no market fact rewritten)")
    print(f"  conflicts         : {totals['conflicts']}")
    print(f"  POST failures     : {totals['post_failed']}")
    print("=" * 64)
    print("Diff `SELECT COUNT(*) FROM comps` before/after against `inserted`.")
    if aborted:
        print("ABORTED — nothing was landing. Fix the server and re-run; "
              "everything already imported is a no-op the second time.",
              file=sys.stderr)
        return 1
    if totals["post_failed"]:
        print("PARTIAL IMPORT — some batches never landed. A silent partial "
              "import is a failed import; re-run after fixing the cause "
              "(re-running is safe: no row is inserted twice).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
