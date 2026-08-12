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
- **Probing a write endpoint on the Mini is itself a write, and can look like an outage.** `POST /api/comics/backfill-year` shells out to Metron synchronously, blocking the single-process server's whole event loop for up to `limit × 30s`; at `limit=3` the `comics-api` health gate reported "server is not responding" ~90s. That is documented behavior, not a crash — probe write endpoints with `dry_run=true` and `limit=1`, and wait rather than restarting.

## Deploy model

- **Nothing ships on merge.** Deploy actions are manual: service restart via `launchctl kickstart`; install refresh via `uv tool install --force --no-cache` / `uv sync --all-packages`.
- Live probe: a `--help` or curl per new endpoint/subcommand on the target — `uv tool install --force` has silently served a stale cached wheel with the new subcommands missing (BUI-455).
- The BUI-383..389 batch shipped two migrations (`bids.group_changed_at` and `group_wins.source` + a unique-index re-key) that sat merged-but-not-deployed until the user asked — the class of thing the deploy checklist exists for.

## Data safety

- The doctrine's backup→diff ritual applies as written — it was converged on here (BUI-514), and the BUI-461 backfill over 22 pending rows is the model case of a legitimate, user-gated post-merge production write.

## Knowledge routing

- `/ce-compound` output → `CONCEPTS.md` + `docs/solutions/`. When a filed doc-sweep ticket will itself edit those files, scope the compound step to reusable principles and traps and let the ticket own the concrete edits (BUI-393).

## Unattended

- **Viable.** Unattended means local and scheduled on the **Mac Mini**. The `.claude/settings.local.json` rules (`git push`, `gh pr create`, `gh pr checks`, `linear issue create`/`comment`/`update`) must be user-authored — verify they exist before scheduling; if a prompt blocks a run, say so in the handoff.
