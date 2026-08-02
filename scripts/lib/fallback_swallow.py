"""fallback_swallow — BUI-606: the `|| echo ""`-style fallback-swallow detector.

Enforces docs/solutions/workflow-issues/
multi-block-skill-shell-state-loss-fallback-swallow.md ("Trap 2"): a shell
pipeline that catches a failure with `|| echo <default>` / `|| true` /
`2>/dev/null` treats every failure the same — including a local/connectivity
failure that never reached the server — and silently substitutes a default
that looks like real data. The doc's own `applies_when` names the shape:
"A shell pipeline uses `|| echo \"\"` / `|| true` / `2>/dev/null` after a curl
or URL-construction step to produce a default on failure."

Two entry points, both stable and meant to be imported directly:

    find_fallback_swallows(text, *, is_markdown)  -> list[(line, message, hint)]
        The pure primitive. Scans already-loaded text and returns 1-indexed
        hits. No filesystem access — safe to call on a string from anywhere
        (a file you already read, a git-diff hunk, an in-memory fixture).
        `is_markdown=True` restricts scanning to fenced ```bash/sh/shell code
        blocks (the shape a `.claude/commands/*.md` skill is written in);
        `is_markdown=False` treats every line as shell (a `.sh` script).

    check_fallback_swallow(root)  -> list[Finding]
        The lint-pack entry point (BUI-606) AND the reusable check BUI-607
        should call directly for its own skill-corpus scan, e.g.:

            from lib.fallback_swallow import check_fallback_swallow
            findings = check_fallback_swallow(repo_root)

        Resolves every path under `root` (no __file__, no getcwd() — see
        scripts/solutions-lint's REGISTRATION SEAM). Scoped to
        `.claude/commands/**/*.md`, the doc's own documented domain.

A hit is suppressed by either escape valve:

  * An `exit`/`return` in the same statement or within the next two lines —
    the doc's own guidance ("hard-stop on local/connectivity failure") in
    its positive form: the fallback still ends in an unconditional stop, so
    nothing downstream can act on the swallowed default. See
    `docs/solutions/design-patterns/scope-status-writes-to-row-id-not-item-id.md`
    for the sibling lint's identical shape of escape valve.
  * An explicit `# fallback-swallow: allow (<reason>)` marker nearby, for a
    fallback a human has deliberately reviewed and decided is safe (e.g. a
    genuine 5xx path with an independent downstream net — see the doc's
    Trap 2 guidance on when falling through is actually defensible).

Caller-scoping note: this detector has no concept of "network call" — it
flags ANY unguarded `|| echo` / `|| true` / `2>/dev/null`-then-`||`, full
stop. That is deliberately broad for the doc's own domain (skill bash
blocks, where the two confirmed instances this lint was built against have
no network call in one case — see the lint's self-test and
scripts/solutions-lint's registration). It is NOT safe to point at an
arbitrary shell script without checking first: plain utility scripts often
have entirely benign local-command fallbacks (`command -v foo || echo
"default"`) that this function cannot distinguish from a swallowed network
failure. `check_fallback_swallow` therefore only walks `.claude/commands/`;
widen its scope deliberately, not by default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SWALLOW_RE = re.compile(r"\|\|\s*(echo\b|true\b)|2>/dev/null\s*\|\|")
_EXIT_RE = re.compile(r"\b(exit|return)\b")
_ALLOW_MARKER_RE = re.compile(r"fallback-swallow:\s*allow", re.IGNORECASE)
# A bare marker with no reason attached is a rubber stamp, not a review —
# require real justification, the same guard scripts/solutions-lint applies
# to its own `non-unique-key-mutation: allow` marker and to
# `advice_only_reason:` (MIN_ADVICE_REASON_CHARS).
_MIN_MARKER_JUSTIFICATION_CHARS = 25
_FENCE_RE = re.compile(r"^```\s*([A-Za-z0-9_+-]*)")
# Only fences explicitly labeled as shell — a bare ``` block is as likely to
# hold example JSON/output as a follow-on shell snippet, and the two known
# instances this lint was built against are both explicit ```bash blocks.
_SHELL_FENCE_LANGS = {"bash", "sh", "shell"}

_MESSAGE = (
    "fallback swallow: `|| echo` / `|| true` / `2>/dev/null`-then-`||` here "
    "has no hard stop (exit/return) within the next couple of lines — a "
    "local/connectivity failure and a genuine remote failure both fall "
    "through to the same silent default"
)
_HINT = (
    "hard-stop on the failure (exit 1 / raise) instead of falling through, "
    "or if this is a deliberately reviewed safe fallback (e.g. a real 5xx "
    "with an independent downstream net), mark it "
    "`# fallback-swallow: allow (<reason>)` — see docs/solutions/"
    "workflow-issues/multi-block-skill-shell-state-loss-fallback-swallow.md"
)


def comment_only_text(lines: list[str]) -> str:
    """Extract just the comment content from a slice of source lines.

    A pure `#`-comment line contributes itself; a code line with a trailing
    `# ...` comment contributes only the part after the first `#`; a code
    line with no `#` at all contributes nothing. Shared by both this lint's
    and scripts/solutions-lint's `non-unique-key-mutation` allowlist-marker
    check: an allowlist marker's "justification" must come from the comment,
    never from surrounding code text — otherwise a bare, unjustified marker
    sitting next to a long code line would pass a naive length check just
    because the code line is long (BUI-606 review caught this: an earlier
    version measured the whole window, so `# fallback-swallow: allow` right
    above a long `curl` line looked "justified" purely by the curl line's
    own length).
    """
    parts: list[str] = []
    for line in lines:
        if "#" in line:
            parts.append(line.split("#", 1)[1])
    return "\n".join(parts)


def marker_has_justification(text_lines: list[str], marker_re: re.Pattern[str]) -> bool:
    """True if `marker_re` matches somewhere in the comment content of
    `text_lines` AND what remains after stripping the marker phrase itself
    is at least `_MIN_MARKER_JUSTIFICATION_CHARS` of real explanation — not
    a bare, unjustified `# <marker>: allow`."""
    comment_text = comment_only_text(text_lines)
    if not marker_re.search(comment_text):
        return False
    residual = marker_re.sub("", comment_text)
    return len(residual.strip()) >= _MIN_MARKER_JUSTIFICATION_CHARS


@dataclass(frozen=True)
class Finding:
    """One violation. `path` is relative to the tree the check was run against."""

    path: str
    line: int
    message: str
    hint: str = ""


def find_fallback_swallows(
    text: str, *, is_markdown: bool
) -> list[tuple[int, str, str]]:
    """Scan already-loaded text for the fallback-swallow shape.

    Returns a list of (1-indexed line, message, hint) triples. Pure — takes
    no path, touches no filesystem, so it's safe to call on text from any
    source.
    """
    lines = text.splitlines()
    hits: list[tuple[int, str, str]] = []
    in_any_fence = False
    in_shell_block = not is_markdown

    for i, raw in enumerate(lines):
        if is_markdown:
            fence = _FENCE_RE.match(raw.strip())
            if fence is not None:
                if in_any_fence:
                    in_any_fence = False
                    in_shell_block = False
                else:
                    in_any_fence = True
                    in_shell_block = fence.group(1).lower() in _SHELL_FENCE_LANGS
                continue
            if not in_shell_block:
                continue

        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        if not _SWALLOW_RE.search(raw):
            continue

        # The marker may sit on a comment line just above the swallow, or
        # trail the swallow line itself; the exit/return guard is looked for
        # from this line onward (same statement or the next couple of
        # lines) — a stop that happened earlier doesn't make *this*
        # fallback safe.
        marker_window = lines[max(0, i - 2) : i + 1]
        if marker_has_justification(marker_window, _ALLOW_MARKER_RE):
            continue
        exit_window = "\n".join(lines[i : i + 3])
        if _EXIT_RE.search(exit_window):
            continue

        hits.append((i + 1, _MESSAGE, _HINT))

    return hits


def check_fallback_swallow(root: Path) -> list[Finding]:
    """Entry point: walk `.claude/commands/**/*.md` under `root` and flag
    every fallback-swallow site. MUST resolve all paths under `root` — no
    `__file__`, no `os.getcwd()` (see scripts/solutions-lint's REGISTRATION
    SEAM) — so it runs identically against the real repo or a fixture tree.
    """
    base = root / ".claude" / "commands"
    if not base.is_dir():
        return []

    findings: list[Finding] = []
    for path in sorted(base.rglob("*.md")):
        relpath = path.relative_to(root).as_posix()
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for line, message, hint in find_fallback_swallows(text, is_markdown=True):
            findings.append(Finding(relpath, line, message, hint))
    return findings
