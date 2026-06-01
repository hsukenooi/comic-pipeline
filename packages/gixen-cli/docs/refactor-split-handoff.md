# Refactor Handoff: Extract Generic gixen-cli, Move Comics to comic-pipeline

**Status:** Active. Linear Project `PER` / "Separate gixen-cli from Comics Use Case" (https://linear.app/hk-iterative/project/separate-gixen-cli-from-comics-use-case-f54d62e4b982). Target date 2026-06-08.

This document is the handoff brief for a fresh Claude Code session continuing this refactor in the `gixen-cli` repo. Read it top to bottom before touching code.

---

## TL;DR

`gixen-cli` is currently a generic Gixen-sniping tool *with comic-specific code grafted into it*. We are extracting the comic code into a new `comic-pipeline` monorepo (which will also absorb `ebay-cli`, `ezship-cli`, and the `/comic:` skill namespace) and leaving `gixen-cli` as a clean, plugin-extensible tool that any Gixen user could install.

The mechanism is a Python entry-point plugin API (`gixen.plugins`) that lets external packages register FastAPI routes, SQLite tables, and dashboard tabs. Comics becomes the first such plugin, living in the new `comic-pipeline` repo.

When this is done, a non-comic Gixen user can `pip install gixen-cli`, get a working snipe tool with a dashboard, and never see the word "comic" anywhere. A comic user installs `gixen-cli` *plus* the `comic-pipeline` overlay package, and gets the full setup.

---

## Why We're Doing This

1. **Modular design forces cleaner boundaries.** Comic-specific Pydantic models, regex, routes, tables, and HTML are currently tangled into a generic tool. Disentangling them via a plugin contract surfaces the seam.
2. **`gixen-cli` becomes publishable.** Other Gixen users could benefit; today the repo is unusable to them because of comic-specific surface area.
3. **`comic-pipeline` becomes the single home for the comic stack.** Today the comic workflow spans 4 repos plus 9 skill files in an Obsidian vault. Consolidation simplifies development and skill PATH management.
4. **Dashboard extensibility.** Today the dashboard can only show snipes. A plugin-tab API lets future overlays (comic collection view, other product overlays) attach their own UI.

We chose this approach (called "Option E" in the original analysis) over four alternatives (one mega-monorepo, keep-everything-separate, partial consolidation, etc.) specifically because it forces modular design *and* yields a shareable artifact.

---

## Current State of This Repo (What's Comic-Specific)

Anything in this list must move out to `comic-pipeline/plugins/gixen-overlay/` during the extraction:

### Backend (`server/`)

- **`server/title_parser.py`** (347 lines) — entirely comic-specific. Parses series/issue/year/grade from eBay titles. Move whole file.
- **`server/db.py`** — tables `comics` and `bid_comics` are comic-specific (columns: `title`, `issue`, `year`, `grade`, `fmv_low`, `fmv_high`, `fmv_comps`, `fmv_confidence`, `fmv_notes`, `fmv_updated_at`, `locg_id`, `locg_variant_id`). The generic `bids` table stays. Schema-creation code for the comic tables moves to the plugin. **Cross-plugin FK:** `bids.comic_id INTEGER REFERENCES comics(id)` (server/db.py:31) — core's `bids` currently has a foreign key into the (soon-to-be-plugin) `comics` table. PER-27 must drop the `REFERENCES comics(id)` declaration and treat `comic_id` as an untyped int link the plugin interprets.
- **`server/main.py`** — 5 dedicated comic routes, plus 3 "generic" routes that are comic-contaminated:
  - **Dedicated (move with plugin):**
    - `GET /v2/comics` (page render)
    - `GET/POST /api/comics`
    - `POST /api/bids/{item_id}/comics/locg` (LOCG link)
    - `POST /api/extract-comics`
  - **Contaminated (need decontamination in core + plugin enrichment hook):**
    - `GET /api/snipes` (server/main.py:829) — `LEFT JOIN comics`, returns 11 comic columns in every row
    - `GET /api/history` (server/main.py:924) — same join, same columns
    - `GET /api/bids` (server/main.py:987) — same join, same columns
    - `POST /api/bids` (`api_add_bid`, server/main.py:781-826) — inline `upsert_comic` call at lines 785–800
    - `PUT /api/bids/{item_id}` (`api_edit_bid`, server/main.py:1026-1082) — comic-specific UPDATE block at lines 1051–1063
  - **Pydantic models — three are contaminated:**
    - `UpsertComicRequest` (server/main.py:606–624) — fully comic-specific, moves whole.
    - `LocgLinkRequest` (server/main.py:693–696) — fully comic-specific, moves whole.
    - `AddBidRequest` (server/main.py:627–663) — has 11 comic-specific optional fields (`comic`, `issue`, `year`, `grade`, `fmv_low`, `fmv_high`, `fmv_comps`, `fmv_confidence`, `fmv_notes`, `locg_id`, `locg_variant_id`). Generic in name, contaminated in shape.
    - `EditBidRequest` (server/main.py:666–678) — has `locg_id` and `locg_variant_id`.

### Frontend (`server/static/`)

- **`v2-comics.html`** — comic dashboard tab. Moves to the plugin.
- **`v2.css`** stays in core (shared chrome).
- **`index.html` (snipes tab) is contaminated** — not just a nav link to `/v2/comics`. The "generic" snipes page renders comic-specific UI: `gradeLabel` function (lines 333–356), `fmtFmv` (302–308), `displayCondition` consuming `comic_grade` (377–380), `displayTitle` consuming `comic_title`/`comic_issue` (358–361), and a "fmv" column in both active and ended tables (419, 429, 451, 461). The demo data fixture (197–287) is also comic-themed. Decontamination in PER-26/28 means either stripping these fields from the core page (then plugin re-injects them) or core ships a truly generic snipes view and the comic plugin ships its own snipes overlay.

### Anywhere else

- **`cli.py` is *not* currently generic** — the handoff originally claimed it was, this is incorrect. Today `cli.py` contains:
  - Nine comic flags on `add`: `--comic`, `--issue`, `--year`, `--grade`, `--fmv-low`, `--fmv-high`, `--fmv-comps`, `--fmv-confidence`, `--fmv-notes`, `--locg-id`, `--locg-variant-id` (cli.py:225–240)
  - Two comic flags on `edit`: `--locg-id`, `--locg-variant-id` (cli.py:324–325)
  - An entire `locg` command group with a `locg link` subcommand (cli.py:361–410)
  - An `extract-comics` command (cli.py:501–510)
  - These all need to move out in PER-30. The 3-hook protocol does **not** currently include CLI command registration — PER-25 / PER-30 will need to decide between adding a `register_cli_commands(group: click.Group)` hook or shipping a separate `comic-cli` binary in `comic-pipeline`.
- `gixen_client.py` and `ebay_bidder.py` are generic scrapers — no comic code, stay in core.
- `tests/` — split during extraction: comic-specific tests (e.g. `tests/test_title_parser.py`) move with the code, generic tests stay.
- **No `pyproject.toml` exists today.** The repo runs via `python cli.py` / `uvicorn server.main:app` against a venv from `requirements.txt`. PER-25 has to bootstrap packaging from scratch — not just add an entry-point group to an existing pyproject.

---

## Target End State

```
gixen-cli/                          (this repo, after the refactor)
├── cli.py                          generic snipe CLI
├── gixen_client.py                 Gixen web-scraper
├── ebay_bidder.py                  Playwright eBay direct bidder
├── server/
│   ├── main.py                     FastAPI app, generic routes only
│   ├── db.py                       schema for `bids` table only
│   ├── plugins.py                  NEW — entry-point loader + protocol
│   └── static/
│       ├── index.html              snipes dashboard tab
│       └── v2.css                  shared styles
├── pyproject.toml                  exposes `gixen.plugins` entry-point group
└── .github/workflows/lint-comic-leakage.yml  CI fails on comic/locg/cgc/fmv terms

comic-pipeline/                     (new repo, separate)
├── plugins/
│   └── gixen-overlay/              gixen-cli plugin: comic routes/tables/UI
│       ├── pyproject.toml          declares entry-point under `gixen.plugins`
│       └── src/
│           ├── title_parser.py     (from gixen-cli)
│           ├── routes.py           comic FastAPI routes
│           ├── db.py               `comics` and `bid_comics` table schemas
│           ├── models.py           UpsertComicRequest, LocgLinkRequest
│           └── static/v2-comics.html
├── apps/
│   ├── ebay/                       (migrated from ~/Projects/ebay-cli/)
│   └── ezship/                     (migrated from ~/Projects/ezship-cli/)
└── skills/                         the 9 /comic: skills, symlinked from .claude/commands/comic/
```

`locg-cli` stays standalone — already in good shape, already publishable, no work needed.

---

## Key Architectural Decisions (Locked)

These three decisions are load-bearing. **Surface them early to the user in the new session before implementing**, in case they want to revisit.

1. **Plugin API: in-process Python entry points (`gixen.plugins` group).** Plugins register via `pyproject.toml` and are loaded at FastAPI startup. Hooks:
   - `register_routes(app: FastAPI)`
   - `register_db_tables(conn: sqlite3.Connection)`
   - `register_dashboard_tabs() -> list[TabSpec]`
   Not HTTP/RPC, not subprocess. Same process means native FastAPI access and zero serialization overhead.

2. **Database: shared SQLite file.** Plugins create their own tables alongside the generic `bids` table in `~/.gixen-server/db.sqlite`. No per-plugin DB. Plugins must namespace their tables (e.g., comic plugin uses `comics`, `bid_comics`) and own their migrations.

3. **Git history: fresh start for `comic-pipeline`.** It is not a fork of `gixen-cli` or anything else. `gixen-cli` keeps its existing repo and history; comic code is removed via normal commits during the extraction (so `git log` still shows the prior comic-era work for anyone spelunking the past).

---

## The Work Plan (13 Linear Issues)

All Issues live under the `PER` (Personal) team. Dependencies enforce order.

### Phase 1 — Prepare `gixen-cli` for Extension (this repo)

Must complete **before** any extraction. These build the plugin contract.

- **PER-25** Add Plugin Entry-Point System to gixen-cli — define `gixen.plugins` group, loader, plugin protocol (3 hooks). Also bootstraps `pyproject.toml` from scratch (none exists today). **Blocks everything else.**
- **PER-26** Refactor FastAPI Routes for Plugin Registration — move generic routes into the core, expose `app` via the hook.
- **PER-27** Refactor SQLite Schema for Plugin-Owned Tables — core owns `bids` only; plugin hook for additional tables; migration path for existing DBs.
- **PER-28** Refactor Dashboard for Plugin-Registered Tabs — core renders `bids` tab + tab framework; plugins inject HTML/CSS/JS.

PER-26, PER-27, PER-28 can be done in parallel after PER-25 lands.

### Phase 2 — Create `comic-pipeline` and Extract Overlay

- **PER-29** Bootstrap comic-pipeline Monorepo — new repo with `apps/`, `skills/`, `plugins/`, scaffolding, CI. Fresh git history.
- **PER-30** Extract Comic Overlay as comic-pipeline Plugin — **highest-risk Issue.** Moves all comic code listed in the "Current State" section above. If the plugin API from PER-25 has gaps, this is where they surface; may spill into a second session. **Likely protocol additions discovered during PER-25 research that PER-30 will need:** (a) `register_cli_commands(group: click.Group)` for the `cli.py` comic surface, (b) a Pydantic-model-extension mechanism for `AddBidRequest`/`EditBidRequest`, (c) a response-enrichment hook (e.g. `enrich_snipes(rows)`) for the contaminated `/api/snipes` / `/api/history` / `/api/bids` routes, (d) an index.html column-injection mechanism or a generic-snipes-view design that doesn't render comic fields. Expect this issue to be **larger than originally scoped** — budget two sessions minimum.
- **PER-31** Migrate ebay-cli into comic-pipeline as apps/ebay
- **PER-32** Migrate ezship-cli into comic-pipeline as apps/ezship

### Phase 3 — Skills Migration

- **PER-33** Move Comic Skills into comic-pipeline and Update PATHs — relocate 9 skills from `~/Projects/Brain v3.0/.claude/commands/comic/` to `comic-pipeline/skills/`, update hardcoded `~/Projects/<repo>` paths, symlink the vault commands dir.

### Phase 4 — Validation

- **PER-34** End-to-End Test the `/comic:buy` Flow — live eBay listing, verify the chain.
- **PER-35** Verify gixen-cli Works Standalone for Generic Users — fresh env, no comic plugin, smoke test.

### Phase 5 — Guardrails and Release

- **PER-36** Add CI Lint to gixen-cli for Comic-Specific Terms — GitHub Action greps for `comic|locg|cgc|fmv` and fails the build on any match.
- **PER-37** Publish gixen-cli v1.0 — bump version, CHANGELOG, tag, README update.

---

## What Lives Where (Other Repos and Paths)

| Thing | Path | Status |
|---|---|---|
| This repo | `~/Projects/gixen-cli/` (https://github.com/hsukenooi/gixen-cli) | Active refactor target |
| `comic-pipeline` (new) | `~/Projects/comic-pipeline/` | To be created in PER-29 |
| `locg-cli` | `~/Projects/locg-cli/` (https://github.com/hsukenooi/locg-cli) | Standalone, untouched |
| `ebay-cli` | `~/Projects/ebay-cli/` (https://github.com/hsukenooi/ebay-cli) | Will be absorbed (PER-31), repo archived after |
| `ezship-cli` | `~/Projects/ezship-cli/` (https://github.com/hsukenooi/ezship-cli) | Will be absorbed (PER-32), repo archived after |
| Comic skills | `~/Projects/Brain v3.0/.claude/commands/comic/` (9 files) | Will be relocated (PER-33), then symlinked back |
| Obsidian vault (skill source, daily notes, project files) | `~/Projects/Brain v3.0/` | Not a code repo; reference only |

---

## Discipline Rules (How to Avoid Re-Tangling)

These exist so future you doesn't undo the work:

1. **Comic-specific code never lands in this repo again.** Before adding *anything* to `gixen-cli`, ask: "Would a non-comic Gixen user want this?" If no, it belongs in the comic plugin in `comic-pipeline`.
2. **CI is the backstop, not the policy.** PER-36 adds a grep-based lint as defense-in-depth. The lint exists because the rule above is easy to forget. If the lint fires, do *not* add the new term to the allowlist — move the offending code to the plugin.
3. **The plugin API is the only sanctioned extension point.** Don't add comic-aware conditional branches in core code "just for now." If the plugin API can't express what you need, *fix the plugin API* (PER-25/26/27/28 patterns) — don't bypass it.
4. **Generic naming for everything in core.** Tables, routes, models, HTML files. `bids`, not `comic_bids`. `/api/auctions`, not `/api/comic-auctions`.

---

## How to Resume in a New Session

1. Read this file. (You're doing that now.)
2. Read `CLAUDE.md` in this repo for any other repo-specific instructions.
3. Check Linear for current Issue status: `linear project view f54d62e4b982` and `linear issue list --project f54d62e4b982`.
4. Find the lowest-numbered Issue without a Done state — that's the next one to work on, respecting the dependency chain above.
5. Before implementing, **confirm the three Key Architectural Decisions are still wanted** by surfacing them to the user. This refactor has not started any code changes yet — there is still time to revisit those.
6. The most natural starting point is **PER-25** (Plugin Entry-Point System), since it unblocks PER-26/27/28/30. Begin there unless the user directs otherwise.

---

## Open Questions / Things Not Yet Decided

- **PyPI publication of `gixen-cli`** — the plan assumes we eventually publish, but the actual PyPI step is in PER-37 and may be deferred.
- **Versioning strategy across the plugin boundary** — should the plugin pin to a specific `gixen-cli` major version? Probably yes (semver on the plugin contract), but not yet specified.
- **Cross-language layout for `comic-pipeline`** — `apps/ezship` is TypeScript. Open question in PER-32 whether to use a pnpm workspace alongside the Python tree or just leave it as a sibling subdirectory with its own `package.json`. Decide during PER-32.
- **Existing user DBs** — current users (just me, in practice) have `~/.gixen-server/db.sqlite` with comic tables. PER-27 needs to specify the migration path: leave the comic tables alone (the plugin will adopt them) vs. drop them and let the plugin recreate. Leaning toward "leave them; plugin's `register_db_tables` uses `CREATE TABLE IF NOT EXISTS`."

---

## Sources

- Linear Project: https://linear.app/hk-iterative/project/separate-gixen-cli-from-comics-use-case-f54d62e4b982
- Linear scope document (attached to the project): "Scope: How Things Work Now / How We Want Them to Work" — https://linear.app/hk-iterative/document/scope-how-things-work-now-how-we-want-them-to-work-825f3d997300
- Original Option-E analysis and architectural reasoning lives in the prior Claude Code session transcript at `~/.claude/projects/-Users-hsukenooi-Projects-Brain-v3-0/915895c4-7009-439c-b44a-7743601501eb.jsonl` (referenced only for archaeology — this doc is the canonical brief).
