# razor-mcp

MCP server (stdio) around Razor's static repo analysis, from `razor-cli-v2`,
plus `razor_fix`, which runs the Step 3 fix loop on a bug in a local repo.

The first four tools are read-only and deterministic: no LLM calls, no
writes. `razor_fix` is the exception: it calls the model with **your own
OpenAI key** (`OPENAI_API_KEY` in the server environment) and spends your
credits, capped per fix.

## Tools

- `map_repo` — stack, file/def/edge counts, PageRank-ordered file list (budgeted).
- `locate` — rank repo files for an issue description: symbol-name matches,
  path matches, PageRank tiebreak.
- `skeleton` — signature-only skeleton of one file (definitions + line numbers, no bodies).
- `module_brief` — brief for one file (skeleton + import edges) or directory
  (top-ranked files with their defined symbols). Output capped.
- `razor_fix` — fix one bug in a local Python repo. See below.

## Fix a bug (razor_fix)

The agent calls `razor_fix` with a repo path and the bug in words. The Step 3
loop (budgeted reads, masking keep-10, allocation guard, GPT-5 mini) runs
locally in a scratch copy of the repo. The repo itself is never modified; the
patch comes back as text for the agent to apply.

- **Patch only if the tests pass.** The loop's fix is graded by re-running
  the scoped tests. A patch is returned only on a pass. Otherwise the tool
  returns no patch, only a summary (what was tried, tokens, cost).
- **Your key, your spend.** `OPENAI_API_KEY` must be set in the server
  environment. Default cap is $0.50 per fix (`cost_cap_usd` to change it);
  the loop stops at the cap and reports `COST_CAPPED`.
- **Scope (v1):** Python repos, git only, with a test command that already
  works in your environment (`test_command`, or pytest auto-detected).
  Best on single-site, long-trajectory fixes. No dependency setup yet: if
  the environment is broken the tool says so instead of fixing it.
- **Runtime:** typically 1-5 minutes per fix. The call blocks; the bridge
  times out at 30 minutes by default (`RAZOR_FIX_TIMEOUT_S` to raise it).
  The vendored Python loop lives in `python/` (verbatim copies of the
  harness files; re-sync them from `razor-fresh/harness` when Step 3
  changes).

## Run

```sh
npm install
OPENAI_API_KEY=sk-... node server.js   # speaks MCP over stdio; diagnostics go to stderr only
```

## Attach to an agent

Claude Code (no install needed — npx fetches it from GitHub):

```sh
claude mcp add razor -- npx -y github:NSmonish/razor-mcp
```

Any client with `.mcp.json` (Claude Code, Cursor, VS Code):

```json
{ "mcpServers": { "razor": { "command": "npx", "args": ["-y", "github:NSmonish/razor-mcp"], "env": { "OPENAI_API_KEY": "sk-your-own-key" } } } }
```

`razor_fix` needs `OPENAI_API_KEY` in the server environment; the other four
tools work without it.

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
- Dev smoke test: `npm run smoke` (spawns the server and calls all five
  tools; `razor_fix` runs against a fixture repo whose tests already pass,
  so it returns SETUP_FAIL without spending anything).
