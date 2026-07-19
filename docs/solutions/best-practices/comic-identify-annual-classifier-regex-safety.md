---
title: "Broadening the comic-identify annual classifier: ReDoS and year-numbered-annual hazards"
date: 2026-07-19
category: docs/solutions/best-practices
module: "apps/ebay comic-identify (comic_identity_year.py)"
problem_type: best_practice
component: service_object
severity: medium
applies_when:
  - "Editing _ANNUAL_EDITION_RE / _classify_edition_kind in apps/ebay/src/comic_identity_year.py"
  - "Broadening any comic-identity regex to accept more separators or tokens"
  - "Adding a heuristic that keys off a year-like digit run in a comic title"
tags:
  - comic-identify
  - regex
  - redos
  - annual
  - apps-ebay
related_docs:
  - "docs/solutions/ui-bugs/collection-check-alias-and-printing-false-positives.md"
  - "docs/solutions/architecture-patterns/evidence-layer-disambiguation-vs-heuristic-gating.md"
---

# Broadening the comic-identify annual classifier: ReDoS and year-numbered-annual hazards

## Context

`_classify_edition_kind` in `apps/ebay/src/comic_identity_year.py` decides whether an eBay title is an "annual" edition. BUI-450 tightened it so an issue-number token must **follow** the word (`_ANNUAL_EDITION_RE = r"\bannual\b\s*#?\s*\d"`). BUI-456 closed two remaining adjacency edge cases in the same regex. Two non-obvious traps surfaced while doing it — both easy to reintroduce the next time this classifier is touched.

## Guidance

### 1. Broaden a separator with a single character class, not stacked `\s*` runs

Widening `\s*#?\s*\d` to also accept `No.` / `:` / `-` by writing alternation between optional whitespace runs — e.g. `\s*(?:no\.?|[:#-])?\s*#?\s*\d` — puts **three adjacent `\s*` runs** next to each other. They can partition a run of whitespace ambiguously, which is classic catastrophic backtracking: a title with a long space run hangs (a 5000-space input ran >120s).

Collapse the whole gap to **one character-class quantifier** followed by the digit:

```python
# ReDoS-safe: one class, one quantifier, then the required digit.
_ANNUAL_EDITION_RE = re.compile(r"\bannual\b[\s#:.no-]*\d", re.IGNORECASE)
```

This is provably linear (verified to 500k chars in ~7ms). Keep **word-internal letters out of the class** so the run can't leap across a following word to a distant digit — `"annual note 5"`, `"annual-versary 3"` must stay single-issue (the `t`/`v`/`e` break the run before the digit).

### 2. Don't key an annual guard on "a 4-digit year after the word"

The tempting fix for the promo-year false positive (`"ASM #252 annual 2024 sale"` mis-filing as a phantom `ASM Annual #252`) is to reject a 4-digit year right after "annual". **That breaks real year-numbered annuals** — IDW `Sonic the Hedgehog Annual 2022/2023`, UK year-annuals, and bare-volume-year forms like `Star Wars 2021 Annual #1`.

Key instead on a **`#N` issue token appearing *before* the word "annual"**. A genuine annual names its series first and never precedes "annual" with a `#`-issue token (volume years are bare — `2021 Annual`, never `#2021`). So a preceding `#N` is positive evidence that the digit *after* "annual" is a promo/marketing year:

```python
_ISSUE_HASH_RE = re.compile(r"#\s*\d")
annual_m = _ANNUAL_EDITION_RE.search(title)
if annual_m and not _ISSUE_HASH_RE.search(title, 0, annual_m.start()):
    return "annual"
```

## Why This Matters

- **The ReDoS is invisible in unit tests** — normal-length titles pass instantly; only a pathological input reveals it. A single-character-class rewrite removes the failure mode by construction rather than hoping no such title arrives.
- **Year-numbered annuals are a real, published shape**, not a corner case. A year-rejection heuristic silently demotes them to single-issue and mis-files them. The `#`-before-annual key closes the damaging misfile (an existing issue number reclassified as a nonexistent annual) while preserving those annuals.
- **Both classifier error directions here are recoverable (duplicate-buy), never false-ownership-of-a-valuable-key** — that is what justifies accepting the residual below rather than chasing zero error.

## When to Apply

Any edit to `_ANNUAL_EDITION_RE` / `_classify_edition_kind`, or any new comic-identity heuristic that inspects a digit run. Re-verify against `test_comic_identify.py::TestComicIdentifyAnnualAdjacency` (both directions) and confirm seller-scan's `hard_reject` is unaffected — it uses a **separate** `_EDITION_PATTERNS` in `comic_identity.py`, not `_classify_edition_kind`.

## Examples

Accepted residual (deliberately not closed): the bare-number-before variant `"ASM 252 annual 2024"` (no `#`) still classifies as annual, because distinguishing a bare issue number from a bare volume year reintroduces the BUI-129 hazard. Lock the accepted false-negative in a named test so the trade-off is explicit rather than silently re-"fixed" later.

| Title | Result |
|-------|--------|
| `ASM #252 annual 2024 sale` | single-issue (guard: `#252` precedes "annual") |
| `X-Men Annual #1` | annual |
| `Annual No. 1` / `Annual: 1` / `Annual-#5` | annual (broadened separator) |
| `Star Wars 2021 Annual #1` | annual (bare volume-year preserved) |
| `annual note 5` | single-issue (word-internal letters break the run) |
