// adapter.cjs — read-only wrapper around Razor's static analysis.
// It never writes to the repo. All functions take plain JSON-able inputs
// and return plain JSON-able outputs.
'use strict';

const fs = require('fs');
const path = require('path');

// Static analysis lives in ./lib (copies of razor-cli-v2's scanner,
// codegraph and modulemap) so the shipped package is self-contained.
const { scanRepo } = require('./lib/scanner.js');
const { buildCodeGraph, skeletonSection } = require('./lib/codegraph.js');
const { buildModuleMap } = require('./lib/modulemap.js');

// ---- path safety -----------------------------------------------------------
// repo_path must be an existing directory. Every file argument must resolve
// inside it (symlinks resolved), so a client cannot read outside the repo.
function resolveRepo(repoPath) {
  if (!repoPath || typeof repoPath !== 'string') throw new Error('repo_path is required');
  const real = fs.realpathSync(repoPath);
  if (!fs.statSync(real).isDirectory()) throw new Error(`not a directory: ${repoPath}`);
  return real;
}

function resolveInside(repo, rel) {
  const target = path.resolve(repo, rel);
  const real = fs.existsSync(target) ? fs.realpathSync(target) : target;
  if (real !== repo && !real.startsWith(repo + path.sep)) {
    throw new Error(`path escapes repo: ${rel}`);
  }
  return { abs: real, rel: path.relative(repo, real).split(path.sep).join('/') };
}

// ---- graph cache -----------------------------------------------------------
// Building the graph reads the repo (up to 4000 files, capped). Cache it per
// process so repeated tool calls are fast. map_repo with rebuild=true refreshes.
const cache = new Map();

function getGraph(repoPath, rebuild) {
  const repo = resolveRepo(repoPath);
  if (!rebuild && cache.has(repo)) return { repo, ...cache.get(repo) };
  const scan = scanRepo(repo);
  const graph = buildCodeGraph(repo, scan);
  if (!graph) throw new Error(`no code files found under: ${repo}`);
  const entry = { scan, graph };
  cache.set(repo, entry);
  return { repo, ...entry };
}

// ---- tokenize --------------------------------------------------------------
const STOP = new Set(['the', 'and', 'for', 'with', 'from', 'into', 'this', 'that', 'when', 'where', 'which', 'should', 'would', 'could', 'about', 'after', 'before', 'under', 'over', 'issue', 'error', 'bug', 'fix', 'fails', 'failed', 'failing', 'is', 'are', 'was', 'were', 'be', 'been', 'has', 'have', 'it', 'its', 'of', 'on', 'in', 'to', 'at', 'by', 'or', 'not', 'no', 'do', 'does', 'as', 'an']);

function tokenize(text) {
  const out = new Set();
  for (const raw of String(text || '').split(/[^A-Za-z0-9_]+/)) {
    if (!raw) continue;
    // split camelCase: hashPassword -> hash, Password
    for (const part of raw.replace(/([a-z0-9])([A-Z])/g, '$1 $2').split(/[\s_]+/)) {
      const t = part.toLowerCase();
      if (t.length >= 2 && !STOP.has(t)) out.add(t);
    }
  }
  return out;
}

// ---- tools -----------------------------------------------------------------
function mapRepo(repoPath, opts = {}) {
  const { repo, scan, graph } = getGraph(repoPath, opts.rebuild);
  const stack = scan.stack || scan;
  const budget = Math.min(Math.max(opts.budget || 8000, 1000), 40000);
  const lines = [];
  let bytes = 0;
  for (const f of graph.rankedFiles) {
    const info = graph.files[f];
    const line = `- ${f} (rank ${info.rank}, indegree ${info.indegree}, ${info.defs.length} defs)`;
    if (bytes + line.length + 1 > budget) break;
    lines.push(line);
    bytes += line.length + 1;
  }
  let subsystems = null;
  try {
    const mm = buildModuleMap(repo, scan);
    subsystems = mm.subsystems;
  } catch (_) { /* module map is best-effort */ }
  return JSON.stringify({
    repo,
    stack: {
      language: stack.language || 'unknown',
      framework: stack.framework || 'unknown',
      testing: stack.testing || 'unknown',
    },
    filesAnalyzed: graph.filesAnalyzed,
    defsTotal: graph.defsTotal,
    edges: graph.edges,
    subsystems: subsystems || undefined,
    rankedFiles: lines,
    truncated: lines.length < graph.rankedFiles.length,
  }, null, 2);
}

function locate(repoPath, issueText, topK = 5) {
  const { graph } = getGraph(repoPath, false);
  const k = Math.min(Math.max(topK || 5, 1), 20);
  const tokens = tokenize(issueText);
  const scored = [];
  for (const f of graph.rankedFiles) {
    const info = graph.files[f];
    const matchedDefs = [];
    for (const d of info.defs) {
      const nameLower = d.name.toLowerCase();
      const defTokens = tokenize(d.name);
      let hit = false;
      for (const t of tokens) {
        if (defTokens.has(t) || (t.length >= 4 && nameLower.includes(t))) { hit = true; break; }
      }
      if (hit && matchedDefs.length < 5) matchedDefs.push(d.name);
    }
    const pathTokens = tokenize(f.replace(/\//g, ' '));
    let pathMatches = 0;
    for (const t of tokens) if (pathTokens.has(t)) pathMatches += 1;
    const defMatches = matchedDefs.length;
    const score = defMatches * 3 + pathMatches;
    scored.push({ file: f, score, defMatches, pathMatches, matchedDefs, rank: info.rank, indegree: info.indegree });
  }
  scored.sort((a, b) => (b.score - a.score) || (b.rank - a.rank) || a.file.localeCompare(b.file));
  const top = scored.filter((s) => s.score > 0).slice(0, k);
  return JSON.stringify({
    queryTokens: [...tokens].sort(),
    note: top.length ? undefined : 'no token matches; returning top-ranked files instead',
    results: (top.length ? top : scored.slice(0, k)).map((s) => ({
      file: s.file,
      score: s.score,
      matchedDefs: s.matchedDefs,
      rank: s.rank,
      indegree: s.indegree,
    })),
  }, null, 2);
}

function skeleton(repoPath, file) {
  const { repo, graph } = getGraph(repoPath, false);
  const { rel } = resolveInside(repo, file);
  if (!graph.files[rel]) throw new Error(`not a code file in repo graph: ${rel}`);
  const section = skeletonSection(graph, rel);
  return JSON.stringify({ file: rel, skeleton: section }, null, 2);
}

function moduleBrief(repoPath, target, maxBytes = 8000) {
  const { repo, graph } = getGraph(repoPath, false);
  const { abs, rel } = resolveInside(repo, target);
  const cap = Math.min(Math.max(maxBytes || 8000, 1000), 40000);
  if (fs.statSync(abs).isFile()) {
    if (!graph.files[rel]) throw new Error(`not a code file in repo graph: ${rel}`);
    const info = graph.files[rel];
    let text = skeletonSection(graph, rel);
    text += `\nimported by: ${(info.in || []).slice(0, 10).join(', ') || '(none)'}\nimports: ${(info.out || []).slice(0, 10).join(', ') || '(none)'}\n`;
    return JSON.stringify({ path: rel, kind: 'file', rank: info.rank, brief: text.slice(0, cap) }, null, 2);
  }
  // directory: top-ranked files under it, def names only (token-capped)
  // (rel === '' means the repo root itself, e.g. path '.')
  const under = (rel === '' ? graph.rankedFiles : graph.rankedFiles.filter((f) => f === rel || f.startsWith(rel + '/'))).slice(0, 10);
  if (!under.length) throw new Error(`no code files under: ${rel || '.'}`);
  const parts = under.map((f) => {
    const info = graph.files[f];
    return `${f} (rank ${info.rank}): ${info.defs.map((d) => d.name).join(', ') || '(no defs)'}`;
  });
  return JSON.stringify({ path: rel || '.', kind: 'directory', files: under, brief: parts.join('\n').slice(0, cap) }, null, 2);
}

module.exports = { mapRepo, locate, skeleton, moduleBrief };
