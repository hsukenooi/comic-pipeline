# Collection Details Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `--grade`/`--price` flags into `locg add collection`, add a new `locg update <id>` command, and make `require_auth` detect server-side expired sessions with a clear error.

**Architecture:** Three surgical additions to the existing `client` / `commands` / `cli` / `models` layers. No new modules. `post_my_details` is a form POST — its quirk is that omitted fields get wiped, so `update` does a fetch-merge-POST while `add` can safely send a minimum payload (comic is brand new). Session validation piggybacks on the existing `verify_session()` method with a cached boolean to keep cost at one GET per CLI invocation.

**Tech Stack:** Python 3.9+, argparse, `playwright` (real Chrome via persistent context; replaced `curl_cffi` in PER-38), BeautifulSoup, `pytest` + `unittest.mock` (already in use).

**Spec:** `docs/superpowers/specs/2026-04-20-collection-details-design.md`

---

## File Structure

**Modify:**
- `src/locg/client.py` — add `_server_auth_verified: Optional[bool] = None` to `__init__`, teach `require_auth()` to call `verify_session()`, update `login()` to prime the cache.
- `src/locg/models.py` — add `extract_my_details(soup) -> dict` helper that reads `data-initial` attributes from the `#my-details` tab's form fields.
- `src/locg/commands.py` — extend `cmd_add(client, list_name, comic_id, grade=None, price=None)` with the post-details call; add `cmd_update(client, comic_id, grade=None, price=None, condition=None)`; add the `VALID_GRADES` frozenset and validation helpers.
- `src/locg/cli.py` — add `--grade`/`--price` flags to the `add` subparser (gated on `list == "collection"`), add a new `update` subparser, add the `update` dispatch branch in `main()`, and wire the partial-success stderr-emit + exit-1 for `cmd_add`.

**Create:**
- `tests/fixtures/comic_detail_my_details.html` — captured `#my-details` tab with known `data-initial` values, covering a populated `grading`, `price_paid`, `condition`, plus at least one non-default `<select>` and one non-default `<input>`, AND the `comic-controller` spans on the page root showing the comic is in the user's collection. (Test `cmd_update_rejects_non_collection_comic` will use a second fixture with `collection` NOT active.)
- `tests/fixtures/comic_detail_my_details_not_collected.html` — same structure, but with `comic-controller` spans marking the comic as NOT in the collection.
- `tests/test_client.py` — new file for `LOCGClient.require_auth` tests (there is no existing `test_client.py`).

**Don't touch:**
- `src/locg/parser.py`, `src/locg/config.py`, `src/locg/__main__.py` — no changes needed.

---

## Execution Order

Tasks are ordered to preserve a passing test suite at every commit:

1. **Task 1** — Server-side `require_auth` (lowest-risk foundation; every other task's tests call `require_auth`).
2. **Task 2** — `extract_my_details` parser (pure function, no network).
3. **Task 3** — Grade validation constants + CGC scale whitelist.
4. **Task 4** — `cmd_add` grade/price extension.
5. **Task 5** — CLI wiring for `add --grade/--price` including partial-success stderr emit.
6. **Task 6** — `cmd_update` implementation.
7. **Task 7** — CLI wiring for `update` subcommand.
8. **Task 8** — Update `CLAUDE.md` docs.

---

## Task 1: Server-side `require_auth` validation

**Files:**
- Modify: `src/locg/client.py:24-61` — add field to `__init__`, rewrite `require_auth()`, update `login()`
- Create: `tests/test_client.py`

- [ ] **Step 1: Write the first failing test — `verify_once`**

Create `tests/test_client.py` with this content:

```python
"""Tests for locg.client module."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from locg.client import AuthRequired, LOCGClient


def _make_client_with_session(ci_session: str | None = "abc123") -> LOCGClient:
    """Build an LOCGClient without touching the filesystem."""
    with patch.object(LOCGClient, "_load_cookies"):
        client = LOCGClient()
    # Install a fake cookie jar
    jar = []
    if ci_session:
        cookie = MagicMock()
        cookie.name = "ci_session"
        cookie.value = ci_session
        jar.append(cookie)
    client._session = MagicMock()
    client._session.cookies.jar = jar
    return client


def test_require_auth_verifies_once():
    """require_auth should call verify_session at most once per LOCGClient instance."""
    client = _make_client_with_session()
    client.verify_session = MagicMock(return_value=True)

    client.require_auth()
    client.require_auth()
    client.require_auth()

    assert client.verify_session.call_count == 1
```

- [ ] **Step 2: Run the test — expect FAIL**

```bash
cd /Users/hsukenooi/conductor/workspaces/locg-cli/singapore
PYTHONPATH=src python3 -m pytest tests/test_client.py::test_require_auth_verifies_once -v
```

Expected: FAIL. `verify_session.call_count` will be 0 because the current `require_auth` doesn't call `verify_session`.

- [ ] **Step 3: Implement cached verification in `require_auth`**

Edit `src/locg/client.py`. Change `__init__` (currently at line 27) to initialise the cache field:

```python
    def __init__(self) -> None:
        self._session = cffi_requests.Session(impersonate="chrome")
        self._cookies_loaded = False
        self._server_auth_verified: Optional[bool] = None
        self._load_cookies()
```

Replace the existing `require_auth` (currently at line 59-61):

```python
    def require_auth(self) -> None:
        if not self.is_authenticated:
            raise AuthRequired("Not logged in. Run: locg login")
        if self._server_auth_verified is None:
            # verify_session may raise (429, network, malformed response).
            # Do NOT cache the result on failure — let the exception
            # propagate so the next invocation retries.
            self._server_auth_verified = self.verify_session()
        if self._server_auth_verified is False:
            raise AuthRequired("Session expired. Run: locg login")
```

- [ ] **Step 4: Run the test — expect PASS**

```bash
PYTHONPATH=src python3 -m pytest tests/test_client.py::test_require_auth_verifies_once -v
```

Expected: PASS.

- [ ] **Step 5: Write failing test for expired session**

Append to `tests/test_client.py`:

```python
def test_require_auth_expired_session_raises():
    """If verify_session returns False, require_auth should raise AuthRequired."""
    client = _make_client_with_session()
    client.verify_session = MagicMock(return_value=False)

    with pytest.raises(AuthRequired, match="Session expired"):
        client.require_auth()
```

- [ ] **Step 6: Run the test — expect PASS**

```bash
PYTHONPATH=src python3 -m pytest tests/test_client.py::test_require_auth_expired_session_raises -v
```

Expected: PASS (the implementation already covers this case).

- [ ] **Step 7: Write failing test for no cookie**

Append to `tests/test_client.py`:

```python
def test_require_auth_no_cookie_raises():
    """Without a ci_session cookie, require_auth raises before verify_session."""
    client = _make_client_with_session(ci_session=None)
    client.verify_session = MagicMock(return_value=True)

    with pytest.raises(AuthRequired, match="Not logged in"):
        client.require_auth()

    client.verify_session.assert_not_called()
```

- [ ] **Step 8: Run test — expect PASS**

```bash
PYTHONPATH=src python3 -m pytest tests/test_client.py::test_require_auth_no_cookie_raises -v
```

Expected: PASS.

- [ ] **Step 9: Write failing test for transient error non-caching**

Append to `tests/test_client.py`:

```python
def test_require_auth_does_not_cache_on_transient_error():
    """If verify_session raises, the next require_auth should retry (not cache the failure)."""
    client = _make_client_with_session()
    call_count = {"n": 0}

    def side_effect():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated 429")
        return True

    client.verify_session = MagicMock(side_effect=side_effect)

    # First call — the exception propagates; we should NOT catch AuthRequired,
    # we should catch RuntimeError specifically.
    with pytest.raises(RuntimeError, match="simulated 429"):
        client.require_auth()

    # _server_auth_verified should still be None (not cached)
    assert client._server_auth_verified is None

    # Second call — verify_session is called again, returns True, no raise
    client.require_auth()
    assert call_count["n"] == 2
    assert client._server_auth_verified is True
```

- [ ] **Step 10: Run test — expect PASS**

```bash
PYTHONPATH=src python3 -m pytest tests/test_client.py::test_require_auth_does_not_cache_on_transient_error -v
```

Expected: PASS. Python's assignment `self._server_auth_verified = self.verify_session()` will not execute if `verify_session()` raises, so the cache stays `None`.

- [ ] **Step 11: Update `login()` to prime the cache**

Edit the `login()` method at `src/locg/client.py:104-122`. Replace the final three lines (`logger.debug("Login successful (verified)")` / `return True`) so the full success branch reads:

```python
        # Verify the session is actually valid server-side
        if not self.verify_session():
            logger.debug("Login appeared to succeed but session is not valid server-side")
            return False

        self._server_auth_verified = True
        logger.debug("Login successful (verified)")
        return True
```

- [ ] **Step 12: Add a regression test that login primes the cache**

Append to `tests/test_client.py`:

```python
def test_login_success_primes_verified_cache():
    """On successful login, _server_auth_verified should be True so
    the next require_auth call doesn't re-verify."""
    client = _make_client_with_session(ci_session=None)  # start empty

    # Fake login that sets the cookie then returns success
    def fake_post(path, data=None):
        cookie = MagicMock()
        cookie.name = "ci_session"
        cookie.value = "xyz"
        client._session.cookies.jar.append(cookie)
        resp = MagicMock()
        resp.status_code = 200
        return resp

    client.post = MagicMock(side_effect=fake_post)
    client._save_cookies = MagicMock()
    client.verify_session = MagicMock(return_value=True)

    assert client.login("user", "pass") is True
    assert client._server_auth_verified is True

    # Now require_auth should NOT call verify_session again
    client.require_auth()
    assert client.verify_session.call_count == 1  # only the one from login()
```

- [ ] **Step 13: Run all client tests — expect PASS**

```bash
PYTHONPATH=src python3 -m pytest tests/test_client.py -v
```

Expected: 5 passed.

- [ ] **Step 14: Run the full suite — expect no regressions**

```bash
PYTHONPATH=src python3 -m pytest tests/ -v
```

Expected: all existing tests + 5 new ones pass.

- [ ] **Step 15: Commit**

```bash
cd /Users/hsukenooi/conductor/workspaces/locg-cli/singapore
git add src/locg/client.py tests/test_client.py
git commit -m "Add server-side session validation to require_auth"
```

---

## Task 2: `extract_my_details` parser + fixture

**Files:**
- Modify: `src/locg/models.py` — add `extract_my_details` at the end of the file
- Create: `tests/fixtures/comic_detail_my_details.html`
- Create: `tests/fixtures/comic_detail_my_details_not_collected.html`
- Modify: `tests/test_models.py` — append new tests
- Modify: `tests/conftest.py` — add fixture loaders

- [ ] **Step 1: Create the collected fixture**

Write `tests/fixtures/comic_detail_my_details.html` with this content (HTML fragment representing the `#my-details` tab of a comic page where the user has the comic in collection with grade 8.5, price 99.99, and a condition note):

```html
<div id="comic-wrap">
  <span class="comic-controller active" data-comic="6512949" data-list="2"></span>
  <span class="comic-controller" data-comic="6512949" data-list="1"></span>
  <span class="comic-controller" data-comic="6512949" data-list="3"></span>
  <span class="comic-controller" data-comic="6512949" data-list="5"></span>
  <link rel="canonical" href="https://leagueofcomicgeeks.com/comic/6512949/x">
  <h1>Test Comic #1</h1>
  <div id="my-details">
    <form>
      <input name="comic_id" value="6512949" data-initial="6512949">
      <input name="copy_num" value="1" data-initial="1">
      <input name="quantity" value="1" data-initial="1">
      <input name="date_purchased" value="4/1/2026" data-initial="4/1/2026">
      <input name="price_paid" value="99.99" data-initial="99.99">
      <input name="purchase_store" value="LCS" data-initial="LCS">
      <select name="media" data-initial="1">
        <option value="" selected=""></option>
        <option value="1">Print</option>
        <option value="2">Digital</option>
        <option value="3">Both</option>
      </select>
      <input name="signature" value="" data-initial="">
      <input name="storage_box" value="Box A" data-initial="Box A">
      <select name="slabbing" data-initial="0">
        <option value="0" selected="">Raw</option>
        <option value="1">Slabbed</option>
      </select>
      <select name="grading" data-initial="8.5">
        <option value="0" selected="">None</option>
        <option value="8.5">8.5</option>
        <option value="9.2">9.2</option>
      </select>
      <select name="grading_company" data-initial="CGC">
        <option value="" selected=""></option>
        <option value="CGC">CGC</option>
        <option value="CBCS">CBCS</option>
      </select>
      <input name="condition" value="white pages" data-initial="white pages">
      <textarea name="notes" data-initial="private note">private note</textarea>
      <input name="owner" value="me" data-initial="me">
    </form>
  </div>
</div>
```

- [ ] **Step 2: Create the not-collected fixture**

Write `tests/fixtures/comic_detail_my_details_not_collected.html` — a copy of the above but with the collection span's `active` class removed:

```html
<div id="comic-wrap">
  <span class="comic-controller" data-comic="6512949" data-list="2"></span>
  <span class="comic-controller" data-comic="6512949" data-list="1"></span>
  <span class="comic-controller" data-comic="6512949" data-list="3"></span>
  <span class="comic-controller" data-comic="6512949" data-list="5"></span>
  <link rel="canonical" href="https://leagueofcomicgeeks.com/comic/6512949/x">
  <h1>Test Comic #1</h1>
  <div id="my-details">
    <form>
      <input name="comic_id" value="6512949" data-initial="6512949">
      <input name="copy_num" value="1" data-initial="1">
      <input name="quantity" value="1" data-initial="1">
      <input name="date_purchased" value="" data-initial="">
      <input name="price_paid" value="" data-initial="">
      <input name="purchase_store" value="" data-initial="">
      <select name="media" data-initial="">
        <option value="" selected=""></option>
        <option value="1">Print</option>
      </select>
      <input name="signature" value="" data-initial="">
      <input name="storage_box" value="" data-initial="">
      <select name="slabbing" data-initial="0">
        <option value="0" selected="">Raw</option>
      </select>
      <select name="grading" data-initial="0">
        <option value="0" selected="">None</option>
      </select>
      <select name="grading_company" data-initial="">
        <option value="" selected=""></option>
      </select>
      <input name="condition" value="" data-initial="">
      <textarea name="notes" data-initial=""></textarea>
      <input name="owner" value="" data-initial="">
    </form>
  </div>
</div>
```

- [ ] **Step 3: Register the fixtures in `conftest.py`**

Edit `tests/conftest.py`. After the existing `comic_detail_html` fixture (line 37-39), append:

```python
@pytest.fixture
def comic_detail_my_details_html():
    with open(FIXTURES / "comic_detail_my_details.html") as f:
        return f.read()


@pytest.fixture
def comic_detail_my_details_not_collected_html():
    with open(FIXTURES / "comic_detail_my_details_not_collected.html") as f:
        return f.read()
```

- [ ] **Step 4: Write the failing test**

Edit `tests/test_models.py`. At the top of the file, ensure `from locg.models import extract_my_details` is added to the existing import block. At the end of the file, append:

```python
def test_extract_my_details_reads_data_initial(comic_detail_my_details_html):
    """extract_my_details must read data-initial attributes, NOT value or selected."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(comic_detail_my_details_html, "html.parser")
    details = extract_my_details(soup)

    # Populated fields
    assert details["comic_id"] == "6512949"
    assert details["price_paid"] == "99.99"
    assert details["condition"] == "white pages"
    assert details["grading"] == "8.5"
    assert details["grading_company"] == "CGC"
    assert details["media"] == "1"  # select with data-initial
    assert details["slabbing"] == "0"
    assert details["notes"] == "private note"
    assert details["owner"] == "me"
    assert details["storage_box"] == "Box A"
    assert details["purchase_store"] == "LCS"
    assert details["date_purchased"] == "4/1/2026"
    assert details["copy_num"] == "1"
    assert details["quantity"] == "1"
    assert details["signature"] == ""


def test_extract_my_details_ignores_visible_selected_markup(comic_detail_my_details_html):
    """The HTML template marks 'None' as selected even when data-initial is 8.5.
    extract_my_details must trust data-initial, not <option selected>."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(comic_detail_my_details_html, "html.parser")
    details = extract_my_details(soup)

    # If the parser incorrectly read <option selected>, grading would be "0".
    # The correct value (from data-initial on the <select>) is "8.5".
    assert details["grading"] != "0"
    assert details["grading"] == "8.5"
```

- [ ] **Step 5: Run tests — expect FAIL**

```bash
PYTHONPATH=src python3 -m pytest tests/test_models.py::test_extract_my_details_reads_data_initial tests/test_models.py::test_extract_my_details_ignores_visible_selected_markup -v
```

Expected: FAIL with `ImportError: cannot import name 'extract_my_details' from 'locg.models'` or similar.

- [ ] **Step 6: Implement `extract_my_details`**

Append to `src/locg/models.py` (after `extract_comic_detail`, which ends around line 287):

```python
# Fields from the #my-details tab that POST /comic/post_my_details will
# accept. Missing fields on the POST get wiped to server defaults, so
# extract_my_details captures every one we want to round-trip.
_MY_DETAILS_FIELDS = (
    "comic_id", "copy_num", "quantity", "date_purchased", "price_paid",
    "purchase_store", "media", "signature", "storage_box", "slabbing",
    "grading", "grading_company", "condition", "notes", "owner",
)


def extract_my_details(soup: BeautifulSoup) -> dict[str, str]:
    """Extract the currently-stored My Details form values from a comic page.

    Reads the ``data-initial`` attribute on each named form field in the
    ``#my-details`` tab. The HTML template bakes in defaults
    (e.g. the ``grading`` select always marks "None" as selected regardless
    of true state), so the visible ``value``/``selected`` markup is unreliable.
    Only ``data-initial`` reflects the stored server state.

    Missing fields are returned as empty strings so that a subsequent
    round-trip POST does not inadvertently wipe them.
    """
    container = soup.find(id="my-details")
    if container is None:
        return {field: "" for field in _MY_DETAILS_FIELDS}

    result: dict[str, str] = {}
    for field in _MY_DETAILS_FIELDS:
        tag = container.find(attrs={"name": field})
        if tag is None:
            result[field] = ""
            continue
        # data-initial wins. Fall back to value (for <input>) or text (for
        # <textarea>) only when data-initial is entirely absent.
        initial = tag.get("data-initial")
        if initial is None:
            if tag.name == "textarea":
                initial = tag.get_text()
            else:
                initial = tag.get("value", "")
        result[field] = initial
    return result
```

- [ ] **Step 7: Run tests — expect PASS**

```bash
PYTHONPATH=src python3 -m pytest tests/test_models.py::test_extract_my_details_reads_data_initial tests/test_models.py::test_extract_my_details_ignores_visible_selected_markup -v
```

Expected: both PASS.

- [ ] **Step 8: Run full suite — expect no regressions**

```bash
PYTHONPATH=src python3 -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add src/locg/models.py tests/fixtures/comic_detail_my_details.html tests/fixtures/comic_detail_my_details_not_collected.html tests/conftest.py tests/test_models.py
git commit -m "Add extract_my_details parser and fixtures"
```

---

## Task 3: Grade validation whitelist

**Files:**
- Modify: `src/locg/commands.py` — add `VALID_GRADES` module-level constant + `_validate_grade` helper
- Modify: `tests/test_commands.py` — test grade validation

- [ ] **Step 1: Write failing test**

Append to `tests/test_commands.py`:

```python
def test_validate_grade_accepts_cgc_scale():
    """All LOCG CGC grades must validate."""
    from locg.commands import _validate_grade
    for g in ("0", "0.1", "0.3", "0.5", "1.0", "1.5", "1.8", "2.0", "2.5",
              "3.0", "3.5", "4.0", "4.5", "5.0", "5.5", "6.0", "6.5",
              "7.0", "7.5", "8.0", "8.5", "9.0", "9.2", "9.4", "9.6",
              "9.8", "9.9", "10.0"):
        assert _validate_grade(g) == g


def test_validate_grade_rejects_invalid():
    """Non-CGC values raise ValueError with a clear message."""
    import pytest
    from locg.commands import _validate_grade
    with pytest.raises(ValueError, match="Invalid grade"):
        _validate_grade("11.0")
    with pytest.raises(ValueError, match="Invalid grade"):
        _validate_grade("nine")
    with pytest.raises(ValueError, match="Invalid grade"):
        _validate_grade("9.3")  # not on LOCG's CGC scale
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
PYTHONPATH=src python3 -m pytest tests/test_commands.py::test_validate_grade_accepts_cgc_scale tests/test_commands.py::test_validate_grade_rejects_invalid -v
```

Expected: FAIL — `ImportError: cannot import name '_validate_grade'`.

- [ ] **Step 3: Add `VALID_GRADES` and `_validate_grade`**

Edit `src/locg/commands.py`. Just below the `VALID_LISTS = list(LIST_IDS.keys())` line (currently at line 28), add:

```python
# LOCG CGC scale values accepted by POST /comic/post_my_details.
# "0" is an explicit "None" (no grade assigned); others match CGC's
# official grade points.  Stored as strings because the server stores
# and returns them as strings.
VALID_GRADES = frozenset({
    "0", "0.1", "0.3", "0.5", "1.0", "1.5", "1.8", "2.0", "2.5",
    "3.0", "3.5", "4.0", "4.5", "5.0", "5.5", "6.0", "6.5",
    "7.0", "7.5", "8.0", "8.5", "9.0", "9.2", "9.4", "9.6",
    "9.8", "9.9", "10.0",
})


def _validate_grade(value: str) -> str:
    """Return *value* if it is on the LOCG CGC scale, else raise ValueError."""
    if value not in VALID_GRADES:
        valid = ", ".join(sorted(VALID_GRADES, key=lambda s: float(s)))
        raise ValueError(
            f"Invalid grade {value!r}. Valid grades: {valid}"
        )
    return value


def _validate_price(value: str) -> str:
    """Coerce *value* via float(); return the canonical string form.

    LOCG stores price_paid as a free-text string but truncates to two
    decimal places in the UI.  We reformat with ``f"{float(v):g}"`` which
    keeps integers tidy (``"390"`` not ``"390.0"``) and decimals readable.
    """
    try:
        f = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid price {value!r}: must be numeric")
    return f"{f:g}"
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
PYTHONPATH=src python3 -m pytest tests/test_commands.py::test_validate_grade_accepts_cgc_scale tests/test_commands.py::test_validate_grade_rejects_invalid -v
```

Expected: both PASS.

- [ ] **Step 5: Add price validation test**

Append to `tests/test_commands.py`:

```python
def test_validate_price_formats_cleanly():
    from locg.commands import _validate_price
    assert _validate_price("390") == "390"
    assert _validate_price("390.00") == "390"
    assert _validate_price("9.99") == "9.99"
    assert _validate_price("0") == "0"


def test_validate_price_rejects_non_numeric():
    import pytest
    from locg.commands import _validate_price
    with pytest.raises(ValueError, match="Invalid price"):
        _validate_price("free")
```

- [ ] **Step 6: Run tests — expect PASS**

```bash
PYTHONPATH=src python3 -m pytest tests/test_commands.py::test_validate_price_formats_cleanly tests/test_commands.py::test_validate_price_rejects_non_numeric -v
```

Expected: PASS (already implemented in Step 3).

- [ ] **Step 7: Commit**

```bash
git add src/locg/commands.py tests/test_commands.py
git commit -m "Add CGC grade whitelist and price validation helpers"
```

---

## Task 4: `cmd_add` grade/price extension

**Files:**
- Modify: `src/locg/commands.py:414-427` — rewrite `cmd_add` signature and body
- Modify: `tests/test_commands.py` — add 3 new tests

- [ ] **Step 1: Write failing test — add with grade and price success**

Append to `tests/test_commands.py`:

```python
def test_cmd_add_with_grade_and_price_calls_both_endpoints(mock_client):
    """cmd_add with grade and price must POST both my_list_move then post_my_details."""
    # First POST: my_list_move (success)
    move_resp = MagicMock()
    move_resp.json.return_value = {"status": "ok"}
    move_resp.status_code = 200
    # Second POST: post_my_details (success)
    detail_resp = MagicMock()
    detail_resp.json.return_value = {"type": "success", "text": "This comic has been updated."}
    detail_resp.status_code = 200
    mock_client.post.side_effect = [move_resp, detail_resp]

    result = cmd_add(mock_client, "collection", 12345, grade="8.5", price="390")

    # Two POSTs in order: move then details
    assert mock_client.post.call_count == 2
    first_call = mock_client.post.call_args_list[0]
    assert first_call[0][0] == "/comic/my_list_move"
    assert first_call[1]["data"] == {"comic_id": 12345, "list_id": 2, "action_id": 1}

    second_call = mock_client.post.call_args_list[1]
    assert second_call[0][0] == "/comic/post_my_details"
    # Minimum payload: comic_id plus only the supplied fields
    assert second_call[1]["data"] == {
        "comic_id": 12345,
        "grading": "8.5",
        "price_paid": "390",
    }

    assert result == {
        "status": "ok",
        "added": True,
        "details_saved": True,
        "text": "This comic has been updated.",
    }
```

- [ ] **Step 2: Run — expect FAIL**

```bash
PYTHONPATH=src python3 -m pytest tests/test_commands.py::test_cmd_add_with_grade_and_price_calls_both_endpoints -v
```

Expected: FAIL (current `cmd_add` doesn't accept `grade`/`price` kwargs).

- [ ] **Step 3: Rewrite `cmd_add`**

Replace `cmd_add` at `src/locg/commands.py:414-427` with:

```python
def cmd_add(
    client: LOCGClient,
    list_name: str,
    comic_id: int,
    grade: Optional[str] = None,
    price: Optional[str] = None,
) -> dict[str, Any]:
    """Add a comic to a list, optionally recording grade and price."""
    client.require_auth()
    if list_name not in LIST_IDS:
        return {"error": f"Invalid list '{list_name}'. Valid lists: {', '.join(VALID_LISTS)}"}

    # grade/price only meaningful for collection
    if (grade is not None or price is not None) and list_name != "collection":
        return {"error": "--grade and --price are only valid when adding to collection"}

    # Step 1: add to list
    move_resp = client.post("/comic/my_list_move", data={
        "comic_id": comic_id,
        "list_id": LIST_IDS[list_name],
        "action_id": 1,
    })
    try:
        move_body = move_resp.json()
    except Exception:
        move_body = {"status": "ok" if move_resp.status_code == 200 else "error"}

    # If move failed, return unchanged — no point attempting details.
    is_move_ok = (
        move_body.get("status") == "ok"
        or move_body.get("type") == "success"
        or (move_resp.status_code == 200 and "error" not in move_body)
    )
    if not is_move_ok:
        return move_body

    # Step 2: if no details supplied, done.
    if grade is None and price is None:
        return move_body

    # Step 3: POST details (minimum payload — comic is new, nothing to preserve).
    payload: dict[str, Any] = {"comic_id": comic_id}
    if grade is not None:
        payload["grading"] = grade
    if price is not None:
        payload["price_paid"] = price

    detail_resp = client.post("/comic/post_my_details", data=payload)
    try:
        detail_body = detail_resp.json()
    except Exception:
        detail_body = {"type": "error", "text": f"HTTP {detail_resp.status_code}"}

    if detail_resp.status_code == 200 and detail_body.get("type") == "success":
        return {
            "status": "ok",
            "added": True,
            "details_saved": True,
            "text": detail_body.get("text", "This comic has been updated."),
        }

    return {
        "status": "partial",
        "added": True,
        "details_saved": False,
        "details_error": detail_body.get("text", f"HTTP {detail_resp.status_code}"),
    }
```

- [ ] **Step 4: Run test — expect PASS**

```bash
PYTHONPATH=src python3 -m pytest tests/test_commands.py::test_cmd_add_with_grade_and_price_calls_both_endpoints -v
```

Expected: PASS.

- [ ] **Step 5: Write test for partial-success on detail failure**

Append to `tests/test_commands.py`:

```python
def test_cmd_add_details_failure_surfaces_partial(mock_client):
    """If post_my_details fails after the comic is added, cmd_add returns partial."""
    move_resp = MagicMock()
    move_resp.json.return_value = {"status": "ok"}
    move_resp.status_code = 200
    detail_resp = MagicMock()
    detail_resp.json.return_value = {"type": "error", "text": "Something went wrong."}
    detail_resp.status_code = 500
    mock_client.post.side_effect = [move_resp, detail_resp]

    result = cmd_add(mock_client, "collection", 12345, grade="8.5")

    assert result["status"] == "partial"
    assert result["added"] is True
    assert result["details_saved"] is False
    assert "Something went wrong" in result["details_error"]
```

- [ ] **Step 6: Run test — expect PASS**

```bash
PYTHONPATH=src python3 -m pytest tests/test_commands.py::test_cmd_add_details_failure_surfaces_partial -v
```

Expected: PASS.

- [ ] **Step 7: Write test for no-details case (backwards compat)**

Append to `tests/test_commands.py`:

```python
def test_cmd_add_without_details_only_calls_move(mock_client):
    """cmd_add without grade/price must behave exactly like the old version."""
    move_resp = MagicMock()
    move_resp.json.return_value = {"status": "ok"}
    move_resp.status_code = 200
    mock_client.post.return_value = move_resp

    result = cmd_add(mock_client, "collection", 12345)

    assert mock_client.post.call_count == 1
    assert mock_client.post.call_args[0][0] == "/comic/my_list_move"
    assert result == {"status": "ok"}


def test_cmd_add_rejects_grade_on_non_collection(mock_client):
    result = cmd_add(mock_client, "pull", 12345, grade="8.5")
    assert "error" in result
    assert "collection" in result["error"].lower()
    mock_client.post.assert_not_called()
```

- [ ] **Step 8: Run tests — expect PASS**

```bash
PYTHONPATH=src python3 -m pytest tests/test_commands.py::test_cmd_add_without_details_only_calls_move tests/test_commands.py::test_cmd_add_rejects_grade_on_non_collection -v
```

Expected: PASS.

- [ ] **Step 9: Run full suite**

```bash
PYTHONPATH=src python3 -m pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add src/locg/commands.py tests/test_commands.py
git commit -m "Extend cmd_add to accept --grade and --price for collection"
```

---

## Task 5: CLI wiring for `add --grade/--price` + partial-success stderr

**Files:**
- Modify: `src/locg/cli.py` — add `--grade`/`--price` flags to `add` subparser (lines 109-111), validate in `main()` before calling `cmd_add`, handle partial-success result
- Modify: `tests/test_cli.py` — add CLI tests

- [ ] **Step 1: Write failing test for CLI add --grade/--price flag parsing**

Read the existing `tests/test_cli.py` shape first to match its conventions:

```bash
head -40 tests/test_cli.py
```

Then append (adjust imports if needed based on the existing file's patterns):

```python
def test_cli_add_collection_with_grade_and_price(monkeypatch, capsys):
    """`locg add collection <id> --grade 8.5 --price 390` must call cmd_add with those values."""
    import sys
    from locg import cli
    calls = {}

    def fake_cmd_add(client, list_name, comic_id, grade=None, price=None):
        calls["args"] = (list_name, comic_id, grade, price)
        return {"status": "ok", "added": True, "details_saved": True, "text": "done"}

    monkeypatch.setattr(cli, "cmd_add", fake_cmd_add)
    monkeypatch.setattr(cli, "LOCGClient", MagicMock)
    monkeypatch.setattr(sys, "argv", ["locg", "add", "collection", "12345", "--grade", "8.5", "--price", "390"])

    try:
        cli.main()
    except SystemExit as e:
        # Only acceptable if code is 0
        assert e.code in (None, 0)

    assert calls["args"] == ("collection", 12345, "8.5", "390")
```

Also add these imports at the top of `test_cli.py` if not already present:

```python
import pytest
from unittest.mock import MagicMock
```

- [ ] **Step 2: Run — expect FAIL**

```bash
PYTHONPATH=src python3 -m pytest tests/test_cli.py::test_cli_add_collection_with_grade_and_price -v
```

Expected: FAIL — either argparse rejects `--grade`, or `cmd_add` isn't called with the new args.

- [ ] **Step 3: Add `--grade`/`--price` to the `add` subparser**

Edit `src/locg/cli.py` around line 108-111. Replace the `add` subparser block with:

```python
    # add
    p = sub.add_parser("add", parents=[common], help="Add a comic to a list")
    p.add_argument("list", choices=VALID_LISTS, help="Target list")
    p.add_argument("comic_id", type=int, help="Comic ID")
    p.add_argument("--grade", help="LOCG CGC grade (collection only, e.g. 8.5, 9.2, 9.8)")
    p.add_argument("--price", help="Purchase price (collection only, numeric)")
```

- [ ] **Step 4: Wire `cmd_add` invocation to pass grade/price**

In `main()`, locate the `elif args.command == "add":` branch (currently line 199-200). Replace it with:

```python
        elif args.command == "add":
            # Validate grade/price up-front so users get argparse-speed feedback.
            grade = getattr(args, "grade", None)
            price = getattr(args, "price", None)
            if (grade is not None or price is not None) and args.list != "collection":
                die("--grade and --price are only valid when adding to collection")
            if grade is not None:
                try:
                    from locg.commands import _validate_grade
                    grade = _validate_grade(grade)
                except ValueError as e:
                    die(str(e))
            if price is not None:
                try:
                    from locg.commands import _validate_price
                    price = _validate_price(price)
                except ValueError as e:
                    die(str(e))
            result = cmd_add(client, args.list, args.comic_id, grade=grade, price=price)
            # Surface partial-success: stdout gets the machine-readable dict
            # (handled by the generic `output()` call below); stderr gets a
            # human-readable error line so users don't miss that details failed.
            if isinstance(result, dict) and result.get("status") == "partial":
                output(result, pretty=args.pretty, fields=fields)
                json.dump(
                    {"error": f"Comic added but details not saved: {result.get('details_error', 'unknown')}"},
                    sys.stderr,
                )
                print(file=sys.stderr)
                sys.exit(1)
```

- [ ] **Step 5: Run test — expect PASS**

```bash
PYTHONPATH=src python3 -m pytest tests/test_cli.py::test_cli_add_collection_with_grade_and_price -v
```

Expected: PASS.

- [ ] **Step 6: Write failing test for partial-success exit code**

Append to `tests/test_cli.py`:

```python
def test_cli_add_partial_success_exits_1(monkeypatch, capsys):
    """Partial success must write JSON to stdout AND error JSON to stderr AND exit 1."""
    import sys
    from locg import cli

    def fake_cmd_add(client, list_name, comic_id, grade=None, price=None):
        return {
            "status": "partial",
            "added": True,
            "details_saved": False,
            "details_error": "Server rejected grade",
        }

    monkeypatch.setattr(cli, "cmd_add", fake_cmd_add)
    monkeypatch.setattr(cli, "LOCGClient", MagicMock)
    monkeypatch.setattr(sys, "argv", ["locg", "add", "collection", "12345", "--grade", "8.5"])

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1

    captured = capsys.readouterr()
    # stdout got the machine-readable dict
    assert "partial" in captured.out
    assert "details_error" in captured.out
    # stderr got a human-readable error
    assert "Server rejected grade" in captured.err
    assert "Comic added but details not saved" in captured.err
```

Ensure `pytest` is imported at the top of the file if it isn't already.

- [ ] **Step 7: Run — expect PASS**

```bash
PYTHONPATH=src python3 -m pytest tests/test_cli.py::test_cli_add_partial_success_exits_1 -v
```

Expected: PASS.

- [ ] **Step 8: Write test for CLI-level grade validation**

Append to `tests/test_cli.py`:

```python
def test_cli_add_rejects_bogus_grade(monkeypatch, capsys):
    import sys
    from locg import cli
    monkeypatch.setattr(cli, "LOCGClient", MagicMock)
    monkeypatch.setattr(sys, "argv", ["locg", "add", "collection", "12345", "--grade", "11.0"])

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1

    captured = capsys.readouterr()
    assert "Invalid grade" in captured.err


def test_cli_add_rejects_grade_on_non_collection_list(monkeypatch, capsys):
    import sys
    from locg import cli
    monkeypatch.setattr(cli, "LOCGClient", MagicMock)
    monkeypatch.setattr(sys, "argv", ["locg", "add", "pull", "12345", "--grade", "8.5"])

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1

    captured = capsys.readouterr()
    assert "collection" in captured.err.lower()
```

- [ ] **Step 9: Run — expect PASS**

```bash
PYTHONPATH=src python3 -m pytest tests/test_cli.py -v -k "add"
```

Expected: all four new CLI tests pass.

- [ ] **Step 10: Run full suite**

```bash
PYTHONPATH=src python3 -m pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 11: Commit**

```bash
git add src/locg/cli.py tests/test_cli.py
git commit -m "Wire --grade and --price into locg add CLI"
```

---

## Task 6: `cmd_update` implementation

**Files:**
- Modify: `src/locg/commands.py` — add `cmd_update` at the bottom
- Modify: `tests/test_commands.py` — add tests

- [ ] **Step 1: Write failing test for update merging data-initial with flags**

Append to `tests/test_commands.py`:

```python
def test_cmd_update_fetches_then_merges(mock_client, comic_detail_my_details_html):
    """cmd_update must fetch the page, parse data-initial, merge flags, then POST."""
    from locg.commands import cmd_update

    # GET returns the detail page (collected version)
    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.text = comic_detail_my_details_html
    mock_client.get.return_value = get_resp

    # POST succeeds
    post_resp = MagicMock()
    post_resp.status_code = 200
    post_resp.json.return_value = {"type": "success", "text": "Updated."}
    mock_client.post.return_value = post_resp

    result = cmd_update(mock_client, 6512949, grade="9.2", price="500", condition="pristine")

    mock_client.get.assert_called_once_with("/comic/6512949/x")
    assert mock_client.post.call_count == 1
    post_call = mock_client.post.call_args
    assert post_call[0][0] == "/comic/post_my_details"
    payload = post_call[1]["data"]

    # User's flags win
    assert payload["grading"] == "9.2"
    assert payload["price_paid"] == "500"
    assert payload["condition"] == "pristine"

    # Other fields preserved from data-initial
    assert payload["comic_id"] == "6512949"
    assert payload["date_purchased"] == "4/1/2026"
    assert payload["media"] == "1"
    assert payload["grading_company"] == "CGC"
    assert payload["notes"] == "private note"
    assert payload["storage_box"] == "Box A"

    assert result == {"type": "success", "text": "Updated."}


def test_cmd_update_only_condition_preserves_grade(mock_client, comic_detail_my_details_html):
    """Supplying only --condition must leave grading untouched (from data-initial)."""
    from locg.commands import cmd_update

    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.text = comic_detail_my_details_html
    mock_client.get.return_value = get_resp

    post_resp = MagicMock()
    post_resp.status_code = 200
    post_resp.json.return_value = {"type": "success", "text": "Updated."}
    mock_client.post.return_value = post_resp

    cmd_update(mock_client, 6512949, condition="new note")

    payload = mock_client.post.call_args[1]["data"]
    assert payload["condition"] == "new note"
    assert payload["grading"] == "8.5"  # preserved from data-initial
    assert payload["price_paid"] == "99.99"  # preserved
```

- [ ] **Step 2: Run — expect FAIL**

```bash
PYTHONPATH=src python3 -m pytest tests/test_commands.py::test_cmd_update_fetches_then_merges tests/test_commands.py::test_cmd_update_only_condition_preserves_grade -v
```

Expected: FAIL — `cannot import name 'cmd_update'`.

- [ ] **Step 3: Implement `cmd_update`**

Append to `src/locg/commands.py` (after `cmd_add`, before `cmd_remove`):

```python
def cmd_update(
    client: LOCGClient,
    comic_id: int,
    grade: Optional[str] = None,
    price: Optional[str] = None,
    condition: Optional[str] = None,
) -> dict[str, Any]:
    """Update grade / price / condition on a comic already in the user's collection.

    Because POST /comic/post_my_details wipes any field it does not receive,
    we must fetch the current server state first, merge the user's flags on
    top, then POST the full dict.
    """
    from locg.models import extract_comic_lists, extract_my_details

    client.require_auth()

    if grade is None and price is None and condition is None:
        return {"error": "update: at least one of --grade, --price, --condition is required"}

    resp = client.get(f"/comic/{comic_id}/x")
    if resp.status_code == 404:
        return {"error": f"Comic {comic_id} not found"}

    soup = parse_page(resp.text)

    # Reject update on comics not in the user's collection.  The server
    # accepts a POST for any comic_id and returns success, which would
    # create orphan detail records.
    entry = extract_comic_lists(soup)
    lists = entry.get("lists") or {}
    if not lists.get("collection"):
        return {
            "error": (
                f"Comic {comic_id} is not in your collection. "
                f"Use: locg add collection {comic_id}"
            )
        }

    # Fetch current server state, merge flags on top.
    payload = extract_my_details(soup)
    if grade is not None:
        payload["grading"] = grade
    if price is not None:
        payload["price_paid"] = price
    if condition is not None:
        payload["condition"] = condition

    post_resp = client.post("/comic/post_my_details", data=payload)
    try:
        body = post_resp.json()
    except Exception:
        body = {"type": "error", "text": f"HTTP {post_resp.status_code}"}
    return body
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
PYTHONPATH=src python3 -m pytest tests/test_commands.py::test_cmd_update_fetches_then_merges tests/test_commands.py::test_cmd_update_only_condition_preserves_grade -v
```

Expected: PASS.

- [ ] **Step 5: Write failing test for non-collection rejection**

Append to `tests/test_commands.py`:

```python
def test_cmd_update_rejects_non_collection_comic(
    mock_client, comic_detail_my_details_not_collected_html
):
    from locg.commands import cmd_update

    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.text = comic_detail_my_details_not_collected_html
    mock_client.get.return_value = get_resp

    result = cmd_update(mock_client, 6512949, grade="8.5")

    # Error returned, no POST fired
    assert "error" in result
    assert "not in your collection" in result["error"]
    mock_client.post.assert_not_called()


def test_cmd_update_no_flags_errors():
    from unittest.mock import MagicMock as _MM
    from locg.commands import cmd_update
    client = _MM()
    client.require_auth = _MM()
    result = cmd_update(client, 12345)
    assert "error" in result
    assert "at least one" in result["error"]
    client.get.assert_not_called()


def test_cmd_update_comic_not_found(mock_client):
    from locg.commands import cmd_update
    get_resp = MagicMock()
    get_resp.status_code = 404
    mock_client.get.return_value = get_resp

    result = cmd_update(mock_client, 99999, grade="8.5")
    assert "error" in result
    assert "not found" in result["error"]
    mock_client.post.assert_not_called()
```

- [ ] **Step 6: Run — expect PASS**

```bash
PYTHONPATH=src python3 -m pytest tests/test_commands.py::test_cmd_update_rejects_non_collection_comic tests/test_commands.py::test_cmd_update_no_flags_errors tests/test_commands.py::test_cmd_update_comic_not_found -v
```

Expected: all PASS.

- [ ] **Step 7: Run full suite**

```bash
PYTHONPATH=src python3 -m pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/locg/commands.py tests/test_commands.py
git commit -m "Add cmd_update with fetch-merge-POST and non-collection rejection"
```

---

## Task 7: CLI wiring for `update` subcommand

**Files:**
- Modify: `src/locg/cli.py` — add `update` subparser after `remove`, add import for `cmd_update`, add dispatch branch
- Modify: `tests/test_cli.py` — add CLI tests

- [ ] **Step 1: Write failing test for update CLI**

Append to `tests/test_cli.py`:

```python
def test_cli_update_passes_all_flags(monkeypatch, capsys):
    import sys
    from locg import cli
    calls = {}

    def fake_cmd_update(client, comic_id, grade=None, price=None, condition=None):
        calls["args"] = (comic_id, grade, price, condition)
        return {"type": "success", "text": "ok"}

    monkeypatch.setattr(cli, "cmd_update", fake_cmd_update, raising=False)
    monkeypatch.setattr(cli, "LOCGClient", MagicMock)
    monkeypatch.setattr(sys, "argv", [
        "locg", "update", "12345",
        "--grade", "8.5", "--price", "390", "--condition", "white pages",
    ])

    try:
        cli.main()
    except SystemExit as e:
        assert e.code in (None, 0)

    assert calls["args"] == (12345, "8.5", "390", "white pages")


def test_cli_update_requires_at_least_one_flag(monkeypatch, capsys):
    import sys
    from locg import cli
    monkeypatch.setattr(cli, "LOCGClient", MagicMock)
    monkeypatch.setattr(sys, "argv", ["locg", "update", "12345"])

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "at least one" in captured.err.lower()


def test_cli_update_rejects_bogus_grade(monkeypatch, capsys):
    import sys
    from locg import cli
    monkeypatch.setattr(cli, "LOCGClient", MagicMock)
    monkeypatch.setattr(sys, "argv", ["locg", "update", "12345", "--grade", "11.0"])

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Invalid grade" in captured.err
```

- [ ] **Step 2: Run — expect FAIL**

```bash
PYTHONPATH=src python3 -m pytest tests/test_cli.py::test_cli_update_passes_all_flags -v
```

Expected: FAIL — argparse doesn't know the `update` command.

- [ ] **Step 3: Add import for `cmd_update`**

Edit `src/locg/cli.py`. In the existing import block from `locg.commands` (starting at line 12), add `cmd_update` to the imported names (keep alphabetical/logical order):

```python
from locg.commands import (
    VALID_LISTS,
    cmd_add,
    cmd_check_lists,
    cmd_collection,
    cmd_collection_has,
    cmd_comic,
    cmd_login,
    cmd_pull_list,
    cmd_read_list,
    cmd_releases,
    cmd_remove,
    cmd_search,
    cmd_series,
    cmd_update,
    cmd_wish_list,
)
```

- [ ] **Step 4: Add the `update` subparser**

Edit `src/locg/cli.py`. After the `remove` subparser block (currently lines 113-116), add:

```python
    # update
    p = sub.add_parser("update", parents=[common], help="Update grade/price/condition on a comic in your collection")
    p.add_argument("id", type=int, help="Comic ID")
    p.add_argument("--grade", help="LOCG CGC grade (e.g. 8.5, 9.2, 9.8)")
    p.add_argument("--price", help="Purchase price (numeric)")
    p.add_argument("--condition", help="Free-text condition notes")
```

- [ ] **Step 5: Add dispatch branch with validation**

In `main()`, after the `elif args.command == "remove":` branch (line 201-202), add:

```python
        elif args.command == "update":
            grade = getattr(args, "grade", None)
            price = getattr(args, "price", None)
            condition = getattr(args, "condition", None)
            if grade is None and price is None and condition is None:
                die("update: at least one of --grade, --price, --condition is required")
            if grade is not None:
                try:
                    from locg.commands import _validate_grade
                    grade = _validate_grade(grade)
                except ValueError as e:
                    die(str(e))
            if price is not None:
                try:
                    from locg.commands import _validate_price
                    price = _validate_price(price)
                except ValueError as e:
                    die(str(e))
            result = cmd_update(client, args.id, grade=grade, price=price, condition=condition)
```

- [ ] **Step 6: Run new CLI tests — expect PASS**

```bash
PYTHONPATH=src python3 -m pytest tests/test_cli.py -v -k "update"
```

Expected: all three new tests pass.

- [ ] **Step 7: Manual sanity check — help output**

```bash
PYTHONPATH=src python3 -m locg update --help
```

Expected: prints usage showing `--grade`, `--price`, `--condition`, and positional `id`.

- [ ] **Step 8: Run full suite**

```bash
PYTHONPATH=src python3 -m pytest tests/ -v
```

Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add src/locg/cli.py tests/test_cli.py
git commit -m "Wire locg update subcommand into CLI"
```

---

## Task 8: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` — add `--grade`/`--price` example to "Managing lists", add `locg update` section

- [ ] **Step 1: Update "Managing lists" section**

Edit `CLAUDE.md`. Locate this block (currently around line 45-50 in the "Managing lists" section):

```markdown
# Add/remove a comic to/from a list (pull, collection, wish, read)
locg add collection <comic_id>
locg remove wish <comic_id>
```

Replace with:

```markdown
# Add/remove a comic to/from a list (pull, collection, wish, read)
locg add collection <comic_id>
locg remove wish <comic_id>

# Add to collection with grade and price at the same time
locg add collection <comic_id> --grade 8.5 --price 390

# Update grade, price, or condition notes on a comic already in collection
locg update <comic_id> --grade 9.2 --price 500 --condition "white pages"
```

- [ ] **Step 2: Update the "Authentication" section**

Find the sentence about session expiry:

```markdown
Session cookies are stored at `~/.config/locg/cookies.json`. Sessions can expire server-side; if you get "Session expired", run `locg login` again.
```

That line is already correct, but add this right after it:

```markdown
Every authenticated command now verifies the session server-side once per invocation (one extra GET). Expired sessions produce `{"error": "Session expired. Run: locg login"}` on stderr and exit 1.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "Document --grade/--price and locg update in CLAUDE.md"
```

- [ ] **Step 4: Final full-suite run**

```bash
PYTHONPATH=src python3 -m pytest tests/ -v
```

Expected: all tests pass across the full suite.

---

## Self-Review Checklist (for the implementer)

After finishing all 8 tasks, verify:

- [ ] `locg add collection 12345 --grade 8.5 --price 390` works end-to-end (can be run manually against a test account if credentials are available).
- [ ] `locg update 12345 --grade 9.2` works and preserves existing `price_paid` / `condition` / `notes`.
- [ ] `locg update 12345 --grade 8.5` on a comic NOT in collection fails with a clear error and does NOT hit `post_my_details`.
- [ ] After an expired session, every authenticated command (`add`, `remove`, `update`, `collection`, `pull-list`, `wish-list`, `read-list`, `check`, `collection has`) surfaces "Session expired. Run: locg login" and exits 1 — not a 500.
- [ ] `locg add collection 12345 --grade 9.2` when `post_my_details` fails writes the `{"status":"partial",...}` dict to stdout, a human-readable error to stderr, and exits 1.
- [ ] All of `--grade 11.0`, `--price free`, `--grade 8.5 pull`, `update` with no flags fail fast with clear messages on stderr (no network call).
- [ ] `PYTHONPATH=src python3 -m pytest tests/ -v` is green.
