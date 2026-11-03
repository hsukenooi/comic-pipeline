---
title: "Sniper keyset scopes: sell.auction.read is granted, buy.* scopes are rejected, and a refresh must not widen the original grant"
date: 2026-11-04
category: integration-issues
module: "apps/ebay (ebay_user_token.py) (BUI-1345)"
problem_type: integration_issue
component: tooling
severity: medium
mechanized_by: test
enforced_by_test: apps/ebay/tests/test_ebay_user_token.py
related_components:
  - apps/ebay
applies_when:
  - "Adding an OAuth scope to `ebay-auth login`"
  - "Calling the Auction GraphQL API (`itemBiddingActivity`) with a user token"
  - "Testing whether the keyset may request a scope"
tags: [ebay, oauth, scope, user-token, refresh-token, graphql, invalid-scope]
---

# Sniper keyset scopes and the no-login scope test

Builds on `ebay-trading-api-oauth-user-tokens-2026-10-09.md`.

## Scopes tested on 2026-10-30

| Scope | Result |
| --- | --- |
| `https://api.ebay.com/oauth/api_scope/sell.auction.read` | granted |
| `https://api.ebay.com/oauth/api_scope/sell.auction` | granted |
| `https://api.ebay.com/oauth/api_scope/sell.inventory.readonly` | granted |
| `buy.auction.read`, `buy.auction`, `buy.marketplace.insights`, `buy.offer.auction` | `invalid_scope` |

`itemBiddingActivity` on `https://graphqlapi.ebay.com/graphql` needs `sell.auction.read`. `ebay-auth login` requests it with the base scope (`SCOPE` in `ebay_user_token.py`).

## No-login scope test

Open this URL in a browser, with the keyset's client id and RuName:

`https://auth.ebay.com/oauth2/authorize?client_id=<id>&redirect_uri=<RuName>&response_type=code&scope=<base>+<scope>`

A rejected scope redirects straight to `auth2.ebay.com/oauth2/errorOauth?errorId=invalid_scope`. A granted scope shows the sign-in page. Run a bogus scope as the negative control. Do not test with `client_credentials`: the app token rejects every user scope, so it proves nothing.

## A refresh must not widen the original grant

eBay rejects a refresh request whose scope is wider than the original grant. A refresh token minted before BUI-1345 holds the base scope only. `exchange_code` now records `granted_scope` in the token file, and the refresh grant sends exactly that. A file without the key refreshes with the base scope, so `ebay-watchlist` keeps working until the next `ebay-auth login`. Callers that need the auction scope call `has_scope(AUCTION_SCOPE)`. `ebay-auth status` lists the granted scopes.
