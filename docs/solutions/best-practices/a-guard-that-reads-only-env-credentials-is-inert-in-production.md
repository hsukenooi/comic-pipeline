---
title: "A guard that reads only env credentials is inert where the deployed process has none (BUI-929 printing guard)"
date: 2026-09-21
category: best-practices
module: apps/ebay (sold_comps.py printing guard), any code path that lazily obtains a credential
problem_type: best_practice
component: service_object
severity: high
mechanized_by: test
enforced_by_test:
  - apps/ebay/tests/test_sold_comps.py::TestPrintingGuardCredentialFallback
related_components:
  - ebay-sold-comps
  - comic-fmv
  - ebay-fetch
applies_when:
  - "Adding a code path that obtains an API credential lazily (a token for a secondary fetch, a guard, a verifier)"
  - "Writing a test fixture that blanks credentials so a test never makes a live call"
  - "Reviewing a diff whose justification for env-only credential lookup is test isolation"
symptoms:
  - "Every unit test is green, the guard's counters report `unverified` for every candidate in production, and nothing is ever dropped or flagged"
  - "A dev machine and CI behave the same (no credentials) while the Mac Mini differs (credentials in a config file the code never reads)"
tags: [credentials, guards, test-isolation, printing-guard, sold-comps, BUI-929, em-batch]
---

# A guard that reads only env credentials is inert where the deployed process has none

## What happened

BUI-929 added a printing guard to `ebay-sold-comps`: a slab comp priced far below its rung fetches the listing's description through the Browse API and is dropped on a "second printing" or "facsimile" token. The guard obtained its OAuth token from `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` environment variables only. The implementing agent chose env-only on purpose: `ebay_fetch.load_config()` falls back to `~/.config/ebay-fetch/config.json`, that file holds real credentials on the dev machine, and an autouse fixture that only deletes env vars could not have isolated a test from it.

Production has no env credentials. On the Mac Mini, `comic-fmv` shells out to `ebay-sold-comps`, and the only credential source is that config file. So the guard was inert: every outlier counted as `printing_unverified`, none was ever dropped, and every unit test passed because the tests never had credentials either. The live spike probe (eight real slabs) caught it: Ultimate Fallout #4 reported zero drops against two known $215 second printings.

## The rule

A credential-reading path is verified against the credential source the deployed process actually has, and the test fixture isolates that source; the code never avoids the source to make the fixture simpler.

Concretely:

1. Name the production source before writing the lookup. Here: `ebay_fetch.CONFIG_FILE`, because the Mini's `comic-fmv` chain sets no env vars.
2. Read it through a module attribute the fixture can repoint (`monkeypatch.setattr(ebay_fetch, "CONFIG_FILE", tmp_path / "absent.json")`), not through a helper that exits the process on failure.
3. Write the test that proves the production source is honored (the file supplies credentials when env is absent) beside the tests that prove isolation (no env and no file yields no token; env wins over the file).

The sibling doc `a-shipped-guard-is-not-a-running-guard.md` (BUI-775) covers the same failure one layer up: a guard proven at its predicate but never exercised at its decision. This one is the credential-shaped instance: the decision was reachable, and the guard walked in with empty hands.

## How it was found

Not by review and not by tests. The em-batch EM ran the merged code on the spike batch against the live provider before closing the ticket, and the drop count was the tell. A guard whose "cannot verify" outcome is silent-keep needs a live probe whose expected answer is a nonzero drop; a run that reports all-unverified is the same as no guard.
