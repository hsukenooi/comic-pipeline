"""The rejected-writes ledger, as a FastAPI route class.

Extracted from `routes.py` (BUI-630) — see that module's `LedgerRoute` import
and `router = APIRouter(route_class=LedgerRoute)`. This module has no routes
of its own; it exists purely to keep `routes.py`'s size down while the CI
`typecheck` job (`.github/workflows/ci.yml`) is updated in the same change to
cover this file too, so the extraction never silently drops code from
typechecking (the concrete reason this move was declined the first time it
was proposed).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.routing import APIRoute

from gixen_overlay.db import record_rejected_write
from server.db import write_transaction
from server.main import _get_db_path, _write_locked

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BUI-601: the rejected-writes ledger, as a route class
# ---------------------------------------------------------------------------
#
# WHY A ROUTE CLASS AND NOT MIDDLEWARE. The obvious shape is
# `app.add_middleware(...)`, and it is impossible here. The plugin's
# `register_routes(app)` hook fires from INSIDE the host's lifespan
# (server.main.lifespan -> _invoke_register_routes), and Starlette seals its
# middleware stack before the lifespan body runs — `add_middleware` there
# raises `RuntimeError: Cannot add middleware after an application has
# started` (measured on this repo's fastapi 0.136.3 / starlette 1.2.1).
#
# A custom `APIRoute` has no such ordering constraint, and is strictly better
# scoped: `APIRouter(route_class=LedgerRoute)` covers exactly this plugin's
# routes and never the host gixen-cli ones. `include_router` preserves it
# (FastAPI re-adds each route with `route_class_override=type(route)`), so
# every route declared on `router` in `routes.py` — including ones added
# years from now — is covered with zero per-endpoint code. That is BUI-601's
# acceptance criterion, and it is why the ticket's stale hand-enumeration of
# "four un-persisted 422 sites" (there are 16 in that file today) is an
# argument FOR the generic mechanism rather than a list to work through.

# BUI-707: GET joined this set — a rejected read now lands in the ledger too
# (previously only mutations did; see `test_failed_read_is_not_recorded`'s
# BUI-698 motivation, where ~65 advisory-read 400s left no trace). Recording
# is gated purely on outcome (exception, or status >= 400 — see
# `ledger_handler` below), never on method, so widening this set changes
# nothing about *how* a request is recorded, only *which methods are eligible
# to be*. No route in this plugin currently registers a method outside this
# set; it stays a closed list rather than "every method" so a future
# HEAD/OPTIONS route is passed through unrecorded until someone decides it
# belongs here, instead of silently inheriting ledger behaviour.
_LEDGER_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_LEDGER_MAX_PAYLOAD_CHARS = 2000
_LEDGER_MAX_DETAIL_CHARS = 1000
# Above this, don't even attempt a JSON parse for redaction — parsing an
# arbitrarily large body to log it would be its own denial of service.
_LEDGER_MAX_PARSE_BYTES = 64 * 1024
# Bodies of these content types are never captured. The overlay has a real
# file-upload endpoint (`POST /api/comics/collection/import`, an XLSX
# `UploadFile` up to MAX_XLSX_BYTES), and reading an upload into memory and
# then into SQLite to log its rejection would be worse than the rejection.
# There is a second, harder reason: FastAPI consumes a multipart request via
# `request.form()`, which streams the parts and never populates the cached
# `_body`, so the bytes are simply not available here without re-reading a
# consumed stream.
_LEDGER_SKIP_CONTENT_TYPES = (
    "multipart/form-data",
    "application/octet-stream",
)
# Any key (JSON, query, or urlencoded field) matching this has its value
# replaced before storage. Deliberately NOT matching a bare "auth": that would
# also redact "author", which is real comic metadata — "authoriz" covers
# Authorization headers and "token" covers auth_token/oauth, so nothing is
# lost. Over-matching is the safe direction, but not at the cost of blanking
# the diagnostic content the ledger exists to preserve.
_SECRET_KEY_RE = re.compile(
    r"passw|secret|token|api[-_]?key|cookie|authoriz|credential|session"
    r"|private[-_]?key",
    re.IGNORECASE,
)
_REDACTED = "<redacted>"
# Bound the time the ledger may spend waiting on the app-wide write lock.
# Hard constraint: recording must not change response behaviour, and a request
# that hangs waiting to log its own failure IS a behaviour change. On timeout
# we drop the row and log — the lock is only ever held for fast, await-free
# local DML (BUI-408), so hitting this at all means something is already wrong.
_LEDGER_WRITE_TIMEOUT_S = 5.0


def _ledger_truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…<truncated from {len(text)} chars>"


def _ledger_redact(obj: Any) -> Any:
    """Recursively blank values whose key looks like a credential."""
    if isinstance(obj, dict):
        return {
            k: (_REDACTED if _SECRET_KEY_RE.search(str(k)) else _ledger_redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_ledger_redact(v) for v in obj]
    return obj


def _ledger_payload_snapshot(request: Request) -> str | None:
    """A bounded, secret-free snapshot of the rejected request body.

    Reads ONLY the body FastAPI already buffered (`Request._body`), never the
    stream. That is deliberate on two counts: re-reading a consumed stream
    raises, and awaiting an unread one could block on a slow client while the
    response sits ready to send. For the class of failure this ledger exists
    to catch — a JSON write rejected by validation — the body is always
    already buffered, because FastAPI reads it before it can decide the
    request is invalid.
    """
    ctype = (request.headers.get("content-type") or "").lower()
    if any(ctype.startswith(skip) for skip in _LEDGER_SKIP_CONTENT_TYPES):
        return f"<body not captured: content-type {ctype.split(';')[0] or 'unknown'}>"
    body = getattr(request, "_body", None)
    if not isinstance(body, (bytes, bytearray)) or not body:
        return None
    if len(body) > _LEDGER_MAX_PARSE_BYTES:
        return f"<body not captured: {len(body)} bytes exceeds snapshot cap>"
    try:
        parsed = json.loads(bytes(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        parsed = None
    if parsed is not None:
        try:
            return _ledger_truncate(
                json.dumps(_ledger_redact(parsed), default=str),
                _LEDGER_MAX_PAYLOAD_CHARS,
            )
        except (TypeError, ValueError):
            return "<body not captured: unserialisable>"
    # Not JSON (form-encoded, plain text, ...). We can't reliably tell a
    # credential field from a data field here, so if anything in it looks like
    # one, withhold the whole thing rather than risk logging it.
    text = bytes(body).decode("utf-8", errors="replace")
    if _SECRET_KEY_RE.search(text):
        return "<body withheld: non-JSON body containing a credential-like key>"
    return _ledger_truncate(text, _LEDGER_MAX_PAYLOAD_CHARS)


def _ledger_query_snapshot(request: Request) -> str | None:
    """The query string with credential-like values blanked.

    The query string is as much "the payload" as the body is for the several
    overlay write endpoints that take their input there, so it gets the same
    treatment — storing it raw would leak a `?token=...` straight past the
    body redaction.
    """
    raw = request.url.query
    if not raw:
        return None
    parts = []
    for pair in raw.split("&"):
        key, sep, _value = pair.partition("=")
        if sep and _SECRET_KEY_RE.search(key):
            parts.append(f"{key}={_REDACTED}")
        else:
            parts.append(pair)
    return _ledger_truncate("&".join(parts), _LEDGER_MAX_PAYLOAD_CHARS)


def _ledger_encode_validation_errors(errors: Any) -> str | None:
    """Encode pydantic validation errors without echoing a secret back out.

    Each error carries the value that failed under `input`. When the failing
    field is itself credential-like (`loc` ends in e.g. `password`), that
    `input` IS the secret — and key-based redaction misses it, because the key
    in the error dict is the innocuous string "input". So the `loc` decides.
    Nested dict inputs (a whole-body rejection) still go through the ordinary
    key-based pass.
    """
    if not isinstance(errors, list):
        return _ledger_encode_detail(_ledger_redact(errors))
    safe = []
    for err in errors:
        if isinstance(err, dict) and any(
            _SECRET_KEY_RE.search(str(part)) for part in (err.get("loc") or ())
        ):
            err = {**err, "input": _REDACTED}
        safe.append(_ledger_redact(err))
    return _ledger_encode_detail(safe)


def _ledger_encode_detail(detail: Any) -> str | None:
    if detail is None:
        return None
    if isinstance(detail, str):
        return _ledger_truncate(detail, _LEDGER_MAX_DETAIL_CHARS)
    try:
        encoded = json.dumps(jsonable_encoder(detail), default=str)
    except Exception:  # noqa: BLE001  # a log line must never out-fail the thing it logs
        encoded = str(detail)
    return _ledger_truncate(encoded, _LEDGER_MAX_DETAIL_CHARS)


def _ledger_classify_exception(exc: BaseException) -> tuple[int, str | None]:
    """Map an exception raised by a handler to (status the CLIENT will see, detail).

    THE TRAP THIS EXISTS FOR (BUI-601): at the route-handler layer a
    `RequestValidationError` has NOT yet been converted to a 422 response —
    FastAPI's app-level exception handler does that further out. So the
    obvious `getattr(exc, "status_code", 500)` yields **500** for the exact
    failure mode BUI-593 was about, while the client actually received
    **422**. A ledger that mis-records the one class of failure it was built
    for is worse than no ledger, so both validation errors are special-cased
    ahead of the generic lookup.
    """
    if isinstance(exc, RequestValidationError):
        return 422, _ledger_encode_validation_errors(exc.errors())
    if isinstance(exc, ResponseValidationError):
        # The handler produced something that doesn't match its response
        # model. The client sees a 500; the errors say what didn't fit.
        return 500, _ledger_encode_validation_errors(exc.errors())
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        detail = getattr(exc, "detail", None)
        return status, _ledger_encode_detail(detail if detail is not None else str(exc))
    return 500, _ledger_truncate(
        f"{type(exc).__name__}: {exc}", _LEDGER_MAX_DETAIL_CHARS
    )


def _ledger_response_detail(response: Response) -> str | None:
    """The response body, if it is already materialised in memory.

    Streaming/file responses have no `.body`; we record nothing rather than
    consuming their iterator, which would break the response.
    """
    body = getattr(response, "body", None)
    if not isinstance(body, (bytes, bytearray)) or not body:
        return None
    return _ledger_truncate(
        bytes(body).decode("utf-8", errors="replace"), _LEDGER_MAX_DETAIL_CHARS
    )


async def _ledger_record(
    request: Request, status: int, detail: str | None
) -> None:
    """Persist one rejection. Callers must treat every failure here as benign.

    Writes through the same `_write_locked()` + `write_transaction()` pair
    every other overlay/gixen-cli writer uses (BUI-408), so a ledger insert
    can never overlap another writer's transaction. The lock is already
    released by the time this runs — the handler's own `async with
    _write_locked()` block, if it had one, unwound as its exception
    propagated — so there is no self-deadlock.
    """
    payload = _ledger_payload_snapshot(request)
    query = _ledger_query_snapshot(request)
    async with _write_locked():
        with write_transaction(_get_db_path()) as wconn:
            record_rejected_write(
                wconn,
                method=request.method,
                path=request.url.path,
                query=query,
                status=status,
                detail=detail,
                payload=payload,
            )


async def _ledger_record_safely(
    request: Request, status: int, detail: str | None
) -> None:
    """`_ledger_record`, bounded in time and with every failure downgraded.

    The two guarantees hard constraint 1 needs: a broken ledger cannot turn a
    working request into an error (every exception becomes a log line), and a
    contended or wedged write lock cannot turn a fast response into a hung one
    (the write is bounded by `_LEDGER_WRITE_TIMEOUT_S`).
    """
    try:
        await asyncio.wait_for(
            _ledger_record(request, status, detail), timeout=_LEDGER_WRITE_TIMEOUT_S
        )
    except Exception:  # noqa: BLE001  # the ledger must never break the request it observes
        logger.exception(
            "rejected-writes ledger failed to record %s %s -> %s",
            request.method,
            request.url.path,
            status,
        )


class LedgerRoute(APIRoute):
    """Persist every 4xx/5xx on an overlay request (BUI-601; GETs BUI-707).

    HARD CONSTRAINT: recording must never change what the client gets.
    Exceptions are re-raised unchanged with a bare `raise`; successful
    responses — GET included — are returned as the identical object; and the
    recording itself is wrapped so that a broken ledger (locked DB, full
    disk, missing table on a version-skewed deploy) degrades to a log line
    instead of converting a working request into a 500.

    GETs were carved out at BUI-601 and added back at BUI-707: the dashboard
    polls several GET endpoints every few seconds, but this route only ever
    records on `>= 400` or a raised exception (see below) — a healthy poll
    was never recorded before and still never is, so the added volume is
    bounded by how often a *rejected* GET actually happens, not by poll
    frequency. A GET that fails the same way on every poll (e.g. a 404 every
    5s) rides the SAME `rejected_writes` table and its existing age + row
    count cap (`db._prune_rejected_writes`) as a retried failing write
    already does — no new throttling was added for this.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def ledger_handler(request: Request) -> Response:
            if request.method not in _LEDGER_METHODS:
                # No route in this plugin uses a method outside
                # _LEDGER_METHODS today; if one ever does, it is passed
                # through unrecorded rather than guessed at.
                return await original(request)
            try:
                response = await original(request)
            except Exception as exc:  # noqa: BLE001  # observe-and-re-raise, never swallow
                status, detail = _ledger_classify_exception(exc)
                await _ledger_record_safely(request, status, detail)
                raise
            if response.status_code >= 400:
                await _ledger_record_safely(
                    request, response.status_code, _ledger_response_detail(response)
                )
            return response

        return ledger_handler
