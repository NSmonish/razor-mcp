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

const server = new McpServer({ name: 'razor', version: '0.1.0' });

function wrap(fn) {
  return async (args) => {
    try {
      return { content: [{ type: 'text', text: fn(args) }] };
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

const transport = new StdioServerTransport();
await server.connect(transport);
console.error('razor-mcp listening on stdio');
