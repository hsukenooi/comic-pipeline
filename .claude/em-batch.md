# em-batch repo profile — comic-pipeline

Loaded by the `/em-batch` skill (`~/.claude/skills/em-batch/SKILL.md`). The doctrine lives there; this file holds only what is specific to this repo. Section names match the skill's §0 contract. When a batch disproves a fact here, update this file at wrap-up.

## Repo shape & conventions

- Python/uv monorepo (`apps/*`, `packages/*`, `plugins/*`, `server/`), ~60k lines. This is the repo the doctrine's evidence was gathered in (the BUI-299..500 batches).
- New tickets → Linear **BUI** team.

## Hot files & import edges

- `server/main.py` / `server/db.py` are the recurring hot pair (all nine tickets in the BUI-383..389 batch touched them).
- The import edge that set the doctrine's rule: BUI-323 refactored `ebay_fetch.py` while BUI-322 imported it — parallel only with public signatures pinned stable.
- The all-hot-file batch happens here (BUI-383..389) but is the **exception**, not the rule — most waves parallelize across packages.

## Context economy

- **Big-file repo: coordinates are line ranges, caller lists, and pasted snippets.** `packages/locg-cli/src/locg/commands.py` is ~5,800 lines — roughly 70k tokens read whole — and a batch routinely puts three agents inside it. Before spawning, grep out the exact line ranges, the real caller list, and the snippet of the pattern to copy: *"Read only `commands.py:3842-4300`; do not read the file whole. The guard to replicate is: `<snippet>`."*
- Name any file safe to read whole (e.g. `metron.py`) and rule out the big files explicitly (BUI-485: the spawn prompt pasted `_disambiguate_series` verbatim and ruled out `commands.py`; the agent never touched the big file).
- Coordinates rot as **stale offsets**: earlier waves rewrite these files, so agents re-grep function/section names and never trust offsets after any merge (2026-07-23: `routes.py`/`commands.py` offsets moved every wave).
- **A grep for a SQL fragment can land in a one-time MIGRATION rather than the live query — confirm the enclosing function before pasting the line number.** These files carry legacy `_migrate_*` helpers whose SELECTs are near-identical to the production ones. 2026-08-12 (BUI-777): the EM grepped `SELECT id, grade, fmv_low, …` in `plugins/gixen-overlay/.../db.py`, got `:602`, and shipped that as the Part 1 target; `:602` is a legacy one-time migration and the real `list_comics` SELECT was at ~2320. The agent caught it, but only because it re-grepped rather than trusting the coordinate — a less careful one would have edited dead code and produced a green PR that changed nothing at runtime. Cheap fix: grep with `-n` and then confirm the nearest preceding `^def `, or grep the *function* first and read within its range.

## Local gates

- `apps/*`: `cd <pkg> && uv run --with pytest pytest`. `packages/*` and `plugins/*`: `uv run pytest`. Report counts per suite.

## Review classes

- The doctrine's generic full-fan-out classes apply literally here: **money, concurrency, correctness gates, external/data behavior** — this is where the 6×/$2k over-bid, the seen-set data-loss, and the batch-crash were caught.
- Persona starting map (chosen post-implementation, off the actual diff): a schema/migration change → correctness + adversarial + data-migration; a concurrency change → correctness + adversarial + reliability; a money-math change → correctness + adversarial.

## CI reality

- Five required gates on `main`: `workspace`, `apps-python`, `lint`, `ezship`, `solutions-lint` (BUI-631: branch protection requires all five; `enforce_admins: false`, so an admin can still bypass in a genuine emergency, and `strict: false`, so a branch need not be up-to-date with `main` to merge).
- **`typecheck` is NON-required** — don't block on it or check it per-PR; one glance per batch at wrap-up (`gh pr checks` on the final PR) is enough.
- Branch protection is the backstop for EM error here — the CI-watch exit code (0 = required gates passed) is trustworthy as a merge signal.

## Risk classes

- **A behavior-preserving refactor** → re-run the affected suite locally rather than trusting the agent's reported counts (BUI-389's `server/fallback.py` extraction: the real risk was a `main↔fallback` circular import, so the EM read the import structure and re-ran both suites before merging).
- **A schema migration** → apply it against a copy and confirm up/down.
- **A money-math change** → re-run the pricing path on a known case.
- **A change to an endpoint with a `dry_run`/preview mode** → check that the agent's test exercises BOTH modes, and read the diff for any arithmetic whose correctness depends on which mode is running. BUI-721 added `next_offset = offset + unresolved_count` to `POST /api/comics/backfill-year`; that is right for a real run (a written row leaves the scanned population) and wrong for a dry run (nothing is written, so no row leaves) — a fully-resolving page advanced `next_offset` by zero and looped forever, which is the very stall the ticket existed to fix, on the endpoint's *default* path. The branch's own paging test covered only the all-unresolvable case, where `unresolved_count == scanned` makes the correct and incorrect formulas coincide, so it passed against both implementations. **A test that passes against the buggy implementation too is the thing to look for here** (see `docs/solutions/workflow-issues/verification-whose-failure-is-indistinguishable-from-success.md`), and it is cheap to check: reproduce, then confirm the new regression test fails against the unfixed line before committing.
- **A guard change is verified at the DECISION, on the DEPLOYED build — never at the predicate, never at merge.** BUI-759 widened a hand-priced marker set and proved it against all 999 live `fmv` rows (exactly the right 12 matched, zero false positives). Criterion met, PR green, ready to close — and the guard still protected **1 of 12 rows**, because an eligibility test upstream (`fmv_runner.py`, `_split_by_db_cache`) gated the whole check behind `bool(book.get("locg_id"))` and 11 of 12 books had none. Only a post-deploy probe of the *decision function* caught it (BUI-775). So for any guard/protection/exclusion diff: call the bucketing function over the live population and assert the rows land in the protected bucket, **and neutralize the short-circuits upstream of it** (here `max_age_days=0`, or a fresh cache hit means the guard is never exercised and the run looks protected for the wrong reason). Also compare the guard's identity key against the *writer's* — BUI-775's root cause was the guard keying on `locg_id` while `upsert_comic` keys on `(title, issue, variant)`.
- **`comics-api` defaults `COMICS_CURL_MAX_TIME=30`, which is shorter than several real calls.** `POST /api/comics/backfill-year` blocks ~30s *per row*, so at `limit=5` a call takes ~150s: curl aborts while the server keeps working and commits the page, leaving the caller with no `next_offset` and a blind retry that pages from a stale offset. Export a generous `COMICS_CURL_MAX_TIME` for any long endpoint and build the loop to **halt and reconcile against the store** on a non-zero exit, never to retry. (Same family as `cli.py`'s `_DEFAULT_SERVER_TIMEOUT=15` reporting 36 failures for 11 committed rows.)
- **A `.backup` copy retains `journal_mode=wal`, so opening it `mode=ro` fails** with "unable to open database file" — a read-only connection cannot create the `-shm` file WAL needs. Open backup copies read-write (they are throwaway) or pass `immutable=1`; the live DB still gets `-readonly`.
- **Probing a write endpoint on the Mini is itself a write, and can look like an outage.** `POST /api/comics/backfill-year` shells out to Metron synchronously, blocking the single-process server's whole event loop for up to `limit × 30s`; at `limit=3` the `comics-api` health gate reported "server is not responding" ~90s. That is documented behavior, not a crash — probe write endpoints with `dry_run=true` and `limit=1`, and wait rather than restarting.

## Deploy model

- **Nothing ships on merge.** Deploy actions are manual: service restart via `launchctl kickstart`; install refresh via `uv tool install --force --no-cache` / `uv sync --all-packages`.
- Live probe: a `--help` or curl per new endpoint/subcommand on the target — `uv tool install --force` has silently served a stale cached wheel with the new subcommands missing (BUI-455).
- **`./scripts/deploy.sh` is blocked by the auto-mode classifier when the EM invokes it directly.** Ask the user to run it (`! ./scripts/deploy.sh`) rather than working around the block; it succeeds when the user asks for it explicitly. Budget for this: a batch whose tickets have post-deploy acceptance criteria will stall here twice if two waves ship code.
- **A merged ticket with a live-verification acceptance criterion stays open until the probe runs.** BUI-767 ("verified by looking at a real flagged row, not by reading the CSS") and BUI-759 ("a default `comic-fmv` run leaves all 12 untouched") both merged green and were both still unproven. Comment "shipped in PR #N, deploy action pending" and close only after the probe — closing at merge is how a guard gets marked Done while not running.
- The BUI-383..389 batch shipped two migrations (`bids.group_changed_at` and `group_wins.source` + a unique-index re-key) that sat merged-but-not-deployed until the user asked — the class of thing the deploy checklist exists for.

## Data safety

- The doctrine's backup→diff ritual applies as written — it was converged on here (BUI-514), and the BUI-461 backfill over 22 pending rows is the model case of a legitimate, user-gated post-merge production write.

## Knowledge routing

- `/ce-compound` output → `CONCEPTS.md` + `docs/solutions/`. When a filed doc-sweep ticket will itself edit those files, scope the compound step to reusable principles and traps and let the ticket own the concrete edits (BUI-393).

## Unattended

- **Viable.** Unattended means local and scheduled on the **Mac Mini**. The `.claude/settings.local.json` rules (`git push`, `gh pr create`, `gh pr checks`, `linear issue create`/`comment`/`update`) must be user-authored — verify they exist before scheduling; if a prompt blocks a run, say so in the handoff.
