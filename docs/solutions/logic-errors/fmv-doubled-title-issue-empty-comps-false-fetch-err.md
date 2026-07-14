---
title: "FMV comp query returns 0 results, misdiagnosed as fetch-err (doubled title/issue)"
date: 2026-07-14
category: docs/solutions/logic-errors/
module: comic-fmv (apps/fmv) + ebay-sold-comps (apps/ebay)
problem_type: logic_error
component: service_object
severity: high
symptoms:
  - "comic-fmv's comp query returns 0 results on every tier for a valid, actively-traded issue (ASM #50, 1st Kingpin)"
  - "the run surfaces the empty result as fetch-err, implying a SerpApi quota or outage"
  - "operator chases the SerpApi key and quota instead of the query itself, even though both are fine"
  - 'the resolved SerpApi _nkw is a doubled quoted phrase, e.g. "The Amazing Spider-Man #50 50"'
root_cause: logic_error
resolution_type: code_fix
tags: [fmv, sold-comps, build-query, fetch-err, title-normalization, bui-346]
related_components:
  - "ebay-sold-comps"
  - "comic-fmv"
  - "comic:buy"
related_issues: [BUI-346]
related_docs:
  - docs/solutions/best-practices/fmv-grade-curve-interpolation-overbid-guards.md
  - docs/solutions/best-practices/fmv-7a-cgc-proxy-not-safely-automatable.md
---

# FMV comp query returns 0 results, misdiagnosed as fetch-err (doubled title/issue)

## Problem

In the `/comic:buy → comic-fmv` handoff, a working-list `title` carrying a leading article and/or an embedded `#<issue>` is passed through verbatim alongside a separate `issue` field. `build_query` in `apps/ebay/src/sold_comps.py` appends the `issue` on top of the title, producing a **doubled quoted phrase** (`"The Amazing Spider-Man #50 50"`) that matches no real eBay listing — so every query tier returns 0 comps. The empty result was then read by the operator as a SerpApi quota/outage, sending the run chasing the API key instead of the query.

## Symptoms

- `comic-fmv` returns 0 comps on the base query, the broadened query, **and** the grade-targeted query — for a book (ASM #50, 1967) with abundant real sold history.
- The all-tiers-empty pattern looks identical to a SerpApi outage or quota exhaustion, prompting an API-key/quota check — both of which are fine.
- The literal `_nkw` string sent to eBay is the tell: a doubled/duplicated issue number inside the quoted phrase, e.g. `"The Amazing Spider-Man #50 50"`.

## What Didn't Work

- **Chasing the SerpApi API key** — the key was valid and unaffected.
- **Assuming SerpApi quota exhaustion** — quota was not the issue.
- **Treating "0 results on every tier" as synonymous with "fetch/outage error"** without first inspecting the literal query string. The diagnosis jumped to "the fetch must be failing" rather than "the query itself might be malformed."

## Solution

Two coordinated fixes landed together (BUI-346, PR #182, merged to `main`):

1. **Normalize at the `fmv_runner` handoff chokepoint** (`apps/fmv/src/fmv_runner.py`) — added `_strip_leading_article`, `_strip_embedded_issue`, `_normalize_book_title`, applied to every book in `run()` immediately after `_read_batch`, before any DB-cache lookup, the `ebay-sold-comps` subprocess, or the DB upsert. This is the one deterministic chokepoint every working list passes through — the `buy.md`/`fmv.md` working-list construction upstream is agent-driven prose, not code, so it can't be trusted to self-normalize. `_normalize_book_title` fails open (no-op) when `title` or `issue` is missing.

2. **Defense-in-depth inside `build_query()`** (`apps/ebay/src/sold_comps.py`) — the same helpers duplicated to strip the article and dedupe the embedded issue before assembling the quoted phrase:

   ```python
   if title:  # truthy-guard keeps a None/empty title byte-for-byte identical to old behavior
       title = _strip_embedded_issue(_strip_leading_article(title), issue)
   parts = [f'"{title} {issue}"']
   ```

   Duplicated rather than shared because `apps/ebay` and `apps/fmv` don't share code — `comic-fmv` **shells out** to the `ebay-sold-comps` console script across a process boundary (per CLAUDE.md's "FMV pipeline shells out across package boundaries"). Both copies carry a comment cross-referencing the other side and this incident.

Verified byte-for-byte:

```python
build_query("The Amazing Spider-Man #50", "50") == build_query("Amazing Spider-Man", "50")
# both → '"Amazing Spider-Man 50" -cgc -cbcs -graded -slab'
```

Re-running the cleaned query returned 52 comps immediately, confirming the query text — not SerpApi — was the fault.

**No change was needed to the empty-vs-fetch-err classification.** `_is_fetch_error` (present since BUI-143) already distinguishes a clean SerpApi 200 with 0 `organic_results` and no `error` key (a genuine empty pool) from a real fetch failure — the doubled-title query hit the "empty, not fetch-err" path correctly even before the fix. A regression test was added tying this classification explicitly to the doubled-title scenario, since the code was already right but nothing pinned it to this real-world case.

## Why This Works

Normalization is enforced at the one true chokepoint on each side of the shell-out boundary (`fmv_runner.run()` before dispatch, `build_query()` before the query is assembled), so it can't be bypassed regardless of what an upstream agent-driven skill hands over, and the two copies can't silently drift because each is commented to reference the other. The truthy-guard on `title` preserves old behavior byte-for-byte for callers that pass no title (previously rendered literally as `"None 50"`). The `(?<!\d)` guard in `_strip_embedded_issue` prevents an issue like `99` from chewing into an unrelated longer number such as the `2099` in `"X-Men 2099"`, so the fix introduces no new class of false stripping.

## Prevention

- **When a comp query returns 0 results on *every* tier (base, broader, grade-targeted), inspect the literal `_nkw` query string before suspecting a SerpApi quota/outage.** A doubled/duplicated issue number in the quoted phrase (`"Title #50 50"`) is the signature of this bug class — check for it first. Treat "all tiers empty" as a query-construction smell first, an API-health smell second.
- Normalize book titles at the handoff boundary — both in `comic-fmv`'s `run()` and defensively inside `ebay-sold-comps`'s `build_query()` — rather than trusting upstream agent prose to hand over a clean title.
- Keep the byte-for-byte equivalence test (`build_query(raw_title, issue) == build_query(clean_title, issue)`) as documentation-by-example; it directly encodes the ASM #50 incident as a regression guard.
- Remember the defense-in-depth duplication is intentional, not an oversight: the two packages are connected only by a console-script shell-out, so a fix in one does not protect the other — both sides need the guard, and both are commented to say so.

## Related Issues

- **BUI-346** — this fix (title normalization + fetch-err distinction).
- **BUI-347 / BUI-348** — same ASM #50 incident, sibling hardening: year-gated modern-variant exclusion terms on rebootable mastheads, and the CGC-proxy FMV tier for sparse-pool vintage keys. See `docs/solutions/best-practices/fmv-7a-cgc-proxy-not-safely-automatable.md` — BUI-348 **implements the "one defensible automation path" that doc anticipated** (an eBay-slab proxy), while its core guidance (don't automate the §7a Heritage-*prose* extractor) stays intact — and `docs/solutions/best-practices/fmv-grade-curve-interpolation-overbid-guards.md` (same "automating a documented FMV heuristic introduces a money-path bug" narrative shape).
