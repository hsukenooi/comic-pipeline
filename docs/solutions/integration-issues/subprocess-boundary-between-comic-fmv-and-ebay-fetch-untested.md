---
title: "A quiet feature needs a positive-path test: the comic-fmv to ebay-fetch subprocess boundary shipped green and did nothing"
date: 2026-09-22
category: integration-issues
module: "apps/fmv (fmv_runner.py _maybe_attach_active_ask_ceiling / _fetch_active_asks) + apps/ebay (ebay_fetch.py search_active_asks and the --active-asks default cap) (BUI-954)"
problem_type: integration_issue
component: service_object
severity: medium
mechanized_by: test
enforced_by_test:
  - "apps/fmv/tests/test_fmv_runner.py::test_a_string_grade_on_a_refused_raw_row_still_fetches"
  - "apps/fmv/tests/test_fmv_runner.py::test_an_unreadable_grade_skips_the_fetch"
  - "apps/ebay/tests/test_ebay_fetch.py::test_main_default_cap_is_one_full_browse_page"
related_components:
  - "apps/fmv"
  - "apps/ebay"
  - "comic-fmv"
  - "ebay-fetch"
symptoms:
  - "PR #552 merged with the apps/fmv and apps/ebay suites green and the deploy SHA assertion passing, yet a live comic-fmv re-run on the 8-listing spike list attached no active-ask token to any refused row"
  - "The runner gate returned early for every batch row because batch input carries grade as a string and the gate accepted only int or float, so the ebay-fetch subprocess was never invoked"
  - "Fixing that gate made one pre-existing unit test issue a real eBay Browse HTTP call, proving no test had ever reached the subprocess boundary"
  - "With the gate fixed, refused rows still came back {\"low\": null, \"n\": 0} because the --active-asks default cap of 50 truncated the 200-item Browse page before any graded listing"
root_cause: test_isolation
resolution_type: code_fix
tags:
  - "active-ask-ceiling"
  - "subprocess-boundary"
  - "silent-early-return"
  - "quiet-feature"
  - "positive-path-test"
  - "autouse-stub"
  - "live-reprobe-after-deploy"
  - "no-import-edge"
  - "bui-954"
---

# A quiet feature needs a positive-path test

## Problem

BUI-954 added a display-only active-ask ceiling to `comic-fmv`: on a refused row
(flag reason set, no band), the runner shells out to `ebay-fetch --active-asks` and
attaches an `asks from $X (nN)` token to the brief. PR #552 merged with over a thousand
green tests and passed the deploy assertion. A live re-run on the 8-listing spike list
showed the token on no refused row at all. Two independent bugs hid behind the same
silence, and neither suite could see either of them.

## Symptoms

- CI green on both app suites; `scripts/deploy.sh` asserted the merged SHA on the Mini.
- The live brief after deploy: no refused row, raw or certified, carried `asks from`.
- A direct probe, `ebay-fetch --active-asks "Fantastic Four #93" --grade 6.0`, returned
  `{"low": null, "n": 0}` while five matching Buy It Now listings were live on eBay.

## What Didn't Work

- **The unit tests for the ceiling stubbed `_fetch_active_asks` directly.** They proved
  the attach-to-row logic, and never exercised the gate above it that decides whether
  to call at all.
- **The deploy assertion.** It proves the right code is running, not that a display-only
  side effect fires. From outside, silence and correctness are the same brief.
- **Blaming the fixture.** The spike fixture's ASM #50 row carried a stale
  `signature_series` label from an older resolver (the current one maps "CGC SS" titles
  to `qualified`). It was a confound, not a cause, and chasing it delayed the real find.

## Solution

Two code fixes, one test fixture, one live probe.

**1. Coerce the grade before the gate** (`_maybe_attach_active_ask_ceiling`, PR #553).
Batch input from `/comic:buy` carries `grade` as a string (`"5.5"`, `"VG 4.0"`), and the
`isinstance` check rejected every one.

```python
# before
grade = inp.get("grade")
if not title or not issue or not isinstance(grade, (int, float)):
    return

# after
grade = _coerce_grade(inp.get("grade"))
if not title or not issue or grade is None:
    return
```

**2. Fetch one full Browse page by default** (`search_active_asks` and the `--max-results`
argparse default, PR #554). eBay's Browse page holds 200 items in best-match order and
graded listings can sit anywhere in it. A cap of 50 truncated the page before them, at the
same request cost as 200.

```python
# before
parser.add_argument("--max-results", type=int, default=50, ...)
# after
parser.add_argument("--max-results", type=int, default=200, ...)
```

**3. An autouse stub with a named opt-out.** Once the gate was fixed, a pre-existing
`run()` test made a real Browse call. The fixture stubs the subprocess for every test
except the class that tests `_fetch_active_asks` itself (missing binary, timeout, bad JSON):

```python
@pytest.fixture(autouse=True)
def _stub_active_ask_fetch(request, monkeypatch):
    if request.cls is not None and request.cls.__name__ == "TestActiveAskCeiling":
        return
    monkeypatch.setattr(fmv_runner, "_fetch_active_asks", lambda *a, **k: None)
```

**4. The live probe.** After both fixes and a redeploy at d5a7ddd, refused raw
Fantastic Four #93 at 6.0 shows `too_wide, asks from $21 (n5)` and refused certified
ASM #50 shows `asks from $1495 (n1)`. Band and cap stayed null on both, so the
display-only contract held.

## Why This Works

Two causes compounded.

**Success looked like silence.** The feature's only observable effect is an optional
token on a refused row, and `n=0` is a legitimate quiet outcome. A silent early return
(bug 1) and a page truncated before the matches (bug 2) are both indistinguishable from
"no asks found" by any check on the output. This is the same trap the [[Fetch Error]]
concept names for sold comps: an absent evidence trail defaults to "nothing exists"
unless something forces it to mean "nothing looked".

**The boundary is where the suites don't meet.** `comic-fmv` reaches `ebay-fetch` over
a PATH subprocess, with no import edge. The fmv suite stubs the call; the ebay suite
parses fixtures. Nothing drove a production-shaped argv (a string grade, a full page)
through both sides at once, so the gate's type check and the cap's default were each
tested only in the world where they didn't matter.

The general rule the batch surfaced: **when a feature can legitimately produce nothing,
a green suite must contain a test that asserts the positive path fires on a
production-shaped input.** A test that only checks the quiet path is chosen correctly
cannot tell a correct skip from a broken one.

## Prevention

1. **One positive-path test per quiet feature.** Assert the side effect fires on a batch
   shaped input, and separately assert the quiet path is chosen for the right reason:

   ```python
   def test_a_string_grade_on_a_refused_raw_row_still_fetches(self):
       row = self._refused_raw_row()
       row["input"]["grade"] = "8.0"          # batch input shape, not a float
       with patch("fmv_runner._fetch_active_asks",
                  return_value={"low": 650.0, "n": 3}) as mock_fetch:
           fmv_runner._maybe_attach_active_ask_ceiling(row)
       mock_fetch.assert_called_once_with("X", "1", 8.0, certifier=None, label=None)
       assert row["fmv"]["active_ask_n"] == 3
   ```

2. **Autouse-stub every subprocess or HTTP seam** so a later gate fix can never send the
   suite to the network, with a named opt-out for the class that tests the seam itself
   (the fixture in Solution step 3).

3. **Pin CLI defaults that encode a provider page size** with a test, so the default is
   a decision and not an inherited number (`test_main_default_cap_is_one_full_browse_page`).

4. **The live re-run after deploy stays the gate** for any change whose success looks
   like silence. "Tests green, production unchanged" is a known failure shape in this
   repo, not a hypothetical; BUI-946 set the rule for pool-shape changes and BUI-954
   extends it to display-only features.

5. **Re-identify a fixture before trusting a field it carries.** A probe fixture built
   under an older resolver carries stale identity fields; rebuild it from the current
   identify path before reading a mismatch as a bug.

## Related Issues

- `docs/solutions/best-practices/a-mock-at-the-contract-boundary-cannot-test-the-contract.md`
  (BUI-658/673): the same no-import-edge boundary out of `apps/fmv`, mocked at the
  transport seam. That doc's failure is a receiver rejecting the payload; this one's is
  the caller never sending it.
- `docs/solutions/best-practices/a-shipped-guard-is-not-a-running-guard.md`: the string
  grade gate is its case 3, "trigger correct, eligibility gate excludes".
- `docs/solutions/logic-errors/unreported-second-pass-reads-as-never-fired.md` (BUI-921):
  the same diagnostic trap one day earlier in the same module.
- `docs/solutions/workflow-issues/verification-whose-failure-is-indistinguishable-from-success.md`:
  the general class.
- `docs/solutions/best-practices/a-fetch-time-exclusion-is-undone-by-an-archive-that-re-admits.md`
  (BUI-946): the origin of the live re-run rule.
- Linear: BUI-954 (the feature and both fixes, PRs #552, #553, #554), BUI-971 (residual:
  a Browse HTTP error inside `--active-asks` still reads as `n: 0`), BUI-565 (the
  per-book crash that read as a clean `n=0` in sold comps).
