# launchd jobs (Mac Mini)

Checked-in LaunchAgent plists for scheduled jobs that must run on the Mac
Mini, where `COMICS_SERVER_URL` already points at the local comics server.
`scripts/deploy.sh` does **not** install or reload these automatically —
install once by hand (below), same as `docs/reference/sentinel-probe-scheduling.md`
already documents for `com.comics.sentinel-probe.plist` (that one predates
this directory and isn't duplicated here).

## com.comics.slab-watch-collect.plist (BUI-951)

Runs `comic-fmv --slab-watch-collect` monthly — the closest launchd
calendar slot to "every four weeks" (`StartCalendarInterval` has no
four-weekly unit; see the plist's own comment). Heartbeat contract:
`slab-watch-collect` in `docs/reference/job-heartbeat-contract.md`.

Install:

```bash
cp scripts/launchd/com.comics.slab-watch-collect.plist \
  "$HOME/Library/LaunchAgents/com.comics.slab-watch-collect.plist"

launchctl unload "$HOME/Library/LaunchAgents/com.comics.slab-watch-collect.plist" 2>/dev/null || true
launchctl load -w "$HOME/Library/LaunchAgents/com.comics.slab-watch-collect.plist"
```

Verify the wiring end to end without waiting a month — this runs the job
once, now, and the heartbeat it records is the proof:

```bash
launchctl kickstart -k "gui/$(id -u)/com.comics.slab-watch-collect"
comics-api GET /api/comics/health/heartbeats
```

`slab-watch-collect` should read `status: ok` with `success_count >= 1`. If
it still reads `never`, check `~/.comics-server/slab-watch-collect.error.log`
— a heartbeat is only written on exit 0 (every fetch AND every ledger write
this run attempted succeeded; see the contract doc for the exact rule).

`RunAtLoad` is deliberately `false`: loading the agent during a deploy must
not spend provider budget as a side effect (same reasoning as
`sentinel-probe`'s plist).

Re-run the install snippet above (`cp` + `unload`/`load -w`) after any change
to the checked-in plist, including a `SLAB_WATCH_MAX_REQUESTS` override.

## com.comics.em-batch-nightly.plist (BUI-972)

Runs `scripts/em-batch-nightly.sh` at 01:00 every night: picks up to
`EM_BATCH_NIGHTLY_CAP` (default 8) unassigned `comics` tickets in Today,
Soon, or Someday, and runs `/em-batch mode:autonomous` over them in a fresh
detached worktree, headless, through implement, review, CI, merge, deploy,
and close. The run's summary goes to Telegram and is appended to that day's
Reflect daily note. The wrapper's header documents the knobs and the
`--dry-run` / `--tickets` switches.

Preconditions, all user-authored (the run never widens its own permissions):

- The allow rules in `.claude/settings.local.json` of the shared checkout
  (listed in `.claude/em-batch.md` under **Unattended**). The run loads
  that file with `--settings` and runs with `--permission-prompts none`, so
  a missing rule is a denied step and a held ticket, never a hung job.
- `gh auth status` green and `~/.config/linear/credentials.toml` readable
  for the launchd user.
- `/bin/bash` has Full Disk Access (already granted for the daily routine),
  or the daily-note append is skipped and only Telegram gets the summary.

Install:

```bash
cp scripts/launchd/com.comics.em-batch-nightly.plist \
  "$HOME/Library/LaunchAgents/com.comics.em-batch-nightly.plist"
launchctl unload "$HOME/Library/LaunchAgents/com.comics.em-batch-nightly.plist" 2>/dev/null || true
launchctl load -w "$HOME/Library/LaunchAgents/com.comics.em-batch-nightly.plist"
```

Check the wiring without a model run, then run it for real once:

```bash
scripts/em-batch-nightly.sh --dry-run
launchctl kickstart -k "gui/$(id -u)/com.comics.em-batch-nightly"
tail -f ~/Library/Logs/em-batch-nightly.log
```

Per-run state (prompt, raw result JSON, summary, handoff) lands in
`~/.local/state/em-batch-nightly/runs/<date>/`. Turn the job off with
`launchctl unload -w` on the installed plist; a lock directory under
`~/.local/state/em-batch-nightly/` keeps two runs from overlapping.
