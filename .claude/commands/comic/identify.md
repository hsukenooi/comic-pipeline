---
name: comic:identify
description: Identify comics from eBay listing URLs. Extracts series, issue, grade, variant, and listing type (auction vs Buy It Now). Use when the user provides eBay listing URLs and needs them identified before pricing or bidding.
---

# Comic Identify

Take eBay listing URLs and turn them into a structured table of comic identifications.

## Step 1: Dispatch the identifier subagent

Extract item IDs from URLs (the number after `/itm/`) or accept raw IDs directly. Then
dispatch the **`comic-identifier` subagent** with:

- **ITEM IDS** — the IDs (or full URLs) you extracted, space-separated
- **CURRENT UTC TIME** — current UTC time in ISO-8601 format (compute it now via
  `date -u +"%Y-%m-%dT%H:%M:%SZ"`)
- **NAME** — give the subagent a name at spawn (e.g. `comic-identifier`, BUI-366)
  so it stays addressable for follow-ups later in the run (see § Follow-ups below)

The subagent runs `ebay_fetch.py --json`, parses the JSON, and returns **only** the
formatted identification table. Raw JSON and intermediate parse steps never appear in
this context.

## Output

The subagent returns a fully-formatted identification table — columns `# | Comic
| Issue | Year | Grade | Variant | Type | Current Price | Bids | Seller | Ends |
Notes`, with the `#` cell linking to the eBay listing. The per-column derivation
contract (confidence-gating, Ends computation, grade signals, no extra API call
for price/bids) is owned by `.claude/agents/comic-identifier.md`. Present the
table as-is; two columns carry weight downstream:

- **Year** — forward it verbatim into `/comic:collection-check` (blank stays
  blank, never backfill a guess). It's a confidence-gated per-issue cover year
  (BUI-316); collection-check.md § Input shape owns the BUI-316/BUI-129
  forwarding rule.
- **Current Price / Bids** — carried forward for `/comic:buy` Steps 4–5; Step 4
  owns the no-re-fetch rule (BUI-359).

Flag Buy It Now listings — they're skipped at the Gixen step.

**Ask user to confirm identifications are correct.**

This table is the input for `/comic:collection-check` and `/comic:fmv`.

## Follow-ups: message the same agent (BUI-366)

The identifier agent keeps the full `ebay_fetch.py` JSON in its context after it
returns the table — item specifics, description text, printing/variant evidence
none of which entered the caller's context. For a follow-up question about a
listing it already fetched (e.g. "does item N's item specifics say first
printing?", "what does the description say about the variant?"), SendMessage
the **same named** agent (§ Step 1 — naming it at spawn is the precondition
that makes this addressable) rather than dispatching a fresh one — the answer
is one tool call from JSON it already holds; a fresh spawn re-fetches and
re-parses everything (in the 2026-07-16 run: 1 tool call vs 9).

## Common Mistakes

| Mistake | Fix |
|---|---|
| Running `ebay_fetch.py` inline instead of dispatching the subagent | Dispatch `comic-identifier` — keeps raw JSON out of this context |
| Using firecrawl browser on eBay | `ebay_fetch.py` calls the Browse API directly, no bot detection |
| Assuming grade when `grade_source` is `"missing"` | The subagent flags it — don't override without evidence |
| Missing variants | The subagent checks both `variant` field and `item_specifics` |
| Treating `condition` field as grade | `condition` is eBay's generic label (e.g. "Like New"); the subagent uses the parsed `grade` field |
