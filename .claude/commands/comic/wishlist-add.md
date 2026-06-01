---
name: comic:wishlist-add
description: Look up a series on Metron and add each of its issues to the local LOCG wish-list cache. Fully offline w.r.t. LOCG — only Metron is queried. Use to wish-list a whole run at once.
requires_locg_cli: ">=0.2.0"
---

# Comic Wishlist Add

Add every issue of a series (or a sub-range) to the local wish-list cache. The
issue count comes from **Metron** (metron.cloud); **no LOCG network access is
required** — adds go straight to `~/.cache/locg/wish-list.json` via
`locg wish-list add`.

## Input

- **Series** (required) — e.g. `"Children of the Vault"`.
- **Issue range** (optional) — e.g. `1-4`, `1,3,5`, or `5-` (from #5 to the end).
  When omitted, add every issue `1 … issue_count`.

## Step 0: Metron credentials guard

Metron requires credentials. They live in `~/.config/locg/.env` as
`METRON_USERNAME` / `METRON_PASSWORD`.

```bash
set -a; . ~/.config/locg/.env 2>/dev/null; set +a
[ -n "$METRON_USERNAME" ] && [ -n "$METRON_PASSWORD" ] && echo "metron creds ok" || echo "MISSING"
```

**If `MISSING`:** Stop with:
> Metron credentials not found. Add `METRON_USERNAME` and `METRON_PASSWORD` to
> `~/.config/locg/.env` and retry.

## Step 1: Look up the series on Metron

```bash
curl -s -u "$METRON_USERNAME:$METRON_PASSWORD" \
  "https://metron.cloud/api/series/?name=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "<SERIES>")"
```

Each result has `id`, `series` (display name incl. year, e.g.
`"Children of the Vault (2023)"`), `year_began`, and `issue_count`.

**Disambiguation:**
- Exactly one result → use it.
- Multiple results → show the user the candidates (`series` + `year_began` +
  `issue_count`) and ask which one. If the user supplied a year, prefer the
  series whose `year_began` matches.
- Zero results → stop and report that Metron has no series by that name (suggest
  the user check spelling or supply the exact Metron title).

Record `issue_count` and the chosen `series` display name.

## Step 2: Resolve the issue list

- No range given → issues `1 … issue_count`.
- Range given → parse it (`1-4` → 1,2,3,4; `1,3,5` → those; `5-` → 5…issue_count).
  Clamp to `1 … issue_count`; warn if the user asked for issues beyond
  `issue_count`.

## Step 3: Preview (dry run) and confirm

Before writing anything, print the exact titles that will be added and ask the
user to confirm. **Stopping here is the dry run.**

```
About to add 4 issues to the wish-list cache:
  - Children of the Vault #1
  - Children of the Vault #2
  - Children of the Vault #3
  - Children of the Vault #4
Proceed? (yes / no)
```

Use the series name as the user typed it for the `#<N>` titles (the simplest,
LOCG-searchable form). Mention that the Metron canonical name is
`<series display name>` in case they prefer that.

## Step 4: Add each issue

On confirmation, add one issue per call:

```bash
locg wish-list add "Children of the Vault #1" --pretty
locg wish-list add "Children of the Vault #2" --pretty
# …
```

Each call appends `{name: "<title>", id: null}` to the wish-list cache and is
idempotent-safe to re-run (it will create a duplicate entry, so don't re-run a
title that already succeeded). Stop and report if any call returns an `error`.

## Step 5: Report

```
**Wish-listed 4 issues of Children of the Vault (2023):**
  #1, #2, #3, #4  →  ~/.cache/locg/wish-list.json (N items total)
```

⚠️ **Sync caveat (BUI-47):** local `wish-list add` entries are **overwritten on
the next `locg collection import`** (import rebuilds the wish-list from the LOCG
export's wish-list rows). To keep these: `locg collection export` and upload the
CSV to LOCG **before** running another `collection import`. See
`packages/locg-cli/docs/processes/locg-collection-wishlist-sync.md`.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Hitting LOCG to get the issue count | Use the Metron series API — `issue_count` is in the series result; LOCG is not needed |
| Guessing the issue count | Always read `issue_count` from Metron; don't assume a run length |
| Adding issues without a preview | Always show the title list and confirm first (Step 3) — that's the dry run |
| Re-running the whole range after a partial failure | Re-add only the issues that didn't succeed; `wish-list add` does not dedupe |
