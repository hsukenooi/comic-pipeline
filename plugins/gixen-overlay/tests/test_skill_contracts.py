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
"""
from __future__ import annotations

import re
from pathlib import Path

import gixen_overlay.routes as overlay_routes

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS_DIR = REPO_ROOT / ".claude" / "commands" / "comic"


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
            if "GIXEN_SERVER_URL" not in line and "/api/comics" not in line:
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
    that actually does the inference."""
    doc = (SKILLS_DIR / "wishlist-add.md").read_text()
    assert "comics_resolve_server" in doc
    assert "comics_health_gate" in doc


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


def test_endpoint_names_are_provider_neutral():
    """CLAUDE.md invariant: comics endpoints are provider-neutral — never
    /api/comics/locg/*. A drift here would leak the provider into the URL the
    skills hard-code."""
    leaky = {p for p in _registered_comics_paths() if "/locg" in p}
    assert not leaky, f"non-neutral comics endpoint(s) registered: {sorted(leaky)}"
