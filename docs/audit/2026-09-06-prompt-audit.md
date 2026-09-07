# Prompt audit — 2026-09-06

Audit of the repo's prompt surface for instructions written for older models or older versions of the repo itself ("cruft"), run via `/claude-api prompt-audit`. Deliverables: this report, the combined patch at `docs/audit/2026-09-06-prompt-audit.patch`, and one patch per Linear ticket under `docs/audit/patches/` (see "Execution plan" at the end). Nothing has been applied.

## Assumptions

- **Scope:** the whole working directory's prompt surface (the request named no file). Inventory below.
- **Target model:** Claude Fable 5.1 — the model running these skills in Claude Code today, and the current flagship. No skill pins a model; the only pin in code is Haiku 4.5 for seller-scan's verifier (`claude-haiku-4-5-20251001`, a current model — not a finding).
- **Provenance:** every file was written between 2026-04-05 and 2026-08-13 (Opus 4.6 → Fable 5 era). So "cruft" here is mostly not Claude-3-era idioms (none found: no scratchpad tags, no prefills, no "think step by step") but **migration-relative text** — prose that describes a diff against a previous version of the skill the model never saw — plus **rotted specifics** (commands, paths, and claims the code has moved past).

## Inventory

| Surface | Files | Lines |
|---|---|---|
| Sub-agent definitions | `.claude/agents/comic-grader.md`, `comic-identifier.md` | 350 |
| Skills | `.claude/commands/comic/*.md` (16 files incl. `references/date-backfill.md`) | 3,712 |
| Rule files | `CLAUDE.md`, `packages/gixen-cli/CLAUDE.md`, `packages/locg-cli/CLAUDE.md`, `.claude/em-batch.md` | 448 |
| Model-call code | `apps/ebay/src/seller_scan.py` (`_build_verification_prompt`, `_verify_via_claude_cli`, `_parse_verification_response`) | ~120 |
| Harness config | `.claude/settings.json` (Stop hook), `settings.local.json` (permissions) | 29 |

Model-call sites (Group 4 count): five. seller-scan's Haiku verifier, the `comic-grader` agent, grade.md's triage pre-pass, and date-backfill's research sub-agent are genuine judgment calls. The `comic-identifier` agent is mostly deterministic (see L5).

## Summary

| Group | Findings |
|---|---|
| 1 — dated prompt text (mostly 1d migration-relative phrasing; one 1a, one 1b, three 1c) | 17 |
| 2 — brittle skill files (rotted commands, paths, and claims) | 11 |
| 3 — tool descriptions | 0 (agent and skill descriptions are precise trigger text; nothing to change) |
| 4 — request config and architecture | 4 (one `replace-with-API-feature`, one verified keep, two flags) |
| Confidence | 13 high, 17 medium, 5 low/flag |

The three findings that matter most:

1. **The identifier agent's fetch command does not run on the Mac Mini as written** (H1). `comic-identifier.md` calls `python src/ebay_fetch.py`; there is no `python` on this machine (only `python3`), while the installed `ebay-fetch` console script does the same job. Every `/comic:identify` and `/comic:buy` Step 1 currently depends on the agent improvising around a failing command.
2. **A stale one-time bootstrap in `CLAUDE.md` would overwrite the live server store** (H3). The "one-time server seed" `cp data/locg/*.json ~/.comics-server/collection-store/` has already been run (the directory exists on the Mac Mini); `data/locg/` is now a gitignored, stale local cache. An agent that reads the line as a setup step destroys the authoritative collection.
3. **Roughly 200 lines across 11 files narrate diffs against earlier versions of the same file** (H6–H13, M7, M10, M15, M16). "This replaces the old rule", "no longer", "as of BUI-543 … now", "it briefly wasn't". The model never saw the earlier version, so the text reads as phantom alternatives and history the executor has to reconcile. `calibration-report.md` alone spends 110 of its 317 lines on it. The patch rewrites each as the current rule with its reason kept and the ticket id kept as a pointer.

Two things the audit deliberately did **not** flag, because the evidence says they are load-bearing: the BUI-569 `SendMessage` delivery lines (M1 — a live probe reproduced the failure they guard), and the repo's habit of citing ticket ids. Ticket ids are pointers to the reason and cost a few tokens; the harm is in the narrative around them, not the id.

## Findings

Ordered by confidence. `file:line` refers to the current file. Action column: `rewrite`/`remove`/`add`/`replace-with-API-feature` have a hunk in the patch; `keep`/`flag` do not.

### High

| # | Location | Evidence | Pattern | Why obsolete | Action |
|---|---|---|---|---|---|
| H1 | `.claude/agents/comic-identifier.md:23,29,36` | `cd … && python src/ebay_fetch.py --json …`; `pip install -e . -q` | G2 volatile specifics (hardcoded path/interpreter) | `which python` → not found on the Mac Mini; `ebay-fetch` is installed at `~/.local/bin` with the same flags. Install hint predates `scripts/install.sh`. | rewrite → `ebay-fetch --json …`, install via `./scripts/install.sh` |
| H2 | `.claude/commands/comic/verify.md:199` | "handled by step 7 of `/comic:collection-add` — it runs inline in the same Playwright session and checks `in_collection`, `wish_removed`, `db_linked`" | G1d fossil; G2 duplicates that disagree | collection-add has had no step 7 and no Playwright since 2026-05-23 (PER-111); the file's own first line says "No Playwright". | rewrite → point at the record-win commit response and collection-sync Step 6 |
| H3 | `CLAUDE.md` (collection section) | "**One-time server seed** (run once on the Mac Mini): `mkdir -p … && cp data/locg/{collection,wish-list}.json …`" | G2 time-sensitive content; G1d fossil | `~/.comics-server/collection-store` exists on this machine; the seed ran in June. `data/locg/` is gitignored and stale, so re-running clobbers the live store. | remove → state where the store lives and that recovery goes through backup/restore |
| H4 | `packages/locg-cli/CLAUDE.md:124` vs `:181` | "Session cookies are stored at `~/.config/locg/cookies.json`" vs "**Note:** Existing `~/.config/locg/cookies.json` is no longer read" | G1d migration note; internal contradiction | The file says both. The Playwright profile (`:153,:176`) is the real location. | rewrite `:124`, remove `:181` |
| H5 | `packages/locg-cli/CLAUDE.md:13-31, 75-137` | `locg search`, `find`, `series`, `comic`, `releases`, `login`, `add`, `remove`, `update`, `check` documented as working commands | G2 API claims with no verification date | locg.com blocks all programmatic access (user-confirmed 2026-08-10, standing). The commands are wired (`locg --help`) but do not work from an agent; an agent reading this file will try them. | add → status banner at the top naming what is blocked and what still works |
| H6 | `.claude/commands/comic/fmv.md:103-106` | "This 'one batch' advice is safe at any batch size **again** (BUI-701). It **briefly wasn't**. … **anymore** — it **no longer** helps" + the 2026-07-31 incident block | G1d migration-relative phrasing; G2 history narrative | ~250 words to state one current rule: dispatch is paced in code, so don't chunk. The workaround it argues against was never in this file's current text. | rewrite → 4-line current rule |
| H7 | `.claude/commands/comic/buy.md:164`; `fmv.md:42` | "This replaces the old 'non-Marvel/DC only' rule, which disabled …"; "This corrects the older … advice, which silently disabled a shipped fix" | G1d migration-relative | Diff against a prior prompt version. The rule and reason ("`ebay-sold-comps` applies per-publisher policy centrally") stand on their own. | rewrite |
| H8 | `.claude/commands/comic/snipe-add.md:17-41, 65-85, 148` | "no EXECUTOR CONTRACT / ORCHESTRATOR NOTES split (unlike `collection-check.md` …)"; pre-flight `echo "${COMICS_SERVER_URL:-UNSET}"` + `curl -sf "$COMICS_SERVER_URL/health"` | G1d patch accretion; G2 duplicates that disagree | `collection-check.md` no longer has that split (BUI-504). The hand-rolled curl is the pattern `CLAUDE.md` says `comics-api` superseded (BUI-510), it skips the hostname resolver every other skill uses, and the pre-flight's "stop if unset" contradicts the file's own direct-Gixen fallback (`:243`). | rewrite → present-tense role section; `comics-api GET /health` pre-flight with the fallback explicitly user-gated |
| H9 | `packages/gixen-cli/CLAUDE.md:11-47` | 12 `pytest tests/test_*.py` lines; `python cli.py …`; "Deploy the server on Mac Mini: `bash server/install.sh`" | G2 volatile specifics | `tests/` has 22 files (10 unlisted); the CLI is the `gixen` console script everywhere else; deploy is `./scripts/deploy.sh` (BUI-612), `server/install.sh` is the first-time LaunchAgent installer. | rewrite → one test command, `gixen …`, deploy.sh |
| H10 | `.claude/commands/comic/wishlist-add.md:288-290, 296-297, 375-376, 227-235, 45, 410` | "this replaces the old per-issue `curl` loop, which turned a 40-issue run into 40 sequential POSTs"; "as of **BUI-387** the year is now **persisted**"; "so you no longer need to SSH"; "the redundant work this step removes" | G1d migration-relative | Each describes what an earlier version did. | rewrite |
| H11 | `.claude/commands/comic/calibration-report.md:26-135` (+ `:174-176, 184-190, 231-234, 244-247, 303-304, 312-317`) | "**What changed in BUI-532, and why:** earlier versions of this skill banned …"; "**BUI-543 update:** … Before BUI-543 the server-side gate required … That was a real gap; it is now closed"; "BUI-532 was a doc-only ticket that rebased this skill's vocabulary but left the server-side aggregate's docstring/sort/gate on the pre-BUI-532 framing — BUI-543 closed that gap. The plan's R4 text … predates both tickets" | G1d migration-relative; G2 history narrative + editor-directed meta | 110 lines of ticket archaeology around ~35 lines of rule. The reasons (censoring, the frozen-`max_bid` confound, why a single loss is noise) are kept; the "what used to be true" is not. | rewrite → present-tense "The signal" section; cross-references updated; history moved to a two-line footer |
| H12 | `.claude/commands/comic/collection-sync.md:196` (+ `:145-154, 184-191, 253-256, 290-293, 440-456, 469-481`) | "The durable fix is record-win populating dates (BUI-210); **until then**, follow …" | G2 duplicates that disagree (`references/date-backfill.md:15` says the fix "landed in code"); G1d ("No client-side split needed", "no longer hard-stops", "no longer removes anything", "the earlier ≤20 rows belief", "BUI-554 removed the partition this used to require", "Its first reading is **3**") | Same file family contradicts itself on whether BUI-210 shipped; the rest are diffs against prior versions and a volatile count. | rewrite (7 hunks) |
| H13 | `.claude/commands/comic/collection-check.md:8-12, 74` (+ `buy.md:110`) | "As of BUI-504 … mechanizes what used to be a ~450-line prose executor … The skill is now just …"; the only step heading is `## Step 4: Decision gate` | G1d migration-relative; fossil heading | Steps 1–3 were mechanized away; "Step 4" with no 1–3 reads as skipped steps. | rewrite; rename heading and its `buy.md` cross-reference |

### Medium

| # | Location | Evidence | Pattern | Why obsolete / verdict | Action |
|---|---|---|---|---|---|
| M1 | `comic-grader.md:184`, `comic-identifier.md:107-109`, `seller-scan.md:123-127` | "A sub-agent's plain-text return does not reach the caller on its own (BUI-569)" | G1d harness workaround, dated 2026-07-31 | **Keep — verified live, and the mechanism is now known.** Two probes with no delivery instruction: an *unnamed* sub-agent's final reply arrived mid-turn as a completion notification, seconds after it finished. A sub-agent spawned with a `name` (the BUI-366 pattern every comic agent uses so it stays addressable for follow-ups) became a long-lived teammate; its reply was delivered, but wrapped in an idle notification that only landed at the caller's **next turn boundary**, 18 minutes after the agent finished (idle at 08:48:21Z, delivered after the turn ended at ~09:06Z). For an orchestrator mid-run that is the BUI-569 wasted round-trip: the result exists but cannot be read until the user prompts again. So the line is required exactly because the agents are named. Worth adding that reason next to the rule; re-probe at each Claude Code release. | keep |
| M2 | `seller-scan.md:39-53, 59-62, 76-80, 111-117, 143, 194`; `grade.md:21`; `locg-cli/CLAUDE.md:7` | `.venv/bin/python src/seller_scan.py …`; `python3 …/grade_photos.py`; `PYTHONPATH=src python3 -m locg` | G2 volatile specifics | `seller-scan`, `grade-photos`, `locg` are installed console scripts (`scripts/install.sh`). The venv path still works, so medium not high. | rewrite |
| M3 | `apps/ebay/src/seller_scan.py:525-527, 570-576, 596-611` | prompt: "Respond with a JSON array containing ONLY the ids …"; code: `--output-format text` + `re.search(r"\[.*\]", text, re.DOTALL)` | G1b JSON-forcing scaffold + regex extraction | `claude -p` supports `--output-format json --json-schema` (verified with one Haiku call: the envelope carries a schema-validated `structured_output`). The scaffold and bracket-hunt are the pre-structured-output shape. Fail-closed drop on a missing/invalid object is kept. | replace-with-API-feature; tests updated (`test_seller_scan.py`: transport class + the bullets-stable anchor); 425/425 pass on the patched tree |
| M4 | `comic-identifier.md:85-93, 95-99` | "`grade_source` is `"missing"` → check the title first; if the grade appears explicitly in the title …"; Ends arithmetic from CURRENT UTC TIME | G4 LLM re-doing deterministic work; G1b arithmetic in prose | `ebay_fetch.extract_grade` already checks specifics → title → description, so `grade_source: "missing"` means the title had no grade; the instruction asks the model to second-guess the parser. Time-remaining is arithmetic the script could emit. | rewrite the grade logic (patch); the Ends field is a code change (see L5) |
| M5 | `calibration-report.md:137-150`; `wishlist-sellers.md:16-26` | "**`COMICS_SERVER_URL` must be set.** Set it once in `~/.zshrc` … `GIXEN_SERVER_URL` is a deprecated alias" | G2 volatile specifics; contradicts the BUI-172 convention | `comics-api` resolves the URL from the env var or the hostname (`scripts/comics-server.sh:28-51`) and health-gates before every call, so calibration-report needs no setup. `wishlist-sellers` is a child process, so it needs `comics_resolve_server` exported first — the pattern `fmv.md:22-28` already uses. | rewrite |
| M6 | `collection-add.md:127-156, 171-181, 370` | the 9 s/win derivation and the 40-win worst case stated three times (prose, bash comments, Common Mistakes) | G1c repetition; G1b arithmetic in prose; keep-list #11 (re-baseline adds) | One statement of the bound suffices. The harness now offers `run_in_background`, which is not subject to the 600 000 ms ceiling — the missing option for the >46-win case the text worries about. | rewrite (consolidate) + add `run_in_background` |
| M7 | `collection-add.md:90-102` | "BUI-422's original `$25` price threshold was removed in BUI-475 after the server-side auto-resolve … was shown to fail open" | G1d migration-relative | The current rule is "no price threshold, no confidence threshold"; the removed threshold is a phantom alternative. | rewrite |
| M8 | `comic-grader.md:93, 120, 183` | PRINT-LAYER rule restated in two parentheticals besides its own section and the output field; step 9 "Be rigorous — do NOT inflate" | G1c repetition as reinforcement; G1a generic virtue | Current models apply a rule stated once; the substantive half of step 9 ("let coverage cap your confidence") is already the CONFIDENCE rule. | rewrite → bare pointers; remove step 9 |
| M9 | `comic-grader.md:176` | "Use the Read tool on every img-XX.jpg" | keep-list #11 re-baseline (Fable 5.1 vision guidance: crop/zoom lifts accuracy on dense or degraded images) | The grader has Bash and PIL 11.3 is importable from `python3`; nothing tells it to zoom on a corner, staple, or suspected mark. | add one crop-and-Read line |
| M10 | `fmv.md:46, 51, 55, 58-60, 130-131` | SerpApi-era recall counts (34→12, 38→21); "Real incident: … (ASM #50, 2026-07-13)"; "the notes prefix is no longer the claim … supported for one release"; "(the FF #16 run, 2026-07-16, burned ~11 tool calls …)" | G2 history narrative; G2 time-sensitive; G1d | Rules kept with a one-clause reason; dated anecdotes and a retired provider's numbers dropped. | rewrite |
| M11 | `grade.md:18, 21, 24, 32, 76, 92, 96` | "(BUI-279 — extracted from this skill so …)"; `python3 …/grade_photos.py`; "(U9)" plan-item labels | G2 history; G2 volatile specifics | `grade-photos` is installed; "U9" is a plan-item label meaningless to the executor. | rewrite |
| M12 | `verify.md:12-14, 22-25, 50-62, 175-177, 189-192` | "as of BUI-507 … no longer reads this file at all"; "the BUI-352/BUI-375 trap this structurally removes"; `cat > working_list.verify.json` in the repo cwd | G1d migration-relative; G2 contradicts the scratch-dir convention (BUI-430) | The file writes a scratch JSON into the working tree; every other skill uses `comics_scratch_dir`. | rewrite (5 hunks) |
| M13 | `wishlist-add.md:194-199, 283-284, 302-303` | the AMM #1 incident told twice; the `year_began` prohibition stated a fourth time | G2 history narrative; G1c repetition | The rule ("printings are distinct collectibles") carries the reason; Step 3 owns the `year_began` rule. | rewrite |
| M14 | `CLAUDE.md` (collection section); `collection-check.md:108-111` | "it reads the MacBook's local store, which is never seeded" | G2 factual drift | The trap is on either machine (the store is local on both; see the collection-check feedback memory). | rewrite |
| M15 | `CLAUDE.md` ("collapses `/comic:collection-add`'s old inline-merge …", "is now gitignored", "chose a pure rename (Option A)"); `locg-cli/CLAUDE.md:52-67, 93-98` | "As of BUI-476 … no longer honor that fallback"; "As of BUI-208 … no longer touches" | G1d migration-relative | Present-tense rules with the ticket kept as a pointer. | rewrite |
| M16 | `buy.md:16-24, 63, 67, 225-246, 263, 304, 318, 324`; `collection-add.md:322-323` | "post-BUI-504 … post-BUI-507"; "so this actually runs"; "BUI-359's original rule, unchanged … no longer applies as-is"; "instead of dispatching `snipe-add.md`'s per-item loop"; "collapses the old separate Step 6 sub-agent"; "This step is now just" | G1d migration-relative | Same class; buy.md is the orchestrator, so its text is read on every run. | rewrite (8 small hunks) |
| M18 | `seller-scan.md:70, 166`; `ezship-add.md:57` | "a `Task`/`Agent` subagent" (old tool name); "BUI-542 split `--all` into …"; "the CLI now exits non-zero" | G2 volatile specifics; G1d | Trivial; included for completeness. | rewrite |

### Low / flag (no hunk)

| # | Location | Evidence | Pattern | Note |
|---|---|---|---|---|
| L1 | `comic-grader.md:22` | batch anti-anchoring guard, "a measured drift toward higher point grades … was observed (BUI-81 U9)" | keep-list #5: prohibition against a demonstrated failure | Measured in June 2026 on an Opus 4.x-era model. Keep, but it is the one grader rule worth re-probing on Fable 5.1: re-grade the frozen BUI-51 fixture (`~/comic-grader-fixtures/bui-51-baseline`) with the guard removed and diff. |
| L2 | `buy.md:172, 195, 223` | "never propose a max bid from a ledger-advisory band" stated at three decision points | G1c repetition | Working redundancy on a money rule that does not disagree with itself; leave unless the copies drift. |
| L3 | `.claude/em-batch.md` | incident narratives per rule; `:53` "deploy.sh is blocked by the auto-mode classifier" | G2 recency trap; undated harness claim | The file is designed as an incident ledger and is user-curated at wrap-up; apply the guide's question ("would this have helped most recent sessions?") there, and date the harness claim. |
| L4 | seller-scan verifier | no per-call token/cost accounting | G4 no token accounting | The M3 envelope carries `total_cost_usd` and `usage`; logging them to stderr is a free win and the prerequisite for measuring any cleanup here. |
| L5 | `comic-identifier` agent (architecture) | fetch → `comic-identify` per title → time-remaining arithmetic → markdown table, all fully determined by two CLIs | G4 LLM executor for a deterministic plan | The one adaptive use is follow-up Q&A over the held JSON (BUI-366). Proposed code change, not a prompt hunk: an `ebay-fetch --table` (or `comic-identify --table`) mode that emits the identification table plus a `time_remaining` field, with the named agent kept only for follow-ups. |

## What was checked before flagging

- `which python` → absent; `ebay-fetch`, `seller-scan`, `comic-identify`, `grade-photos`, `gixen`, `locg`, `comic-fmv`, `comics-api`, `wishlist-sellers` → present in `~/.local/bin`.
- `scripts/comics-api` → `comics_resolve_server` (env var, else hostname) then `comics_health_gate`, then the call.
- `~/.comics-server/collection-store` exists; `scutil --get LocalHostName` → Mac-Mini.
- `ebay_fetch.extract_grade` checks item specifics, then title, then description.
- `claude -p --output-format json --json-schema …` (one Haiku call): envelope has `structured_output` with the validated object.
- Sub-agent delivery probes with no delivery instruction: the unnamed spawn's reply arrived mid-turn as a completion notification; the named spawn's reply arrived only inside an idle notification at the next turn boundary, 18 minutes after it finished (BUI-569 reproduced for the named case the comic agents use).
- Patched tree (`git apply --check` clean): `apps/ebay` seller-scan suite 425/425; `packages/gixen-cli` `test_skill_migration.py` 17/17; `plugins/gixen-overlay` `test_skill_contracts.py` 40/40; `packages/locg-cli` `test_collection_audit_pending.py` 21/21.
- Not independently verified: the LOCG block (H5) rests on the user-confirmed 2026-08-10 standing state; re-confirm before relying on the banner's wording.

## Kept on purpose

Money-safety rules with their reasons (R11 hard-fail, the ledger-advisory cap, the indeterminate-row protocol, the BUI-122 data-loss guards), the fragile-operation scripts in `collection-sync.md` and `collection-add.md` Step 3/3b, the grader's CGC criteria and coverage-driven confidence contract, the creator-run-from-Metron prohibition (a demonstrated, current failure), every ticket id used as a pointer, and all skill/agent `description` trigger text.

## Verification after applying

1. H1/M2: run `/comic:identify` on one listing and `seller-scan <known seller>` once.
2. M3: `cd apps/ebay && uv run --with pytest pytest -q tests/test_seller_scan.py`, then one real `seller-scan --json` and confirm `filtered` reasons still arrive.
3. M8/M9/L1: re-grade the BUI-51 fixture before and after; diff grades, ranges, and confidence.
4. H11: run `/comic:calibration-report` once and check the two-tier rendering is unchanged.

## Execution plan

Filed 2026-09-07 as BUI-889 through BUI-900 (team BUI, label `comics`; 889 and 890 also `bug`). Each ticket's description names its hunks; `docs/audit/patches/<ID>.patch` holds exactly those hunks, generated **cumulatively in the merge order below**, so each one applies cleanly only after the ones above it have merged. `mkpatch.py` (all edits, tagged by finding) and `mkticketpatches.py` (the ticket map and the sequence check) sit beside them for regeneration if a rebase ever breaks one: `python3 docs/audit/patches/mkticketpatches.py` rewrites every patch and re-verifies the sequence in a throwaway worktree. Delete `docs/audit/patches/` once BUI-897 merges.

| Order | Ticket | Title | Hunk tags | Files |
|---|---|---|---|---|
| 1 | BUI-890 | Remove the Stale Server-Seed Instruction From CLAUDE.md | H3, M14 | `CLAUDE.md`, `collection-check.md` |
| 2 | BUI-889 | Fix comic-identifier's Fetch Command and Grade Logic | H1, M4 | `agents/comic-identifier.md` |
| 3 | BUI-894 | Resolve the Contradictions Between Skill Files | H2, H4, H12-210, M12 | `verify.md`, `collection-sync.md`, `locg-cli/CLAUDE.md` |
| 4 | BUI-896 | Mark LOCG Live Access as Blocked in locg-cli's CLAUDE.md | H5 | `locg-cli/CLAUDE.md` (needs the blocked list confirmed first) |
| 5 | BUI-893 | Point the Comic Skills at the Installed Console Scripts | M2, H9, M5, M11 | `seller-scan.md`, `grade.md`, `gixen-cli/CLAUDE.md`, `locg-cli/CLAUDE.md`, `calibration-report.md`, `wishlist-sellers.md` |
| 6 | BUI-892 | Strip Migration-Relative Prose From the Comic Skills | H6, H7, H10, H12, H13, M7, M10, M13, M15, M16, M18 | 10 files, 35 hunks |
| 7 | BUI-891 | Rewrite calibration-report.md in the Present Tense | H11 | `calibration-report.md` |
| 8 | BUI-899 | Run Large record-win Commits in the Background | M6 | `collection-add.md` |
| 9 | BUI-895 | Route snipe-add's Pre-Flight Through comics-api | H8 | `snipe-add.md` (needs the direct-Gixen decision first) |
| 10 | BUI-898 | Re-Baseline the comic-grader Prompt on Fable 5.1 | M8, M9 | `agents/comic-grader.md` plus the fixture re-grade |
| 11 | BUI-897 | Verify seller-scan's Haiku Pass With a JSON Schema | M3 | `apps/ebay/src/seller_scan.py`, `tests/test_seller_scan.py` |
| 12 | BUI-900 | Emit the Identification Table From ebay-fetch | none (new work) | `apps/ebay`, `agents/comic-identifier.md` |

**Step 0.** Commit this report, the combined patch, and `docs/audit/patches/` to main (docs only) so the tickets' references resolve and the patches survive a workspace cleanup.

**Wave 1, orders 1 to 8, by hand, one PR each.** For each ticket, in order: `scripts/start-work.sh <ID>` (In Progress first), branch from fresh `main`, `git apply docs/audit/patches/<ID>.patch`, run `plugins/gixen-overlay` `test_skill_contracts.py`, `packages/gixen-cli` `test_skill_migration.py`, and `packages/locg-cli` `test_collection_audit_pending.py`, open the PR, wait for the five required gates with a bare `gh pr checks` (never `--watch`), merge, then comment with the PR link and close. Order 4 waits on the blocked-command confirmation. Skip nothing: the patches assume every earlier one is in.

**Wave 2, orders 9 to 12, after the two decisions.** BUI-895 needs the direct-Gixen answer (recommendation: keep it reachable from the `gixen` CLI only; an unrecorded snipe is invisible to verify and collection-add). BUI-898 is 24 grader runs against `~/comic-grader-fixtures/bui-51-baseline` (8 listings × baseline, patched, guard-removed) in one sub-agent job that emits a diff table for review. BUI-897 is code with tests already in the patch (425/425 pass) and deserves correctness plus adversarial review. BUI-900 is a new CLI mode, about half a day, last or Someday. These three suit `/em-batch`.

**Deploy and probes.** After waves 1 and 2, the user runs `! ./scripts/deploy.sh` on the Mac Mini (auto mode blocks an agent invoking it), then the Done-when probes: one `/comic:identify` (889), one `seller-scan --json` (893, 897), one `/comic:calibration-report` (891), one standalone `/comic:snipe-add` (895). Those tickets stay In Progress until their probe runs. BUI-899's criterion needs a batch above 46 wins; either accept a long In Progress or change the criterion to a simulated long call.
