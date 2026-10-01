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

## Do not use `--allowedTools` for this

`--allowedTools` only gates permission prompts. Every built-in tool schema still ships on every API call.

## Measured (2026-10-01, fixture book 01-uxm-210)

| Flags | Fixed prompt per call | Built-in tool schemas |
| --- | --- | --- |
| `--allowedTools Read,Bash` | 48.8k tokens | about 30k |
| `--tools Read,Bash` + empty MCP | 15k tokens | about 5k harness total |

Cache-read per run fell from about 235k to 92.5k and cache-create from about 58k to 36k, with the same turn count and output size.
