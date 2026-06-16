"""Property + unit tests for the centralized parsers (BUI-189).

Locks the invariants that the money-affecting parsers used to get wrong:
thousands separators in prices, and decimal/variant-suffix issue tokens.
"""
from __future__ import annotations

from hypothesis import given, strategies as st

from locg.parsing import (
    extract_price,
    normalize_issue_key,
    split_full_title,
    trailing_issue_token,
)


# --- extract_price -----------------------------------------------------------

def test_extract_price_keeps_thousands_separator():
    # The old \$(\d+\.?\d*) read these as 1.0 / 1.0 — a catastrophic underprice.
    assert extract_price("$1,234.56") == 1234.56
    assert extract_price(" · $1,000") == 1000.0


def test_extract_price_full_decimal_and_zero():
    assert extract_price("$4.99") == 4.99
    assert extract_price("$0") == 0.0
    assert extract_price("$0.00") == 0.0


def test_extract_price_none_when_absent():
    assert extract_price("no price here") is None


@given(
    dollars=st.integers(min_value=0, max_value=9_999_999),
    cents=st.integers(min_value=0, max_value=99),
)
def test_extract_price_roundtrips_with_commas(dollars, cents):
    text = f"${dollars:,}.{cents:02d}"          # e.g. "$1,234.56"
    assert extract_price(text) == round(dollars + cents / 100, 2)


# --- issue tokens ------------------------------------------------------------

# digits, an optional .decimal/.suffix group, and an optional trailing letter run
_ISSUE = st.from_regex(r"[1-9][0-9]{0,3}(?:\.[0-9A-Za-z]{1,3})?[A-Za-z]{0,2}", fullmatch=True)
_SERIES = st.text(
    alphabet=st.characters(min_codepoint=65, max_codepoint=90),  # A-Z only
    min_size=1, max_size=8,
)


def test_split_full_title_known_cases():
    assert split_full_title("Thor #154") == ("Thor", "154")
    assert split_full_title("Fantastic Four Annual #6") == ("Fantastic Four Annual", "6")
    assert split_full_title("X #1.MU") == ("X", "1.MU")        # BUI-175, no truncation
    assert split_full_title("Watchmen") == ("Watchmen", None)


@given(series=_SERIES, issue=_ISSUE)
def test_split_full_title_never_truncates_issue(series, issue):
    """The captured token must equal the issue we put in — no BUI-175 truncation."""
    _, token = split_full_title(f"{series} #{issue}")
    assert token == issue


def test_trailing_issue_token_fixes_decimal_keeps_anchor():
    # BUI-189: the reconciler's trailing token now captures decimals (was None)…
    assert trailing_issue_token("ASM #1.MU") == "1.mu"
    assert trailing_issue_token("Thor #154") == "154"
    # …while keeping the end-anchored "trailing #N only" semantics.
    assert trailing_issue_token("ASM #300 Newsstand") is None


@given(token=_ISSUE)
def test_normalize_issue_key_idempotent_and_lower(token):
    k = normalize_issue_key(token)
    assert k == normalize_issue_key(k)
    assert k == k.lower()


def test_normalize_issue_key_strips_leading_zeros():
    assert normalize_issue_key("007") == "7"
    assert normalize_issue_key("0") == "0"
