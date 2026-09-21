"""Source-parsing canary: no `fmv` query filters by grade alone (BUI-925).

`(comic_id, grade)` stopped being the whole key of an `fmv` row in BUI-924 —
`(comic_id, grade, certifier, label)` is. Every SQL string in this plugin that
narrows `fmv` by grade therefore has to narrow by certifier too, or it returns
"either row" the moment a CGC slab's price sits beside a raw one at the same
grade. What comes back from those queries is the number a bid cap is computed
from, so "either row" is a several-times-wrong cap, silently.

This is a CHECK, not a note, for the reason
docs/solutions/best-practices/a-shipped-guard-is-not-a-running-guard.md gives:
the ten lookups BUI-925 fixed were found by reading the file, and the next one
will be added by someone who never read this ticket. A grep heuristic in prose
closes nothing (the `WHERE item_id` class shipped five incidents after being
documented); a test that fails the build does.

Parsed with `ast`, not a raw grep, per
docs/solutions/architecture-patterns/http-only-contracts-need-a-source-parsing-canary.md
(BUI-673): a reformat, a line-wrap or a move into an f-string must not be able
to turn this into a silent no-op.

Two levels, because overlay SQL is written both ways:

1. **Per self-contained query.** Any string holding a whole `SELECT ... FROM
   fmv`/`JOIN fmv` with a grade predicate must itself name `certifier`. This
   is the level that matters for `_resolve_fmv_for_link`, which holds four
   separate queries in one function — checking only the function would let
   three of them carry the filter and cover for a fourth that does not.

2. **Per function, over every string it contains.** Catches the fragment
   style (`clauses.append("f.grade BETWEEN ? AND ?")` assembled into an
   f-string later), where no single string is a whole query.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "gixen_overlay"

# A reference to the `fmv` TABLE. `\b` after `fmv` is what keeps `fmv_history`
# and `bid_fmvs` out: `_` is a word character, so `fmv_history` does not match.
_FMV_TABLE_RE = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+fmv\b", re.IGNORECASE)

# A grade PREDICATE, not a grade mention. `ON CONFLICT(comic_id, grade,
# certifier, label)` and `INSERT INTO fmv (comic_id, grade, ...)` list the
# column followed by a comma; `ORDER BY f.grade` has no operator at all. Only
# a comparison filters.
_GRADE_PREDICATE_RE = re.compile(
    r"\b(?:(\w+)\.)?grade\s*(?:=|<|>|!=|<>|BETWEEN\b|IN\s*\()", re.IGNORECASE
)

_CERTIFIER_RE = re.compile(r"\bcertifier\b", re.IGNORECASE)
_SELECT_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)

# Aliases bound to the `fmv` table, e.g. the `f` of `JOIN fmv f`.
_FMV_ALIAS_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+fmv\s+(?:AS\s+)?(\w+)", re.IGNORECASE
)

# Functions exempted from the rule, each with the reason. Deliberately a
# NAMED, tiny allow-list rather than a `_migrate_*` wildcard: an exemption
# that covers a shape covers every future member of that shape too, and the
# one function here is exempt for a fact about its own gate, not about its
# name.
_EXEMPT: dict[str, str] = {
    "_migrate_fmv_split": (
        "one-shot migration off the pre-fmv-table schema. It runs only when "
        "`comics.grade` still exists (its own PRAGMA gate), and a DB in that "
        "shape predates the fmv table entirely — so it cannot hold a slab "
        "row, and `(comic_id, grade)` really is the whole key there."
    ),
}


def _sources() -> list[tuple[str, str]]:
    return sorted(
        (p.name, p.read_text(encoding="utf-8")) for p in _SRC.glob("*.py")
    )


def _string_literals(node: ast.AST) -> list[str]:
    """Every SQL-looking string inside `node`, f-strings flattened.

    An f-string's interpolations are dropped and its literal parts joined —
    `f"... WHERE {where}"` therefore contributes its literal skeleton, which
    is what carries the table name. Adjacent string literals are already
    merged into one `ast.Constant` by the parser.
    """
    out: list[str] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
        elif isinstance(n, ast.JoinedStr):
            out.append("".join(
                part.value for part in n.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            ))
    return out


def _unguarded_grade_predicate(sql: str, aliases: set[str]) -> str | None:
    """The first grade predicate in `sql` that is NOT paired with certifier.

    Returns the matched text, or None when the string is clean. A predicate
    qualified with an alias that is not bound to `fmv` (`cp.grade` on the
    comps table) is not this contract's business and is skipped.
    """
    for m in _GRADE_PREDICATE_RE.finditer(sql):
        qualifier = m.group(1)
        if qualifier is not None and qualifier.lower() not in aliases:
            continue
        if not _CERTIFIER_RE.search(sql):
            return m.group(0)
    return None


def _fmv_aliases(text: str) -> set[str]:
    return {"fmv"} | {m.group(1).lower() for m in _FMV_ALIAS_RE.finditer(text)}


def _violations() -> list[str]:
    """Every (file, function, offending SQL) pair that breaks the contract."""
    found: list[str] = []
    for filename, source in _sources():
        tree = ast.parse(source)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name in _EXEMPT:
                continue
            strings = _string_literals(fn)
            combined = "\n".join(strings)
            if not _FMV_TABLE_RE.search(combined):
                continue
            aliases = _fmv_aliases(combined)

            # Level 1 — each self-contained query, on its own.
            for sql in strings:
                if not (_SELECT_RE.search(sql) and _FMV_TABLE_RE.search(sql)):
                    continue
                hit = _unguarded_grade_predicate(sql, _fmv_aliases(sql))
                if hit is not None:
                    found.append(
                        f"{filename}:{fn.name} — query filters on {hit!r} "
                        f"with no certifier filter:\n{sql.strip()}"
                    )

            # Level 2 — the function's SQL taken together, for the fragment
            # style where no one string is a whole query.
            hit = _unguarded_grade_predicate(combined, aliases)
            if hit is not None:
                found.append(
                    f"{filename}:{fn.name} — the SQL this function assembles "
                    f"filters on {hit!r} but never names certifier"
                )
    return found


def test_every_fmv_grade_query_also_filters_certifier():
    violations = _violations()
    assert not violations, (
        "An `fmv` query narrows by grade without narrowing by certifier. Once "
        "a slab price sits beside a raw one at the same grade, that query "
        "returns EITHER row — and what it returns is what a bid cap is "
        "computed from. Add the certifier (and label) to the query, or, if "
        "the call site genuinely cannot have a slab row, add it to _EXEMPT "
        "with the reason.\n\n" + "\n\n".join(violations)
    )


def test_the_detector_can_actually_fail():
    """The canary must be able to die (BUI-605's self-test rule).

    A contract test whose detector silently stopped matching — a renamed
    table, a reformatted query, a regex that no longer fires — passes forever
    and protects nothing. This asserts the detector still flags the exact
    shape BUI-925 removed.
    """
    pre_fix = (
        "SELECT id AS fmv_id FROM fmv WHERE comic_id=? AND grade=? LIMIT 1"
    )
    assert _unguarded_grade_predicate(pre_fix, _fmv_aliases(pre_fix)) is not None

    fixed = (
        "SELECT id AS fmv_id FROM fmv "
        "WHERE comic_id=? AND grade=? AND certifier=? AND label=? LIMIT 1"
    )
    assert _unguarded_grade_predicate(fixed, _fmv_aliases(fixed)) is None

    # Joined form, and the fragment form assembled across strings.
    joined = "SELECT f.id FROM fmv f JOIN comics c ON c.id = f.comic_id WHERE f.grade = ?"
    assert _unguarded_grade_predicate(joined, _fmv_aliases(joined)) is not None

    fragment = "FROM fmv f\nf.grade BETWEEN ? AND ?"
    assert _unguarded_grade_predicate(fragment, _fmv_aliases(fragment)) is not None


def test_detector_ignores_non_fmv_grade_filters():
    """A `comps`/`fmv_history` grade filter is not this contract's business.

    Guards against the opposite failure: a detector so broad that the only way
    to keep the suite green is to stop trusting it.
    """
    comps = "SELECT * FROM comps cp WHERE cp.comic_id = ? AND cp.grade = ?"
    assert _unguarded_grade_predicate(comps, _fmv_aliases(comps)) is None
    assert not _FMV_TABLE_RE.search("SELECT * FROM fmv_history WHERE grade = ?")
    assert not _FMV_TABLE_RE.search("JOIN bid_fmvs bf ON bf.bid_id = b.id")


def test_detector_ignores_grade_mentions_that_are_not_filters():
    """A column list or an ORDER BY is not a filter, and must not be flagged."""
    insert = (
        "INSERT INTO fmv (comic_id, grade, low, high) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(comic_id, grade, certifier, label) DO NOTHING"
    )
    # certifier is present here anyway; strip it to prove the shape alone
    # does not trip the predicate matcher.
    bare = "INSERT INTO fmv (comic_id, grade, low, high) VALUES (?, ?, ?, ?)"
    assert _unguarded_grade_predicate(bare, _fmv_aliases(bare)) is None
    assert _unguarded_grade_predicate(insert, _fmv_aliases(insert)) is None
    order_by = "SELECT f.grade FROM fmv f ORDER BY f.grade"
    assert _unguarded_grade_predicate(order_by, _fmv_aliases(order_by)) is None


@pytest.mark.parametrize("name,reason", sorted(_EXEMPT.items()))
def test_every_exemption_still_names_a_real_function(name: str, reason: str):
    """An exemption must not outlive the function it excuses.

    If `_migrate_fmv_split` is renamed or deleted, the entry silently starts
    excusing nothing — and the next function to take that name inherits a
    blanket pass it was never argued for.
    """
    assert reason.strip(), f"{name} is exempt with no stated reason"
    defined = {
        fn.name
        for _, source in _sources()
        for fn in ast.walk(ast.parse(source))
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert name in defined, (
        f"_EXEMPT names {name!r}, which no longer exists in the overlay. "
        f"Remove the exemption, or point it at the function that replaced it."
    )
