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

The subagent runs `ebay-fetch --identify` (one call, BUI-900) and returns **only** the
formatted identification table. Raw JSON and intermediate parse steps never appear in
this context.

## Output

The subagent returns a fully-formatted identification table — columns `# | Comic
| Issue | Year | Grade | Variant | Type | Current Price | Bids | Seller | Ends |
Notes`, with the `#` cell linking to the eBay listing. The per-column derivation
contract (confidence-gating, Ends computation, grade signals, no extra API call
for price/bids) is owned by `ebay-fetch --identify` in
`apps/ebay/src/ebay_fetch.py` (BUI-900); the agent passes its output through. Present the
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

The identifier agent stays addressable after it returns the table. For a
follow-up question about a listing it already identified (e.g. "does item N's
item specifics say first printing?", "what does the description say about the
variant?"), SendMessage the **same named** agent (§ Step 1 — naming it at spawn
is the precondition that makes this addressable) rather than dispatching a
fresh one: it answers from one `ebay-fetch --json <id>` call for that listing,
whose item specifics and description snippet never enter the caller's context,
where a fresh spawn re-runs the whole identify step.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Running `ebay-fetch` inline instead of dispatching the subagent | Dispatch `comic-identifier` — keeps the fetch, its stderr, and any follow-up JSON out of this context, and keeps the agent addressable for follow-ups |
| Using firecrawl browser on eBay | `ebay-fetch` calls the Browse API directly, no bot detection |
| Assuming grade when `grade_source` is `"missing"` | The subagent flags it — don't override without evidence |
| Missing variants | The subagent checks both `variant` field and `item_specifics` |
| Treating `condition` field as grade | `condition` is eBay's generic label (e.g. "Like New"); the subagent uses the parsed `grade` field |
