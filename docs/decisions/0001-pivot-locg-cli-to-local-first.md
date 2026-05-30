# 1. Pivot locg-cli to a local-first collection tool

- Status: Accepted
- Date: 2026-05-30
- Issue: BUI-25

## Context

`locg-cli` began as a CLI wrapper around League of Comic Geeks (LOCG): it
scraped/authenticated against the site to power `search`, `releases`, `comic`,
`series`, and the login-required list views (`pull-list`, `wish-list`,
`read-list`, `add`, `remove`, `update`, `check`, `collection has`). All of that
network access flows through `src/locg/client.py`, a Playwright + real-Chrome
client (there is no `api.py`).

Two things have since changed:

1. **The LOCG surface is effectively closed.** Requests now return `403` /
   Cloudflare interstitials even for read-only endpoints. The Playwright client
   exists precisely because plain HTTP stopped working; keeping it alive is a
   Cloudflare treadmill we will keep losing.
2. **The codebase already drifted local-first.** The valuable, working surface is
   now the local collection cache and its sync helpers: `collection import`
   (XLSX → cache), `collection export` (cache → LOCG-compatible CSV),
   `collection status`/`check`/`doctor`/`record-win`, and `wish-list add`/
   `remove` against the local cache. These are backed by `collection_cache.py`,
   `collection_io.py`, and `config.py` — not by `client.py`. The one live data
   source that still works is **Metron** (`metron.py`).

In practice the tool is already "a local comic-book database with LOCG as an
import/export sync target," not "a CLI for LOCG."

## Decision

Formally pivot `locg-cli` to a **local-first collection management tool**. The
local cache is the source of truth. LOCG is reduced to a bulk **import/export**
sync target (XLSX in, CSV out). **Metron** is the only live network source we
invest in going forward.

We **keep the `locg` name.** Renaming is cosmetic, the tool is still tightly
coupled to LOCG's data model and import/export format, and a rename would churn
the installed binary, docs, and muscle memory for no functional gain. We will
revisit the name only if/when a second first-class sync target makes `locg`
actively misleading.

## Consequences

### Positive
- We stop maintaining the Cloudflare-fighting Playwright client for everyday use.
- The supported surface matches what actually works, so docs and the
  first-run `doctor` walkthrough can stop pointing users at dead commands.
- A clear identity ("local cache is source of truth; LOCG via import/export;
  Metron for live lookups") makes downstream decisions tractable.

### Negative / trade-offs
- Live LOCG features (real-time search, releases, server-side lists) are gone.
  This is already true in practice — the pivot just makes it official.
- Anything that wanted a "look it up on LOCG" fallback must use Metron instead.
  **Downstream constraint:** this makes **BUI-33**'s proposed "LOCG title-search
  fallback" for variant resolution non-viable. **BUI-33 is therefore
  Metron-only** (resolve variants via `session.issue(metron_id).variants`).

## Follow-up actions (tracked separately, not part of this ADR)

These are intentionally deferred so the pivot decision lands without a large
deletion diff:

1. **Retire the dead LOCG-live commands** and their handlers: `search`,
   `releases`, `comic`, `series`, the series-scoped search, `pull-list`,
   `wish-list` (live view), `read-list`, `add`, `remove`, `update`, `check`, and
   `collection has`. Remove their `cmd_*` functions from `commands.py` and their
   subparsers from `cli.py`.
2. **Remove `client.py`** (the Playwright/Cloudflare client) and the
   `playwright` dependency once nothing imports it. Confirm `cli.py` /
   `commands.py` no longer reference `LOCGClient` / `AuthRequired` first.
3. **Update the README** to describe the local-first identity and the supported
   import/export/Metron surface; drop documentation for the retired commands.
4. **Update `collection doctor`** so the first-run walkthrough reflects the
   local-first flow rather than LOCG login.

File each as its own issue so this ADR stays a decision record, not a migration.
