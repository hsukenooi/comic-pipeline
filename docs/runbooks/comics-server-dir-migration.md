# Runbook: migrate the comics-server data dir `~/.gixen-server` → `~/.comics-server` (BUI-220)

> **EXECUTED 2026-07-20 (BUI-463).** This migration has been run on the Mac Mini and verified:
> LaunchAgent label is `com.comics.server` (PID confirmed running), data dir is `~/.comics-server`,
> `.env` `DB_PATH` points at `/Users/hsukenooi/.comics-server/db.sqlite`, and served row counts
> (bids=500, comics=683, fmv=779, bid_fmvs=487, collection row_count=2870) matched the
> pre-migration backup at `~/comics-server-migration-backup-20260720/` on the Mini. `install.sh`
> (`packages/gixen-cli/server/install.sh`) was updated post-execution to write the
> `com.comics.server` LaunchAgent going forward (see the step 4 correction below — at execution
> time it still wrote the pre-migration name due to a since-reverted BUI-459 scoping, so the
> plist was written by hand instead). This doc is kept for historical reference and in case the
> migration ever needs to be re-run on another machine.

**Run this on the Mac Mini only.** It is the live half of BUI-220 Tier 3 and is intentionally **not** done by merging the PR — the code ships a safe fallback so nothing breaks until you run these steps deliberately.

## Background

BUI-220 renamed the self-hosted server in code/prose from "the gixen server" to **the comics server** (Gixen is only the external bidding service). The on-disk data dir is the last piece. The code now resolves the data dir with a safe fallback (`server.db.resolve_server_dir`):

1. `~/.comics-server` if it exists, else
2. `~/.gixen-server` if it exists (the live Mini, pre-migration), else
3. `~/.comics-server` (fresh installs).

So **before** this migration the server keeps booting from `~/.gixen-server`; **after** it, from `~/.comics-server`. Both `COMICS_SERVER_URL` (canonical) and `GIXEN_SERVER_URL` (deprecated alias) are accepted by every caller, so clients don't need to change in lockstep.

> ⚠️ The launchd `.env` pins an **absolute** `DB_PATH` (`$HOME/.gixen-server/db.sqlite`), and `server.main` honors `DB_PATH` over the resolver. So moving the dir is **not** enough — you must also fix `DB_PATH` in the moved `.env` (step 3), or the server will still read the old path.
>
> ⚠️ `GIXEN_USERNAME` / `GIXEN_PASSWORD` in `.env` are **genuine Gixen bidding credentials** — they keep the `gixen` name. Do **not** rename them.

## Steps (Mac Mini)

```sh
# 0. Confirm current state: server up, data in the legacy dir
curl -sf http://localhost:8080/health && echo OK
ls ~/.gixen-server            # db.sqlite, collection-store/, .env, ebay_cookies.json, ebay_browser/

# 1. Stop the old LaunchAgent
launchctl unload ~/Library/LaunchAgents/com.gixen.server.plist

# 2. Move the data dir (preserves db.sqlite, collection-store/, cookies, browser profile)
mv ~/.gixen-server ~/.comics-server

# 3. Fix the absolute DB_PATH inside the moved .env (keep the Gixen creds!)
#    Change:  DB_PATH=$HOME/.gixen-server/db.sqlite
#    To:      DB_PATH=$HOME/.comics-server/db.sqlite
$EDITOR ~/.comics-server/.env

# 4. Re-deploy: as of BUI-463, install.sh writes the com.comics.server LaunchAgent and starts it.
#    It will NOT clobber the existing .env (it only generates one when absent), so your
#    creds + corrected DB_PATH survive.
#    (Historical note: at the time this runbook was executed on 2026-07-20, install.sh had been
#    deliberately scoped BACK to the pre-migration com.gixen.server name by BUI-459 — the rename
#    below hadn't landed yet — so this step was done by hand: write the com.comics.server plist,
#    `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.comics.server.plist`, then load it.)
cd <repo>/packages/gixen-cli/server && ./install.sh

# 4b. `launchctl bootstrap` registers the job but does not reliably start it — expect `runs = 0`
#     in `launchctl print gui/$(id -u)/com.comics.server` until you force a start:
launchctl kickstart -k "gui/$(id -u)/com.comics.server"

# 5. Remove the old LaunchAgent file (install.sh added the new one but left the old)
rm -f ~/Library/LaunchAgents/com.gixen.server.plist

# 6. Verify the comics server is serving from the new dir
curl -sf http://localhost:8080/health && echo OK
launchctl list | grep com.comics.server
# spot-check data survived:
curl -sf "http://localhost:8080/api/comics/collection/status"
curl -sf "http://localhost:8080/api/comics/wish-list" | head -c 200
```

## MacBook (client)

Update `~/.zshrc` to the canonical variable (the old one still works with a deprecation warning, but switch it):

```sh
# was: export GIXEN_SERVER_URL=http://mac-mini.tail9b7fa5.ts.net:8080
export COMICS_SERVER_URL=http://mac-mini.tail9b7fa5.ts.net:8080
```

Then `source ~/.zshrc` and confirm a skill call resolves cleanly (no deprecation warning).

## After the dir no longer exists

Once `~/.gixen-server` is gone and everything is confirmed working, the few remaining **prose** references to `~/.gixen-server` paths (left verbatim during BUI-220 because they were still accurate) can be updated to `~/.comics-server`:

- `CLAUDE.md` (one-time server-seed note), `.claude/commands/comic/collection-sync.md` (backup path), `.claude/commands/comic/references/date-backfill.md` (`~/.gixen-server/.env`), `packages/locg-cli/docs/processes/locg-collection-wishlist-sync.md`.

Done as of BUI-463 (2026-07-20) — all four were updated to `~/.comics-server` / `com.comics.server` once the migration was confirmed live on the Mini.

The resolver's `~/.gixen-server` fallback branch in `server/db.py` (and the inline one in `cleanup_duplicates.py`) is deliberately left alone — it's now dead on the Mini but harmless, and protects any other machine that hasn't migrated yet.

## Rollback

If anything is wrong after step 4: `launchctl unload ~/Library/LaunchAgents/com.comics.server.plist`, `mv ~/.comics-server ~/.gixen-server`, revert the `.env` `DB_PATH`, `launchctl load -w ~/Library/LaunchAgents/com.gixen.server.plist`. The fallback resolver makes the pre-migration state fully functional.
