#!/usr/bin/env bash
# Stop hook (BUI-1292): warn about untracked docs/**/*.md written during THIS session,
# once per session. Reads the hook JSON (session_id, transcript_path) on stdin.
# Session start = the transcript file's birth time (BSD `stat -f %B`, GNU fallback
# `stat -c %W`). If it cannot be determined, stay silent. Never fails loudly: exit 0.
set -u
trap 'exit 0' ERR

input="$(cat 2>/dev/null)" || exit 0
sid="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null)" || exit 0
tp="$(printf '%s' "$input" | jq -r '.transcript_path // empty' 2>/dev/null)" || exit 0
[ -n "$sid" ] && [ -n "$tp" ] && [ -f "$tp" ] || exit 0
case "$sid" in */* | .*) exit 0 ;; esac

marker_dir="${TMPDIR:-/tmp}/comic-pipeline-stop-nag"
[ -e "$marker_dir/$sid" ] && exit 0

start="$(stat -f %B "$tp" 2>/dev/null || stat -c %W "$tp" 2>/dev/null)" || exit 0
case "$start" in '' | *[!0-9]*) exit 0 ;; esac
[ "$start" -gt 0 ] || exit 0

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
files=""
while IFS= read -r -d '' f; do
  case "$f" in *.md) ;; *) continue ;; esac
  m="$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f" 2>/dev/null)" || continue
  [ "$m" -gt "$start" ] 2>/dev/null || continue
  files="${files:+$files, }$f"
done < <(git ls-files -z --others --exclude-standard -- docs 2>/dev/null)
[ -n "$files" ] || exit 0

mkdir -p "$marker_dir" 2>/dev/null && : >"$marker_dir/$sid" 2>/dev/null || true
jq -n --arg f "$files" '{systemMessage: ("⚠️  Untracked planning doc(s) not committed (lost on workspace cleanup): " + $f + " — commit before ending the session.")}' 2>/dev/null
exit 0
