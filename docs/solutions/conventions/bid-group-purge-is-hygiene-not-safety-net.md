---
title: "Gixen bid groups: purge is hygiene, not the win-safety mechanism"
date: 2026-07-19
category: conventions
module: "gixen-cli (bid groups, server/main.py + server/fallback.py classification) + .claude/commands/comic/{snipe-add,buy}.md"
problem_type: convention
component: background_job
severity: medium
related_components:
  - "database"
applies_when:
  - "The working list has 2+ listings of the same comic and the user wants at most one copy"
  - "Deciding which Gixen `group` number to assign, or whether to group two auctions at all"
  - "Explaining to a user (or a future editor of snipe-add.md/buy.md) why `gixen purge` is optional"
symptoms:
  - "Confusion over whether skipping `gixen purge` risks winning two copies of the same book"
  - "Grouping two auctions that end within ~2 minutes of each other and both landing WON"
tags: [bid-groups, purge, phantom-won, gixen, snipe-add, buy, BUI-363, BUI-371, BUI-381]
---

# Gixen bid groups: purge is hygiene, not the win-safety mechanism

## Context

BUI-363 introduced Gixen bid groups so a working list with 2+ listings of the
same comic can snipe every copy while risking winning only one: per Gixen's
own semantics, once one snipe in a `group N` wins, Gixen auto-cancels the
other group members.

That auto-cancel is necessary but not sufficient for safety. BUI-146 found
that a cancelled-but-still-live sibling's auction still ends and still gets an
eBay-price-based WON/LOST inference — if the final price came in under that
sibling's own `max_bid`, the naive heuristic stamps a **phantom WON** on an
auction the group never actually bid on. The original mitigation was
operational: run `gixen purge` promptly after a group win, so the cancelled
sibling gets tombstoned `REMOVED` before the price-inference heuristic ever
sees it. That made purge timing load-bearing for correctness — a real-money
risk if a user forgot to purge before an ended-sibling sync ran.

BUI-371/BUI-381 closed that gap structurally instead of operationally: the
server now classifies a group-cancelled sibling `REMOVED` from **durable
upstream evidence** (vanish-time observations plus the append-only
`group_wins` ledger) at three classification sites, before the eBay-price
fallback ever runs — regardless of whether, or when, anyone runs `gixen
purge`. See `docs/solutions/architecture-patterns/evidence-layer-disambiguation-vs-heuristic-gating.md`
and its companion `durable-evidence-store-encode-unknowns-and-identity-precisely.md`
for the full mechanism (vanish-time disambiguation, the `group_wins` ledger,
and the representation traps that arc closed).

## Guidance

Four rules are all a skill or user needs to use bid groups safely:

1. **Same `group N` for every copy** of the book you want at most one of —
   omit `group` (or leave it `0`) if the user genuinely wants multiple copies.
2. **Pick an N unused by any live snipe** — check `gixen list` (shows each
   snipe's group); reusing a live group merges unrelated books into one
   win-at-most-one set.
3. **Don't group auctions ending within ~2 minutes of each other** — Gixen
   cancels siblings *after* a win, so near-simultaneous endings can both be
   bid and both won. Group them anyway if that's unavoidable, but warn the
   user and let them pick one to snipe instead.
4. **`gixen purge` is optional hygiene, not the win-safety mechanism.** It
   keeps the live Gixen list and dashboard tidy by removing cancelled
   siblings. It is **not** what prevents a phantom WON — the server
   classifies an unpurged sibling `REMOVED` on its own, from durable evidence,
   once its auction ends. Purge whenever convenient; correctness doesn't ride
   on when (or whether) you do.

## Why This Matters

Getting rule 4 wrong reintroduces exactly the BUI-146 risk class as a
*procedural* dependency — "the user must remember to purge" — that BUI-371/
381 removed as a structural one. Both `snipe-add.md` and `buy.md` used to
carry the full narrative above at length (independently, ~45 and ~27 lines
respectively) to justify rule 4 to a reader who might otherwise "fix" a
perceived gap by re-coupling purge timing to safety. This doc is the single
source for that justification; the skills link here instead of repeating it.

## Related

- `docs/solutions/architecture-patterns/evidence-layer-disambiguation-vs-heuristic-gating.md`
- `docs/solutions/architecture-patterns/durable-evidence-store-encode-unknowns-and-identity-precisely.md`
- `CONCEPTS.md` → Bidding & Snipes cluster (Bid Group, Tombstone, Phantom WON)
- Tickets: BUI-363 (bid groups), BUI-146 (phantom-WON accepted-risk), BUI-371/
  BUI-381 (evidence-layer + ledger hardening), BUI-437 (this dedup)
- `.claude/commands/comic/snipe-add.md` § Bid groups, `.claude/commands/comic/buy.md`
  § Duplicate listings of the same comic → Gixen bid group
