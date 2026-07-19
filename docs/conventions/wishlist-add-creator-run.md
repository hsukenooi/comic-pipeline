# Creator runs in `/comic:wishlist-add` (BUI-134): "add X's run on series Y"

Extracted from `.claude/commands/comic/wishlist-add.md` (BUI-448) so the
common numeric-range wish-list-add path doesn't carry this whole sub-feature's
prose on every invocation. The skill keeps a 2-line pointer to this doc plus
the load-bearing "never enumerate from memory" rule inline — read that rule in
the skill file first; it applies regardless of whether you end up here.

"Add John Romita Jr.'s run on Uncanny X-Men to the wish-list" has **no
ground-truth source in model memory** — and memory silently drops DISCONTINUOUS
runs. Asked for JR JR's Uncanny X-Men pencils, an agent recalls only #175–211
and misses his ~1993 second stint (#287, #300–311).

**Never enumerate a creator run from memory — for ANY claim, not just a
wish-list write.** This includes a bare conversational question ("what was
Erik Larsen's Spider-Man run?") with no wish-list intent at all. BUI-340: asked
exactly that as a plain question, an agent answered from model memory (said
#19–43) instead of grounding it in Metron, because invoking the full
wish-list-add flow felt like the wrong tool for "just answer a question." The
Metron-credited run was actually #18–23. The fix isn't "remember to ground
it" — it's reaching for the right tool, which now exists:

- **Just answering a question, no write intended** → `locg creator-run`
  (below) — read-only, zero cache/file writes, prints the resolved issue
  list/range. This is the tool to reach for by default whenever a creator-run
  claim is needed, wish-list-add or not.
- **Actually adding the run's gap issues to the wish-list** → `locg wish-list
  add --creator …` (this section) — resolves the same way, then writes.

## Read-only lookup: `locg creator-run`

For a plain question — no wish-list write intended — use the read-only
counterpart instead of the write path:

```bash
locg creator-run "The Amazing Spider-Man" \
  --creator "Erik Larsen" --series-id <METRON_SERIES_ID> --role penciller
```

It calls the same `resolve_creator`/`resolve_creator_run` Metron methods
described below (same id-pinning, same discontinuous-stint handling, same
per-issue role confirmation) and prints `{creator, creator_id, issues,
issue_numbers, warnings, ...}` — no collection check, no wish-list dedup, no
cache read or write of any kind. Use this whenever the ask is "what was X's
run" rather than "add X's run."

## Writing the run to the wish-list

To actually add the run's gap issues to the wish-list, ground it in Metron's
per-issue creator credits via the `locg` resolver:

```bash
# series = the LOCG-searchable title used for the "<series> #<N>" wish entries
# --series-id = the Metron series id (from the Step 1 series lookup)
# --creator   = the EXACT Metron creator name (disambiguates JR vs Sr by id)
# --role      = credit role to filter by (default: penciller)
locg wish-list add "Uncanny X-Men" \
  --creator "John Romita Jr." --series-id <METRON_SERIES_ID> --role penciller
```

What it does, in order:
1. **Pins the creator's Metron id** (`/creator/?name=`). "John Romita Jr." and
   "John Romita" (Sr.) are distinct ids — the resolver matches by id, never a
   loose name string. An ambiguous or unknown name is a **hard error**, not a
   guess; pass the exact Metron creator name.
2. **Resolves the EXACT issue set** the creator holds `--role` on, from each
   issue's Metron credits. The candidate set comes from Metron's issue-list
   `creator` filter (so BOTH stints are in scope), then each issue's credits
   confirm the role. This returns the discontinuous #287/#300–311 stint that
   memory drops.
3. **Filters owned + already-wishlisted issues** before any write — owned via
   the same per-issue collection check (by that issue's **cover year**, never
   `year_began`, BUI-129), already-wishlisted via the local cache.
4. Appends the remaining `"<series> #<N>"` titles to the wish-list cache.

**Role is EXPLICIT.** The default `penciller` matches **only** a `Penciller`
credit — it does NOT auto-include `Breakdowns`, `Layouts`, `Co-Penciller`, etc.
To widen the run, pass that role name explicitly (`--role breakdowns`); you can
only request one role per call.

**Low-confidence WARNING on thin credits.** Metron's credit data is sparse/
occasionally wrong on older Silver/Bronze books. An issue in the candidate set
that Metron has **no credits at all** for is reported in the result's `warnings`
(not silently treated as "not in the run"). Surface these to the user — the run
membership for those issues is unverified and may need a manual eyeball.

The result JSON carries `added`, `already_owned`, `already_wishlisted`,
`warnings`, `creator`/`creator_id` (the pinned Metron id), and `run_issue_count`.
Show the user the preview (added vs skipped vs warnings) and confirm before this
is treated as final, same as the numeric-range path.

> Note: the `locg wish-list add` path writes the **local** cache. The
> server-backed `POST /api/comics/wish-list` flow (Steps 1–6 of
> `/comic:wishlist-add`) is the machine-visible path; the creator-run resolver
> is currently a local-cache convenience for enumerating the exact issue set.
> When adding to the server, feed the resolved issue numbers into the Step 5
> `POST` batch.
