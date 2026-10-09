"""ebay-auth: obtain and inspect the eBay user token (BUI-1209)."""

import argparse
import importlib.metadata
import sys

import ebay_user_token as eut


def _version_string() -> str:
    try:
        pkg_version = importlib.metadata.version("ebay-tools")
    except importlib.metadata.PackageNotFoundError:
        pkg_version = "unknown"
    try:
        from _ebay_build_stamp import GIT_DATE, GIT_SHA
    except ImportError:
        GIT_SHA, GIT_DATE = "unknown", "unknown"
    return f"ebay-auth {pkg_version} (git {GIT_SHA}, {GIT_DATE})"


def cmd_login(args):
    client_id, _, _ = eut.load_config()
    runame = eut.load_runame()
    print("Open this URL, sign in with your buyer account, and approve:\n")
    print(eut.consent_url(client_id, runame))
    print("\nPaste the full redirected URL (the code expires in 5 minutes):")
    try:
        pasted = input("> ")
    except EOFError:
        raise eut.UserTokenError("no redirect URL provided on stdin") from None
    if not pasted.strip():
        raise eut.UserTokenError("empty redirect URL")
    body = eut.exchange_code(eut.extract_code(pasted), runame)
    days = body.get("refresh_token_expires_in", 0) / 86400
    print(f"Saved user token to {eut.TOKEN_FILE} (refresh token valid ~{days:.0f} days).")
    return 0


def cmd_status(args):
    state, details = eut.describe_state()
    print(f"state: {state}")
    for key, value in details.items():
        print(f"{key}: {value}")
    if state in ("missing", "expired"):
        print(f"Re-login needed: {eut.RELOGIN_HINT}")
        return eut.EXIT_RELOGIN
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ebay-auth", description="eBay user token management")
    parser.add_argument("--version", action="version", version=_version_string())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="run the browser consent flow and save the token")
    sub.add_parser("status", help="show token state and expiry (never the token)")
    args = parser.parse_args(argv)
    try:
        return {"login": cmd_login, "status": cmd_status}[args.command](args)
    except eut.UserTokenError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
