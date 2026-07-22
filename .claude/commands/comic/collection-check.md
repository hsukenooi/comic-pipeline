---
name: comic:collection-check
description: Check if identified comics are already in your collection via the comics server API. Use when deciding whether to buy a comic to avoid duplicates.
---

# Comic Collection Check

Check whether identified comics are already in your collection. As of BUI-504
the whole check is **one CLI call** — `locg collection check-batch` mechanizes
what used to be a ~450-line prose executor (server resolve → health gate →
status → batch check → stale-cache downgrade → the false-match flags). The
skill is now just the input shape, the decision gate, and carry-forward.

> **Hard-fail rule (R11) is enforced by the exit code.** `check-batch` exits
> **non-zero** on ANY failure — unreachable server, non-200, timeout, or the
> never-imported 409 — and renders NO verdicts. **A non-zero exit is a STOP:**
> tell the user and halt; never treat it as "not in collection" (a silent miss
> buys a duplicate). Do not re-derive "not owned" from a failed call.

## Run the check

```bash
locg collection check-batch items.json --table
```

- `--table` prints the human-facing table + status banners (present this to the
  user). Omit `--table` for the structured JSON (per-row `verdict` + `flags`).
- Reads `items.json` (or stdin with `-`). Exit 1 = STOP; only a `0` exit
  carries real verdicts.

### Input shape

`items.json` is `{"items": [{"series", "issue", "year"?, "variant"?}]}` — one
entry per comic in the working list:

```json
{"items":[{"series":"Amazing Spider-Man","issue":"300","year":"1988"},
          {"series":"Uncanny X-Men","issue":"179","variant":"Newsstand"}]}
```

**`year` is a per-issue COVER year, never a series start year (BUI-129).** The
server gates a match on `release_date.startswith(year)`, so a long-running
series' first-published year (e.g. `1963` for *Uncanny X-Men*, whose issues
shipped 1975–1991) filters out every owned row and false-negatives the whole
run. Forward the `/comic:identify` **Year column exactly as emitted** — present
means a confidence-gated per-issue cover year (BUI-316), safe to disambiguate
volumes; **blank means omit `year`** (never backfill a guess). A correct
verdict beats year-gated extras.

### What the flags mean

The `Notes` column carries advisory flags the CLI computes — Pattern **A**
(Giant-Size/Annual/King-Size conflation), **C** (unrecognized series spelling),
**D** (masthead-alias, unconfirmed volume), **D2** (cross-volume ambiguity),
**D3** (no-year rebootable-masthead match), **E** (printing conflict). Flag
semantics live in `packages/locg-cli/src/locg/check_batch.py`. **Flags FLAG,
they never DECIDE (R11)** — the CLI never flips a verdict or invents ownership;
the user resolves each flagged row at the decision gate below.

## Step 4: Decision gate

Present the table, then ask the user how to handle results:

- **Skip** comics already in collection (`✅`, most common).
- **Continue anyway** — condition upgrade; they want a better copy.
- **Wishlisted-not-owned (`📋`)** — not a duplicate risk; proceed like any
  `not_in_cache` comic, but worth a callout (already flagged as wanted).
- **Stale-cache rows** (`⚠️ Not in cache … stale`) — surface separately so the
  user can manually verify on LOCG before bidding.
- **Flagged rows (A / C / D / D2 / D3 / E)** — surface separately and do **not**
  act on the raw verdict: an A/E possible-false-positive should not be
  auto-skipped, and a C/D/D2/D3 flag should not be auto-bid. Let the user
  resolve each before the row leaves this skill.

## Carry-forward

Remove skipped comics from the working list before passing it to `/comic:fmv`.
Kept rows carry forward their identify-emitted fields plus this step's flags so
whoever reads the row next (grading, FMV) can see why it's still in play.

## Single-item spot check (fallback)

For a one-off ownership check outside the working-list flow, hit the single-item
endpoint directly (same matcher, same verdict shape). Same R11 rule: a failed
call is a hard STOP, never a silent "not owned".

```bash
comics-api GET /api/comics/collection/check -G \
  --data-urlencode "series=Amazing Spider-Man" \
  --data-urlencode "issue=300" \
  --data-urlencode "year=1988"
```

> Never use the `locg collection check` CLI (no `-batch`) for ownership — it
> reads the MacBook's local store, which is never seeded and always returns
> `not_in_cache`. `check-batch` and the curl above both hit the Mac Mini's
> authoritative store.
