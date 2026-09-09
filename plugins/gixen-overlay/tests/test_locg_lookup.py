"""Unit tests for the Metron-backed year-fallback resolver (BUI-719)."""
from __future__ import annotations

import json
import os
import subprocess
from unittest.mock import MagicMock

import pytest

from gixen_overlay import locg_lookup
from gixen_overlay.locg_lookup import LocgResolution, resolve_year_and_locg


def _mock_run(responses):
    """Build a subprocess.run replacement that pops a response per call.

    Each response is either a dict (json-encoded for stdout) or an exception
    instance to raise.
    """
    calls = []
    queue = list(responses)

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return MagicMock(returncode=0, stdout=json.dumps(item), stderr="")

    fake_run.calls = calls
    return fake_run


def test_resolver_returns_year_on_clean_match(monkeypatch):
    fake = _mock_run([
        {
            "status": "ok",
            "series": "Uncanny X-Men",
            "issue": "211",
            "year": 1986,
            "metron_id": 42,
            "series_id": 7,
            "series_name": "Uncanny X-Men",
        },
    ])
    monkeypatch.setattr(subprocess, "run", fake)

    result = resolve_year_and_locg("Uncanny X-Men", "211")
    assert result == LocgResolution(year=1986)
    assert result.locg_id is None
    assert result.locg_variant_id is None

    # A single CLI call, to the new resolve-year subcommand. `--` forces
    # argparse to treat series/issue as plain positionals regardless of a
    # leading dash (e.g. a "-1"-style one-shot issue label).
    assert len(fake.calls) == 1
    assert fake.calls[0] == [locg_lookup.LOCG_CMD, "resolve-year", "--", "Uncanny X-Men", "211"]


def test_resolver_returns_none_when_result_has_error(monkeypatch):
    fake = _mock_run([{"error": "Could not unambiguously resolve 'Nonexistent Series' #1 on Metron"}])
    monkeypatch.setattr(subprocess, "run", fake)
    assert resolve_year_and_locg("Nonexistent Series", "1") is None


def test_resolver_returns_none_when_year_missing(monkeypatch):
    fake = _mock_run([{"status": "ok", "series": "Series", "issue": "1"}])
    monkeypatch.setattr(subprocess, "run", fake)
    assert resolve_year_and_locg("Series", "1") is None


def test_resolver_returns_none_when_year_not_int(monkeypatch):
    fake = _mock_run([{"status": "ok", "year": "1986"}])
    monkeypatch.setattr(subprocess, "run", fake)
    assert resolve_year_and_locg("Series", "1") is None


def test_resolver_returns_none_when_cli_missing(monkeypatch):
    fake = _mock_run([FileNotFoundError("locg not on PATH")])
    monkeypatch.setattr(subprocess, "run", fake)
    assert resolve_year_and_locg("Series", "1") is None


def test_resolver_returns_none_on_timeout(monkeypatch):
    fake = _mock_run([subprocess.TimeoutExpired(cmd="locg", timeout=30)])
    monkeypatch.setattr(subprocess, "run", fake)
    assert resolve_year_and_locg("Series", "1") is None


def test_resolver_disabled_short_circuits(monkeypatch):
    monkeypatch.setenv("LOCG_FALLBACK_DISABLED", "1")

    def boom(*_a, **_kw):
        raise AssertionError("subprocess.run should not be invoked when disabled")

    monkeypatch.setattr(subprocess, "run", boom)
    assert resolve_year_and_locg("Series", "1") is None


@pytest.mark.parametrize("series,issue", [("", "1"), ("Series", ""), ("", "")])
def test_resolver_returns_none_for_empty_inputs(monkeypatch, series, issue):
    def boom(*_a, **_kw):
        raise AssertionError("subprocess.run should not be invoked for empty inputs")

    monkeypatch.setattr(subprocess, "run", boom)
    assert resolve_year_and_locg(series, issue) is None


def test_resolver_returns_none_on_nonzero_exit(monkeypatch):
    def fake(cmd, **kwargs):
        return MagicMock(returncode=2, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake)
    assert resolve_year_and_locg("Series", "1") is None


def test_resolver_returns_none_on_invalid_json(monkeypatch):
    def fake(cmd, **kwargs):
        return MagicMock(returncode=0, stdout="not json", stderr="")

    monkeypatch.setattr(subprocess, "run", fake)
    assert resolve_year_and_locg("Series", "1") is None


def test_resolver_returns_none_on_non_dict_json(monkeypatch):
    """`locg resolve-year` always prints a JSON object; a stray list/scalar
    (e.g. from a mismatched LOCG_CMD pointing at some other tool) is treated
    the same as any other malformed response — fail-soft, never a guess."""
    fake = _mock_run([[{"year": 1986}]])
    monkeypatch.setattr(subprocess, "run", fake)
    assert resolve_year_and_locg("Series", "1") is None


# ---------------------------------------------------------------------------
# BUI-788: the `outcome` out-param — fail-soft without failing SILENT.
#
# Every failure class above returns the same bare None, so a row Metron merely
# throttled was indistinguishable from a row Metron genuinely cannot resolve.
# `outcome` records which it was, so the caller can say what a re-run would
# recover instead of filing all of them under "Metron doesn't know this issue".
# ---------------------------------------------------------------------------

def _reason(monkeypatch, responses, series="Series", issue="1"):
    """Run the resolver against canned subprocess output; return the outcome."""
    monkeypatch.setattr(subprocess, "run", _mock_run(responses))
    outcome: dict[str, object] = {}
    assert resolve_year_and_locg(series, issue, outcome=outcome) is None
    return outcome


def test_outcome_reports_throttled_from_the_childs_own_verdict(monkeypatch):
    """`locg resolve-year` classifies its own failure (BUI-788 in locg-cli);
    the reason is passed through verbatim rather than re-derived here."""
    outcome = _reason(monkeypatch, [{"error": "Metron was throttled", "reason": "throttled"}])
    assert outcome == {"reason": "throttled", "retryable": True}


def test_outcome_reports_unresolvable_as_terminal(monkeypatch):
    outcome = _reason(monkeypatch, [{"error": "no exact-name series", "reason": "unresolvable"}])
    assert outcome == {"reason": "unresolvable", "retryable": False}


def test_outcome_reports_timeout_separately_from_unresolvable(monkeypatch):
    """A killed child says NOTHING about whether the row is resolvable — it
    says we ran out of budget. Conflating the two is the BUI-788 defect: with
    a 30s parent budget against a 60s child sleep cap, a throttled row timed
    out every single time and was filed as permanently unresolvable."""
    outcome = _reason(monkeypatch, [subprocess.TimeoutExpired(cmd="locg", timeout=30)])
    assert outcome == {"reason": "timeout", "retryable": True}


def test_outcome_unlabelled_child_error_is_retryable_not_unresolvable(monkeypatch):
    """An older `locg` sends an error with no `reason`. Guessing 'unresolvable'
    there would quietly recreate this ticket's bug, so an unlabelled failure
    defaults to the retryable side."""
    outcome = _reason(monkeypatch, [{"error": "something went wrong"}])
    assert outcome["reason"] == "cli_error"
    assert outcome["retryable"] is True


def test_outcome_unknown_child_reason_passes_through_and_stays_retryable(monkeypatch):
    """A reason from a NEWER locg than this overlay is reported as-is; an
    unrecognized reason is retryable, because the cost of over-retrying is one
    wasted lookup and the cost of under-retrying is a permanently lost row."""
    outcome = _reason(monkeypatch, [{"error": "?", "reason": "some_future_reason"}])
    assert outcome == {"reason": "some_future_reason", "retryable": True}


@pytest.mark.parametrize("responses", [
    [FileNotFoundError("locg not on PATH")],
    [{"status": "ok", "year": "1986"}],   # success envelope, unusable year
])
def test_outcome_reports_cli_error_for_infrastructure_faults(monkeypatch, responses):
    outcome = _reason(monkeypatch, responses)
    assert outcome["reason"] == "cli_error"
    assert outcome["retryable"] is True


def test_outcome_reports_nonzero_exit_as_cli_error(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: MagicMock(returncode=2, stdout="", stderr="boom")
    )
    outcome: dict[str, object] = {}
    assert resolve_year_and_locg("Series", "1", outcome=outcome) is None
    assert outcome["reason"] == "cli_error"


def test_outcome_reports_disabled_and_invalid_input_as_terminal(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _mock_run([]))

    monkeypatch.setenv("LOCG_FALLBACK_DISABLED", "1")
    outcome: dict[str, object] = {}
    assert resolve_year_and_locg("Series", "1", outcome=outcome) is None
    assert outcome == {"reason": "disabled", "retryable": False}

    monkeypatch.delenv("LOCG_FALLBACK_DISABLED")
    outcome = {}
    assert resolve_year_and_locg("", "1", outcome=outcome) is None
    assert outcome == {"reason": "invalid_input", "retryable": False}


def test_outcome_untouched_on_success(monkeypatch):
    """The out-param is write-only and only ever written on failure, so a
    caller can reuse one dict across a loop without a stale reason leaking
    onto a row that actually resolved."""
    monkeypatch.setattr(subprocess, "run", _mock_run([{"status": "ok", "year": 1986}]))
    outcome: dict[str, object] = {}
    assert resolve_year_and_locg("Series", "1", outcome=outcome) == LocgResolution(year=1986)
    assert outcome == {}


def test_resolver_still_returns_bare_none_without_an_outcome_dict(monkeypatch):
    """`outcome` is optional and keyword-only: the `extract-comics` call site
    passes none and must keep its exact pre-BUI-788 behaviour."""
    monkeypatch.setattr(subprocess, "run", _mock_run([{"error": "x", "reason": "throttled"}]))
    assert resolve_year_and_locg("Series", "1") is None


# ---------------------------------------------------------------------------
# BUI-788: the child's rate-limit sleep is capped by OUR subprocess budget.
#
# `locg.metron`'s own default cap is 60s — twice LOCG_TIMEOUT_SECONDS — so a
# throttled child was guaranteed to be killed mid-sleep and could never report
# anything. Deriving the child's cap from the parent's budget makes the two
# consistent by construction instead of by keeping two constants in two
# packages in sync by hand.
# ---------------------------------------------------------------------------

def test_child_sleep_cap_is_strictly_under_the_subprocess_budget():
    """The invariant BUI-788 exists to hold. If a later edit raises the cap or
    lowers the timeout past each other, a throttled row silently goes back to
    being unreportable."""
    assert 0 < locg_lookup.LOCG_RATE_LIMIT_SLEEP_CAP_SECONDS < locg_lookup.LOCG_TIMEOUT_SECONDS


def test_child_env_carries_the_sleep_cap_and_preserves_the_rest(monkeypatch):
    monkeypatch.setenv("METRON_USERNAME", "someone")
    monkeypatch.delenv("METRON_RATE_LIMIT_MAX_SLEEP", raising=False)
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return MagicMock(returncode=0, stdout=json.dumps({"status": "ok", "year": 1986}), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert resolve_year_and_locg("Series", "1") == LocgResolution(year=1986)

    env = captured["env"]
    assert env["METRON_RATE_LIMIT_MAX_SLEEP"] == str(
        locg_lookup.LOCG_RATE_LIMIT_SLEEP_CAP_SECONDS
    )
    # The child still needs the rest of our environment — credentials, the
    # store dir, PATH. Replacing rather than copying os.environ would break it.
    assert env["METRON_USERNAME"] == "someone"
    assert "PATH" in env


def _captured_child_env(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return MagicMock(returncode=0, stdout=json.dumps({"status": "ok", "year": 1986}), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    resolve_year_and_locg("Series", "1")
    return captured["env"]


def test_operator_cap_may_shorten_the_child_sleep(monkeypatch):
    """An explicit value in ~/.comics-server/.env is honored when it is
    shorter than our budget — the cap stays tunable without a code change."""
    monkeypatch.setenv("METRON_RATE_LIMIT_MAX_SLEEP", "3")
    assert float(_captured_child_env(monkeypatch)["METRON_RATE_LIMIT_MAX_SLEEP"]) == 3.0


@pytest.mark.parametrize("operator_value", ["120", "60", "abc", "0", "-5", ""])
def test_operator_cap_can_never_exceed_our_budget(monkeypatch, operator_value):
    """A longer (or unusable) operator value must be clamped, not obeyed.

    We kill the child at LOCG_TIMEOUT_SECONDS no matter what, so a longer
    sleep cannot be waited out — honoring one would restore the BUI-788
    defect from a config file, the hardest place to notice it. An unusable
    value is just as dangerous: `locg.metron` would reject it and fall back
    to ITS OWN 60s default, which is over our budget.
    """
    monkeypatch.setenv("METRON_RATE_LIMIT_MAX_SLEEP", operator_value)
    effective = float(_captured_child_env(monkeypatch)["METRON_RATE_LIMIT_MAX_SLEEP"])
    assert 0 < effective <= locg_lookup.LOCG_RATE_LIMIT_SLEEP_CAP_SECONDS
    assert effective < locg_lookup.LOCG_TIMEOUT_SECONDS


def test_child_env_never_mutates_our_own_environment(monkeypatch):
    """The cap is scoped to the CHILD. The comics server also runs Metron
    in-process (record-win batches, the audit sweeps) and those callers were
    tuned for the full 60s cap — leaking this short one into `os.environ`
    would silently shorten their retries too."""
    monkeypatch.delenv("METRON_RATE_LIMIT_MAX_SLEEP", raising=False)
    env = _captured_child_env(monkeypatch)
    assert "METRON_RATE_LIMIT_MAX_SLEEP" in env
    assert "METRON_RATE_LIMIT_MAX_SLEEP" not in os.environ


def test_child_reason_is_length_bounded(monkeypatch):
    """The reason becomes a JSON key in the endpoint's roll-up, so a
    malformed child must not be able to inflate a response through it."""
    monkeypatch.setattr(
        subprocess, "run", _mock_run([{"error": "x", "reason": "z" * 5000}])
    )
    outcome: dict[str, object] = {}
    resolve_year_and_locg("Series", "1", outcome=outcome)
    assert len(outcome["reason"]) <= 64
