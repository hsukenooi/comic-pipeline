---
title: "feat: Read the eBay watchlist into the buy workflow"
type: feat
status: active
date: 2026-10-09
origin: spike in session 2026-10-09 (scratch script, not in repo)
---

# feat: Read the eBay watchlist into the buy workflow

## Summary

Add an eBay user token to `apps/ebay`, a `ebay-watchlist` console script that reads the watchlist through the Trading API, and a `/comic:watchlist` skill that hands live watched auctions to `/comic:buy`. You approve access once in a browser. Every later run is unattended until the refresh token expires, about 18 months later.

---

## Problem Frame

The listings you watch on eBay are the books you're considering. Today they reach the pipeline only when you copy URLs into `/comic:buy` by hand. The pipeline can't read them because every eBay call in the repo uses an app-level `client_credentials` token, which reaches public Browse data only (`ebay_fetch.py:449-456`, `shipped_orders.py:66-72`).

No REST Buy API exposes a member's watchlist. The only supported read is the XML Trading API call `GetMyeBayBuying` with a `WatchList` container, authorized by a user token.

---

## Spike Findings (2026-10-09)

- **The call works with the base scope.** An OAuth user token for `https://api.ebay.com/oauth/api_scope`, sent in `X-EBAY-API-IAF-TOKEN`, returned all 22 watched items. The keyset needed no new scopes.
- **The keyset already had an OAuth RuName.** `Hsu_Ken_Ooi-HsuKenOo-Sniper-zgkoooqyn` on the production "Sniper" keyset. No portal setup was needed.
- **The refresh token lasts 547 days.** It was issued 2026-10-09, so it expires around 2028-04-09. Access tokens last 2 hours and are minted from it without a browser.
- **The token is on the Mac Mini** at `~/.config/ebay-fetch/user_token_production.json` (mode 0600). It holds eBay's raw token response and no issue timestamp, so expiry must fall back to the file's mtime.
- **The watchlist keeps ended listings.** 12 of 22 items had ended. Any consumer must filter on `EndTime`.
- **The watchlist isn't all comics.** It held novels (Ted Chiang, The Expanse) and Buy It Now lots. Auctions were 9 of 22.
- **Deprecation status.** `GetMyeBayBuying`, `AddToWatchList`, and `RemoveFromWatchList` aren't on eBay's deprecation list as of 2026-10-09. eBay is retiring other Trading calls one by one (for example, `GeteBayDetails` on 2027-03-15), so this is a standing risk.

---

## Requirements

- R1. A user token is obtained once through the OAuth authorization-code flow and stored at `~/.config/ebay-fetch/user_token_production.json`, mode 0600. No token or client secret is ever printed or logged.
- R2. Each run mints a fresh access token from the refresh token and caches it until 5 minutes before expiry. eBay doesn't rotate refresh tokens (verified 2026-10-09: a refresh response holds only `access_token`, `expires_in`, and `token_type`), so concurrent runs are safe. Cache writes are atomic (write to a temporary file, then rename).
- R3. Within 30 days of refresh-token expiry, every command prints a warning to stderr that names the re-login command.
- R4. An expired, revoked, or missing token fails loudly with a dedicated exit code and the re-login command. It never yields an empty watchlist. An empty list means the watchlist is empty, and nothing else.
- R5. `ebay-watchlist` returns live listings only by default (`EndTime` later than now). `--include-ended` keeps ended ones. `--type auction|bin|all` defaults to `all`.
- R6. Output formats are a table (default), `--json` (item ID, title, listing type, end time, current price, bid count, seller, URL), and `--urls` (one URL per line, ready for `/comic:identify`).
- R7. Pagination reads every page (200 entries per page).
- R8. `/comic:watchlist` excludes items that already have a snipe (`gixen list --json`). It flags non-comic items from the identify step and drops them rather than pricing them. It hands the rest to `/comic:buy`, which keeps its own collection-check and FMV gates.
- R10. `/comic:watchlist` flags any auction ending within `--min-minutes` (default 30) as likely too late. Gixen has no add cutoff (its bid offset is 1 to 15 seconds), but identify, grade, and FMV take minutes. The skill lists flagged auctions separately and hands them to `/comic:buy` only if you pick them.
- R9. `scripts/install.sh` and `scripts/deploy.sh` install the new console scripts. `deploy.sh` asserts their `--version` SHA like the others.

---

## Design

### Components

| Component | Location | Role |
|---|---|---|
| `ebay_user_token.py` | `apps/ebay/src/` | Load, refresh, cache, and expiry-check the user token. Raises `UserTokenError` with a re-login hint. |
| `ebay-auth` | `apps/ebay` console script | `login` runs the consent flow. `status` prints the token's state and expiry without secrets. |
| `ebay-watchlist` | `apps/ebay` console script | Calls `GetMyeBayBuying`, paginates, filters, and formats. |
| `/comic:watchlist` | `.claude/commands/comic/watchlist.md` | Lists live watched auctions not yet sniped and hands them to `/comic:buy`. |

### Login flow (`ebay-auth login`)

1. Print the consent URL built from the keyset's `client_id`, the RuName, and `api_scope`.
2. You open it in a browser, sign in with your buyer account, and approve.
3. You paste the redirected URL. The command extracts `code` and exchanges it within its 5-minute life.
4. The command writes the token file with an added `obtained_at` timestamp.

The RuName is read from `~/.config/ebay-fetch/config.json` (`runame` key). The command accepts an existing token file without `obtained_at` and falls back to its mtime.

### Error contract

| Condition | Exit code | Message names |
|---|---|---|
| Token file missing | 3 | `ebay-auth login` |
| Refresh rejected (`invalid_grant`) | 3 | `ebay-auth login` |
| Trading `Ack=Failure` | 4 | eBay error code and long message |
| Network or HTTP error | 5 | status and endpoint |

### Out of scope

- `BidList`, `WonList`, and `LostList`. The same token reaches them, and `WonList` could later cross-check `/comic:collection-add`. File separately if wanted.
- Adding or removing watched items (`AddToWatchList`, `RemoveFromWatchList`).
- A scheduled run. `/comic:watchlist` is user-invoked like the other buy skills.

---

## Risks

- **eBay decommissions `GetMyeBayBuying`.** No REST replacement exists today. Mitigation: the error contract makes failure loud. The fallback is a user-present scrape of the watchlist page.
- **A password change revokes the token.** Not confirmed for OAuth tokens (open as of 2026-10-09). Mitigation: R4 fails loudly, and re-login takes a minute.
- **Token file loss.** The file isn't in the repo or backups. Re-login restores it. No data is lost.

---

## Delivery

1. **Token store and `ebay-auth`.** R1 through R4 and R9 for `ebay-auth`, plus a `docs/solutions/` doc on Trading calls with OAuth user tokens (`mechanized_by: test`).
2. **`ebay-watchlist`.** R5 through R7 and R9. Depends on 1.
3. **`/comic:watchlist` skill.** R8 and R10. Depends on 2.

Tests mock the token endpoint and the Trading endpoint with recorded XML from the spike (titles and IDs only, no tokens). Run with `uv run --with pytest pytest` from `apps/ebay`.
