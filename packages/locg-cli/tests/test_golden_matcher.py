"""Golden-fixture regression for the collection matcher (BUI-190).

One frozen table of (collection-state, query) → expected ownership verdict,
capturing every documented matcher bug in one place so a normalization change
can't silently reopen one:

* GSFF false positive — "Giant-Size Fantastic Four" must not be satisfied by an
  owned "Fantastic Four Annual" (qualifier words are part of series identity).
* leading-article Hulks (BUI-45) — an owned "The Incredible Hulk" is found by a
  query that dropped the article.
* BUI-129 X-Men — a series START year wrongly filters out a mid-run owned issue;
  omitting year (or passing the cover year) finds it.
* BUI-175 — decimal/point issues (#1.MU) match without truncation.
* BUI-176 — a variant-qualified query still finds the owned base issue.
* the copies-owned gate (in_collection=0 is wish-list/read, not owned).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def _make_cache(tmp_path: Path):
    from locg.collection_cache import CollectionCache
    return CollectionCache(
        path=tmp_path / "collection.json",
        lock_path=tmp_path / "collection.lock",
        audit_path=tmp_path / "import-history.jsonl",
    )


def _row(full_title: str, release_date: str = "", in_collection: int = 1) -> dict[str, Any]:
    return {"full_title": full_title, "release_date": release_date,
            "in_collection": in_collection}


def _seed(cache, rows):
    cache.apply(lambda p: p["comics"].extend(rows), command="golden-seed")


# (name, seed_rows, query{series, issue, variant?, year?}, expected_status)
GOLDEN = [
    ("exact_owned",
     [_row("Amazing Spider-Man #300", "1988-05-01")],
     {"series": "Amazing Spider-Man", "issue": "300"}, "in_collection"),

    ("gsff_not_confused_with_ff_annual",
     [_row("Fantastic Four Annual #6", "1968-11-01")],
     {"series": "Giant-Size Fantastic Four", "issue": "2"}, "not_in_cache"),

    ("ff_annual_not_satisfied_by_base_query",
     [_row("Fantastic Four Annual #6", "1968-11-01")],
     {"series": "Fantastic Four", "issue": "6"}, "not_in_cache"),

    ("leading_article_hulk",
     [_row("The Incredible Hulk #341", "1987-11-17")],
     {"series": "Incredible Hulk", "issue": "341"}, "in_collection"),

    ("xmen_series_start_year_misses",
     [_row("Uncanny X-Men #137", "1980-09-01")],
     {"series": "Uncanny X-Men", "issue": "137", "year": "1963"}, "not_in_cache"),

    ("xmen_no_year_finds",
     [_row("Uncanny X-Men #137", "1980-09-01")],
     {"series": "Uncanny X-Men", "issue": "137"}, "in_collection"),

    ("xmen_cover_year_finds",
     [_row("Uncanny X-Men #137", "1980-09-01")],
     {"series": "Uncanny X-Men", "issue": "137", "year": "1980"}, "in_collection"),

    ("decimal_issue_mu",
     [_row("The Amazing Spider-Man #1.MU", "2014-01-01")],
     {"series": "The Amazing Spider-Man", "issue": "1.MU"}, "in_collection"),

    ("decimal_issue_point_one",
     [_row("Amazing Spider-Man #20.1", "2011-01-01")],
     {"series": "Amazing Spider-Man", "issue": "20.1"}, "in_collection"),

    ("issue_1_does_not_match_1_5",
     [_row("Uncanny X-Men #1.5", "1995-01-01")],
     {"series": "Uncanny X-Men", "issue": "1"}, "not_in_cache"),

    ("variant_query_finds_base_issue",
     [_row("Spawn #1", "1992-05-01")],
     {"series": "Spawn", "issue": "1", "variant": "newsstand"}, "in_collection"),

    ("wishlist_only_row_not_owned",
     [_row("Fantastic Four #48", "1966-03-01", in_collection=0)],
     {"series": "Fantastic Four", "issue": "48"}, "not_in_cache"),
]


@pytest.mark.parametrize("name,rows,query,expected", GOLDEN, ids=[c[0] for c in GOLDEN])
def test_matcher_golden(tmp_path, monkeypatch, name, rows, query, expected):
    import locg.commands as cmds
    cache = _make_cache(tmp_path)
    monkeypatch.setattr(cmds, "CollectionCache", lambda: cache)
    _seed(cache, rows)
    result = cmds.cmd_collection_check(**query)
    assert result["match_status"] == expected


# --- normalization invariants (Hypothesis, BUI-190) --------------------------

from hypothesis import given, strategies as st  # noqa: E402
from locg.collection_cache import _normalize_series_key  # noqa: E402


@given(name=st.text(min_size=1, max_size=40))
def test_normalize_series_key_is_always_lowercase(name):
    k = _normalize_series_key(name)
    assert k == k.lower()


def test_normalize_series_key_strips_leading_article():
    base = _normalize_series_key("Incredible Hulk")
    assert _normalize_series_key("The Incredible Hulk") == base
    assert _normalize_series_key("A History of Violence") == _normalize_series_key(
        "History of Violence"
    )


def test_normalize_series_key_strips_year_range_and_vol():
    base = _normalize_series_key("Uncanny X-Men")
    assert _normalize_series_key("Uncanny X-Men (1963 - 2011)") == base
    assert _normalize_series_key("Uncanny X-Men (Vol. 1)") == base
    assert _normalize_series_key("Uncanny X-Men (2024)") == base
