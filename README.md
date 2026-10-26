# razor-mcp

MCP server (stdio) around Razor's static repo analysis, from `razor-cli-v2`.
Read-only: tools never write to the repo. No LLM calls inside the tools —
every result is deterministic for a given repo state.

## Tools

- `map_repo` — stack, file/def/edge counts, PageRank-ordered file list (budgeted).
- `locate` — rank repo files for an issue description: symbol-name matches,
  path matches, PageRank tiebreak.
- `skeleton` — signature-only skeleton of one file (definitions + line numbers, no bodies).
- `module_brief` — brief for one file (skeleton + import edges) or directory
  (top-ranked files with their defined symbols). Output capped.

## Run

```sh
npm install
node server.js        # speaks MCP over stdio; diagnostics go to stderr only
```

## Attach to an agent

Claude Code (no install needed — npx fetches it from GitHub):

```sh
claude mcp add razor -- npx -y github:NSmonish/razor-mcp
```

Any client with `.mcp.json` (Claude Code, Cursor, VS Code):

```json
{ "mcpServers": { "razor": { "command": "npx", "args": ["-y", "github:NSmonish/razor-mcp"] } } }
```

Local path instead (if you cloned the repo yourself):

```sh
claude mcp add razor -- node /path/to/razor-mcp/server.js
```

## Notes

- `repo_path` must be an existing directory; file arguments must resolve
  inside it (symlinks resolved), so a client cannot read outside the repo.
- The graph is cached per server process; `map_repo` with `rebuild: true`
  refreshes it after the repo changes.
- The v2 tree is required by path and never modified.
- Dev smoke test: `npm run smoke` (spawns the server and calls all four
  tools against `razor-cli-v2`).
