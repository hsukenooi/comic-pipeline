#!/usr/bin/env python3
"""seller-scan: Match an eBay seller's active listings against your LOCG wish list."""

import argparse
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

from comic_identity import (  # noqa: F401 — BUI-253 Step 1: re-exported for callers
    ComicIdentity,
    EDITION_LABELS,
    _digital_reject,
    _EDITION_PATTERNS,
    _foreign_edition_reject,
    _LOT_MEMBER,
    _LOT_RE,
    _normalize,
    _reprint_reject,
    _second_print_reject,
    _series_tokens,
    _strip_grades,
    _title_paren_years,
    _title_volume,
    _trading_card_reject,
    era_mismatch,
    foreign_edition_examples,
    hard_reject,
    identify_comic,
    later_printing_examples,
    lot_count_mismatch,
    publication_year_mismatch,
    score_against_wish,
    series_volume,
    series_year_range,
    should_reject,
)
from ebay_fetch import (
    UnknownSellerError,
    get_token,
    load_config,
    load_seller_aliases,
    parse_item_summary,
    resolve_seller_username,
    save_seller_alias,
    search_seller_listings,
)


# ─── Server URL resolution (BUI-220) ──────────────────────────────────────────

_DEPRECATION_WARNED = False


def _server_base():
    """Return the comics server base URL (trailing slash trimmed), or "".

    BUI-220: the canonical env var is COMICS_SERVER_URL; GIXEN_SERVER_URL is a
    deprecated alias still read as a fallback. Using only the old var emits a
    one-line deprecation warning to stderr (once).
    """
    global _DEPRECATION_WARNED
    base = os.environ.get("COMICS_SERVER_URL", "").rstrip("/")
    if base:
        return base
    legacy = os.environ.get("GIXEN_SERVER_URL", "").rstrip("/")
    if legacy and not _DEPRECATION_WARNED:
        print(
            "warning: GIXEN_SERVER_URL is deprecated; use COMICS_SERVER_URL",
            file=sys.stderr,
        )
        _DEPRECATION_WARNED = True
    return legacy


# ─── Wish list fetching ───────────────────────────────────────────────────────

def fetch_wish_list():
    """Fetch the wish list from the comics server API. Returns a list of
    {id, name} dicts.

    BUI-88 (R10): seller-scan lives in apps/ebay, which is NOT a uv workspace
    member and cannot import locg-cli — so it fetches the wish-list over HTTP
    from the server's /api/comics/wish-list endpoint instead of shelling out to
    the `locg` CLI. Fails loudly on any unreachable-server / non-200 / bad-JSON
    condition (never returns a partial or empty list silently) so a scan can't
    run against a stale or empty wish list because the server was down.
    """
    base = _server_base()
    if not base:
        print(
            "Error: COMICS_SERVER_URL is not set — cannot reach the wish-list API.\n"
            "Set it in ~/.zshrc (MacBook → http://mac-mini.tail9b7fa5.ts.net:8080; "
            "Mac Mini → http://localhost:8080).",
            file=sys.stderr,
        )
        sys.exit(1)
    url = f"{base}/api/comics/wish-list"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching wish list from {url}: {e}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error parsing wish list JSON from {url}: {e}", file=sys.stderr)
        sys.exit(1)


# ─── Seen-tracking (BUI-113) ──────────────────────────────────────────────────

def fetch_seen_item_ids(seller):
    """Best-effort: return the set of item_ids already surfaced in a prior scan.

    BUI-113: unlike fetch_wish_list (which hard-fails — an empty wish-list would
    cause a wrong "no match"), seen-tracking is non-fatal. A failed read only
    risks re-showing a listing you've seen (mildly annoying, always safe);
    hiding everything because the server is down could silently suppress a real
    buy. So any failure → empty set + a warning, and the scan continues showing
    all matches.
    """
    base = _server_base()
    if not base:
        return set()
    url = f"{base}/api/comics/seller-scan/seen"
    try:
        resp = requests.get(url, params={"seller": seller}, timeout=10)
        resp.raise_for_status()
        return set(resp.json().get("item_ids", []))
    except (requests.exceptions.RequestException, ValueError) as e:
        print(
            f"Warning: could not fetch seen item IDs ({e}); showing all matches",
            file=sys.stderr,
        )
        return set()


def record_items_seen(item_ids, seller):
    """Best-effort: mark surfaced item_ids as seen so future scans skip them.

    Warns on failure but never aborts (see fetch_seen_item_ids for the rationale).
    """
    base = _server_base()
    if not base or not item_ids:
        return
    url = f"{base}/api/comics/seller-scan/seen"
    try:
        resp = requests.post(
            url, json={"item_ids": item_ids, "seller": seller}, timeout=10
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Warning: could not record seen item IDs ({e})", file=sys.stderr)


# ─── Matching ─────────────────────────────────────────────────────────────────
# BUI-253 Step 1: the deterministic title-parsing / reject helpers that used to
# live here (grade-stripping, lot detection, edition patterns, era/volume
# disambiguation, the reject lexicons, hard_reject, should_reject) now live in
# comic_identity.py and are imported above — this module keeps the wish-item
# prep, the fuzzy scorer, the LLM verify layer, and the CLI.

def _parse_wish_name(name):
    """Parse 'Series Name #N' into (series, issue_number) or (name, None)."""
    m = re.match(r"^(.*?)\s*#(\d+\w*)\s*$", name.strip())
    if m:
        return m.group(1).strip(), m.group(2)
    return name.strip(), None


def prepare_wish_items(wish_list):
    """Augment wish list items with parsed series + issue for matching."""
    out = []
    for item in wish_list:
        name = item.get("name", "")
        series, issue = _parse_wish_name(name)
        tokens = _series_tokens(series)
        if not tokens or issue is None:
            continue
        # BUI-226: carry raw LOCG series_name (decorated, e.g.
        # "The Amazing Spider-Man (Vol. 1) (1963 - 1998)") and release year
        # for era-gate disambiguation.  _series_name may be None for ~9% of
        # local-only items that were added without a Metron round-trip.
        release_date = item.get("release_date") or ""
        out.append({
            "id": item.get("id"),
            "name": name,
            "series": series,
            "issue": issue,
            "_tokens": tokens,
            "_series_name": item.get("series_name"),
            "_release_year": release_date[:4] or None,
        })
    return out


def match_listing(title, wish_items):
    """Return (best_wish_item, score) or (None, 0.0) for an eBay listing title.

    Requires:
    - Issue number present in title as #N or as isolated digits
    - At least 50% of series tokens present in title

    BUI-253 Step 2: the actual scoring math for a single (title, wish) pair
    now lives in comic_identity.score_against_wish — this function is just
    identify_comic(title) -> score_against_wish(identity, wish) per wish item,
    picking the best-scoring wish above the 0.65 floor. identify_comic() runs
    ONCE per title (not per wish item): it caches the grade-stripped/
    normalized title on the identity (ComicIdentity._title_norm) so
    score_against_wish never redoes that work per wish item, matching the
    pre-refactor performance (O(1) shared string processing, not
    O(len(wish_items))). See score_against_wish's docstring for why the
    scoring behavior itself is unchanged.
    """
    identity = identify_comic(title)
    best = None
    best_score = 0.0

    for wish in wish_items:
        score = score_against_wish(identity, wish)
        if score > best_score:
            best_score = score
            best = wish

    if best_score >= 0.65:
        return best, best_score
    return None, 0.0


# ─── Claude verification ──────────────────────────────────────────────────────

# Maximum candidates per Claude CLI call.  At ~30–50 output tokens/verdict,
# an 8 096-token cap limits a single call to ~150–270 candidates before
# silent truncation.  BUI-297: dropped from 100 → 30.  A single chunk-level
# transport timeout was fail-closing the *entire* chunk; at 100 that lost 28
# real wish-list matches in one 2026-07-11 comics4less scan.  A smaller chunk
# both (a) is far less likely to time out at all and (b) bounds the blast
# radius when it does — combined with the bisection retry below, a persistent
# timeout now drops at most a handful of candidates, not a hundred.
_VERIFY_CHUNK_SIZE = 30

# BUI-297: on a chunk-level *timeout* the chunk is split in half and each half
# retried, recursing down to this floor.  A single stubborn candidate that
# still can't be verified at the floor is counted as *dropped* (never verified),
# never silently discarded.
_VERIFY_BISECT_FLOOR = 1

# BUI-297: cap the bisection recursion depth so a verifier that hangs (times
# out) on EVERY call can't fan out into ~2·chunk sequential 180s timeouts
# (~3h) before the circuit breaker below trips.  At depth 3 a 30-candidate
# chunk issues at most 2⁴−1 = 15 calls before dropping the un-isolated leaves,
# vs 59 unbounded — still isolating a timeout down to small (~2–4 candidate)
# groups, but bounding the worst-case hang to minutes, not hours.
_VERIFY_MAX_BISECT_DEPTH = 3

# BUI-297: distinct exit code for an INCOMPLETE run — one or more candidates
# were never verified (dropped) — so a caller/skill can tell it apart from a
# clean "0 matches" scan (exit 0) and knows to re-run.
_EXIT_INCOMPLETE = 3

# BUI-298: exit code for a batch where at least one seller couldn't be
# resolved/fetched (its slot carries an `error`) but NO seller was incomplete.
# Kept distinct from _EXIT_INCOMPLETE=3 so a caller can tell "some seller
# couldn't even be scanned" apart from "every requested seller was scanned but
# verification didn't fully complete for at least one."
_EXIT_SELLER_ERROR = 2

# BUI-324: exit code for a batch where at least one seller's WORKER CRASHED —
# an unhandled exception inside _scan_one_seller_impl, isolated by the
# _scan_one_seller wrapper (BUI-319) — as opposed to an expected, resolvable
# per-seller failure (unknown seller name / listing-fetch transport error),
# which stays on _EXIT_SELLER_ERROR=2. Before this ticket both cases shared
# exit 2 and were only distinguishable by grepping the per-seller `error`
# string for "seller scan crashed:" vs "unknown seller"/"listing fetch
# failed" — a monitoring caller can now branch on the exit code alone. Picked
# as the next unused code (0-3 already taken) rather than renumbering any
# existing tier, so an existing monitor keyed on 0/1/2/3 keeps working.
_EXIT_SELLER_CRASHED = 4

# BUI-270/BUI-297: the `claude` verifier is globally unavailable (CLI missing,
# broken auth, or every chunk failed transport). verify_with_claude sys.exit(1)s
# on this. It's the MOST severe outcome — nothing could be verified and the run
# is truncated — so it takes priority over incomplete/crashed/seller-error/clean.
# Multi-seller exit-code priority (main()):
#   1 (_EXIT_VERIFIER_DOWN)  — verifier globally down, run truncated
#   3 (_EXIT_INCOMPLETE)     — verifier worked but some candidates never verified
#   4 (_EXIT_SELLER_CRASHED) — a seller's worker crashed unexpectedly (BUI-324)
#   2 (_EXIT_SELLER_ERROR)   — a seller couldn't be resolved/fetched
#   0                        — clean
_EXIT_VERIFIER_DOWN = 1


# BUI-307: seller-level parallelism. The multi-seller loop (BUI-298) ran each
# seller's listing-fetch + chunked Claude verification fully SEQUENTIALLY, so
# wall-clock scaled linearly with sellers × chunks and a single stuck 180s
# verify timeout blocked every seller queued behind it. `claude -p` calls are
# stateless, so scanning sellers in a bounded thread pool is safe.
#
# Capped at 3 deliberately: each worker's per-seller listing fetch hits the eBay
# Browse API, and 2–3 concurrent workers is the safe ceiling that parallelizes
# the common small batches without risking Browse rate limits — do NOT raise
# this without re-checking eBay's Browse quota. Chunks WITHIN a seller still run
# sequentially (verify_with_claude is unchanged), so BUI-297's per-chunk circuit
# breaker keeps its sequential-chunk assumption; and the breaker state
# (transport_ok) is a local of each verify_with_claude call, so it stays
# per-seller — never shared or mutated across the concurrent sellers.
_SELLER_SCAN_MAX_WORKERS = 3


# ─── Rejected-candidate cache (BUI-301) ────────────────────────────────────────
# BUI-149: a model-REJECTED candidate is correctly never marked "seen" (it's
# not a genuine match) — but that means every scan re-fetches and re-verifies
# it, paying the Claude CLI verification cost on the same rejected candidate
# forever. This cache is deliberately SEPARATE from the genuine-match seen-set
# (fetch_seen_item_ids/record_items_seen above, server-side): it never marks
# anything as a real match, it only remembers "the model already rejected this
# (listing, wish) pair recently" so a repeat scan can skip re-verifying it. An
# entry expires after _REJECTED_CACHE_TTL_SEC, so a candidate is always
# eventually re-checked — a transient misjudgement or an edited listing gets
# another chance rather than being suppressed forever.
#
# Keyed by the (item_id, wish_name) PAIR, not item_id alone: match_listing
# picks the single best-scoring wish per listing, so the same listing can pair
# to a different wish on a later run (a false-positive wish was bought/removed,
# leaving a genuine one). Keying on item_id alone would then skip the genuine
# re-pairing for the whole TTL — the exact "never suppress a real match"
# invariant this feature must not break. The wish_name is part of the key so a
# rejection only suppresses the pair the model actually judged.
#
# Stored as a single JSON file ({pair_key: iso_timestamp}), mirroring the
# tmp→rename atomic-write idiom used by ebay_search_cache.py / ebay_fetch.py's
# aspects cache, but keyed by a timestamp *inside* the file (not file mtime)
# since many pairs share one file.
_REJECTED_CACHE_PATH: Path = Path.home() / ".cache" / "seller-scan" / "rejected.json"
_REJECTED_CACHE_TTL_SEC: int = 14 * 24 * 3600  # 14 days

# Separator joining (item_id, wish_name) into one JSON-string key. \x1f (ASCII
# unit separator) never appears in an eBay item_id or a comic wish name, so it
# can't collide with either field's content.
_REJECTED_CACHE_KEY_SEP = "\x1f"


def _rejected_cache_key(item_id: str, wish_name: str) -> str:
    """Compose the (item_id, wish_name) pair key for the rejected cache."""
    return f"{item_id}{_REJECTED_CACHE_KEY_SEP}{wish_name}"


def _rejected_cache_entry_age_sec(iso_ts: str, now: float) -> float:
    """Return the age in seconds of an ISO-8601 timestamp relative to *now*
    (epoch seconds).

    An unparseable timestamp is treated as infinitely old, and a future-dated
    one (negative age, e.g. from clock skew or a hand-edited file) as expired,
    so a corrupt or bogus entry can't wedge a candidate out of re-verification
    forever — the caller drops any entry this reports as expired.
    """
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return float("inf")
    age = now - dt.timestamp()
    # Future-dated (age < 0) → treat as expired: a legitimate entry is only ever
    # read by a later process, so a negative age means a bad clock/edit, not a
    # fresh entry. Reads never happen in the same process that wrote the entry.
    return age if age >= 0 else float("inf")


def _load_rejected_cache() -> dict[str, str]:
    """Return {pair_key: iso_timestamp} of recently model-rejected candidates.

    A missing or corrupt file returns {} (never raises). Entries older than
    _REJECTED_CACHE_TTL_SEC (or future-dated — see _rejected_cache_entry_age_sec)
    are dropped here so the cache stays bounded and every rejection is
    eventually re-checked, rather than growing forever.
    """
    if not _REJECTED_CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(_REJECTED_CACHE_PATH.read_text())
        if not isinstance(data, dict):
            return {}
    except Exception:  # noqa: BLE001 — corrupt/partial file → empty cache
        return {}
    now = time.time()
    return {
        key: ts
        for key, ts in data.items()
        if _rejected_cache_entry_age_sec(ts, now) <= _REJECTED_CACHE_TTL_SEC
    }


def _save_rejected_cache(cache: dict[str, str]) -> None:
    """Persist the rejected-candidate cache (atomic tmp→rename write).

    Best-effort: an OSError (disk full, read-only FS, permission) is warned and
    swallowed, never raised. The cache is a cost optimization — a failed write
    must fall open to normal re-verification, not abort the scan (and, in a
    multi-seller batch, crash before any seller's results are printed). Mirrors
    record_items_seen's best-effort contract.
    """
    try:
        _REJECTED_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _REJECTED_CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache))
        tmp.replace(_REJECTED_CACHE_PATH)
    except OSError as e:
        print(
            f"Warning: could not persist rejected-candidate cache ({e})",
            file=sys.stderr,
        )


# BUI-307: serialize the rejected-cache read-modify-write across the seller-level
# worker threads of THIS process. _save_rejected_cache's atomic tmp→rename makes
# a *single* write safe, but under seller parallelism two workers could each load
# {X}, add a different entry, and write — a plain last-writer-wins overwrite would
# silently DROP the other worker's rejection (and, because both write the same
# `.tmp` path, could interleave into a corrupt file mid-write). This lock makes
# the save a locked re-read → merge → write so no rejection is lost and only one
# thread IN THIS PROCESS touches the `.tmp` file at a time. Reads at the START of
# verify_with_claude stay lock-free on purpose: a stale skip-decision only costs
# a redundant re-verify (safe, fail-open), and the atomic rename means a lock-
# free reader never observes a partially-written file.
#
# Scope: intra-process only. Two *separate* seller-scan processes racing the same
# file is a pre-existing condition (unchanged by BUI-307) — atomic rename keeps
# it from corrupting readers, and the worst case is a lost cache entry (a
# redundant re-verify next run), never a wrong buy. That's out of scope here.
_REJECTED_CACHE_LOCK = threading.Lock()


def _record_rejections(new_entries: dict[str, str]) -> None:
    """Merge freshly model-rejected ``{pair_key: iso_ts}`` entries into the
    on-disk rejected cache under _REJECTED_CACHE_LOCK (BUI-307).

    Re-reads the current on-disk cache *inside* the lock before merging, so a
    concurrent seller-scan worker's entries are preserved rather than clobbered
    by a last-writer-wins overwrite. Best-effort: inherits _save_rejected_cache's
    fail-open OSError handling (a persist failure warns, never raises).
    """
    if not new_entries:
        return
    with _REJECTED_CACHE_LOCK:
        cache = _load_rejected_cache()
        cache.update(new_entries)
        _save_rejected_cache(cache)


class _VerifyTimeout(RuntimeError):
    """A `claude` CLI verification call that timed out.

    BUI-297: a subclass of RuntimeError (so existing `except RuntimeError`
    catches still work) that lets the caller distinguish a *timeout* — worth a
    bisection retry, since a smaller/faster call may fit the window — from other
    transport failures (nonzero exit / empty output / exec error) where retrying
    smaller chunks can never help and only amplifies load on an already-failing
    dependency (bad auth, rate-limit).
    """


def _verify_via_claude_cli(prompt: str) -> str:
    """Run the verify prompt through the `claude` CLI (subscription auth, no
    ANTHROPIC_API_KEY needed — BUI-270). The prompt goes via stdin (not argv)
    to avoid ARG_MAX/escaping issues on a chunk of candidates.

    Raises _VerifyTimeout on a timeout and RuntimeError on any other transport
    failure (nonzero exit, exec error, or empty stdout) so the caller can fold
    it into the fail-closed chunk-drop path — a CLI hiccup must never leak an
    unverified match — while bisecting only on the timeout.
    """
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "claude-haiku-4-5-20251001",
             "--output-format", "text"],
            input=prompt, capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise _VerifyTimeout(f"claude CLI timed out: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"claude CLI failed to start: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "claude CLI failed")
    if not result.stdout.strip():
        raise RuntimeError("claude CLI returned empty output")
    return result.stdout


def _build_verification_prompt(chunk, edition_words, foreign_examples, later_printing_examples):
    """Build the Claude verification prompt text for one chunk of candidates.

    Assembles the numbered listing/wish pairs (with an optional "Correct
    series:" hint per candidate) into the verification prompt template.
    """
    pairs_parts = []
    for idx, cand in enumerate(chunk, 1):
        pair_text = (
            f'{idx}. Listing: "{cand["title"]}"\n'
            f'   Wish item: "{cand["wish_name"]}"'
        )
        sn = cand.get("_series_name")
        if sn:
            pair_text += f"\n   Correct series: {sn}"
        pairs_parts.append(pair_text)
    pairs = "\n".join(pairs_parts)
    return f"""You are a comic book expert. For each listing/wish-item pair, decide if the listing is a genuine match — same series, same issue number, same edition type.

Reject if:
- Different series sharing words (Spider-Man Noir vs Amazing Spider-Man, X-Factor vs X-Men, Superior/Ultimate Spider-Man vs Amazing Spider-Man)
- {edition_words}, or special edition matching a regular series issue (and vice versa)
- Lot listing where the issue number appears in the lot size
- Promotional reprint (Trick or Read, LCSD, Amazon promo, Undeluxe)
- Modern renumbered issue matching an original issue number (e.g. #10 (811))
- Series name only in a subtitle or story description, not the actual series
- Different series VOLUME / relaunch: if the 'Correct series' line shows a specific era, reject listings where 'vol N' or a (YYYY) indicates a different era than the one shown
- Foreign-language or foreign-market reprint/edition (e.g. {foreign_examples}, or any Spanish/French/German/Italian-language edition) when the wish item is the original US edition
- Numbered sequential run or complete multi-issue set (e.g. "Books 1-4", "Issues 1 through 6", "complete set", "full run") when the wish item is a single specific issue
- Later printing / reprint of a key issue (e.g. {later_printing_examples}, or a bare "reprint") when the wish item means the original first print. Newsstand and Direct editions are NOT reprints — keep those

Respond with a JSON array containing ONLY the ids you are REJECTING, each with a brief reason:
[{{"id": 3, "reason": "X-Factor not X-Men"}}, {{"id": 7, "reason": "annual vs regular"}}]

If nothing is rejected, return [].

Any candidate id NOT present in your response is treated as genuine.

Pairs:
{pairs}"""


def _parse_verification_response(text, chunk, chunk_label):
    """Parse one chunk's Claude verification response.

    Returns `(kept, filtered_info)` — `kept` is the subset of `chunk` NOT
    rejected; `filtered_info` is a list of `{item_id, title, wish_name,
    reason}` dicts, one per model-rejected candidate (BUI-298: threaded back
    as data, not just printed to stderr, so `--json` output can carry the
    "Filtered N false positive(s)" reasons inline — see requirement #2).

    Returns None if the response could not be parsed/validated.  None means
    "drop this chunk" (fail-closed): catches json.JSONDecodeError plus
    KeyError/ValueError/TypeError during id validation, emits the warning
    messages, and prints the BUI-149 rejected-candidate stderr listing.
    """
    json_match = re.search(r"\[.*\]", text, re.DOTALL)
    if not json_match:
        print(
            f"Warning: could not parse Claude response for candidates "
            f"{chunk_label}; dropping chunk (fail-closed)",
            file=sys.stderr,
        )
        return None

    try:
        rejected_list = json.loads(json_match.group())
    except json.JSONDecodeError:
        print(
            f"Warning: invalid JSON in Claude response for candidates "
            f"{chunk_label}; dropping chunk (fail-closed)",
            file=sys.stderr,
        )
        return None

    # Validate: every returned object must have an int id strictly within
    # the chunk's 1-based range.  A missing key, non-int id, or out-of-range
    # id indicates a malformed response → reject the whole chunk (fail-closed).
    valid_ids = set(range(1, len(chunk) + 1))
    try:
        rejected_ids: dict[int, str] = {}
        for v in rejected_list:
            rid = v["id"]  # KeyError if missing
            if not isinstance(rid, int):
                raise ValueError(f"non-int id: {rid!r}")
            if rid not in valid_ids:
                raise ValueError(f"id {rid} out of range 1..{len(chunk)}")
            rejected_ids[rid] = v.get("reason", "")
    except (KeyError, ValueError, TypeError) as exc:
        print(
            f"Warning: invalid rejected-ids in Claude response for candidates "
            f"{chunk_label} ({exc}); dropping chunk (fail-closed)",
            file=sys.stderr,
        )
        return None

    # BUI-149: surface each rejected candidate (with the model's reason) to
    # stderr so the user can override if the verifier was wrong.  BUI-298:
    # also collect the same info as data (`filtered_info`) so `--json` output
    # can carry it inline, not just print it — the stderr printing stays for
    # anyone watching the terminal.
    filtered_info = []
    if rejected_ids:
        print(
            f"Filtered {len(rejected_ids)} likely false positive(s) "
            f"(Claude verification):",
            file=sys.stderr,
        )
        for idx, cand in enumerate(chunk, 1):
            if idx in rejected_ids:
                reason = rejected_ids[idx]
                line = f"  - {cand.get('title', '?')}  ↮  {cand.get('wish_name', '?')}"
                if reason:
                    line += f"  — {reason}"
                print(line, file=sys.stderr)
                filtered_info.append({
                    "item_id": cand.get("item_id"),
                    "title": cand.get("title"),
                    "wish_name": cand.get("wish_name"),
                    "reason": reason,
                })

    kept = [cand for idx, cand in enumerate(chunk, 1) if idx not in rejected_ids]
    return kept, filtered_info


def _verify_chunk(chunk, base_index, prompt_ctx, depth=0):
    """Verify one chunk. Returns ``(kept, dropped, filtered, transport_ok)``.

    `base_index` is the 0-based global offset of this chunk within `matches`,
    used only to render the human-readable 1-based candidate range in warnings
    (so a bisected sub-chunk still reports its true global position).  `depth`
    is the bisection recursion depth, capped at `_VERIFY_MAX_BISECT_DEPTH`.

    The per-candidate outcomes map onto the return components (BUI-297 — the
    crux of that ticket; `filtered` added in BUI-298):

    - **kept**: a successfully-parsed model response did NOT reject it.
    - **model-rejected** → **filtered** (BUI-298: this used to only print to
      stderr; now also returned as `{item_id, title, wish_name, reason}`
      data so `--json` output can carry it inline): a successfully-parsed
      response explicitly rejected it, with the reason surfaced to stderr
      AND returned here.  This is still a safe, silent-from-the-table drop —
      excluded from `kept` — the model made a real judgement, so re-surfacing
      it as a *candidate* on the next run would be noise (it's just also
      reported as data now, not re-verified).
    - **dropped** (loud — the never-verified case BUI-297 exists to fix): a
      candidate that never received a usable verdict, because the transport
      failed (timeout / nonzero exit / empty output, even after bisection down
      to the floor) or the response was unparseable.  These MUST be reported
      loudly and MUST NOT be marked seen, so they resurface on re-run.

    `transport_ok` counts calls where the model actually returned text
    (regardless of parseability) — it proves the verifier is reachable and
    feeds the caller's global-failure safety net + circuit breaker.
    """
    edition_words, foreign_examples, later_examples = prompt_ctx
    chunk_label = f"{base_index + 1}–{base_index + len(chunk)}"

    # Build pairs text.  When the candidate carries a decorated series name
    # (_series_name, stripped from output before printing — see the
    # underscore-key filter in main()), include it as a "Correct series:"
    # hint so Haiku knows the exact era the user wants.
    prompt = _build_verification_prompt(
        chunk, edition_words, foreign_examples, later_examples
    )

    try:
        text = _verify_via_claude_cli(prompt)
    except _VerifyTimeout as exc:
        # Timeout — the chunk was NEVER verified.  BUI-297: before giving up,
        # bisect and retry each half; a 30-candidate chunk that times out often
        # succeeds when split (smaller prompt / faster call), so one timeout no
        # longer drops the whole chunk.  Bounded by _VERIFY_MAX_BISECT_DEPTH so
        # a verifier that hangs on every call can't fan out for hours.
        if len(chunk) > _VERIFY_BISECT_FLOOR and depth < _VERIFY_MAX_BISECT_DEPTH:
            print(
                f"Warning: claude CLI verification timed out for candidates "
                f"{chunk_label} ({exc}); bisecting and retrying each half",
                file=sys.stderr,
            )
            mid = len(chunk) // 2
            lk, ld, lf, lok = _verify_chunk(
                chunk[:mid], base_index, prompt_ctx, depth + 1
            )
            rk, rd, rf, rok = _verify_chunk(
                chunk[mid:], base_index + mid, prompt_ctx, depth + 1
            )
            return lk + rk, ld + rd, lf + rf, lok + rok
        # Floor / max depth reached and it still times out.  These are "never
        # verified" candidates, NOT model rejections — count them as dropped so
        # the caller fails loudly and never marks them seen.
        print(
            f"Warning: claude CLI verification never completed for candidates "
            f"{chunk_label} ({exc}); counting as DROPPED (not verified, not "
            f"marked seen — will resurface on re-run)",
            file=sys.stderr,
        )
        return [], list(chunk), [], 0
    except RuntimeError as exc:
        # Non-timeout transport failure (nonzero exit / empty output / exec
        # error).  BUI-297: bisection can't fix these — retrying smaller chunks
        # just amplifies load on an already-failing dependency (bad auth,
        # rate-limit).  Drop immediately; the circuit breaker + safety net in
        # verify_with_claude turn an all-failing run into a fast, loud exit.
        print(
            f"Warning: claude CLI verification failed for candidates "
            f"{chunk_label} ({exc}); counting as DROPPED (not verified, not "
            f"marked seen — will resurface on re-run)",
            file=sys.stderr,
        )
        return [], list(chunk), [], 0

    parsed = _parse_verification_response(text, chunk, chunk_label)
    if parsed is None:
        # The model responded but the response was unparseable/invalid — no
        # usable verdict, so these candidates were never actually verified.
        # BUI-297: this is a loud DROP, not a silent fail-closed discard, and
        # is distinct from a genuine model rejection (which _parse_verification_
        # response returns as (kept, filtered_info), not None).
        return [], list(chunk), [], 1
    kept, filtered_info = parsed
    return kept, [], filtered_info, 1


def verify_with_claude(matches, *, use_rejected_cache=False, stats=None):
    """Split candidates into genuine matches (`kept`), never-verified ones
    (`dropped`), and model-rejected ones with their reasons (`filtered`),
    using chunked claude CLI calls.  Returns ``(kept, dropped, filtered)``.

    BUI-317: when `stats` is given a dict, it is populated with
    ``{"skipped": N}`` — the count of candidates skipped via the BUI-301
    rejected cache (0 when `use_rejected_cache` is False, or when nothing in
    `matches` happened to be cached). This is an out-parameter rather than a
    4th return value on purpose: `verify_with_claude` has two existing
    callers (`_scan_one_seller` here and `wishlist_sellers.py`, plus dozens
    of tests that unpack the 3-tuple) that don't care about this count —
    widening the tuple would force every one of them to change. `stats`
    defaults to None (opt-in, no-op) so none of that is disturbed.

    Candidates are processed in batches of at most _VERIFY_CHUNK_SIZE per call
    so that a large run never silently truncates the response.  Indices in each
    prompt are 1-based and local to the chunk so the correlation logic is
    identical to the single-call version.

    Failure handling (BUI-270 + BUI-297 + BUI-298):

    - A genuine **model rejection** (parsed response explicitly rejects a
      candidate) is a safe silent-from-the-table drop — surfaced to stderr AND
      returned as `{item_id, title, wish_name, reason}` data in `filtered`
      (BUI-298, so `--json` output can carry it inline), but NOT counted as
      `dropped` (existing BUI-149 behavior: it's a real judgement, not a
      never-verified candidate).
    - A **never-verified** candidate (chunk transport failure that survives
      bisection, or an unparseable response) is returned in `dropped`.  BUI-297:
      the caller must fail loudly and must never mark these seen — folding them
      into the silent model-rejection path is exactly the bug that lost 28 real
      matches on a comics4less scan.
    - A **global** verifier failure fails LOUDLY here: a preflight
      shutil.which("claude") check (CLI not installed) and an
      all-chunks-transport-failed safety net (broken auth / every call timing
      out) both sys.exit(1) rather than return an empty `kept` that reads as
      "this seller has no matching books".

    BUI-301 (`use_rejected_cache`, opt-in): when true, candidates whose
    (item_id, wish_name) pair was model-rejected within the last
    _REJECTED_CACHE_TTL_SEC (see the rejected-candidate cache above) are
    skipped entirely — no CLI call, no chunk, no entry in `kept`/`dropped`/
    `filtered` — so a scan doesn't keep paying Claude CLI cost to re-reject the
    same pair every run. They resurface for verification once the cache entry
    expires. `dropped` (never-verified) candidates are never written to this
    cache, only genuine model rejections. Default OFF so this shared verifier's
    other caller (wishlist_sellers, which has its own permanent title-keyed
    verdict cache) is unaffected — only seller-scan opts in.
    """
    # BUI-317: initialize the out-param once up front so every early-return
    # path below (empty matches, all-skipped) leaves it populated without a
    # per-branch guard; the post-partition assignment overwrites it with the
    # real count on the normal path.
    if stats is not None:
        stats["skipped"] = 0

    if not matches:
        return [], [], []

    # BUI-270 preflight: a missing `claude` binary is a global failure — every
    # chunk would fail and the caller would render an empty table that looks
    # like a genuine no-match.  Fail loudly once with an actionable hint.
    if shutil.which("claude") is None:
        print(
            "Error: the `claude` CLI was not found on PATH. Claude "
            "verification cannot run — refusing to surface unverified matches. "
            "Install and authenticate the claude CLI, then re-run.",
            file=sys.stderr,
        )
        sys.exit(1)

    # BUI-301: skip (listing, wish) pairs the model already rejected recently —
    # see the rejected-candidate cache section above for why this is a separate
    # cache from the genuine-match seen-set, and why the key is the pair, not
    # item_id alone.
    rejected_cache = _load_rejected_cache() if use_rejected_cache else {}
    to_verify = []
    skipped = 0
    for cand in matches:
        item_id = cand.get("item_id")
        wish_name = cand.get("wish_name")
        if (
            item_id is not None
            and wish_name is not None
            and _rejected_cache_key(item_id, wish_name) in rejected_cache
        ):
            skipped += 1
            continue
        to_verify.append(cand)
    if stats is not None:
        # BUI-317: set unconditionally (not just `if skipped`) so a caller
        # reading `stats["skipped"]` after the call always finds a value,
        # including the common zero case.
        stats["skipped"] = skipped
    if skipped:
        print(
            f"  {skipped} candidate(s) skipped (rejected by Claude within the "
            f"last {_REJECTED_CACHE_TTL_SEC // 86400} days; cached — use a "
            f"fresh listing edit or wait for the cache to expire to force "
            f"re-verification)",
            file=sys.stderr,
        )
    if not to_verify:
        # Load-bearing, NOT redundant with the `if not matches` guard above:
        # a run where every candidate was skipped by the rejected cache would
        # otherwise fall through with transport_ok == 0 and wrongly trip the
        # BUI-270 "verifier globally broken" sys.exit(1) safety net below.
        return [], [], []

    # BUI-253 Step 5: render the reject-list bullets that duplicate a
    # deterministic comic_identity lexicon FROM that lexicon, once per call —
    # so the prompt and the code can't silently drift apart (e.g. this used
    # to hand-type "Mexican La Prensa" as the only foreign-edition example,
    # missing every other _FOREIGN_EDITION_MARKERS entry). Computed outside
    # the chunk loop since it's the same text for every chunk.
    prompt_ctx = (
        ", ".join(EDITION_LABELS),
        foreign_edition_examples(),
        later_printing_examples(),
    )

    kept: list = []
    dropped: list = []
    filtered: list = []
    # BUI-270 safety-net counter: a successful transport call (model actually
    # returned text, regardless of whether we could parse it) proves the
    # verifier is reachable.  If EVERY call fails at the transport layer, the
    # verifier is globally broken (bad auth / all timeouts) — an environment
    # failure, not a genuine "everything rejected" — so we hard-fail below
    # rather than return an empty list that looks like no-match.
    transport_ok = 0
    for chunk_start in range(0, len(to_verify), _VERIFY_CHUNK_SIZE):
        chunk = to_verify[chunk_start : chunk_start + _VERIFY_CHUNK_SIZE]
        k, d, f, ok = _verify_chunk(chunk, chunk_start, prompt_ctx)
        kept.extend(k)
        dropped.extend(d)
        filtered.extend(f)
        transport_ok += ok
        # BUI-297 circuit breaker: if an entire chunk — including every bisected
        # retry — produced zero successful model calls, the verifier is globally
        # broken.  Stop now instead of repeating the (depth-bounded) bisection
        # fan-out for every remaining chunk; the safety net below then hard-fails
        # loudly.  A healthy run clears this after chunk 1.
        if transport_ok == 0:
            break

    # BUI-270 safety net: at least one chunk existed (matches is non-empty) but
    # not a single transport call succeeded → the verifier is globally broken
    # (e.g. subscription auth failed, or every call timed out).  Fail loudly
    # rather than return empty lists the caller would render as "no matches".
    if transport_ok == 0:
        print(
            "Error: claude CLI verification failed for every candidate chunk "
            "(no successful model call). The verifier appears globally "
            "unavailable — likely broken subscription auth or repeated "
            "timeouts. Refusing to surface unverified matches; check `claude` "
            "auth and re-run.",
            file=sys.stderr,
        )
        sys.exit(1)

    # BUI-301: remember this run's genuine model rejections (by (item_id,
    # wish_name) pair) so the next scan can skip re-verifying them until the TTL
    # expires. `dropped` (never-verified candidates) is deliberately excluded —
    # those must resurface on the very next run, not be treated as a rejection.
    if use_rejected_cache and filtered:
        now_iso = datetime.now(timezone.utc).isoformat()
        new_entries: dict[str, str] = {}
        for f in filtered:
            item_id = f.get("item_id")
            wish_name = f.get("wish_name")
            if item_id is not None and wish_name is not None:
                new_entries[_rejected_cache_key(item_id, wish_name)] = now_iso
        # BUI-307: merge under the lock (re-reading on-disk state) instead of
        # writing back this call's start-of-run `rejected_cache` snapshot — a
        # concurrent worker may have added entries since we loaded, and a plain
        # overwrite of the whole snapshot would drop them.
        _record_rejections(new_entries)

    return kept, dropped, filtered


# ─── Output ───────────────────────────────────────────────────────────────────

def _strip_private(rows):
    """Strip private pipeline fields (keys starting with `_`, e.g.
    `_series_name`/`_release_year` — BUI-245) from a list of candidate dicts.

    Those fields exist only to feed verify_with_claude's "Correct series:"
    era hint and were never meant to reach the user-facing table/JSON output.
    """
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]


def _trunc(text, width):
    if not text:
        return ""
    return text if len(text) <= width else text[: width - 1] + "…"


def print_matches(matches, seller_label=None):
    """Print match results as a human-readable table.

    `seller_label`, when given, prints a `=== <label> ===` header first
    (BUI-298: distinguishes sellers in a multi-seller run's output).
    """
    if seller_label is not None:
        print(f"\n=== {seller_label} ===")
    if not matches:
        print("No matches found.")
        return

    cols = [
        ("Listing Title", "title", 40),
        ("Wish List Item", "wish_name", 28),
        ("Price", "current_price", 9),
        ("Ends", "end_date", 12),
        ("URL", "listing_url", 45),
    ]

    header = "  ".join(h.ljust(w) for h, _, w in cols)
    print(header)
    print("-" * len(header))

    for m in matches:
        row = "  ".join(
            _trunc(str(m.get(k) or ""), w).ljust(w)
            for _, k, w in cols
        )
        print(row)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _seller_result(seller, username, *, matches=None, dropped=None,
                   filtered=None, skipped=0, error=None, crashed=False):
    """Build one seller's result slot for the --json `sellers` array.

    BUI-298: a single factory for the per-seller shape so its keys are defined
    in one place — the fetch-error, unknown-seller, and success paths all go
    through here rather than hand-building three drift-prone literals.
    `incomplete` is derived (true iff there are dropped/never-verified
    candidates); `matches`/`dropped`/`filtered` already have their private
    pipeline fields stripped by the caller.

    BUI-317: `skipped` is the count of candidates the BUI-301 rejected cache
    skipped entirely (no CLI call) — surfaced as `skipped_cached_candidates`
    so an unattended caller can see cache coverage (how much verification
    cost this scan avoided) without scraping stderr.

    BUI-324: `crashed` marks a slot whose `error` came from an unexpected
    worker exception (the _scan_one_seller crash-isolation wrapper), not a
    normal resolvable failure (unknown seller / listing fetch). It drives
    main()'s distinct `_EXIT_SELLER_CRASHED` exit code, and is surfaced here
    too (not just via the exit code) so a `--json` caller inspecting a
    specific seller's slot doesn't have to pattern-match its `error` string
    to tell the two apart.
    """
    dropped = dropped or []
    return {
        "seller": seller,
        "username": username,
        "matches": matches or [],
        "dropped_candidates": dropped,
        "filtered": filtered or [],
        "skipped_cached_candidates": skipped,
        "incomplete": bool(dropped),
        "error": error,
        "crashed": crashed,
    }


def _scan_one_seller(seller_arg, username, token, base_url, wish_items,
                    max_results, show_all):
    """Scan one seller — crash-isolating wrapper around `_scan_one_seller_impl`.

    BUI-319: this runs as a ThreadPoolExecutor worker (see main()). Before
    this wrapper existed, any non-SystemExit exception raised inside the
    impl (a bug in matching, a malformed listing, anything unanticipated)
    propagated through `future.result()` in main()'s as_completed loop
    uncaught, crashing the entire multi-seller batch after
    `shutdown(wait=True)` — the sequential loop had the same crash-before-
    print gap (not a regression), but concurrency widens the blast radius
    from "this seller" to "every seller in the batch". Catching Exception
    here turns a crash into an ordinary per-seller `error` result instead,
    matching how a listing-fetch failure is already handled inside the impl.

    SystemExit is deliberately NOT caught (it isn't an Exception subclass) —
    verify_with_claude's global-verifier-down guard relies on it propagating
    so main()'s `except SystemExit` branch can trip the drain-then-cancel-
    the-tail path (BUI-307) rather than being masked as an ordinary
    per-seller error.
    """
    try:
        return _scan_one_seller_impl(
            seller_arg, username, token, base_url, wish_items,
            max_results, show_all,
        )
    except Exception as e:  # noqa: BLE001 — BUI-319: isolate a per-seller crash
        print(
            f"Error: seller '{username}' crashed during scan ({e!r}); "
            "isolating it — the rest of this batch continues",
            file=sys.stderr,
        )
        # BUI-319: use repr (`{e!r}`) not str for the structured `error` field —
        # an unexpected crash's exception TYPE (e.g. KeyError vs ValueError) is
        # load-bearing triage signal for an unattended --json caller deciding
        # whether to retry or file a bug, and str() drops it. This deliberately
        # differs from the listing-fetch branch's `{e}` (a known RequestException
        # where the type adds nothing) — here the type is the whole point.
        return _seller_result(
            seller_arg, username, error=f"seller scan crashed: {e!r}",
            crashed=True,
        )


def _scan_one_seller_impl(seller_arg, username, token, base_url, wish_items,
                    max_results, show_all):
    """Scan one seller's active listings against the (already-fetched) wish
    list and verify candidates with Claude. Returns a per-seller result dict:

        {"seller", "username", "matches", "dropped_candidates", "filtered",
         "skipped_cached_candidates", "incomplete", "error"}

    BUI-298: this is the per-seller body of what used to be all of main() —
    extracted so main() can fetch the wish list + OAuth token ONCE and loop
    this function over N sellers. A listing-fetch transport failure for THIS
    seller is caught here (not left to propagate) so one bad seller in a
    multi-seller batch can't abort the rest — it's recorded in `error` and
    the batch continues. Alias resolution happens in the caller (main), since
    an unresolvable seller never has a `username` to reach this function with.

    BUI-319: any OTHER exception raised in this body (not just the listing-
    fetch RequestException handled below) is caught one level up, by the
    `_scan_one_seller` wrapper — this function itself stays free of a
    catch-all so its existing, more specific error handling (e.g. the
    listing-fetch branch immediately below) still produces the most useful
    message before the generic safety net would ever see it.
    """
    print(f"Fetching listings for seller '{username}'...", file=sys.stderr)
    try:
        raw_listings = search_seller_listings(
            username, token, base_url, max_results=max_results
        )
    except requests.exceptions.RequestException as e:
        print(
            f"Error fetching listings for seller '{username}': {e}",
            file=sys.stderr,
        )
        return _seller_result(
            seller_arg, username, error=f"listing fetch failed: {e}"
        )
    print(f"  {len(raw_listings)} listings fetched", file=sys.stderr)

    # Match
    seen_ids = set()
    candidates = []
    for raw in raw_listings:
        listing = parse_item_summary(raw)
        # BUI-184: defense in depth — parse_item_summary already coerces null
        # titles to "" via `or ""`, but skip explicitly here too so that a
        # title-less listing never reaches .lower() or match_listing at all.
        title = listing.get("title")
        if not title:
            continue
        if "cgc" in title.lower():
            continue
        if listing["item_id"] in seen_ids:
            continue
        seen_ids.add(listing["item_id"])
        wish, score = match_listing(title, wish_items)
        if not wish:
            continue
        # BUI-245: run the same deterministic reject chain wishlist_sellers uses
        # (hard_reject, era_mismatch, reprint/digital/trading-card/foreign-edition/
        # second-print) against the wish item match_listing resolved, so a
        # cross-series false positive (e.g. "Spectacular Spider-Man #15" vs wish
        # "The Amazing Spider-Man #15") is dropped here rather than reaching Haiku
        # with no era hint.
        if should_reject(
            title, wish["series"], wish["issue"],
            wish.get("_series_name"), wish.get("_release_year"),
        ):
            continue
        candidates.append({
            **listing,
            "wish_id": wish["id"],
            "wish_name": wish["name"],
            "match_score": round(score, 2),
            # Private fields (BUI-245): carry the decorated series name so
            # verify_with_claude's "Correct series:" era hint activates for this
            # candidate too. Stripped before output — see _strip_private below.
            "_series_name": wish.get("_series_name"),
            "_release_year": wish.get("_release_year"),
        })

    # BUI-113: drop matches already surfaced in a prior scan (default). --all
    # skips the filter (and its server fetch); the short-circuit on an empty
    # candidate list skips the fetch/Claude/record entirely. Seen-tracking is
    # keyed by THIS seller's username — never merged across sellers in a batch.
    if candidates and not show_all:
        seen = fetch_seen_item_ids(username)
        before = len(candidates)
        candidates = [c for c in candidates if c["item_id"] not in seen]
        hidden = before - len(candidates)
        if hidden:
            print(
                f"  {hidden} already-seen match(es) hidden (use --all to show)",
                file=sys.stderr,
            )

    if candidates:
        print(f"  {len(candidates)} candidate(s) — verifying with Claude...", file=sys.stderr)
        # BUI-301: seller-scan opts into the rejected-candidate cache so a
        # (listing, wish) pair Claude already rejected isn't re-verified (re-
        # paying CLI cost) every run until the TTL expires. BUI-317: --all
        # ("show me everything again") also bypasses this cache — a full
        # force-re-verify for "recheck this seller, I think that rejection
        # was wrong" — rather than leaving no way to override a stale/bad
        # rejection short of waiting out the 14-day TTL.
        verify_stats = {}
        matches, dropped, filtered = verify_with_claude(
            candidates, use_rejected_cache=not show_all, stats=verify_stats
        )
        skipped = verify_stats.get("skipped", 0)
    else:
        matches, dropped, filtered = [], [], []
        skipped = 0

    # BUI-297: `dropped` are candidates the verifier never reached a verdict on
    # (timeout / transport failure surviving bisection, or an unparseable
    # response) — semantically distinct from a genuine model rejection.  These
    # MUST be reported loudly (they are NOT "0 genuine matches"), so the banner
    # below distinguishes a clean verified-empty result from an INCOMPLETE run.
    if dropped:
        print(
            f"  INCOMPLETE: {len(dropped)} candidate(s) for '{username}' were "
            f"NEVER verified (claude CLI timeout/transport failure). They are "
            f"NOT recorded as seen and WILL resurface on re-run — re-run to "
            f"verify them.",
            file=sys.stderr,
        )
    print(f"  {len(matches)} genuine match(es) verified", file=sys.stderr)

    # BUI-319: build the result slot BEFORE recording the seen-set, then return
    # the already-built object after. record_items_seen commits an irreversible
    # server-side "these item_ids were surfaced" mark; if the result-building
    # (_strip_private / _seller_result) ran AFTER it and crashed, the crash-
    # isolation wrapper would convert that into an error slot with matches=[]
    # while the seen-set stayed committed — the item_ids would be hidden on the
    # re-run, silently losing a genuine match (the BUI-297 lost-match class).
    # Constructing the result first means nothing crashable runs after the seen
    # write, so a poisoned-seen-but-dropped-matches window can't exist.
    result = _seller_result(
        seller_arg, username,
        matches=_strip_private(matches),
        dropped=_strip_private(dropped),
        filtered=filtered,
        skipped=skipped,
    )

    # BUI-113 / BUI-297 / BUI-298 INVARIANT: only *kept* matches for THIS
    # seller are ever recorded as seen, keyed by THIS seller's username.
    # `dropped` (never-verified) candidates are deliberately NEVER passed here
    # — marking them seen would suppress them on the next scan and silently
    # lose a real wish-list match (the BUI-297 bug). This must also never be
    # merged across sellers in a batch — each seller's seen-set is independent.
    # Runs under --all too — --all means "show me everything again", not "forget".
    if matches:
        record_items_seen([m["item_id"] for m in matches], username)

    return result


def _version_string() -> str:
    """BUI-314: staleness signal for a `uv tool install`ed binary.

    `_ebay_build_stamp` is generated at build time by hatch_build.py from the
    git HEAD of the source tree the wheel was built from; it's absent when
    running from an unbuilt checkout (e.g. `uv run` here in tests), so fall
    back to "unknown" rather than failing.
    """
    try:
        pkg_version = importlib.metadata.version("ebay-tools")
    except importlib.metadata.PackageNotFoundError:
        pkg_version = "unknown"
    try:
        from _ebay_build_stamp import GIT_DATE, GIT_SHA
    except ImportError:
        GIT_SHA, GIT_DATE = "unknown", "unknown"
    return f"seller-scan {pkg_version} (git {GIT_SHA}, {GIT_DATE})"


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="seller-scan",
        description="Match one or more eBay sellers' listings against your LOCG wish list.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=_version_string(),
        help="Print the installed version and the git SHA/date it was built "
             "from, then exit. Use this to check for a stale `uv tool install` "
             "(see scripts/install.sh).",
    )
    parser.add_argument(
        "sellers",
        nargs="+",
        help="One or more eBay store names (resolved via your alias map), "
             "/usr/ or _ssn= URLs, or raw login usernames (with --username). "
             "BUI-298: passing multiple sellers fetches the wish list + OAuth "
             "token ONCE and scans them in a single internal loop — always "
             "prefer this over invoking seller-scan once per seller.",
    )
    parser.add_argument(
        "--username",
        default=None,
        help="eBay login username to scan directly, bypassing the alias map "
             "(for a one-off seller not yet in your aliases). Single-seller only.",
    )
    parser.add_argument(
        "--add-alias",
        default=None,
        metavar="USERNAME",
        help="Register the given login USERNAME for the store name in the "
             "positional arg, persist it, then scan. Single-seller only.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as a single JSON object (BUI-298: always an "
             "object — never a bare array — with a per-seller breakdown; "
             "see main()'s docstring / the skill doc for the shape)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="show_all",
        help="Show every wish-list match, including ones surfaced in a prior "
             "scan. By default (BUI-113) already-seen matches are hidden. "
             "Newly-surfaced matches are recorded as seen either way. BUI-317: "
             "also force-re-verifies every candidate by bypassing the BUI-301 "
             "rejected-candidate cache, for 'recheck this seller, I think "
             "that rejection was wrong' — normal runs skip re-verifying a "
             "pair Claude already rejected within the last 14 days.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=1000,
        help="Maximum listings to fetch per seller (default: 1000)",
    )
    parser.add_argument(
        "--env",
        choices=["production", "sandbox"],
        default=None,
        help="eBay environment (overrides config)",
    )
    args = parser.parse_args(argv)

    # BUI-298: --username/--add-alias are single-seller conveniences (override
    # or register ONE alias) — ambiguous across a multi-seller batch, so
    # refuse rather than silently applying either to only the first seller.
    if (args.username or args.add_alias) and len(args.sellers) > 1:
        parser.error(
            "--username/--add-alias apply to exactly one seller (applying "
            "them across a multi-seller batch would be ambiguous) — pass a "
            "single seller when using them"
        )

    # BUI-298: persist a new --add-alias BEFORE loading the alias map, so the
    # map read below already contains it and resolve_seller_username finds it
    # (guaranteed single-seller by the guard above — args.sellers[0] is the
    # only seller). Regression guard: BUI-298 hoisted load_seller_aliases()
    # out of the per-seller loop; if the save stayed inside the loop, resolve
    # would run against the stale pre-save map and false-fail as "unknown
    # seller" even though the alias was written to disk.
    if args.add_alias:
        save_seller_alias(args.sellers[0], args.add_alias)
        print(
            f"Registered alias '{args.sellers[0].strip().lower()}' → '{args.add_alias.strip()}'",
            file=sys.stderr,
        )

    aliases = load_seller_aliases()

    # Auth — fetched ONCE for the whole batch (BUI-298; previously each
    # single-seller invocation re-fetched its own token).
    client_id, client_secret, base_url = load_config()
    if args.env:
        from ebay_fetch import PRODUCTION_BASE, SANDBOX_BASE
        base_url = PRODUCTION_BASE if args.env == "production" else SANDBOX_BASE
    token = get_token(client_id, client_secret, base_url)

    # Wish list — fetched + prepped ONCE for the whole batch (BUI-298;
    # previously each single-seller invocation re-fetched the full ~815-item
    # wish list — 9x redundant HTTP for a 9-seller scan).
    print("Fetching LOCG wish list...", file=sys.stderr)
    wish_list = fetch_wish_list()
    wish_items = prepare_wish_items(wish_list)
    print(f"  {len(wish_items)} matchable wish list items", file=sys.stderr)

    # BUI-307: aggregate into per-seller slots indexed by input position, so the
    # final `results` list is in args.sellers order REGARDLESS of the (now
    # concurrent) completion order — output stays deterministic and identical to
    # the old sequential loop's ordering. A slot left None (a seller that was
    # never collected — e.g. cancelled after a global verifier failure) is
    # dropped from `results` at the end.
    slots: list = [None] * len(args.sellers)

    # Resolve every seller FIRST (cheap, local alias-map lookup — no API), in
    # the main thread. An unresolvable seller fills its slot immediately and is
    # never submitted to the pool. A store name is NOT a username and would
    # silently scan every seller (BUI-68); BUI-298: an unresolvable seller does
    # NOT abort the batch — its slot records the error and the batch continues.
    # (Any --add-alias was already persisted + folded into `aliases` above,
    # before the map was loaded — single-seller only.)
    resolvable = []  # (idx, seller_arg, username)
    for idx, seller_arg in enumerate(args.sellers):
        try:
            username = resolve_seller_username(
                seller_arg, aliases, username_override=args.username
            )
        except UnknownSellerError as e:
            print(
                f"Error: unknown seller '{e.store}'. A store name is not an eBay "
                "login username, so the scan can't run safely.\n"
                "  Find the username: open one of the seller's listings, click "
                "'See other items', and copy the _ssn= value from the URL.\n"
                f"  Then either:\n"
                f"    seller-scan {e.store} --add-alias <username>   (saves it for next time)\n"
                f"    seller-scan {e.store} --username <username>     (one-off)",
                file=sys.stderr,
            )
            slots[idx] = _seller_result(
                seller_arg, None, error=f"unknown seller '{e.store}'"
            )
            continue
        resolvable.append((idx, seller_arg, username))

    # BUI-307: scan resolvable sellers CONCURRENTLY, bounded to
    # _SELLER_SCAN_MAX_WORKERS. The old loop ran sellers strictly sequentially,
    # so one stuck 180s verify timeout blocked every seller behind it; a bounded
    # pool lets healthy sellers finish while one hangs, and caps eBay Browse API
    # concurrency at the worker count. Each seller's chunked verification still
    # runs sequentially inside its own _scan_one_seller (BUI-297 breaker intact).
    #
    # BUI-298 reliability, preserved under BUI-307 concurrency: a GLOBAL verifier
    # failure (missing claude CLI / every chunk failed transport) makes
    # verify_with_claude sys.exit(1), which a worker thread turns into the
    # future's exception, re-raised here by future.result(). On the first such
    # failure we record that seller's error slot and CANCEL the not-yet-started
    # tail (a global failure hits every remaining seller identically, so there's
    # no point fanning it out) — but we do NOT stop draining `as_completed`.
    #
    # This is load-bearing: a seller that already finished (or is mid-flight)
    # recorded its genuine matches as *seen* (record_items_seen) before returning.
    # If we broke out here without collecting its slot, main() would print
    # nothing for it, yet the re-run that _EXIT_VERIFIER_DOWN prompts would find
    # those item_ids already seen and hide them — silently losing a real
    # wish-list match (the exact BUI-297 lost-match class). So we keep collecting
    # every future that actually ran; only the cancelled (never-started) tail is
    # skipped — those recorded nothing, so their empty slots drop out cleanly.
    verifier_down = False
    with ThreadPoolExecutor(max_workers=_SELLER_SCAN_MAX_WORKERS) as executor:
        future_to_meta = {
            executor.submit(
                _scan_one_seller, seller_arg, username, token, base_url,
                wish_items, args.max_results, args.show_all,
            ): (idx, seller_arg, username)
            for idx, seller_arg, username in resolvable
        }
        for future in as_completed(future_to_meta):
            idx, seller_arg, username = future_to_meta[future]
            try:
                slots[idx] = future.result()
            except CancelledError:
                # A not-yet-started seller we cancelled after a global verifier
                # failure below — it was never scanned and recorded nothing, so
                # leave its slot None (dropped from `results`).
                continue
            except SystemExit as e:
                # verify_with_claude's global-failure guard (the ONLY sys.exit in
                # this call path) fired — the verifier is down for every seller.
                verifier_down = True
                slots[idx] = _seller_result(
                    seller_arg, username,
                    error=f"claude verifier globally unavailable (exit {e.code}) — "
                          "re-run the batch once it is reachable",
                )
                # Cancel only the not-yet-started sellers; already-running ones
                # keep going and are still collected on later as_completed turns.
                for f in future_to_meta:
                    f.cancel()

    results = [s for s in slots if s is not None]

    any_incomplete = any(r["incomplete"] for r in results)
    any_error = any(r["error"] for r in results)
    # BUI-324: a crashed slot's `error` is also truthy (so any_error above and
    # the non-json "Error: ..." printing below stay unchanged), but the exit
    # code below checks this separately so a worker crash gets its own
    # _EXIT_SELLER_CRASHED instead of the generic _EXIT_SELLER_ERROR.
    any_crashed = any(r.get("crashed") for r in results)

    if args.json_output:
        # BUI-298 (fold-in A): --json is ALWAYS a top-level object — never a
        # bare array — so a caller can branch exit-code-first and then drill
        # into a stable shape regardless of clean/dirty run. This replaces the
        # BUI-297 polymorphic shape (bare array on clean, object only when
        # something was dropped), which existed only because that PR couldn't
        # also update the skill's parser — BUI-298 does, so the ambiguity is
        # gone. `incomplete` is true iff ANY seller's slot is incomplete.
        print(json.dumps({
            "incomplete": any_incomplete,
            "sellers": results,
        }, indent=2))
    else:
        multi = len(results) > 1
        for r in results:
            if r["error"]:
                if multi:
                    print(f"\n=== {r['seller']} ===")
                print(f"Error: {r['error']}")
                continue
            print_matches(
                r["matches"],
                seller_label=(f"{r['seller']} ({r['username']})" if multi else None),
            )

    # BUI-298/BUI-324 exit-code priority (see the exit-constant comments above):
    # verifier-down (1) > incomplete (3) > crashed (4) > seller-error (2) > clean (0).
    if verifier_down:
        return _EXIT_VERIFIER_DOWN
    if any_incomplete:
        return _EXIT_INCOMPLETE
    if any_crashed:
        return _EXIT_SELLER_CRASHED
    if any_error:
        return _EXIT_SELLER_ERROR
    return 0


if __name__ == "__main__":
    sys.exit(main())
