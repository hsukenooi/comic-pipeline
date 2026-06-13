# Concepts

> Shared domain vocabulary for this project — entities, named processes, and status concepts with project-specific meaning. Seeded with core domain vocabulary, then accretes as ce-compound and ce-compound-refresh process learnings; direct edits are fine. Glossary only, not a spec or catch-all.

## Collection & Lists

### Collection
The canonical record of the comics you own. The **gixen server store is the source of truth**; League of Comic Geeks (LOCG) is a downstream mirror used for browsing and bulk sync, not the system of record.

### Wish List
Comics you want but do not own. Distinct from the Pull List. Reads of the wish list (e.g. seller scanning) come from the server, not LOCG.

### Pull List
Comics you subscribe to receive as new releases through your local comic shop. Managed on LOCG and **never modified by the collection sync** — the bulk-import format has no pull-list field, so syncing cannot add to or remove from it.

### Win-Sourced Entry
A Collection entry created by recording a won eBay auction, before it has round-tripped through LOCG. *Known in code and tickets as:* `agent_win`.

Win-sourced entries carry no publisher (record-win does not supply one) and often a best-guess release date, which is why reconciling them against a LOCG export must tolerate a missing publisher and match on year rather than exact date.

### Import-Sourced Entry
A Collection entry that originated from — or has round-tripped through — a LOCG export. *Known in code and tickets as:* `locg_export`. The counterpart to a Win-Sourced Entry.

### Pending Push
A Collection entry that has been recorded locally but not yet confirmed present on LOCG. Clearing pending entries is the goal of a Collection Sync; an entry stays pending until it reappears in a LOCG export and reconciles.

## Sync Processes

### Record-Win
The process of recording a won eBay auction into the Collection as a Win-Sourced Entry.

### Collection Sync
The round-trip that mirrors the Collection up to LOCG and reconciles it back: export the pending entries to a bulk-import file, upload it to LOCG, re-export from LOCG, and re-import to clear pending.

The export is **owned-safe**: it never instructs LOCG to un-collect a book you own. LOCG's bulk import treats an `In Collection=0` row as "remove from collection," so the export pushes only genuinely-new wishes you do not already own. The re-import is reconciliation-based: it matches a pending Win-Sourced Entry to its LOCG counterpart even when LOCG has canonicalized the publisher or release date, and never creates a duplicate-identity entry.
