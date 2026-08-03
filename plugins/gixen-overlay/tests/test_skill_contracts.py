"""Skill <-> endpoint/script contract harness (BUI-173).

The /comic:* skills document contracts that actually live in code — endpoint
paths, response field sets, status/confidence enums, score thresholds, units,
command names. Nothing kept prose and code in sync, and 16 of the 36 seam-audit
findings were `contract-mismatch` born exactly that way. This harness asserts
the contracts hold against the real code so a future rename/field/enum change
that breaks a skill's documented contract fails CI instead of biting at runtime.

HOW TO EXTEND (the runbook's instruction): as each documented-contract drift is
fixed — BUI-142 (dollars vs cents), BUI-148 (grade_from_description), BUI-150
(status mapping), BUI-161/162/163 (fmv.md fields / 4th confidence level / a
command that doesn't exist), BUI-167 (score thresholds) — add a test HERE
asserting the now-consistent contract, so the same drift fails CI next time.
Add the assertion in the per-skill batch that fixes the drift, not before (the
contract is still violated until then, so the assertion would fail early).

BUI-607 (skill-lint family): the section below ("SKILL LINT") generalizes this
harness from spot-checked contracts to four corpus-wide drift classes: every
fenced ```bash block must be shellcheck-clean, every `file.md § Section`/
`file.md Step N` cross-reference must resolve to a real heading, two idioms
already outlawed in CLAUDE.md prose must not reappear, and BUI-606's
fallback-swallow detector must stay clean over the skill corpus (reusing that
detector, not a second implementation of it — see lib/fallback_swallow.py's
own module docstring). Each check degrades/skips only where an external binary
(shellcheck) is genuinely unavailable; the other three always run.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import gixen_overlay.routes as overlay_routes
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS_DIR = REPO_ROOT / ".claude" / "commands" / "comic"

# scripts/lib is a package (has __init__.py) but sits outside the plugin's own
# package tree, so it needs REPO_ROOT/"scripts" on sys.path to import — see
# BUI-606's fallback_swallow module docstring ("Two entry points...").
_SCRIPTS_DIR = str(REPO_ROOT / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from lib.fallback_swallow import check_fallback_swallow  # noqa: E402


def _registered_comics_paths() -> set[str]:
    """Every /api/comics* path the overlay actually registers."""
    return {
        rt.path
        for rt in overlay_routes.router.routes
        if getattr(rt, "path", "").startswith("/api/comics")
    }


def _referenced_comics_paths() -> dict[str, set[str]]:
    """Concrete /api/comics* paths each skill doc references -> set of doc names.

    Strips query strings and trailing punctuation; skips glob/wildcard (`*`) and
    path-param (`{...}`) forms that aren't a single concrete endpoint.
    """
    refs: dict[str, set[str]] = {}
    for md in sorted(SKILLS_DIR.glob("*.md")):
        text = md.read_text()
        for raw in re.findall(r"/api/comics/[^\s)`\"'>]*", text):
            path = raw.split("?")[0]
            # Skip glob/wildcard summaries and path-param forms BEFORE stripping
            # punctuation (else a trailing `*` gets silently removed first).
            if "*" in path or "{" in path:
                continue
            path = path.rstrip("/.,;:")
            if path:
                refs.setdefault(path, set()).add(md.name)
    return refs


def test_every_skill_referenced_endpoint_is_registered():
    """Endpoint-existence contract: a skill cannot reference an /api/comics/*
    path the overlay doesn't serve. Catches a route rename/removal that would
    silently 404 a skill (the BUI-150-class drift, generalised)."""
    registered = _registered_comics_paths()
    referenced = _referenced_comics_paths()

    missing = {
        path: docs for path, docs in referenced.items() if path not in registered
    }
    assert not missing, (
        "skill(s) reference /api/comics endpoints the overlay does not register: "
        + "; ".join(f"{p} (in {sorted(d)})" for p, d in sorted(missing.items()))
    )


def test_harness_actually_found_endpoints():
    """Guard the guard: if the extraction silently matched nothing, the contract
    test above would pass vacuously. Pin a known seam so a broken regex fails."""
    referenced = _referenced_comics_paths()
    assert "/api/comics/collection/check" in referenced, (
        "contract harness extracted no endpoint refs — extraction is broken"
    )


def test_identify_documents_grade_from_description():
    """BUI-148: ebay_fetch.py emits a third grade signal, grade_from_description
    (a grade found only in the listing body), but identify.md's field table
    omitted it — so the skill mislabelled description-graded listings as having
    no grade anywhere. Assert the field the script emits is documented, and the
    skill keys its strong-warning rule on it (grade_from_description null too).

    BUI-195: the identify fetch+parse step moved into the comic-identifier
    subagent (`.claude/agents/comic-identifier.md`) to keep raw JSON out of the
    main context — so the parse contract (this field's table) now lives there.
    Accept the field documented in EITHER identify.md or the subagent, so the
    BUI-148 contract holds wherever the identify flow keeps its parse table."""
    fetch_src = (REPO_ROOT / "apps" / "ebay" / "src" / "ebay_fetch.py").read_text()
    assert '"grade_from_description"' in fetch_src, (
        "ebay_fetch.py no longer emits grade_from_description — update this contract"
    )
    identify = (SKILLS_DIR / "identify.md").read_text()
    identifier_agent = (
        REPO_ROOT / ".claude" / "agents" / "comic-identifier.md"
    ).read_text()
    assert "grade_from_description" in identify or "grade_from_description" in identifier_agent, (
        "the identify flow must document grade_from_description (the script emits "
        "it) — in identify.md or the comic-identifier subagent (BUI-195)"
    )


def test_seller_scan_doc_threshold_matches_emit_floor():
    """BUI-167: seller-scan.md cited a 0.5 'partial overlap' score and a 0.7
    gate, but seller_scan.py only emits matches at score >= 0.65, so a 0.5 is
    never surfaced. Assert the doc references the real 0.65 floor and no longer
    carries the dead '0.5 means partial' advice."""
    src = (REPO_ROOT / "apps" / "ebay" / "src" / "seller_scan.py").read_text()
    assert "best_score >= 0.65" in src, (
        "seller_scan.py emit floor changed — update the doc + this contract"
    )
    doc = (SKILLS_DIR / "seller-scan.md").read_text()
    assert "0.65" in doc, "seller-scan.md must reference the real 0.65 emit floor"
    assert "0.5 means partial series overlap" not in doc, (
        "dead '0.5 partial overlap' advice resurfaced — 0.5 is below the 0.65 floor"
    )


def test_snipe_show_documents_server_mode_statuses():
    """BUI-150: in server/thin-client mode /api/snipes returns the INTERNAL
    mapped status (the values of _GIXEN_TERMINAL_MAP: WON/LOST/FAILED/ENDED),
    not the raw Gixen strings. snipe-show.md's Result mapping must cover those,
    or every non-win ended snipe renders a bare internal word instead of a
    human label."""
    main_src = (REPO_ROOT / "packages" / "gixen-cli" / "server" / "main.py").read_text()
    # The internal terminal status values the server can emit.
    internal = {"WON", "LOST", "FAILED", "ENDED"}
    for status in internal:
        assert f'"{status}"' in main_src, f"{status} no longer in gixen-cli — update contract"
    doc = (SKILLS_DIR / "snipe-show.md").read_text()
    for status in internal:
        assert f"`{status}`" in doc, (
            f"snipe-show.md must map the server-mode status {status} to a label"
        )


def test_snipe_show_does_not_swallow_fetch_errors():
    """BUI-151: the fetch must not blanket-2>/dev/null, which hid server-down
    errors and rendered empty tables as 'you have no snipes'."""
    doc = (SKILLS_DIR / "snipe-show.md").read_text()
    assert "gixen list --json 2>/dev/null" not in doc


def test_no_skill_swallows_a_server_curl():
    """BUI-151 generalized (BUI-186): no /comic:* skill may pipe a comics-server
    curl to 2>/dev/null or '|| echo', which would hide a server-down / non-200 as
    an empty 'no results' answer the agent could act on (dupe-buy, price-on-no-
    data). Server calls must fail loud — via comics_curl/comics_get/comics_post
    or a bare `curl -sf`/`--fail`."""
    swallow_re = re.compile(r"\|\|\s*echo")
    offenders = []
    for md in sorted(SKILLS_DIR.glob("*.md")):
        for i, line in enumerate(md.read_text().splitlines(), 1):
            # Only lines that actually hit the comics server with curl.
            if "curl" not in line:
                continue
            if (
                "COMICS_SERVER_URL" not in line
                and "GIXEN_SERVER_URL" not in line
                and "/api/comics" not in line
            ):
                continue
            if "2>/dev/null" in line or swallow_re.search(line):
                offenders.append(f"{md.name}:{i}: {line.strip()}")
    assert not offenders, (
        "a server curl swallows failure into empty output:\n" + "\n".join(offenders)
    )


def test_shared_server_wrapper_is_fail_loud():
    """BUI-191 (R2/BUI-186): the shared comics-server wrapper must stay fail-loud
    — comics_curl forces --fail-with-body (non-200 → non-zero) and bounds the
    call with --max-time, and the comics_get/comics_post aliases exist. A future
    edit that drops the fail flag would let a non-200 read as empty success."""
    sh = (REPO_ROOT / "scripts" / "comics-server.sh").read_text()
    assert "--fail-with-body" in sh, "comics_curl no longer forces --fail-with-body"
    assert "--max-time" in sh, "comics_curl lost its --max-time bound (BUI-186)"
    for fn in ("comics_curl()", "comics_get()", "comics_post()"):
        assert fn in sh, f"shared wrapper no longer defines {fn}"


def test_ezship_dedup_documented_and_implemented():
    """BUI-191 (BUI-180): order submission is deduped by tracking number. Assert
    the code keeps the ledger guard AND ezship-add.md documents that a re-run
    won't double-submit, so the seam can't silently regress to a double-ship."""
    api = (REPO_ROOT / "apps" / "ezship" / "src" / "api.ts").read_text()
    ledger = REPO_ROOT / "apps" / "ezship" / "src" / "order-ledger.ts"
    assert ledger.exists(), "order-ledger.ts (BUI-180 dedup) is gone"
    assert "findSubmittedOrder" in api, "submitNewOrder no longer checks the ledger"
    assert "recordSubmittedOrder" in api, "submitNewOrder no longer records on success"
    doc = (SKILLS_DIR / "ezship-add.md").read_text()
    assert "BUI-180" in doc and "tracking number" in doc.lower(), (
        "ezship-add.md must document the tracking-number dedup (re-run is a no-op)"
    )


def test_fmv_batch_maps_by_id_not_position():
    """BUI-191 (BUI-174/187): the FMV batch maps subprocess results to books by an
    echoed id, not list position. Assert ebay-sold-comps echoes the id and
    fmv_runner maps by it — a regression to positional mapping fails CI."""
    sold = (REPO_ROOT / "apps" / "ebay" / "src" / "sold_comps.py").read_text()
    runner = (REPO_ROOT / "apps" / "fmv" / "src" / "fmv_runner.py").read_text()
    assert '"_req_id"' in sold or "_req_id" in sold, (
        "sold_comps no longer echoes the _req_id correlation id (BUI-174/187)"
    )
    assert "_req_id" in runner and "results_by_id" in runner, (
        "fmv_runner no longer maps results back by id (BUI-174/187)"
    )
    # And the positional-ordinal mapping must not have crept back in.
    assert "needs_indices[ordinal]" not in runner, (
        "fmv_runner reintroduced positional subprocess mapping (BUI-174)"
    )


def test_wish_list_add_year_seam():
    """BUI-184: the wish-list-add owned-guard forwards a per-issue cover year so
    the year-gated masthead fallback can catch a base-masthead-stored owned book.
    Assert the model carries `year`, the endpoint forwards it, and the skill
    documents the correct (cover year) vs wrong (year_began) source."""
    overlay_src = REPO_ROOT / "plugins" / "gixen-overlay" / "src" / "gixen_overlay"
    models = (overlay_src / "models.py").read_text()
    routes = (overlay_src / "routes.py").read_text()
    assert "year: str | None" in models, "WishListAddRequest lost its year field (BUI-184)"
    assert "cmd_collection_check(series=series, issue=issue, year=req.year)" in routes, (
        "wish-list-add owned-guard no longer forwards the per-issue year (BUI-184)"
    )
    doc = (SKILLS_DIR / "wishlist-add.md").read_text()
    assert "cover year" in doc and "year_began" in doc, (
        "wishlist-add.md must document passing the per-issue cover year, never year_began"
    )


def test_ezship_declared_value_units_agree():
    """BUI-142: cli.ts's -d/--declared-value is in CENTS, but ezship-add.md
    described it as dollars in its primary flow → an agent told "$25" ran
    `-d 25` (25 cents), a 100x under-declaration. Assert the doc agrees with the
    CLI unit and instructs the cents conversion."""
    cli = (REPO_ROOT / "apps" / "ezship" / "src" / "cli.ts").read_text()
    assert "--declared-value <cents>" in cli, (
        "cli.ts declared-value unit changed — re-check the doc + this contract"
    )
    doc = (SKILLS_DIR / "ezship-add.md").read_text()
    assert "Declared value in dollars" not in doc, (
        "ezship-add.md describes -d as dollars while the CLI takes cents"
    )
    assert "cents" in doc, "ezship-add.md must state -d is in cents"


def test_fmv_batch_schema_documents_consumed_fields():
    """BUI-161: ebay-sold-comps reads `publisher` (search-query noise filter) and
    fmv_runner reads `variant` (distinct comic_id per edition, BUI-28), but the
    documented --batch schema omitted both. Assert the fields the code consumes
    are documented in fmv.md (and buy.md, which repeats the schema)."""
    sold = (REPO_ROOT / "apps" / "ebay" / "src" / "sold_comps.py").read_text()
    runner = (REPO_ROOT / "apps" / "fmv" / "src" / "fmv_runner.py").read_text()
    assert 'book.get("publisher")' in sold, "sold_comps no longer reads publisher"
    assert 'inp.get("variant")' in runner or 'inp["variant"]' in runner
    for skill in ("fmv.md", "buy.md"):
        doc = (SKILLS_DIR / skill).read_text()
        assert "publisher" in doc, f"{skill} batch schema omits publisher (BUI-161)"
        assert "variant" in doc, f"{skill} batch schema omits variant (BUI-161)"


def test_fmv_grade_confidence_enum_matches_code():
    """BUI-162: fmv.md documented 3 confidence levels but fmv_math normalizes 4
    (adds medium-low, which haircuts to 0.70 vs low's 0.60). Assert every
    documented level matches _GRADE_CONF_NORMALIZE's keys."""
    math_src = (REPO_ROOT / "apps" / "fmv" / "src" / "fmv_math.py").read_text()
    keys = set(re.findall(r'"(high|medium|medium-low|low)":', math_src))
    assert keys == {"high", "medium", "medium-low", "low"}, keys
    doc = (SKILLS_DIR / "fmv.md").read_text()
    assert "medium-low" in doc, "fmv.md must document the 4th level medium-low (BUI-162)"


def test_fmv_doc_uses_real_command_name():
    """BUI-163: fmv.md referenced a non-existent `gixen fmv` fallback command;
    the real CLI is `comic-fmv`. Assert the dead command name is gone."""
    doc = (SKILLS_DIR / "fmv.md").read_text()
    assert "gixen fmv" not in doc, "fmv.md references the non-existent `gixen fmv` command"
    assert "comic-fmv" in doc


def test_wishlist_add_resolves_server_via_shared_convention():
    """BUI-170: wishlist-add's Step 0 was comment-only — it never inferred/SET
    GIXEN_SERVER_URL when unset (missing the Mac Mini -> localhost mapping), so
    it aborted on the Mac Mini. Assert it routes through the shared convention
    that actually does the inference — since BUI-510, that's `comics-api`,
    which resolves + health-gates internally on every call rather than relying
    on a once-per-skill export."""
    doc = (SKILLS_DIR / "wishlist-add.md").read_text()
    assert "comics-api" in doc


def test_wishlist_add_reconciles_series_names():
    """BUI-171: wishlist-add must reconcile the Metron series name to the LOCG
    catalog spelling (like collection-check does) before the ownership check, or
    an alt-spelling false `not_in_cache` wish-lists an owned book."""
    doc = (SKILLS_DIR / "wishlist-add.md").read_text()
    assert "/api/comics/collection/series-names" in doc, (
        "wishlist-add must call series-names to reconcile the catalog spelling"
    )


def test_snipe_add_documents_failed_add_policy():
    """BUI-168: snipe-add mandated sequential adds but gave no policy for a
    mid-batch `gixen add` failure (a server that dies after the pre-flight
    health check). Assert the skill documents a failed-add state + halt/continue
    guidance, so a partial batch isn't reported as all-success."""
    doc = (SKILLS_DIR / "snipe-add.md").read_text()
    assert "❌ Failed" in doc, "snipe-add output has no Failed state"
    assert "Handling a failed add" in doc, "snipe-add has no mid-batch failure policy"


def test_snipe_add_documents_blocked_status_and_remediation():
    """BUI-644: snipe-add.md's "Handling Policy Advisories" section predates
    BUI-623's POLICY_BLOCK_<CODE> blocking mode — it used to claim an advisory
    "never blocks the write" and never mentioned BLOCKED/🚫 at all, and the
    Output status-values list omitted `blocked` entirely. Assert the skill now
    documents the BLOCKED status/icon, and — the one place this doc can
    mislead an operator into a wrong action (see
    docs/solutions/conventions/add-batch-row-status-contract.md) — that it
    tells the operator NOT to use the advisory remediations (gixen edit/
    remove) on a blocked row, since a blocked write never landed and there is
    nothing to edit or remove."""
    doc = (SKILLS_DIR / "snipe-add.md").read_text()
    assert "🚫 Blocked" in doc, "snipe-add output has no Blocked state"
    assert "gixen edit`/`gixen remove` are the wrong remediation" in doc, (
        "snipe-add must warn that the advisory remediations (edit/remove) are "
        "wrong for a blocked row — there is no live snipe to edit or remove"
    )


def test_endpoint_names_are_provider_neutral():
    """CLAUDE.md invariant: comics endpoints are provider-neutral — never
    /api/comics/locg/*. A drift here would leak the provider into the URL the
    skills hard-code."""
    leaky = {p for p in _registered_comics_paths() if "/locg" in p}
    assert not leaky, f"non-neutral comics endpoint(s) registered: {sorted(leaky)}"


# =============================================================================
# SKILL LINT (BUI-607) — four corpus-wide drift classes, generalizing the
# spot-checked contracts above. See this module's docstring for the overview.
# =============================================================================

_FENCE_RE = re.compile(r"^```\s*([A-Za-z0-9_+-]*)")
_SHELL_FENCE_LANGS = {"bash", "sh", "shell"}


def _iter_shell_blocks(text: str) -> list[tuple[int, list[str]]]:
    """Return (first_line_no, lines) for every fenced ```bash/sh/shell block
    in `text`. `first_line_no` is the 1-indexed line of the block's first
    content line (the line right after the opening fence), matching how a
    human reading the file would count."""
    lines = text.splitlines()
    blocks: list[tuple[int, list[str]]] = []
    in_fence = False
    in_shell = False
    block_start = 0
    block_lines: list[str] = []
    for i, raw in enumerate(lines, 1):
        fence = _FENCE_RE.match(raw.strip())
        if fence is not None:
            if in_fence:
                if in_shell:
                    blocks.append((block_start, block_lines))
                in_fence = False
                in_shell = False
                block_lines = []
            else:
                in_fence = True
                in_shell = fence.group(1).lower() in _SHELL_FENCE_LANGS
                block_start = i + 1
                block_lines = []
            continue
        if in_fence and in_shell:
            block_lines.append(raw)
    return blocks


# --- Class 1: shellcheck every fenced bash block -----------------------------
#
# Calibration note (BUI-607): running shellcheck unmodified over the real
# corpus produced ~60 findings across 20+ of the 78 blocks, and every single
# one was a false positive from the SAME root cause — these blocks are
# copy/fill-in-the-blank templates for a human/agent, not standalone scripts.
# Two artifact classes, both neutralized below rather than suppressed by
# disabling codes (disabling doesn't help: SC1009/SC1072/SC1073 are outright
# parse failures that stop shellcheck from checking the rest of the block):
#
#   1. Doc placeholders — `<working_list.json>`, `{tracking}`, `[options]` —
#      collide with real bash redirection (`<`/`>`), brace-expansion (`{}`),
#      and glob (`[]`) syntax. Replaced with an inert PLACEHOLDER_* token
#      before shellcheck ever sees the line.
#   2. Cross-block references — a block only has the lines physically inside
#      its own fence, never a shebang, and often references `source`d files
#      (comics-server.sh) shellcheck can't resolve from a piped fragment.
#
# After neutralizing (1) and disabling the three fragment-inherent codes for
# (2) below, the real corpus is 100% clean — confirmed by rerunning both
# unmodified and calibrated against all 78 blocks. The one non-artifact hit
# this calibration surfaced (SC2154 on collection-sync.md's Step 3b reusing
# Step 2's `$ts`/`$EXPORT_JSON` across a fresh-shell block boundary — the
# exact Trap 1 class documented in multi-block-skill-shell-state-loss-
# fallback-swallow.md) was a genuine bug, fixed in this same change
# (collection-sync.md now re-derives both at the top of Step 3b). SC2154 is
# deliberately left ENABLED for that reason: it has real signal here, and
# ShellCheck's own ALL-CAPS-is-probably-an-env-var heuristic means it never
# fired on the (ALL_CAPS-by-convention) vars this corpus treats as
# environment-provided (COMICS_SERVER_URL, EXPORT_JSON, BACKUP_JSON, ...).
_PLACEHOLDER_ANGLE_RE = re.compile(r"<([A-Za-z0-9_.\-]+)>")
_PLACEHOLDER_BRACE_RE = re.compile(r"\{([A-Za-z0-9_.\-]+)\}")
# Square-bracket placeholder ("[options]"): only a lone bracketed word with no
# adjoining non-space char, so a real glob (`file[0-9].txt`) or test (`[ -f x
# ]`, which always has surrounding spaces inside the brackets) is untouched.
_PLACEHOLDER_BRACKET_RE = re.compile(r"(?<!\S)\[([A-Za-z][A-Za-z0-9_\-]*)\](?!\S)")

_SHELLCHECK_DISABLED_CODES = (
    # "Not following: X was not specified as input" — every skill sources
    # scripts/comics-server.sh or scripts/metron-curl.sh by a relative/
    # dynamic path a single-block fragment can never resolve.
    "SC1091",
    # "ShellCheck can't follow non-constant source" — same root cause, the
    # `source "$(git rev-parse --show-toplevel)/..."` dynamic-path form.
    "SC1090",
    # "var appears unused" — BY DESIGN in this corpus: a `## Step` block
    # routinely sets a var (BACKUP_PATH, CSV, ts, EXPORT_JSON) that a LATER,
    # separately-executed Step block reads (each Step is its own fresh shell
    # — see multi-block-skill-shell-state-loss-fallback-swallow.md). Flagging
    # that as "unused" would flag the documented architecture, not a bug.
    # (SC2154 — "referenced but not assigned", the other half of that same
    # cross-block pattern — is deliberately left enabled; see above.)
    "SC2034",
)


def _neutralize_doc_placeholders(line: str) -> str:
    def _token(m: re.Match[str]) -> str:
        return "PLACEHOLDER_" + re.sub(r"[.\-]", "_", m.group(1).upper())

    line = _PLACEHOLDER_ANGLE_RE.sub(_token, line)
    line = _PLACEHOLDER_BRACE_RE.sub(_token, line)
    line = _PLACEHOLDER_BRACKET_RE.sub(_token, line)
    return line


def _run_shellcheck_on_corpus(root: Path) -> list[tuple[str, int, str]]:
    """Run shellcheck over every fenced bash/sh/shell block under
    `root/.claude/commands/comic/*.md`. Returns (skill_filename,
    block_start_line, shellcheck_output) triples, one per block with any
    finding. Caller must confirm shellcheck is on PATH first — this does not
    skip on a missing binary."""
    skills_dir = root / ".claude" / "commands" / "comic"
    findings: list[tuple[str, int, str]] = []
    for md in sorted(skills_dir.glob("*.md")):
        text = md.read_text()
        for start, block_lines in _iter_shell_blocks(text):
            neutralized = [_neutralize_doc_placeholders(line) for line in block_lines]
            script = "#!/usr/bin/env bash\n" + "\n".join(neutralized) + "\n"
            proc = subprocess.run(
                [
                    "shellcheck",
                    "-s", "bash",
                    "-f", "gcc",
                    "-e", ",".join(_SHELLCHECK_DISABLED_CODES),
                    "-",
                ],
                input=script,
                capture_output=True,
                text=True,
            )
            if proc.stdout.strip():
                findings.append((md.name, start, proc.stdout.strip()))
    return findings


def _skip_if_no_shellcheck() -> None:
    if shutil.which("shellcheck") is None:
        pytest.skip(
            "shellcheck not installed — this must degrade gracefully on a "
            "clean local checkout (BUI-607); CI installs it explicitly, see "
            "the gixen-overlay job in .github/workflows/ci.yml"
        )


def test_skill_bash_blocks_pass_shellcheck():
    """BUI-607: every fenced ```bash block in a /comic:* skill is a copy/run
    instruction for an agent, so a real quoting/redirection/reference bug in
    one is a bug the agent will execute. Skips (never fails) when shellcheck
    isn't on PATH — see `_skip_if_no_shellcheck`."""
    _skip_if_no_shellcheck()
    findings = _run_shellcheck_on_corpus(REPO_ROOT)
    assert not findings, "shellcheck found issue(s) in skill bash block(s):\n" + "\n".join(
        f"{name}:{line}\n{out}" for name, line, out in findings
    )


def test_shellcheck_harness_actually_scans_something():
    """Guard the guard (mirrors test_harness_actually_found_endpoints): if
    fence/language extraction silently matched nothing, the test above would
    pass vacuously every run. Pin the known corpus size with slack (78 bash/
    sh/shell blocks across 15 skills as of BUI-607) rather than exact
    equality, so adding a skill doesn't require editing this pin. Runs
    unconditionally — no shellcheck binary needed, this only tests our own
    fence-parsing."""
    total = sum(
        len(_iter_shell_blocks(md.read_text())) for md in sorted(SKILLS_DIR.glob("*.md"))
    )
    assert total >= 70, (
        f"expected >=70 fenced bash/sh/shell blocks in the skill corpus, found {total} "
        "— fence/language extraction may be broken"
    )


def test_shellcheck_check_detects_a_real_violation(tmp_path):
    """Prove the check can fail (the same discipline solutions-lint's
    --self-test enforces, BUI-605): our own wiring — fence extraction,
    placeholder neutralization, the disabled-code list — could silently stop
    catching anything even though shellcheck itself works fine, e.g. a
    disabled code that's broader than intended would mask this too."""
    _skip_if_no_shellcheck()
    skill_dir = tmp_path / ".claude" / "commands" / "comic"
    skill_dir.mkdir(parents=True)
    (skill_dir / "fixture.md").write_text(
        "# Fixture\n\n"
        "## Step 1\n\n"
        "```bash\n"
        "cp $SRC_FILE $DEST_FILE\n"  # SC2086: unquoted expansion, word-splitting bug
        "```\n"
    )
    findings = _run_shellcheck_on_corpus(tmp_path)
    assert findings, "shellcheck wiring did not flag a deliberately unquoted expansion — check is vacuous"


def test_shellcheck_check_passes_clean_bash(tmp_path):
    """...and stays silent on a block that's actually fine, including the
    doc-placeholder idioms the neutralizer exists for — proves the
    neutralizer isn't just suppressing everything wholesale."""
    _skip_if_no_shellcheck()
    skill_dir = tmp_path / ".claude" / "commands" / "comic"
    skill_dir.mkdir(parents=True)
    (skill_dir / "fixture.md").write_text(
        "# Fixture\n\n"
        "## Step 1\n\n"
        "```bash\n"
        'comic-fmv --batch <working_list.json> --out <results.json> --brief\n'
        'gixen add {item_id} {max_bid} --comic-id {comic_id}\n'
        "npx tsx src/cli.ts new -t {tracking} -c {carrier} [options]\n"
        '```\n'
    )
    findings = _run_shellcheck_on_corpus(tmp_path)
    assert not findings, f"clean placeholder-only bash incorrectly flagged: {findings}"


# --- Class 2: intra-/cross-skill section-reference resolution ---------------
#
# docs/solutions/conventions/relocating-skill-sections-leaves-stale-inbound-
# references.md documents the drift class: a skill cites another skill's
# section by `file.md § Name` / `file.md § N` / `file.md Step N`, and a later
# rename/relocation/renumbering in the target orphans the pointer silently
# (CI stays green — there was no cross-reference linter before this one).
#
# Scope, deliberately narrow: only `file.md`-prefixed references, where
# `file.md` names another skill actually in SKILLS_DIR. Two things this does
# NOT attempt, both considered and rejected during calibration:
#   - Bare self-references with no filename ("see § Provider request budget",
#     "the math spec's §7") — real instances of both patterns exist in the
#     corpus, but "the math spec's §7" is an alias for a *docs/* file
#     (fmv-math-spec.md) with no `.md` mention nearby to key off of; treating
#     unprefixed "§ N" as "this file's own heading N" would have produced a
#     false positive there (fmv.md itself has no numbered headings — the
#     spec's headings are the target). Filename-prefixed refs only.
#   - References to non-skill files (docs/, .claude/agents/) — real and
#     common (e.g. `comic-grader.md`, `fmv-math-spec.md`), but validating
#     those needs whole-repo path resolution, a different and larger check.
#     Silently out of scope, not silently mis-flagged.
_CROSSREF_RE = re.compile(
    r"\b(?P<file>[a-z][a-z0-9-]*\.md)`?(?:'s)?\s*"
    r"(?:§\s*(?P<sec>[^\n]{1,80})|(?P<bare_step>Step\s+\d+(?:\.\d+)?))"
)
_CROSSREF_STOP_CHARS = ")].;,—–\n"
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def _extract_headings(text: str) -> list[str]:
    """Markdown headings only — explicitly OUTSIDE any fenced code block. A
    bash comment (`# BUI-138: ...`) is indistinguishable from a level-1
    markdown heading by `_HEADING_RE` alone, and this corpus's bash blocks are
    full of them; without this exclusion they'd leak into the heading list
    and could make a genuinely orphaned reference resolve by accident (a
    descriptor coincidentally substring-matching comment text)."""
    headings = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line.strip()) is not None:
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m:
            headings.append(m.group(2).strip())
    return headings


def _load_skill_headings(skills_dir: Path) -> dict[str, list[str]]:
    return {md.name: _extract_headings(md.read_text()) for md in skills_dir.glob("*.md")}


def _trim_crossref_descriptor(raw: str) -> str:
    for ch in _CROSSREF_STOP_CHARS:
        idx = raw.find(ch)
        if idx != -1:
            raw = raw[:idx]
    return raw.strip()


def _descriptor_resolves(descriptor: str, headings: list[str]) -> bool:
    """Longest-prefix match: try shrinking word-windows of `descriptor`
    against the target's headings (case-insensitive substring), so a
    reference like "Input shape owns the BUI-316..." resolves on its real
    2-word heading name "Input shape" without needing to guess exactly where
    the heading name ends and the surrounding sentence begins. A single-word
    candidate must be >=6 chars to count, so a short common word can't count
    as a spuriously "resolved" match."""
    words = descriptor.split()
    for n in range(min(len(words), 6), 0, -1):
        candidate = " ".join(words[:n])
        if n == 1 and len(candidate) < 6:
            continue
        candidate_lower = candidate.lower()
        if any(candidate_lower in h.lower() for h in headings):
            return True
    return False


def _crossref_findings(root: Path) -> list[tuple[str, int, str, str]]:
    """Returns (source_file, line, target_file, descriptor) for every
    `file.md § .../Step N` reference that does not resolve to a real heading
    in the named target skill file."""
    skills_dir = root / ".claude" / "commands" / "comic"
    headings_by_file = _load_skill_headings(skills_dir)
    findings: list[tuple[str, int, str, str]] = []
    for md in sorted(skills_dir.glob("*.md")):
        for i, line in enumerate(md.read_text().splitlines(), 1):
            for m in _CROSSREF_RE.finditer(line):
                fname = m.group("file")
                target_headings = headings_by_file.get(fname)
                if target_headings is None:
                    continue  # not a skill file (docs/, agents/, ...) — out of scope
                if m.group("bare_step"):
                    descriptor = m.group("bare_step")
                else:
                    descriptor = _trim_crossref_descriptor(m.group("sec"))
                    num_m = re.match(r"^(\d+[a-z]?)\b", descriptor)
                    if num_m:
                        descriptor = num_m.group(1)  # "8 owns the..." -> "8"
                if not descriptor:
                    continue
                if descriptor.lower().startswith("step "):
                    stepnum = descriptor.split(None, 1)[1]
                    ok = any(
                        re.search(rf"\bStep\s+{re.escape(stepnum)}\b", h, re.IGNORECASE)
                        for h in target_headings
                    )
                elif re.match(r"^\d+[a-z]?$", descriptor):
                    ok = any(
                        re.match(rf"^{re.escape(descriptor)}[.:)]", h) for h in target_headings
                    )
                else:
                    ok = _descriptor_resolves(descriptor, target_headings)
                if not ok:
                    findings.append((md.name, i, fname, descriptor))
    return findings


def test_no_skill_crossref_is_orphaned():
    """BUI-607, generalizing relocating-skill-sections-leaves-stale-inbound-
    references.md: every `file.md § Section`/`file.md Step N` reference from
    one skill into another must resolve to a real heading in the target.
    Caught live at authoring time: buy.md cited `fmv.md §8`/`fmv.md §6`, but
    BUI-438 moved that math into docs/conventions/fmv-math-spec.md — fmv.md
    carries no numbered headings anymore. Fixed alongside this check landing
    (buy.md now cites the spec doc directly)."""
    findings = _crossref_findings(REPO_ROOT)
    assert not findings, "orphaned cross-skill section reference(s):\n" + "\n".join(
        f"{src}:{line}: -> {target} § {descriptor!r} (no matching heading)"
        for src, line, target, descriptor in findings
    )


def test_crossref_harness_actually_scans_something():
    """Guard the guard: pin a floor on how many `file.md §...`/`file.md Step
    N` references the extractor finds across the real corpus, so a broken
    regex silently matching nothing doesn't make the test above pass
    vacuously. 12 resolvable refs exist as of BUI-607; assert well under
    that so a legitimate future edit removing one doesn't require re-pinning."""
    skills_dir = REPO_ROOT / ".claude" / "commands" / "comic"
    total_refs = sum(
        1
        for md in skills_dir.glob("*.md")
        for line in md.read_text().splitlines()
        for _ in _CROSSREF_RE.finditer(line)
    )
    assert total_refs >= 10, (
        f"expected >=10 file.md cross-references in the skill corpus, found {total_refs} "
        "— extraction may be broken"
    )


def test_crossref_check_detects_a_real_violation(tmp_path):
    """Prove it can fail: a reference to a section the target genuinely
    doesn't have."""
    skill_dir = tmp_path / ".claude" / "commands" / "comic"
    skill_dir.mkdir(parents=True)
    (skill_dir / "a.md").write_text(
        "# A\n\nSee b.md § Nonexistent Section for details.\n"
    )
    (skill_dir / "b.md").write_text("# B\n\n## Real Section\n\nContent.\n")
    findings = _crossref_findings(tmp_path)
    assert findings, "crossref check did not flag a reference to a genuinely missing section"


def test_crossref_check_passes_on_resolvable_and_non_reference_text(tmp_path):
    """...and stays clean both on a real resolvable reference AND on the two
    false-positive shapes calibration found in the live corpus: "file.md at
    Step N" (describes which of the *caller's* own steps reads file.md, not
    a pointer into file.md) and "file.md references ... (§ X)" (meta-prose
    about another document's citations, not a citation itself — both real
    sentences in buy.md/snipe-add.md)."""
    skill_dir = tmp_path / ".claude" / "commands" / "comic"
    skill_dir.mkdir(parents=True)
    (skill_dir / "a.md").write_text(
        "# A\n\n"
        "See b.md § Real Section for details.\n"
        "Reads `b.md` at Step 2.5 of this flow.\n"
        "`b.md` references specific parts of this file (§ Nonexistent, other stuff).\n"
    )
    (skill_dir / "b.md").write_text("# B\n\n## Real Section\n\nContent.\n")
    findings = _crossref_findings(tmp_path)
    assert not findings, f"non-reference prose incorrectly flagged as an orphaned crossref: {findings}"


# --- Class 3: forbidden idioms already outlawed in CLAUDE.md prose ----------
#
# Both scoped to fenced bash/sh/shell blocks only (a prose *warning* against
# the idiom, like collection-check.md's blockquote citing the bare CLI form
# by name, lives outside a code fence and must not self-trigger the lint).
_LOCG_BARE_CHECK_RE = re.compile(r"\blocg collection check\b(?!-batch)")


def _is_bare_collection_check_curl(line: str) -> bool:
    """`curl ... $COMICS_SERVER_URL.../api/comics/collection/check` (the
    single-item ownership endpoint) bypassing comics-api/check-batch — the
    literal curl analog of the bare-CLI idiom above (CLAUDE.md's BUI-352
    trap: a bare curl silently hits an empty host when the var is unset).

    Deliberately NOT "any curl touching $COMICS_SERVER_URL" — the existing
    test_no_skill_swallows_a_server_curl above already established, and this
    corpus already relies on, "a bare curl -sf/--fail" as a *sanctioned*
    fail-loud idiom for other endpoints (e.g. snipe-add.md's `curl -sf
    "$COMICS_SERVER_URL/health"`, which matches comics_health_gate's own
    internal implementation). A blanket ban here would flag that already-
    reviewed, already-tested pattern — two lints disagreeing about the same
    line is exactly what BUI-607's coordinates warn against replicating.
    """
    if "curl" not in line:
        return False
    if "$COMICS_SERVER_URL" not in line and "$GIXEN_SERVER_URL" not in line:
        return False
    if "/api/comics/collection/check/batch" in line:
        return False
    return "/api/comics/collection/check" in line


def _forbidden_idiom_findings(root: Path) -> list[tuple[str, int, str]]:
    skills_dir = root / ".claude" / "commands" / "comic"
    findings: list[tuple[str, int, str]] = []
    for md in sorted(skills_dir.glob("*.md")):
        for start, block_lines in _iter_shell_blocks(md.read_text()):
            for offset, raw in enumerate(block_lines):
                stripped = raw.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                lineno = start + offset
                if _LOCG_BARE_CHECK_RE.search(raw):
                    findings.append((
                        md.name, lineno,
                        "bare `locg collection check` (no -batch) reads the MacBook's "
                        "never-seeded local store — use `locg collection check-batch` "
                        "or the comics-api wrapper (BUI-504/BUI-510)",
                    ))
                if _is_bare_collection_check_curl(raw):
                    findings.append((
                        md.name, lineno,
                        "bare curl to /api/comics/collection/check bypasses the "
                        "resolve+health-gate comics-api provides — silently hits an "
                        "empty host when COMICS_SERVER_URL is unset (BUI-352); use "
                        "`comics-api GET /api/comics/collection/check`",
                    ))
    return findings


def test_no_skill_uses_forbidden_ownership_idioms():
    """BUI-607: bare `locg collection check` (no -batch) and a bare curl to
    /api/comics/collection/check both bypass the sanctioned check-batch/
    comics-api paths — both already outlawed in CLAUDE.md prose (the
    BUI-352/BUI-504/BUI-510 traps). Either reads a never-seeded local store
    or an unresolved/unhealth-gated host instead of the Mac Mini's
    authoritative collection store."""
    findings = _forbidden_idiom_findings(REPO_ROOT)
    assert not findings, "forbidden ownership-check idiom(s) found:\n" + "\n".join(
        f"{name}:{line}: {msg}" for name, line, msg in findings
    )


def test_forbidden_idiom_check_detects_a_real_violation(tmp_path):
    """Prove it can fail: one fixture per idiom."""
    skill_dir = tmp_path / ".claude" / "commands" / "comic"
    skill_dir.mkdir(parents=True)
    (skill_dir / "fixture.md").write_text(
        "# Fixture\n\n"
        "```bash\n"
        'locg collection check "Amazing Spider-Man" 300\n'
        'curl -sf "$COMICS_SERVER_URL/api/comics/collection/check?series=x"\n'
        "```\n"
    )
    findings = _forbidden_idiom_findings(tmp_path)
    assert len(findings) == 2, f"expected both forbidden idioms flagged, got {findings}"


def test_forbidden_idiom_check_passes_on_sanctioned_calls(tmp_path):
    """...and stays clean on the sanctioned replacements, a prose warning
    outside a code fence (must not self-trigger — mirrors collection-check.md
    § the docs use a blockquote to name the very idiom this lint forbids),
    and a bare `curl -sf .../health` (a different, already-sanctioned idiom;
    see `_is_bare_collection_check_curl`'s docstring)."""
    skill_dir = tmp_path / ".claude" / "commands" / "comic"
    skill_dir.mkdir(parents=True)
    (skill_dir / "fixture.md").write_text(
        "# Fixture\n\n"
        "> Never use the `locg collection check` CLI (no `-batch`) for ownership.\n\n"
        "```bash\n"
        "locg collection check-batch items.json --table\n"
        "comics-api GET /api/comics/collection/check -G --data-urlencode series=x\n"
        'comics-api POST /api/comics/collection/check/batch\n'
        'curl -sf "$COMICS_SERVER_URL/health"\n'
        "```\n"
    )
    findings = _forbidden_idiom_findings(tmp_path)
    assert not findings, f"sanctioned calls incorrectly flagged: {findings}"


# --- Class 4: fallback-swallow over the skill corpus (BUI-606's detector) ---
#
# Reuses lib.fallback_swallow.check_fallback_swallow directly per BUI-607's
# coordinates — writing a second swallow heuristic here is explicitly out of
# scope (two detectors disagreeing about the same files is the outcome this
# split was designed to prevent). collection-add.md:212 is BUI-606-adjudicated
# EXEMPT (cosmetic message-selection before an unconditional exit 1) and
# wishlist-add.md's former creds-block swallow is already fixed — neither is
# re-litigated here.
def test_no_skill_fallback_swallow():
    """BUI-607 invoking BUI-606: run the shared fallback-swallow detector over
    the live skill corpus. A finding here means a new `|| echo`/`|| true`/
    `2>/dev/null`-then-`||` shape landed without a hard stop — see
    lib/fallback_swallow.py and docs/solutions/workflow-issues/
    multi-block-skill-shell-state-loss-fallback-swallow.md."""
    findings = check_fallback_swallow(REPO_ROOT)
    assert not findings, "fallback-swallow finding(s) in the skill corpus:\n" + "\n".join(
        f"{f.path}:{f.line}: {f.message}" for f in findings
    )


def test_fallback_swallow_detector_is_wired_up(tmp_path):
    """Guard the guard: BUI-606's own module is responsible for proving the
    detector itself can fail (its own self-test fixtures). This test only
    proves *our* import + invocation actually reaches it, so a typo'd path or
    signature drift here doesn't silently no-op — a known-bad fixture must
    still be flagged when scanned through this same entry point."""
    skill_dir = tmp_path / ".claude" / "commands"
    skill_dir.mkdir(parents=True)
    (skill_dir / "fixture.md").write_text(
        "# Fixture\n\n"
        "```bash\n"
        "curl -sf \"$COMICS_SERVER_URL/api/comics/wish-list\" 2>/dev/null || echo '[]'\n"
        "```\n"
    )
    findings = check_fallback_swallow(tmp_path)
    assert findings, "check_fallback_swallow did not flag a known-bad fixture — wiring is broken"


# --- Class 5: cross-`## Step`-block variable state loss (BUI-634) -----------
#
# SC2154 ("referenced but not assigned", Class 1) already catches SOME of
# this class — the one bug Class 1's calibration found this way,
# collection-sync.md Step 3b's `$ts`/`$EXPORT_JSON` (see Class 1's
# calibration comment above), was exactly a variable set in one `## Step`
# block and read in a later one. But ShellCheck has a verified blind spot:
# it treats ALL-CAPS names as possibly-exported environment variables and
# stays silent on them. Identical code, only the case differs:
#
#   -d "{\"backup_path\": \"$BACKUP_PATH\"}"   # shellcheck: silent (ALL-CAPS)
#   -d "{\"backup_path\": \"$backup_path\"}"   # SC2154 (lowercase): warns
#
# This corpus names cross-step variables in ALL-CAPS almost exclusively
# (BACKUP_PATH, EXPORT_JSON, CSV, SCRATCH), so SC2154 catches the lowercase
# minority and misses the majority of the class it was kept enabled for.
# ShellCheck can't be made to see the rest even by tuning flags: Class 1
# pipes ONE fenced block into the binary at a time (a block never has a
# shebang or its siblings' content — see Class 1's own comment), so it
# structurally cannot know a variable was set in an EARLIER, separately
# piped-in block. This check tracks assignments itself, across fenced
# blocks, grouped by the `## Step` heading each block falls under — this
# corpus's own documented shell-boundary unit (see the SC2034 comment
# above: "each `## Step` block ... is its own fresh shell"). It flags a
# `$VAR`/`${VAR}` reference in a later `## Step` whose only assignment (in
# the whole file) lives under an earlier `## Step`, regardless of case.
_STEP_HEADING_RE = re.compile(r"^##\s+Step\b", re.IGNORECASE)

# Assignment forms this corpus actually uses: `FOO=...`, `export FOO=...`,
# `local`/`declare`/`readonly FOO=...`, `FOO+=...`, `for FOO in ...`, and
# `read [-r] FOO` (including `IFS=... read -r FOO`). Anchored at the start of
# the (stripped) line so a JSON/curl body fragment like `"title=Children of
# the Vault #1"` — not at line start — can never match.
_CROSS_STEP_ASSIGN_RE = re.compile(
    r"^\s*(?:export\s+|local\s+|declare\s+(?:-\w+\s+)*|readonly\s+)?"
    r"([A-Za-z_][A-Za-z0-9_]*)\+?=(?!=)"
)
_CROSS_STEP_FOR_RE = re.compile(r"^\s*for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")
_CROSS_STEP_READ_RE = re.compile(r"\bread\s+(?:-\w+\s+)*([A-Za-z_][A-Za-z0-9_]*)\b")

# Reference form: `$VAR` / `${VAR...}`. Deliberately excludes command
# substitution (`$(...)` — `(` is neither `{` nor a letter) and positional/
# special parameters (`$1`, `$@`, `$?`, `$$`, ... all start with a digit or
# symbol, never `[A-Za-z_]`) by construction, not by a denylist.
_CROSS_STEP_REF_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")

# A fenced block's heredoc body lines are never real shell statements (an
# assignment-shaped line inside one is literal payload, e.g. verify.md's
# `cat > working_list.verify.json <<'EOF' ... EOF` JSON template), so they're
# never scanned for assignments. A QUOTED delimiter (`<<'EOF'`/`<<"EOF"`)
# additionally disables bash variable expansion inside the body, so a `$VAR`
# there is literal text too and must not be scanned as a reference; an
# UNQUOTED delimiter (`<<EOF`) still expands, so those bodies stay in scope
# for reference scanning.
_HEREDOC_START_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _step_boundaries(text: str) -> list[tuple[int, int]]:
    """(heading_line_no, step_index) for every `## Step` heading, OUTSIDE any
    fenced code block, in document order. Mirrors Class 2's `_extract_headings`
    fence-skip for the same reason stated there: this corpus's bash comments
    all use single-`#` today, so a `## Step`-shaped line can't currently hide
    inside a fence, but this check shouldn't depend on that staying true.
    `step_index` is a simple 0-based ordinal, not a parse of "Step 3b" into
    (3, "b") — this check only needs "earlier than" / "same as", and document
    order already gives that."""
    boundaries: list[tuple[int, int]] = []
    idx = 0
    in_fence = False
    for i, line in enumerate(text.splitlines(), 1):
        if _FENCE_RE.match(line.strip()) is not None:
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _STEP_HEADING_RE.match(line):
            boundaries.append((i, idx))
            idx += 1
    return boundaries


def _step_index_for_line(line_no: int, boundaries: list[tuple[int, int]]) -> int:
    """-1 if the line precedes every `## Step` heading in the file (a
    preamble block, or a file that doesn't use `## Step` headings at all)."""
    current = -1
    for heading_line, step_idx in boundaries:
        if heading_line <= line_no:
            current = step_idx
        else:
            break
    return current


def _cross_step_state_findings(root: Path) -> list[tuple[str, int, str, str]]:
    """Returns (skill_filename, line, var, snippet) for every `$VAR`/`${VAR}`
    reference in a fenced bash/sh/shell block under a `## Step` heading whose
    ONLY assignment (anywhere in the file) lives under an EARLIER `## Step`
    heading — the BUI-634 class: each `## Step` is a separate shell, so that
    assignment is gone by the time this reference runs.

    Not flagged, by construction rather than a denylist:
      - Files with 0-1 `## Step` headings — no "later" heading can exist, so
        this class of bug cannot occur (most skills don't use `## Step` at
        all; this check only applies to the multi-shell `## Step` corpus).
      - A var assigned again anywhere under the SAME `## Step` heading as the
        reference (any block under that heading, not only the referencing
        one) — matches this corpus's own established re-derive convention
        (e.g. Step 3b already re-deriving `$ts`/`$EXPORT_JSON`); genuinely
        fine, not a bug.
      - A var with NO assignment anywhere in the file — env vars
        (`$COMICS_SERVER_URL`), loop/read-introduced names that are never
        ALSO an earlier cross-step assignment, and `$1`/`$@`/... (excluded by
        the reference regex's construction, not detected as "assigned" or
        "referenced" at all). Nothing this check can say is wrong; SC2154
        already covers true reference-with-no-assignment-anywhere for the
        lowercase half of this class (see Class 1's calibration note).
      - Full-comment lines (`#...`) — skipped for both assignment and
        reference scanning, so an explanatory `# ... $VAR ...` comment can't
        self-trigger.
      - Heredoc body lines — never scanned for assignments; scanned for
        references only when the delimiter is unquoted (see
        `_HEREDOC_START_RE`'s comment above).
    """
    skills_dir = root / ".claude" / "commands" / "comic"
    findings: list[tuple[str, int, str, str]] = []
    for md in sorted(skills_dir.glob("*.md")):
        text = md.read_text()
        boundaries = _step_boundaries(text)
        if len(boundaries) < 2:
            continue  # no "later" `## Step` heading can exist

        assigned_steps: dict[str, set[int]] = {}
        ref_events: list[tuple[int, int, str, str]] = []  # (line, step_idx, var, raw)

        for start, block_lines in _iter_shell_blocks(text):
            heredoc_delim: str | None = None
            heredoc_literal = False
            for offset, raw in enumerate(block_lines):
                line_no = start + offset
                if heredoc_delim is not None:
                    if raw.strip() == heredoc_delim:
                        heredoc_delim = None
                        heredoc_literal = False
                    elif not heredoc_literal:
                        step_idx = _step_index_for_line(line_no, boundaries)
                        for vm in _CROSS_STEP_REF_RE.finditer(raw):
                            ref_events.append((line_no, step_idx, vm.group(1), raw.strip()))
                    continue
                if raw.strip().startswith("#"):
                    continue
                step_idx = _step_index_for_line(line_no, boundaries)
                m = _CROSS_STEP_ASSIGN_RE.match(raw)
                if m:
                    assigned_steps.setdefault(m.group(1), set()).add(step_idx)
                m = _CROSS_STEP_FOR_RE.match(raw)
                if m:
                    assigned_steps.setdefault(m.group(1), set()).add(step_idx)
                for rm in _CROSS_STEP_READ_RE.finditer(raw):
                    assigned_steps.setdefault(rm.group(1), set()).add(step_idx)
                for vm in _CROSS_STEP_REF_RE.finditer(raw):
                    ref_events.append((line_no, step_idx, vm.group(1), raw.strip()))
                hd = _HEREDOC_START_RE.search(raw)
                if hd:
                    heredoc_delim = hd.group(2)
                    heredoc_literal = bool(hd.group(1))

        for line_no, step_idx, var, raw in ref_events:
            if step_idx < 0:
                continue
            steps = assigned_steps.get(var)
            if not steps or step_idx in steps:
                continue
            if any(s < step_idx for s in steps):
                findings.append((md.name, line_no, var, raw))
    return findings


def test_no_skill_references_cross_step_variable_without_reassignment():
    """BUI-634: SC2154 (Class 1) has a verified ALL-CAPS blind spot and, even
    setting that aside, can't see across fenced blocks at all — it analyzes
    each one in isolation. This check tracks assignments itself, across
    `## Step` boundaries, catching the class regardless of case. A finding
    here means a skill sets a variable in one `## Step` and reads it (unset)
    in a later one — the exact collection-sync.md `BACKUP_PATH`/`CSV` shape
    that motivated this check, both fixed alongside it landing (BUI-634)."""
    findings = _cross_step_state_findings(REPO_ROOT)
    assert not findings, (
        "cross-`## Step` variable reference without reassignment:\n"
        + "\n".join(f"{name}:{line}: ${var} -- {raw}" for name, line, var, raw in findings)
    )


def test_cross_step_state_harness_actually_scans_something():
    """Guard the guard (mirrors Class 1/2's harness-scans-something tests): if
    `_STEP_HEADING_RE`/`_step_boundaries` silently matched nothing, every
    skill file would look like it has <2 `## Step` headings, every file would
    be skipped, and the main test above would pass vacuously regardless of
    what bugs exist. Pin a floor — with slack — on how many real skill files
    have >=2 `## Step` headings (5 as of BUI-634: buy.md, collection-add.md,
    collection-sync.md, grade.md, wishlist-add.md)."""
    multi_step_files = [
        md.name
        for md in sorted(SKILLS_DIR.glob("*.md"))
        if len(_step_boundaries(md.read_text())) >= 2
    ]
    assert len(multi_step_files) >= 4, (
        f"expected >=4 skill files with >=2 `## Step` headings, found "
        f"{len(multi_step_files)} ({multi_step_files}) — Step-heading "
        "extraction may be broken"
    )


def test_cross_step_state_check_detects_a_real_violation(tmp_path):
    """Prove it can fail: the ticket's required fixture shape — FOO assigned
    in one `## Step` block, referenced (unset) in a later one."""
    skill_dir = tmp_path / ".claude" / "commands" / "comic"
    skill_dir.mkdir(parents=True)
    (skill_dir / "fixture.md").write_text(
        "# Fixture\n\n"
        "## Step 1\n\n"
        "```bash\n"
        'FOO="x"\n'
        "```\n\n"
        "## Step 2\n\n"
        "```bash\n"
        'echo "$FOO"\n'
        "```\n"
    )
    findings = _cross_step_state_findings(tmp_path)
    assert findings, "did not flag a cross-`## Step` reference to an unreassigned variable"
    assert findings[0][2] == "FOO"


def test_cross_step_state_check_passes_when_reassigned(tmp_path):
    """...and stays clean on the ticket's required counter-fixture: the same
    file, but the second `## Step` block re-assigns FOO before the
    reference — the sanctioned re-derive pattern this corpus already uses
    (Step 3b's `$ts`/`$EXPORT_JSON`, and this same change's collection-sync.md
    fix)."""
    skill_dir = tmp_path / ".claude" / "commands" / "comic"
    skill_dir.mkdir(parents=True)
    (skill_dir / "fixture.md").write_text(
        "# Fixture\n\n"
        "## Step 1\n\n"
        "```bash\n"
        'FOO="x"\n'
        "```\n\n"
        "## Step 2\n\n"
        "```bash\n"
        'FOO="y"\n'
        'echo "$FOO"\n'
        "```\n"
    )
    findings = _cross_step_state_findings(tmp_path)
    assert not findings, f"reassigned variable incorrectly flagged: {findings}"


def test_cross_step_state_check_ignores_false_positive_shapes(tmp_path):
    """...and stays clean on the shapes explicitly flagged as false-positive
    risks: a positional parameter (`$1`), a loop variable (`for`), same-block
    assign-then-use, a variable referenced only within the SAME `## Step`
    it's assigned in — and, the one that actually needs a LATER `## Step` to
    be meaningful, a `$VAR`-shaped literal inside a quoted heredoc body in a
    later Step. Without the heredoc protection this WOULD look like the
    real-violation shape (BAR assigned only in Step 1, "referenced" in Step
    2) — it stays clean only because a quoted `<<'EOF'` delimiter disables
    bash expansion, so `$BAR` inside is literal payload text, not a real
    reference (mirrors verify.md's own `<<'EOF'` JSON template)."""
    skill_dir = tmp_path / ".claude" / "commands" / "comic"
    skill_dir.mkdir(parents=True)
    (skill_dir / "fixture.md").write_text(
        "# Fixture\n\n"
        "## Step 1\n\n"
        "```bash\n"
        'echo "$1"\n'
        'for ITEM in a b c; do echo "$ITEM"; done\n'
        'BAR="inline"; echo "$BAR"\n'
        "```\n\n"
        "## Step 2\n\n"
        "```bash\n"
        "cat > payload.json <<'EOF'\n"
        '{"backup_path": "$BAR"}\n'
        "EOF\n"
        "```\n"
    )
    findings = _cross_step_state_findings(tmp_path)
    assert not findings, f"false-positive shape(s) incorrectly flagged: {findings}"
