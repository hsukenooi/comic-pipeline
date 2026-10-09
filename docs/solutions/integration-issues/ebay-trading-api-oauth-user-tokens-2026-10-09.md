---
title: "Trading API calls take an OAuth user token in X-EBAY-API-IAF-TOKEN, minted from a 0600 refresh token"
date: 2026-10-09
category: integration-issues
module: "apps/ebay (ebay_user_token.py, ebay_auth.py) (BUI-1209)"
problem_type: integration_issue
component: tooling
severity: medium
mechanized_by: test
enforced_by_test: apps/ebay/tests/test_ebay_user_token.py
related_components:
  - apps/ebay
applies_when:
  - "Calling an eBay Trading API (XML) endpoint such as GetMyeBayBuying for member data"
  - "Adding a command that needs the signed-in buyer's data rather than public Browse data"
  - "Handling an eBay refresh token that expires, is revoked, or is missing"
tags: [ebay, trading-api, oauth, user-token, refresh-token, iaf-token, secrets, invalid-grant]
---

# Trading API calls take an OAuth user token in `X-EBAY-API-IAF-TOKEN`

## Context

Every app-level eBay call here uses a `client_credentials` token, which reaches public Browse data only. Member data, such as the watchlist, comes from the XML Trading API and needs a user token. The 2026-10-09 spike proved the base scope `https://api.ebay.com/oauth/api_scope` and the keyset's existing RuName are enough.

## Guidance

- **Send the token in the IAF header.** Trading calls take `X-EBAY-API-IAF-TOKEN: <access token>`, not an `<eBayAuthToken>` XML element. Keep `X-EBAY-API-CALL-NAME`, `X-EBAY-API-SITEID`, and `X-EBAY-API-COMPATIBILITY-LEVEL` as usual.
- **Get the refresh token once, in a browser.** `ebay-auth login` prints the consent URL, reads the pasted redirect URL, and exchanges the `code` (5-minute life) with `grant_type=authorization_code` and the RuName as `redirect_uri`.
- **Mint access tokens with the refresh grant.** `grant_type=refresh_token` plus `scope`, Basic-authed with the client id and secret. Access tokens last 2 hours. eBay does not rotate the refresh token, so concurrent runs are safe.
- **Cache the access token atomically.** Reuse it until 5 minutes before expiry, written with `atomic_write_json(..., mode=0o600)`.
- **Store the refresh token at mode 0600** (`~/.config/ebay-fetch/user_token_production.json`). Never print or log a token or the client secret. `ebay-auth status` reports state and expiry only.
- **Expiry is `obtained_at + refresh_token_expires_in`.** A file saved from eBay's raw response has no `obtained_at`, so fall back to its mtime. Warn on stderr within 30 days.
- **`invalid_grant` means re-login.** An expired, revoked, or missing token raises `UserTokenError` (exit code 3) naming `ebay-auth login`. Never return an empty result in its place. Network and HTTP failures use exit code 5.

## Enforcement

`apps/ebay/tests/test_ebay_user_token.py` covers the refresh and cache margin, `invalid_grant` and missing-file exits, the mtime fallback, the 30-day warning, file modes, the login exchange, and that `status` never prints secrets.
