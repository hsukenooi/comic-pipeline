# Collection Details: `add --grade/--price`, `update`, and server-side `require_auth`

Date: 2026-04-20
Branch: `hsukenooi/collection-details`

## Problem

Three related gaps in the `locg` CLI:

1. `locg add collection <id>` cannot record grade or price at the same time. Users who want to capture purchase details for a new comic must call a separate, undocumented endpoint afterward.
2. There is no way to update grade, price, or condition notes on a comic already in the collection. The underlying `/comic/post_my_details` endpoint exists but is not exposed.
3. `require_auth` only checks that `ci_session` is present in the local cookie file. When the server-side session has expired, write commands fail with a generic 500 instead of a clear "re-login" message.

## Endpoint reference (verified empirically against the live site)

### `POST /comic/my_list_move`

Existing. Form fields: `comic_id`, `list_id`, `action_id` (1 = add, 0 = remove). Used unchanged.

### `POST /comic/post_my_details`

Not previously used in this CLI. Discovered by reading `jquery-general.min.js` (`saveMyDetails` function) and inspecting the "My Details" tab of a comic page.

- **Required fields:** `comic_id`. Missing `comic_id` → HTTP 500 with `{"type":"error","text":"Something went wrong. Please try again later."}`.
- **Optional fields** (all populate the form on the comic page):
  `copy_num`, `quantity`, `date_purchased` (M/D/YYYY), `price_paid` (string, free text), `purchase_store`, `media` (1=Print / 2=Digital / 3=Both), `signature`, `storage_box`, `slabbing` (0=Raw / 1=Slabbed), `grading` (string from the LOCG CGC scale — `"0"` = None, `"0.1"`, `"0.3"`, `"0.5"`, `"1.0"`, `"1.5"`, `"1.8"`, `"2.0"`, `"2.5"`, `"3.0"`, `"3.5"`, `"4.0"`, `"4.5"`, `"5.0"`, `"5.5"`, `"6.0"`, `"6.5"`, `"7.0"`, `"7.5"`, `"8.0"`, `"8.5"`, `"9.0"`, `"9.2"`, `"9.4"`, `"9.6"`, `"9.8"`, `"9.9"`, `"10.0"`), `grading_company` (empty / CGC / CBCS / PGX / Other), `condition` (free text — the "grade/condition notes" field), `notes` (free text; summernote-rich), `owner`.
- **Success response:** HTTP 200, JSON `{"type":"success","text":"This comic has been updated.","detail":"..."}`.
- **Error response:** HTTP 500, JSON `{"type":"error","text":"..."}`.
- **Behaviour when a field is omitted:** the server treats the missing field as blank and writes the default. Omitted fields are *wiped*, not preserved. Verified by POSTing only `{comic_id, condition}` and observing that a previously-set `grading=8.5` reverted to `0.0` and `price_paid=99.99` reverted to `""`.
- **Server does not validate that the comic exists or is owned.** A bogus `comic_id` returns `{"type":"success",...}`. Membership enforcement is LOCG's responsibility; we trust the success signal.

### Reading existing details from `GET /comic/<id>/x`

The `#my-details` tab embeds the form. Each `<select>` / `<input>` / `<textarea>` carries a `data-initial` attribute holding the currently-stored server value. **Parsers must read `data-initial`, not the visible `value`/`selected` markup**, because the HTML template bakes in defaults that do not reflect stored state (e.g. the `grading` select always marks the "None" option as `selected=""` regardless of the real value).

### Session validity signal

Anonymous responses to authenticated endpoints (e.g. `GET /comic/get_comics?list=collection`) contain an element with `data-user="0"`. Logged-in responses do not. The existing `_check_session_valid` and `LOCGClient.verify_session` methods already use this signal.

## Goals

- `locg add collection <id> --grade 8.5 --price 390` works end-to-end.
- `locg update <id> --grade 8.5 --price 390 --condition "white pages"` works on comics already in the collection.
- If the detail-save step fails after the comic is added, the user is told clearly that the comic was added but details were not saved.
- Expired sessions surface a clear "Session expired. Run: `locg login`" message instead of a 500, across every auth-required command.
- No more than one additional HTTP request per CLI invocation for the session check.

## Non-goals

- Exposing every field of the My Details form as a CLI flag. Only `grade`, `price`, and `condition` are wired through.
- Reading-back tag (`tags[]` / `clists[]`) or rich-notes editing.
- Automatic re-login on expired session.

## Design

### 1. `locg add collection <id> --grade G --price P`

CLI changes (`cli.py`):

- `add` subparser gains two optional flags: `--grade` and `--price`. Both are only meaningful when `list == "collection"`. If provided with any other list, error out before making any request.

Command changes (`commands.py`):

- `cmd_add(client, list_name, comic_id, grade=None, price=None)`:
  1. Call `require_auth` (with the new server-side check — see §3).
  2. `POST /comic/my_list_move` as today.
  3. If the move response is not success, return it unchanged.
  4. If neither `grade` nor `price` was supplied, return the move response.
  5. Otherwise build a minimum payload: `{comic_id}` plus whichever of `grading=<grade>`, `price_paid=<price>` were supplied. Post to `/comic/post_my_details`.
  6. If the detail response has `type == "success"`, return `{"status": "ok", "added": true, "details_saved": true, ...server text...}`.
  7. If the detail response fails (non-200 or `type != "success"`), return `{"status": "partial", "added": true, "details_saved": false, "details_error": "<server text or HTTP status>"}` and ensure the CLI exits non-zero for this partial state.

Why minimum payload (not fetch-merge-post): the comic was just added, so it has no pre-existing details to preserve. Wiping "missing" fields back to defaults is harmless.

Validation:

- `--grade`: whitelist against the LOCG CGC scale listed above. `"0"` is valid (explicit "None"). Unknown values → clean `die("Invalid --grade value. Valid: 0, 0.1, 0.3, …, 10.0")`, exit 1.
- `--price`: coerced via `float()`. Non-numeric → `die("Invalid --price value: must be numeric")`, exit 1. The server expects a string, so we re-format with `f"{float(p):g}"` (keeps `"390"` tidy, allows decimals).

### 2. `locg update <id>`

CLI changes (`cli.py`):

- New subparser `update`.
  - Positional: `id` (int, comic ID).
  - Flags: `--grade`, `--price`, `--condition`. All optional; at least one must be provided.

Command changes (`commands.py`, new function `cmd_update`):

1. Call `require_auth`.
2. If none of `--grade`, `--price`, `--condition` was provided, `die("update: at least one of --grade, --price, --condition is required")` with exit 1.
3. `GET /comic/<id>/x`. If 404, `die("Comic <id> not found")` with exit 1.
4. Parse list membership from the fetched page via the existing `extract_comic_lists` helper. If `collection` is not in the returned lists, `die("Comic <id> is not in your collection. Use: locg add collection <id>", code=1)`. This reuses the page we already fetched — zero extra HTTP cost. Rationale: `post_my_details` accepts any `comic_id` and returns success even for comics not in the user's collection, so without this check `update` would silently write orphan detail records.
5. Parse the `#my-details` tab via a new helper `extract_my_details(soup) -> dict` in `models.py`. The helper walks the tab's `<input>`, `<select>`, `<textarea>` tags and reads each name's **`data-initial`** attribute.
6. Merge: overwrite `grading` / `price_paid` / `condition` with the supplied flag values. Flags not given leave the existing value in place.
7. `POST /comic/post_my_details` with the merged dict.
8. Return the server's JSON response.

Fields that `extract_my_details` must capture (these are the `post_my_details` form fields that hold per-user state and can be wiped by a partial POST):

`comic_id`, `copy_num`, `quantity`, `date_purchased`, `price_paid`, `purchase_store`, `media`, `signature`, `storage_box`, `slabbing`, `grading`, `grading_company`, `condition`, `notes`, `owner`.

Tag fields (`tags[]`, `tags_list[]`, `new_tag`, `new_tag_type`, `clists`) are deliberately **not** round-tripped. We have not empirically verified the effect of omitting them on pre-existing tag state (my test comic had no custom tags). The `detail` string in the server response (e.g. `" added tags () for ..."`) is passed through to the user so any server-side tag change is observable. If users report tag loss we extend `extract_my_details` to capture them.

Validation for flags:
- `--grade`: whitelist against the LOCG CGC scale (same as §1).
- `--price`: `float()` coerce, reformat with `f"{float(p):g}"`.
- `--condition`: free text, no validation.

On `post_my_details` failure (non-200 or `type != "success"`), `die(details_error, code=1)` — no partial-state handling since `update` does no prior write step.

### 3. Server-side session validation in `require_auth`

Client changes (`client.py`):

- `LOCGClient.__init__` initialises `self._server_auth_verified: Optional[bool] = None`.
- `require_auth()` becomes:
  1. If no `ci_session` cookie → `raise AuthRequired("Not logged in. Run: locg login")`. (Unchanged.)
  2. If `self._server_auth_verified is None`, call `self.verify_session()`. Cache the boolean result (`True` or `False`) only when `verify_session` returns cleanly. If `verify_session` raises (HTTP 429, network error, malformed response), **do not cache** — let the exception propagate up to `cli.main`, which turns it into exit 4 via the generic `Exception` handler. Rationale: transient errors should not poison the cache; the next invocation should try again.
  3. If the cached result is `False` → `raise AuthRequired("Session expired. Run: locg login")`.
- `login()` sets `self._server_auth_verified = True` on success. A fresh login does not need re-verification (login already calls `verify_session` internally).

Cost: exactly one extra GET per CLI invocation for any auth-required command. Read commands already self-validate via `_check_session_valid` inside `_fetch_user_list_page`; the upfront `require_auth` check now makes the expired-session error uniform across every authenticated command, including `add`, `remove`, `update`, `check`, and `collection has`.

Alternative considered and rejected: lazy check only before write commands. Would save one GET per read command. Rejected because (a) it adds a per-command special case, (b) the inconsistency is the exact bug the task calls out, (c) the existing self-validation only fires on list-response markup, not on detail-page or JSON responses used by `check`/`collection has`.

### Error-surfacing details

- `AuthRequired` continues to exit code 1 via `cli.die(str(e), code=1)`.
- `add` partial-success state (comic added, details failed):
  - `cmd_add` returns the dict `{"status":"partial", "added": true, "details_saved": false, "details_error": "..."}`.
  - `cli.main` prints the dict to stdout via `output()` (machine-readable JSON, unchanged pipeline for scripts), AND writes a short error line to stderr via `die`-style `{"error": "Comic added but details not saved: <details_error>"}`, then exits 1.
  - Concrete wiring: after `output(result, ...)`, `cli.main` inspects `result.get("status") == "partial"`, writes the stderr error, and calls `sys.exit(1)`.
  - Rationale: stdout stays machine-parseable (scripts get the full dict), stderr stays human-readable (standard UNIX split), exit 1 signals partial failure. No silent partial success, no lost structured data.

## Tests

Add unit tests that hit the parsers and command helpers with fixture HTML (no network). Reuse the `tests/fixtures/` pattern.

- `tests/fixtures/comic_detail_my_details.html` — a captured `#my-details` tab with known `data-initial` values. Acquired by fetching `/comic/<id>/x` against a logged-in session, extracting the `#my-details` subtree, and checking in. The fixture must cover: a populated `grading` select, a non-empty `price_paid`, a non-empty `condition`, at least one non-default `<select>`, and at least one non-default `<input>`.
- `tests/test_models.py::test_extract_my_details` — asserts `extract_my_details` reads `data-initial` (not `value`/`selected`) for every captured field.
- `tests/test_commands.py::test_cmd_add_with_grade_and_price` — mocks `LOCGClient.post` to record the POST bodies and assert `my_list_move` precedes `post_my_details` with the expected minimum payload.
- `tests/test_commands.py::test_cmd_add_details_failure_surfaces_partial` — mocks `post_my_details` to return a 500; asserts return dict has `status == "partial"` and `added == True`.
- `tests/test_commands.py::test_cmd_update_fetches_then_merges` — mocks `GET /comic/<id>/x` with the fixture and asserts the subsequent `post_my_details` body contains merged fields (user flag wins, other fields preserved from `data-initial`).
- `tests/test_client.py::test_require_auth_verifies_once` — mocks the session GET to return valid markup, calls `require_auth` twice, asserts only one extra GET fired.
- `tests/test_client.py::test_require_auth_expired_session` — mocks the session GET to return `data-user="0"` markup; asserts `AuthRequired("Session expired. Run: locg login")` is raised.
- `tests/test_client.py::test_require_auth_does_not_cache_on_transient_error` — mocks `verify_session` to raise on the first call (simulating 429/network), asserts the exception propagates AND `_server_auth_verified` remains `None` so the next `require_auth` call will retry.
- `tests/test_commands.py::test_cmd_update_rejects_non_collection_comic` — mocks `GET /comic/<id>/x` returning a page where `extract_comic_lists` yields no `collection` entry; asserts `cmd_update` calls `die` before any POST to `post_my_details` fires.

## Documentation

Update `CLAUDE.md` "CLI Usage" and "Managing lists" sections to show the new flags and `locg update` command.

## Out of scope / follow-ups

- Exposing remaining fields (`media`, `slabbing`, `grading_company`, `signature`, `notes`, `storage_box`, `purchase_store`, `date_purchased`, `quantity`) as flags on `update` / `add`.
- A general `locg add pull <id> --grade ...` (grade/price are collection-specific; pull list does not take details).
- Tag editing (`--tag add/remove`) — separate concern.
