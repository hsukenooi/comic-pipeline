# Headless grader runs

Reference for any harness that runs the `comic-grader` body through `claude -p` (fixture gates, benchmarks, one-off re-grades). BUI-1089.

## Flag set

```sh
claude -p --model <model> \
  --system-prompt-file <agent body, frontmatter stripped> \
  --tools Read,Bash \
  --strict-mcp-config --mcp-config <path to a file containing {"mcpServers":{}}> \
  --max-turns 8 --output-format json < job.txt > out.json
```

- `--tools Read,Bash` removes every other built-in tool from the request, schemas included.
- `--strict-mcp-config` with an empty `--mcp-config` stops user and project MCP servers from adding their tool schemas.
- `--max-turns 8` is a backstop, not a budget.

## Launch from outside the repo (BUI-1177)

Run `claude -p` with its working directory set to a directory outside the repo (the run's workdir). From the repo root the seat also auto-loads the project CLAUDE.md and the auto-memory MEMORY.md (48.5k characters), which adds about 17.7k tokens to every API call (28.6k from the repo root against 10.9k from a non-repo directory, same flags, 2026-10-06). `grade-seats` sets `cwd=<workdir>` on every seat, retry, and adjudicator process. A harness script must `cd` to its output directory before `claude -p`. Job text paths must be absolute.

## Do not use `--allowedTools` for this

`--allowedTools` only gates permission prompts. Every built-in tool schema still ships on every API call.

## Measured (2026-10-01, fixture book 01-uxm-210)

| Flags | Fixed prompt per call | Built-in tool schemas |
| --- | --- | --- |
| `--allowedTools Read,Bash` | 48.8k tokens | about 30k |
| `--tools Read,Bash` + empty MCP | 15k tokens | about 5k harness total |

Cache-read per run fell from about 235k to 92.5k and cache-create from about 58k to 36k, with the same turn count and output size.

## Lean orchestrator (BUI-1107)

A headless `/comic:grade` orchestrator needs only Read, Bash, and Write under `SEAT_DISPATCH = headless`. Launch it with `--tools`, not `--allowedTools`:

```sh
scripts/grade-benchmark.sh <label> [model] [item ids...]
# which runs:
claude -p "/comic:grade <ids>" --model claude-fable-5-1 --tools Read,Bash,Write \
  --strict-mcp-config --mcp-config <path to a file containing {"mcpServers":{}}> \
  --output-format json
```

Add no prompt suffix. `grade.md` Step 2 already requires `grade-seats` in the foreground with a 600000 ms Bash timeout. A backgrounded `grade-seats` dies with the exiting headless process, which killed all six seats on an Opus run (2026-10-02).

`scripts/grade-benchmark.sh` is the canonical runner. It runs from its own checkout, sums orchestrator and seat usage with `scripts/grade-benchmark-sum.py`, and finds the seat envelopes from the run directory named in the result. Defaults: the three-listing benchmark (336816244968, 267800242816, 298630965109), Fable, outputs under `$BENCH_DIR`.

### Measured (2026-10-06, three-listing benchmark)

| Run | Flags | Orchestrator cache-read | API calls | Whole-run cache-read |
| --- | --- | --- | --- | --- |
| BUI-1095 (before) | `--allowedTools`, Fable | 253k | 4 | not recorded with seats |
| Lean, Fable run 1 | `--tools Read,Bash,Write` | 188k | 5 | 2,048k |
| Lean, Fable run 2 | same | 414k | 8 | 2,102k |
| Lean, Opus | same | 136k | 4 | 1,814k |

- The per-call context fell from about 63k to about 44k (first call about 45k cache-create). That is the tool-schema saving, about 26% on a like-for-like call count.
- A 4-call run costs about 137k at this base, so the 130k target is out of reach by tool trimming alone. The rest of the 44k is CLAUDE.md, the memory index, the skill list, and the skill body.
- Cache-read is driven by call count. Run 2 spent 8 calls because the orchestrator inspected Hulk #180 photos itself after a MEDIUM-LOW consensus.
- Opus completed every seat with no zero-byte envelope and no prompt suffix.
