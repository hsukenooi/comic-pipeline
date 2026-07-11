#!/usr/bin/env python3
"""seller-scan: Match an eBay seller's active listings against your LOCG wish list."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

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
    """Fetch the wish list from the gixen server API. Returns a list of
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

    Returns the subset of `chunk` NOT rejected (the list to extend `kept`
    with), or None if the response could not be parsed/validated.  None means
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
    # stderr so the user can override if the verifier was wrong.
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

    return [cand for idx, cand in enumerate(chunk, 1) if idx not in rejected_ids]


def _verify_chunk(chunk, base_index, prompt_ctx, depth=0):
    """Verify one chunk. Returns ``(kept, dropped, transport_ok)``.

    `base_index` is the 0-based global offset of this chunk within `matches`,
    used only to render the human-readable 1-based candidate range in warnings
    (so a bisected sub-chunk still reports its true global position).  `depth`
    is the bisection recursion depth, capped at `_VERIFY_MAX_BISECT_DEPTH`.

    The three return components map to the three per-candidate outcomes
    (BUI-297 — the crux of this ticket):

    - **kept**: a successfully-parsed model response did NOT reject it.
    - **model-rejected** (silent, safe — the existing BUI-149 behavior): a
      successfully-parsed response explicitly rejected it, with the reason
      surfaced to stderr.  These land in neither return list beyond being
      excluded from `kept` — the model made a real judgement, so re-surfacing
      them on the next run would be noise.
    - **dropped** (loud — the never-verified case this ticket exists to fix): a
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
            lk, ld, lok = _verify_chunk(
                chunk[:mid], base_index, prompt_ctx, depth + 1
            )
            rk, rd, rok = _verify_chunk(
                chunk[mid:], base_index + mid, prompt_ctx, depth + 1
            )
            return lk + rk, ld + rd, lok + rok
        # Floor / max depth reached and it still times out.  These are "never
        # verified" candidates, NOT model rejections — count them as dropped so
        # the caller fails loudly and never marks them seen.
        print(
            f"Warning: claude CLI verification never completed for candidates "
            f"{chunk_label} ({exc}); counting as DROPPED (not verified, not "
            f"marked seen — will resurface on re-run)",
            file=sys.stderr,
        )
        return [], list(chunk), 0
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
        return [], list(chunk), 0

    parsed = _parse_verification_response(text, chunk, chunk_label)
    if parsed is None:
        # The model responded but the response was unparseable/invalid — no
        # usable verdict, so these candidates were never actually verified.
        # BUI-297: this is a loud DROP, not a silent fail-closed discard, and
        # is distinct from a genuine model rejection (which _parse_verification_
        # response returns as the kept subset, not None).
        return [], list(chunk), 1
    return parsed, [], 1


def verify_with_claude(matches):
    """Split candidates into genuine matches (`kept`) and never-verified ones
    (`dropped`), using chunked claude CLI calls.  Returns ``(kept, dropped)``.

    Candidates are processed in batches of at most _VERIFY_CHUNK_SIZE per call
    so that a large run never silently truncates the response.  Indices in each
    prompt are 1-based and local to the chunk so the correlation logic is
    identical to the single-call version.

    Failure handling (BUI-270 + BUI-297):

    - A genuine **model rejection** (parsed response explicitly rejects a
      candidate) is a safe silent drop — surfaced to stderr, excluded from the
      output, and NOT counted as dropped (existing BUI-149 behavior).
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
    """
    if not matches:
        return [], []

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
    # BUI-270 safety-net counter: a successful transport call (model actually
    # returned text, regardless of whether we could parse it) proves the
    # verifier is reachable.  If EVERY call fails at the transport layer, the
    # verifier is globally broken (bad auth / all timeouts) — an environment
    # failure, not a genuine "everything rejected" — so we hard-fail below
    # rather than return an empty list that looks like no-match.
    transport_ok = 0
    for chunk_start in range(0, len(matches), _VERIFY_CHUNK_SIZE):
        chunk = matches[chunk_start : chunk_start + _VERIFY_CHUNK_SIZE]
        k, d, ok = _verify_chunk(chunk, chunk_start, prompt_ctx)
        kept.extend(k)
        dropped.extend(d)
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

    return kept, dropped


# ─── Output ───────────────────────────────────────────────────────────────────

def _trunc(text, width):
    if not text:
        return ""
    return text if len(text) <= width else text[: width - 1] + "…"


def print_matches(matches):
    """Print match results as a human-readable table."""
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

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="seller-scan",
        description="Match an eBay seller's listings against your LOCG wish list.",
    )
    parser.add_argument(
        "seller",
        help="eBay store name (resolved via your alias map), a /usr/ or _ssn= URL, "
             "or use --username for a raw login username",
    )
    parser.add_argument(
        "--username",
        default=None,
        help="eBay login username to scan directly, bypassing the alias map "
             "(for a one-off seller not yet in your aliases)",
    )
    parser.add_argument(
        "--add-alias",
        default=None,
        metavar="USERNAME",
        help="Register the given login USERNAME for the store name in the "
             "positional arg, persist it, then scan",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output matches as JSON array",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="show_all",
        help="Show every wish-list match, including ones surfaced in a prior "
             "scan. By default (BUI-113) already-seen matches are hidden. "
             "Newly-surfaced matches are recorded as seen either way.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=1000,
        help="Maximum listings to fetch from seller (default: 1000)",
    )
    parser.add_argument(
        "--env",
        choices=["production", "sandbox"],
        default=None,
        help="eBay environment (overrides config)",
    )
    args = parser.parse_args(argv)

    # Resolve the store name to an eBay login username before anything else —
    # a store name is NOT a username and would silently scan every seller (BUI-68).
    if args.add_alias:
        save_seller_alias(args.seller, args.add_alias)
        print(
            f"Registered alias '{args.seller.strip().lower()}' → '{args.add_alias.strip()}'",
            file=sys.stderr,
        )
    aliases = load_seller_aliases()
    try:
        username = resolve_seller_username(
            args.seller, aliases, username_override=args.username
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
        return 2

    # Auth
    client_id, client_secret, base_url = load_config()
    if args.env:
        from ebay_fetch import PRODUCTION_BASE, SANDBOX_BASE
        base_url = PRODUCTION_BASE if args.env == "production" else SANDBOX_BASE
    token = get_token(client_id, client_secret, base_url)

    # Fetch wish list
    print("Fetching LOCG wish list...", file=sys.stderr)
    wish_list = fetch_wish_list()
    wish_items = prepare_wish_items(wish_list)
    print(f"  {len(wish_items)} matchable wish list items", file=sys.stderr)

    # Fetch seller listings
    print(f"Fetching listings for seller '{username}'...", file=sys.stderr)
    raw_listings = search_seller_listings(
        username, token, base_url, max_results=args.max_results
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
            # candidate too. Stripped before output — see the json_output block.
            "_series_name": wish.get("_series_name"),
            "_release_year": wish.get("_release_year"),
        })

    # BUI-113: drop matches already surfaced in a prior scan (default). --all
    # skips the filter (and its server fetch); the short-circuit on an empty
    # candidate list skips the fetch/Claude/record entirely.
    if candidates and not args.show_all:
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
        matches, dropped = verify_with_claude(candidates)
    else:
        matches, dropped = [], []

    # BUI-297: `dropped` are candidates the verifier never reached a verdict on
    # (timeout / transport failure surviving bisection, or an unparseable
    # response) — semantically distinct from a genuine model rejection.  These
    # MUST be reported loudly (they are NOT "0 genuine matches"), so the banner
    # below distinguishes a clean verified-empty result from an INCOMPLETE run.
    if dropped:
        print(
            f"  INCOMPLETE: {len(dropped)} candidate(s) were NEVER verified "
            f"(claude CLI timeout/transport failure). They are NOT recorded as "
            f"seen and WILL resurface on re-run — re-run to verify them.",
            file=sys.stderr,
        )
    print(f"  {len(matches)} genuine match(es) verified", file=sys.stderr)

    # BUI-245: strip private pipeline fields (_series_name, _release_year) —
    # they exist only to feed verify_with_claude's era hint and were never
    # meant to reach the user-facing table/JSON output.
    def _strip_private(rows):
        return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]

    matches = _strip_private(matches)
    dropped = _strip_private(dropped)

    if args.json_output:
        # BUI-297: the historical --json shape is a bare array of matches, and
        # the /comic:seller-scan skill parses it as such — so keep that shape
        # on a clean run for backward compatibility.  Only when candidates were
        # dropped (the loud, exceptional path, which also exits non-zero) do we
        # switch to an object carrying the `dropped_candidates` field, so a
        # consumer can see exactly what still needs verifying.
        if dropped:
            print(json.dumps(
                {"matches": matches, "dropped_candidates": dropped}, indent=2
            ))
        else:
            print(json.dumps(matches, indent=2))
    else:
        print_matches(matches)

    # BUI-113 / BUI-297 INVARIANT: only *kept* (genuinely verified) matches are
    # ever recorded as seen.  `dropped` candidates (never verified) are
    # deliberately NOT passed here — marking them seen would suppress them on
    # the next scan and silently lose a real wish-list match (the BUI-297 bug).
    # Do not add `dropped` to this call.
    # Runs under --all too — --all means "show me everything again", not "forget".
    if matches:
        record_items_seen([m["item_id"] for m in matches], username)

    # BUI-297: a distinct non-zero exit code signals an incomplete verification
    # run (never-verified candidates dropped) so callers/skills can tell it
    # apart from a clean "0 matches" scan (exit 0).
    return _EXIT_INCOMPLETE if dropped else 0


if __name__ == "__main__":
    sys.exit(main())
