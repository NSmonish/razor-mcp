#!/usr/bin/env node
// razor-mcp — MCP server (stdio) around razor-cli-v2's static analysis.
// Read-only: tools never write to the repo. All diagnostics go to stderr;
// stdout carries the MCP protocol and must stay clean.
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';
import { createRequire } from 'module';

const require = createRequire(import.meta.url);
const adapter = require('./adapter.cjs');
const fixBridge = require('./fix_bridge.cjs');
const compareBridge = require('./compare_bridge.cjs');

const server = new McpServer({ name: 'razor', version: '0.3.0' });

function wrap(fn) {
  return async (args) => {
    try {
      return { content: [{ type: 'text', text: fn(args) }] };
    } catch (err) {
      return { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true };
    }
  };
}

function wrapAsync(fn) {
  return async (args) => {
    try {
      const text = await fn(args);
      return { content: [{ type: 'text', text }] };
    } catch (err) {
      return { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true };
    }
  };
}

server.tool(
  'map_repo',
  'Build (or reuse a cached) static map of a repo: stack, file counts, and the PageRank-ordered file list, budgeted. Read-only.',
  {
    repo_path: z.string().describe('Absolute path to the repo root'),
    budget: z.number().optional().describe('Max bytes of the file list (default 8000)'),
    rebuild: z.boolean().optional().describe('Rebuild the graph instead of reusing the cache'),
  },
  wrap(({ repo_path, budget, rebuild }) => adapter.mapRepo(repo_path, { budget, rebuild })),
);

server.tool(
  'locate',
  'Rank repo files for an issue description using static signals only: symbol-name matches, path matches, PageRank tiebreak. No LLM, deterministic.',
  {
    repo_path: z.string().describe('Absolute path to the repo root'),
    issue_text: z.string().describe('Bug report or task description'),
    top_k: z.number().optional().describe('How many candidates to return (default 5, max 20)'),
  },
  wrap(({ repo_path, issue_text, top_k }) => adapter.locate(repo_path, issue_text, top_k)),
);

server.tool(
  'skeleton',
  'Signature-only skeleton of one repo file: definition lines with line numbers, no bodies. Read-only.',
  {
    repo_path: z.string().describe('Absolute path to the repo root'),
    file: z.string().describe('Repo-relative path to the file'),
  },
  wrap(({ repo_path, file }) => adapter.skeleton(repo_path, file)),
);

server.tool(
  'module_brief',
  'Brief for one module: a file gets its skeleton plus import edges; a directory gets its top-ranked files with their defined symbols. Output capped. Read-only.',
  {
    repo_path: z.string().describe('Absolute path to the repo root'),
    path: z.string().describe('Repo-relative file or directory path'),
    max_bytes: z.number().optional().describe('Max bytes of the brief (default 8000)'),
  },
  wrap(({ repo_path, path: target, max_bytes }) => adapter.moduleBrief(repo_path, target, max_bytes)),
);

server.tool(
  'razor_fix',
  'Fix one bug in a local Python repo with the Step 3 loop (budgeted reads, masking keep-10, allocation guard, GPT-5 mini, temp 0.0). Runs locally in a scratch copy; the repo is never modified. Returns a patch ONLY if the scoped tests pass on it, otherwise no patch and a summary. Typical run 1-5 minutes, hard-capped by cost_cap_usd (default 0.50). Needs OPENAI_API_KEY in the server environment (your own key; the spend is yours). Python-only, git repos only, and the test command must already work in your environment.',
  {
    repo_path: z.string().describe('Absolute path to the repo root'),
    issue_text: z.string().describe('Bug report in your own words'),
    failing_tests: z.array(z.string()).optional().describe('Failing test node IDs, e.g. tests/test_x.py::test_y (optional; when absent, the test command decides what fails)'),
    test_command: z.string().optional().describe('Test command run from the repo dir, e.g. pytest tests/test_x.py -q (optional; auto-detects pytest)'),
    cost_cap_usd: z.number().optional().describe('Hard per-fix cap in USD (default 0.50)'),
  },
  wrapAsync(({ repo_path, issue_text, failing_tests, test_command, cost_cap_usd }) => fixBridge.runFix({ repo_path, issue_text, failing_tests, test_command, cost_cap_usd })),
);

server.tool(
  'razor_compare',
  'Run one bug twice on fresh scratch copies, once with the plain naive loop and once with the Step 3 loop, and return a side-by-side table: tokens, billed cost, solved or not, time, and a patch per arm that solved. Same prompt, same test gate, same model (GPT-5 mini), your own key. The comparison measures the harness naive loop vs Step 3, not your real agent, and one bug is one sample. Typical pair 2-10 minutes, hard-capped by cost_cap_usd for the pair (default 1.00, $0.50 per arm). Needs OPENAI_API_KEY in the server environment. Python-only, git repos only, and the test command must already work in your environment.',
  {
    repo_path: z.string().describe('Absolute path to the repo root'),
    issue_text: z.string().describe('Bug report in your own words'),
    failing_tests: z.array(z.string()).optional().describe('Failing test node IDs, e.g. tests/test_x.py::test_y (optional; when absent, the test command decides what fails)'),
    test_command: z.string().optional().describe('Test command run from the repo dir, e.g. pytest tests/test_x.py -q (optional; auto-detects pytest)'),
    cost_cap_usd: z.number().optional().describe('Hard total cap in USD for the pair (default 1.00)'),
  },
  wrapAsync(({ repo_path, issue_text, failing_tests, test_command, cost_cap_usd }) => compareBridge.runCompare({ repo_path, issue_text, failing_tests, test_command, cost_cap_usd })),
);

const transport = new StdioServerTransport();
await server.connect(transport);
console.error('razor-mcp listening on stdio');
