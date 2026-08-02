---
date: 2026-08-01
topic: repo-improvements
focus: open-ended (surprise-me mode)
mode: repo-grounded
---

# Ideation: Repository Improvements (Surprise-Me)

Generated 2026-08-01 via ce-ideate: 3 grounding agents (codebase scan, docs/solutions learnings survey, external web research) + 6 ideation frames (~46 raw ideas) → adversarial filter → 7 survivors. Dispositioned into Linear 2026-08-02 (see Disposition section).

## Grounding Context

### Codebase context

**Shape:** Python/TypeScript monorepo for comic-collecting automation. `packages/gixen-cli` (eBay snipe bidding + FastAPI comics server + SQLite `bids` table), `packages/locg-cli` (collection/wish-list cache, Metron matcher), `plugins/gixen-overlay` (comic routes/tables/dashboard as plugin into gixen-cli), `apps/ebay|fmv|ezship` (standalone CLIs, uv-tool-installed, shell-out/HTTP boundaries only), `.claude/commands/comic/` (16 orchestrator skills). ~90 test files, per-package pytest. Production on a Mac Mini (launchd `com.comics.server`). docs/solutions/ has ~50 categorized past-problem docs; CONCEPTS.md codifies domain vocabulary.

**Pain points (from scan):** uv stale-wheel deploy trap (BUI-455, recurs); plugin→host private-API coupling; FMV money-path complexity (8+ learned failure modes); collection identity-key fragility (provider relabeling manufactures duplicates annually); snipe status inference TOCTOU/phantom-WON classes; wish-list BUI-122 data-loss class (safety lives in exactly ONE enforcement layer — the year-blind owned-safe export; all else is surfacing).

**Recent activity:** high churn of small focused fixes, zero architectural work. Premise-failure learning cycle (BUI-573..579), four falsified FMV pool-shape signals (BUI-578/582/592/590), status-vocab grounding, title normalization, TOCTOU fix.

### Past learnings (docs/solutions meta-patterns)

1. **Dominant recurring trap class in EVERY category: "fails green"** — silent failure wearing a healthy face; ≥9 independent docs (no-op test invocations, non-required mypy, cross-package contracts breaking while per-package suites pass, the BUI-593 422-rejected-writes incident, vacuous drained-partition guards, `|| echo ""` swallows, fetch-err vs genuine-zero shape identity, stale-wheel deploys). Highest-leverage direction per the learnings survey: cross-cutting silent-failure observability.
2. **Premise-drift is the most chronic workflow cost** — verify-ticket-premise doc tags 25+ tickets, unbounded accretion; its remedy is prose, not mechanism.
3. **HTTP-only contracts need source-parsing canaries proven able to fail** (BUI-588/593).
4. **Recurring bug class: mutations keyed on non-unique ids** (`WHERE item_id` — 5 incidents, 2 packages); documented grep heuristic, closed only by discipline.
5. **Guard strictness must match consequence** — fail-closed gates scoped to only the destructive branch; `DO NOT REMOVE` blocks enumerating reproduced harms.
6. **Skills are code without code's tooling** — 4 docs on skill/markdown drift; skill lint is a concrete uncovered automation direction.

**Documented dead ends (hard exclusions honored throughout):** automating fmv §7a CGC-proxy (BUI-326 Won't Do); any FIFTH pool-shape advisory signal (four falsified on measurement); scoping/conditioning the owned-safe export enforcement layer; re-litigating PriceCharting (BUI-525) or bestOfferAccepted bias (BUI-552); bulk-removing wish-list conflicts; gating the eBay WON-inference.

### External context (web research 2026-08-01)

Prior art: CLZ+CovrPrice (live pricing sold as $90/yr premium tier), Card Ladder (brokerage-style portfolio valuation), WatchCharts, grading-ROI calculators, PSA grade-probability predictors, Myibidder (group snipes). Adjacent: camelcamelcamel (per-product price-history partitioning, graceful degradation to cached data), healthchecks.io dead-man's-switch (ping on SUCCESS; absence alerts), provider-reliability stacks (auto reordering, per-provider breaker thresholds). Market signals: eBay sold-data APIs structurally closed — scraping reliance is imposed, not fixable; Gixen outages/silent failed bids are vendor-characteristic. Cross-domain: Kelly fractional sizing ≈ bid cap as uncertainty-discounted fraction of FMV edge.

## Topic Axes

Decomposition skipped — surprise-me mode.

## Ranked Ideas

### 1. Silent-Failure Observability Stack
**Description:** One layer that alerts on the *absence of success* rather than the presence of errors: (a) `heartbeats` table + `/api/heartbeat/{job}` — every recurring job (gixen sync, wishlist-sellers, collection-sync, FMV refresh) pings on success; a watchdog flags any job whose last success exceeds its declared cadence; (b) FastAPI middleware persisting every 4xx/5xx on mutating `/api/comics/*` into a `rejected_writes` table with payload snapshot + replay; (c) 2–3 sentinel books (deep liquid pools, e.g. ASM #300) + one negative-control query run on schedule — sentinel n=0 means the instrument broke, never used as FMV; (d) per-snipe outcome watchdog: alert when a PENDING snipe's terminal status never arrives by deadline (never gates the WON-inference).
**Basis:** direct: "fails green" in ≥9 docs/solutions docs; BUI-593 (every FMV write 422'd, stored nowhere, all guards green); verified un-persisted 422 sites routes.py:348,456,985,1255. external: healthchecks.io dead-man's-switch; negative-control methodology (Lipsitch et al.).
**Rationale:** One mechanism family retires all nine documented fails-green shapes plus future unimagined ones — it monitors the invariant ("this path succeeds regularly"), not enumerated failure modes. Sentinels legitimately sidestep the falsified pool-shape class: the expected answer is known in advance.
**Workflow lens:** silence inverts from "assume it worked" to "silence means healthy"; debugging sessions start at the failing layer with evidence (read the rejected_writes row) instead of theory selection — the exact failure mode of BUI-585.
**Downsides:** Alert-fatigue tuning; each job needs a declared cadence + success definition; the watchdog itself needs an external ping; sentinel runs spend provider budget.
**Confidence:** 85% · **Complexity:** Medium (phased) · **Status:** Unexplored → Project created (see Disposition)

### 2. Learning-to-Lint: Compile docs/solutions into CI (+ supersession metadata)
**Description:** `mechanized_by:`/`lint:` frontmatter carrying an executable check per solutions doc; CI job runs the pack; /ce-compound prompts "what part of this learning is a check?" per new doc. Seeds: `WHERE item_id` non-unique-key-mutation lint (5 incidents, 2 packages), `|| echo ""` fallback-swallow detector, skill-lint family extending the BUI-173 test_skill_contracts.py harness (shellcheck fenced blocks, cross-reference resolution, forbidden idioms: bare `locg collection check`, bare `curl $COMICS_SERVER_URL`). Companion: `status: active|corrected|falsified|superseded` + `superseded_by:` frontmatter, backfilled for known corrections (4 canceled pool-shape signals, BUI-579, BUI-559).
**Basis:** direct: grounding meta-patterns ("documented grep heuristic could be a CI lint. Closed only by discipline today"); ruff exception-hygiene CI job + BUI-173 harness as in-repo precedent; only one solutions doc carries any correction marker today.
**Rationale:** ~50 learned-trap docs whose half-life is bounded by whether an agent re-reads them; compiled, each learning protects every future commit unconditionally, and falsified claims become machine-dead.
**Workflow lens:** CI does the remembering; /ce-compound gains a completion criterion (an incident is closed when *enforced*, not documented); the per-session "read the correction before repeating" briefing ritual retires.
**Downsides:** Not every learning mechanizes (advice-only tag needed); noisy lints erode trust.
**Confidence:** 85% · **Complexity:** Low-Medium · **Status:** Unexplored → Project created

### 3. Deploy Attestation + One-Command Deploy
**Description:** Stamp git SHA into every uv-tool-installed package at build time (hatch build hook → `_build_info.py`); `gixen --version`/`locg --version` report it; server exposes it via `/api/health` (from git at startup — editable .venv is a second mechanism). `scripts/deploy.sh` runs the documented ritual idempotently (`--force --no-cache` installs, `uv sync --all-packages`, `launchctl kickstart`) then asserts every deployed SHA == merged HEAD, failing loudly. Optional: dashboard staleness banner; later `resurrect.sh` restore path.
**Basis:** direct: BUI-455 ("--force alone silently reinstalls the STALE cached wheel"), BUI-365, BUI-377 — three incidents of "deployed code isn't merged code," defended today by ~25 lines of CLAUDE.md prose.
**Rationale:** The purest fails-green instance: every test passes — on the wrong code. Attestation makes all future stale-deploy modes self-announcing; the CLAUDE.md folklore becomes deletable.
**Workflow lens:** the stale-deploy confounder drops out of every future differential diagnosis; the ritual runs at the worst moment (post-merge context switch) today and becomes one red/green command.
**Downsides:** Build-hook wiring across 4 packages; unattended auto-deploy deliberately deferred (rollback story needed on a money system).
**Confidence:** 90% · **Complexity:** Low-Medium · **Status:** Unexplored → loose Issue created

### 4. Mechanized Premise Preflight (`premise-check <BUI-ID>`)
**Description:** Pre-work step wired into ticket intake: pull the ticket via the Linear CLI, extract named symbols, file paths, constants, quoted strings, and row-count claims; grep the repo for each; optionally re-run cited read-only SQL to re-measure claimed counts; cross-check docs/solutions falsification records; emit a premise report — confirmed / drifted / absent / renamed-near-miss — posted to the issue before implementation starts.
**Basis:** direct: verify-ticket-premise-before-implementing.md tags 25+ tickets, 18 applies_when bullets, still accreting; "the one-command check (grep the named symbol) is prose, not mechanism." Five of six ideation frames converged on this independently — the strongest convergence signal in the run.
**Rationale:** Premise-drift is the most chronic documented workflow cost; mechanized, verification cost stays constant as the trap catalog grows, and the doc stops accreting.
**Workflow lens:** every ticket starts from verified ground; stop-and-report becomes a cheap, legitimized outcome; the backlog itself gets more trustworthy (stale tickets flagged at pickup, not executed).
**Downsides:** Free-text claim extraction has false-positive risk (keep killer items small); advisory-vs-blocking to decide; extractor is itself code to maintain.
**Confidence:** 75% · **Complexity:** Medium (v1 scoped) · **Status:** Unexplored → loose Issue created (v1 scope)

### 5. Capital-Commitment Layer at `gixen add`
**Description:** Treat order entry as the choke point it is (SEC 15c3-5 analogy): (a) pre-trade checks in CLI/server — challenge a bid > N× linked FMV high, an FMV older than K days, a duplicate live snipe on the same item, an aggregate PENDING-exposure ceiling; (b) replace snipe-add's flat 80%-of-FMV-high cap with a deterministic discount function of (fmv.confidence, comps n, FMV range width, grade range width) — Kelly-style shading, advisory; (c) an immutable `decisions` row per snipe recording FMV inputs, collection-check verdict, grade output, computed cap + rule, executing skill.
**Basis:** direct: verified cli.py:289-326 — max_bid's only validation is Decimal-parse; FMV-link failure is non-blocking; unused confidence/comps columns in the fmv schema. external: Kelly fractional sizing; order-audit-trail practice.
**Rationale:** The pipeline quantifies uncertainty exhaustively (grade panels, confidence tiers, comps counts) then discards all of it at the moment money commits. Gates order entry, never status classification (distinct from the excluded WON-inference gating).
**Workflow lens:** the approval moment becomes informed rather than vigilant; retro disputes become row lookups; the trust boundary moves from "the agent followed the prose" to "the system won't pass an out-of-policy bid" — the survivor that most directly buys delegation headroom.
**Downsides:** Money path — ship advisory-first with explicit override; never block a snipe on ledger failure.
**Confidence:** 80% · **Complexity:** Medium · **Status:** Unexplored → Project created (plan-first)

### 6. Comps Data Flywheel
**Description:** Stop discarding paid-for data: (a) persist every individual sold comp (title, price, date, grade-context, provider, query) keyed by comic identity — today the fmv row keeps only low/high/count; (b) harvest first-party comps: every WON/LOST bid carries winning_bid plus our own photo-grade — write a first_party_comps row at classification (the only comps source whose grade is *known*); (c) downstream views fall out: append-only FMV history per recompute, staleness queries, Card Ladder-style `/api/comics/portfolio` (cost basis vs current FMV, gain/loss).
**Basis:** direct: verified fmv schema (`comps INTEGER` is a count; UNIQUE(comic_id,grade) upsert destroys priors); bids.winning_bid populated at classification; BUI-565/570 (re-runs compound provider budget). external: camelcamelcamel history partitioning; Card Ladder; CLZ+CovrPrice pricing. reasoned: scraping reliance is structurally imposed and historical data can never be backfilled — delay is unrecoverable loss.
**Rationale:** Converts the most fragile external dependency into a depreciating one; reframes FMV from "input to one bid" into "valuation of everything owned." Dead-end-safe: no pool-shape signal, no bid gating.
**Workflow lens:** pricing questions become local/instant/free; outages degrade to "priced from cached comps, labeled" instead of blocking the buy at auction deadline; first-party comps accrue with zero added ritual.
**Downsides:** Schema + retention decisions; first-party comps must start as a labeled, non-blended tier (must not become the 9th FMV money-path failure mode).
**Confidence:** 80% · **Complexity:** Medium-High (phased) · **Status:** Unexplored → Project created (plan-first)

### 7. Identity Spine + Quarantine
**Description:** (a) Make metron_id (already threaded through 5 locg-cli modules) the primary identity anchor with a versioned variant-string alias table (library-science authority records): provider relabels — the annual `(YYYY - Present)` → `(YYYY - YYYY)` generator, reused `Vol. N` labels, masthead renames — become alias appends instead of manufactured duplicates. (b) Add a `quarantined` state to the collection store: removes a row from every matcher candidate pool (record-win, collection-check, conflicts, wishlist dedup) while keeping it present for sync round-trips so LOCG never re-emits it — the missing third state between full-citizen and unsafe-delete, generalizing the BUI-563/564 fix beyond record-win.
**Basis:** direct: the BUI-546/554/560/581/591/596 normalizer patch series with its falsification tail (BUI-559/574); "LOCG reuses Vol. N labels"; foreign-edition compounding loop "fixed for record-win only"; bids tombstone as precedent. external: MARC/VIAF authority control.
**Rationale:** Each normalizer patch fixes one variant class and risks over-folding; an ID-anchored spine inverts the risk profile — a lookup table cannot over-fold — and retires the annual relabel tax permanently. The most consequential data-model decision available in the repo.
**Workflow lens:** identity incidents demote from code tickets (branch/tests/falsification risk) to data edits; suspect rows get dispositioned once instead of re-litigated per audit; the class "investigation caused by identity drift" largely exits the backlog.
**Downsides:** Highest complexity + migration risk, adjacent to the BUI-122 data-loss class — stage it (alias table consulted before the heuristic first; re-key later); must preserve the matcher-vs-identity normalization doctrine (opposite pressures, never share the function).
**Confidence:** 70% · **Complexity:** High (quarantine alone: Low-Medium, ships first) · **Status:** Unexplored → Project created (plan-first)

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| 1 | Guard-liveness registry / mutation-test every guard | Meta-mechanism covered by survivor 1 + the existing prove-it-can-fail convention; standing registry cost exceeds incremental value |
| 2 | Nightly tri-party reconciliation w/ aged breaks | Folded into survivor 1 as a later phase; standalone it duplicates /comic:verify + the DLQ; break-aging noted as the novel bit |
| 3 | Fetch-write conservation check per FMV run | Subsumed by survivor 1's DLQ + heartbeats (same incident class, weaker mechanism) |
| 4 | Minimum Equipment List for degraded ops | Reasoned-only basis; governance artifact heavy for a solo operator; revisit if skill count keeps growing |
| 5 | Conflict-holds registry | Real and well-grounded but narrow; below the strategic cut — file directly as a BUI ticket when wanted |
| 6 | Second snipe provider (Myibidder standby) | Expensive second stateful money-path integration; survivor 1's snipe watchdog delivers most of the risk reduction (detect → bid manually) at a fraction of the cost |
| 7 | Zero-ritual unattended auto-deploy | Folded into survivor 3 minus the unattended part — auto-shipping a bad merge to a money system needs a rollback story first |
| 8 | Resurrection drill (Mini as cattle) | Real gap, but key data has external copies and backup discipline is documented; optional extension of survivor 3 |
| 9 | Mark-to-market portfolio (standalone) | Folded into survivor 6 as the payoff view |
| 10 | FMV append-only history (standalone) | Folded into survivor 6 tier 1 |
| 11 | Uncertainty-priced caps / pre-trade gate / decision ledger (standalone trio) | Folded into survivor 5 — three frames converged on the same choke point |
| 12 | Heartbeats / DLQ / sentinels / snipe watchdog (standalone quartet) | Folded into survivor 1 — one architecture, phased |
| 13 | Supersession metadata / skill lint (standalone pair) | Folded into survivor 2 as companion + seed pack |

## Disposition (2026-08-02)

Linear mapping (BUI team): **5 Projects + 2 loose Issues.**

| Survivor | Linear primitive | Label | Planning doc? |
|---|---|---|---|
| 1 Observability stack | Project, 4 sub-issues | comics | No — job→cadence→success contract table is a deliverable of the first issue |
| 2 Learning-to-lint | Project, 4 sub-issues | comics | No — the convention spec IS the design artifact |
| 3 Deploy attestation | loose Issue | comics | No |
| 4 Premise preflight | loose Issue (v1 scope) | comics | No — promote to Project only if scope grows |
| 5 Capital-commitment layer | Project, plan-first | comics | Yes — /ce-brainstorm → /ce-plan before sub-issues |
| 6 Comps data flywheel | Project, plan-first | comics | Yes — comp identity key depends on Identity Spine direction; settle keying before schema |
| 7 Identity spine + quarantine | Project, plan-first | locg-cli | Yes — deepest plan; staged rollout argued in writing |

Planning docs are written just-in-time (one /ce-brainstorm session when each project starts, seeded from this doc), not in advance — pre-written plans for weeks-later work are the premise-drift trap this repo documents. Open-questions stubs live in each plan-first project's first issue. Sequencing: Observability + deploy-attestation Issue first.
