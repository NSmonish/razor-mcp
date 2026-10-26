// code graph — symbol-level static analysis: definitions (classes, functions,
// methods, arrows, exported names) and file-level reference edges, ranked so
// the most-depended-upon code surfaces first. deterministic and dependency-
// free, in the same spirit as modulemap.js: line-based extraction, no AST,
// no invented symbols — a definition exists only if its line was read, and
// an edge exists only if an import statement resolved to a real file
// (resolution reuses modulemap's parsers/resolvers verbatim).
//
// this is the substrate for the v2 pack: the ranked repo map in the entry,
// the skeletons index, repo-graph.json, and scripts/repo-graph.js all read
// this one structure. the file universe is the scanner's own list
// (scan.files), so the scanner's caps bound this pass too.
const fs = require('fs');
const path = require('path');
const {
  parsePythonImports, resolvePythonRef, pythonPackageOf,
  parseJsImports, resolveJsSpecifier, subsystemOf,
} = require('./modulemap');

const MAX_READ_BYTES = 200 * 1024; // match scanner.js / modulemap.js
const CODE_EXTS = new Set(['.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs']);
const SIG_MAX = 160;               // one signature never runs away
const DOC_MAX = 120;               // one doc line, truncated

function readSafe(root, rel) {
  try {
    const fd = fs.openSync(path.join(root, rel), 'r');
    const buf = Buffer.alloc(MAX_READ_BYTES);
    const n = fs.readSync(fd, buf, 0, MAX_READ_BYTES, 0);
    fs.closeSync(fd);
    return buf.slice(0, n).toString('utf8');
  } catch (_) {
    return '';
  }
}

const truncate = (s, max) => (s.length > max ? s.slice(0, max - 1).trimEnd() + '…' : s);

// ---- python definitions ---------------------------------------------------
// a definition's signature may span lines (long def headers): keep joining
// trimmed lines until the parens balance or a small cap is hit. returns the
// signature text and the index of its last line, so the docstring hunt starts
// after the header, not inside it.
function pythonSignature(lines, start) {
  let text = lines[start].trim();
  let depth = 0;
  for (const c of text) { if (c === '(') depth += 1; else if (c === ')') depth -= 1; }
  let end = start;
  while (depth > 0 && end + 1 < lines.length && end - start < 4) {
    end += 1;
    const part = lines[end].trim();
    text += ' ' + part;
    for (const c of part) { if (c === '(') depth += 1; else if (c === ')') depth -= 1; }
  }
  return { text: truncate(text.replace(/\s+/g, ' '), SIG_MAX), end };
}

// the first doc line under a definition: a docstring's first content line,
// or a `#` comment. anything else (code, blank-only gap > 1 line) -> null.
function pythonDoc(lines, after) {
  for (let i = after + 1; i < Math.min(lines.length, after + 3); i++) {
    const t = lines[i].trim();
    if (!t) continue;
    const q = t.startsWith('"""') ? '"""' : t.startsWith("'''") ? "'''" : null;
    if (q) {
      let body = t.slice(3);
      const close = body.indexOf(q);
      if (close !== -1) body = body.slice(0, close);
      return body.trim() ? truncate(body.trim(), DOC_MAX) : null;
    }
    if (t.startsWith('#')) return truncate(t.replace(/^#+\s*/, ''), DOC_MAX) || null;
    return null;
  }
  return null;
}

function parsePythonDefs(text) {
  const defs = [];
  const lines = text.split('\n');
  const classStack = []; // enclosing classes: { indent, name }
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const t = raw.trim();
    if (!t || t.startsWith('#')) continue;
    const indent = raw.length - raw.trimStart().length;
    while (classStack.length && indent <= classStack[classStack.length - 1].indent) classStack.pop();

    let m = t.match(/^class\s+([A-Za-z_]\w*)/);
    if (m) {
      const sig = pythonSignature(lines, i);
      defs.push({
        name: m[1], kind: 'class', line: i + 1, signature: sig.text,
        doc: pythonDoc(lines, sig.end),
      });
      classStack.push({ indent, name: m[1] });
      continue;
    }
    m = t.match(/^(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(/);
    if (m) {
      const sig = pythonSignature(lines, i);
      const parent = classStack.length ? classStack[classStack.length - 1].name : null;
      defs.push({
        name: m[1], kind: parent ? 'method' : 'function', line: i + 1,
        signature: sig.text, parent, doc: pythonDoc(lines, sig.end),
      });
      continue;
    }
    // module-level callable assignment: `app = Flask(__name__)`,
    // `handler = make_handler(...)` — a real entry point in flask-style code.
    if (indent === 0) {
      m = t.match(/^([A-Za-z_]\w*)\s*=\s*[A-Za-z_][\w.]*\s*\(/);
      if (m) {
        defs.push({ name: m[1], kind: 'callable', line: i + 1, signature: truncate(t, SIG_MAX), doc: pythonDoc(lines, i) });
      }
    }
  }
  return defs;
}

// ---- js / ts definitions --------------------------------------------------
// the comment line(s) directly above a definition: `//` lines or a /* */
// block. returns the block's first content line, or null.
function jsDocAbove(lines, i) {
  const block = [];
  for (let j = i - 1; j >= 0 && block.length < 6; j--) {
    const t = lines[j].trim();
    if (/^\/\//.test(t)) { block.unshift(t.replace(/^\/\/+\s*/, '')); continue; }
    if (/\*\/$/.test(t) || /^\*/.test(t) || /^\/\*/.test(t)) {
      block.unshift(t.replace(/^\/\*+\s*/, '').replace(/\*+\/$/, '').replace(/^\*\s*/, ''));
      if (/^\/\*/.test(t)) break;
      continue;
    }
    break;
  }
  const first = block.map((s) => s.trim()).find((s) => s);
  return first ? truncate(first, DOC_MAX) : null;
}

function parseJsDefs(text) {
  const defs = [];
  const seen = new Set(); // first definition of a name wins (source order)
  const lines = text.split('\n');
  const push = (name, kind, i, signature) => {
    if (seen.has(name)) return;
    seen.add(name);
    defs.push({ name, kind, line: i + 1, signature, doc: jsDocAbove(lines, i) });
  };
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const t = raw.trim();
    if (!t || t.startsWith('//')) continue;
    let m = t.match(/^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(/);
    if (m) { push(m[1], 'function', i, truncate(t.replace(/\{[^]*$/, '').trim() || t, SIG_MAX)); continue; }
    m = t.match(/^(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)/);
    if (m) { push(m[1], 'class', i, truncate(t, SIG_MAX)); continue; }
    m = t.match(/^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>/);
    if (m) { push(m[1], 'function', i, truncate(t, SIG_MAX)); continue; }
    m = t.match(/^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function/);
    if (m) { push(m[1], 'function', i, truncate(t, SIG_MAX)); continue; }
    // exported names: `export { a, b as c }` — the exported name is what
    // other files reference, so record that one.
    m = t.match(/^export\s*\{([^}]*)\}/);
    if (m) {
      for (const part of m[1].split(',')) {
        const name = part.trim().split(/\s+as\s+/).pop().trim();
        if (/^[A-Za-z_$][\w$]*$/.test(name)) push(name, 'export', i, truncate(t, SIG_MAX));
      }
      continue;
    }
    // plain exported value: `export const PORT = 3000` — still a name other
    // files import, worth one line in the skeleton.
    m = t.match(/^export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=/);
    if (m) { push(m[1], 'const', i, truncate(t.replace(/;\s*$/, ''), SIG_MAX)); continue; }
  }
  return defs;
}

// ---- ranking ---------------------------------------------------------------
// damped pagerank over the file graph (aider's move: most-referenced code
// first). iterations are over the path-sorted file list and every sum runs
// in that same order, so the floats — and therefore the ranking — are
// deterministic for a given repo state.
function computeRanks(files, outEdges) {
  const N = files.length;
  const indegree = new Map(files.map((f) => [f, 0]));
  for (const set of outEdges.values()) {
    for (const to of set) indegree.set(to, (indegree.get(to) || 0) + 1);
  }
  const d = 0.85;
  let rank = new Map(files.map((f) => [f, 1 / N]));
  for (let it = 0; it < 30; it++) {
    let dangling = 0;
    for (const f of files) {
      const out = outEdges.get(f);
      if (!out || !out.size) dangling += rank.get(f);
    }
    const next = new Map(files.map((f) => [f, (1 - d) / N + (d * dangling) / N]));
    for (const from of files) {
      const set = outEdges.get(from);
      if (!set || !set.size) continue;
      const share = (d * rank.get(from)) / set.size;
      for (const to of [...set].sort()) next.set(to, next.get(to) + share);
    }
    rank = next;
  }
  return { rank, indegree };
}

// ---- main -----------------------------------------------------------------
// build the code graph for a scanned repo. returns null when the repo has no
// code files at all, else:
// {
//   filesAnalyzed, defsTotal, edges,          // counts (edges = file pairs)
//   files: { [path]: { defs, out, in, rank, indegree } },
//   rankedFiles: [path, ...]                  // rank desc, indegree desc, path
// }
// defs are in source order; a symbol's rank is its file's rank, tie-broken
// by definition order (its index in defs).
function buildCodeGraph(root, scan) {
  const files = ((scan && scan.files) || [])
    .map((f) => f.split(path.sep).join('/'))
    .filter((f) => CODE_EXTS.has(path.posix.extname(f)))
    .sort();
  if (!files.length) return null;
  const fileSet = new Set(files);

  const pyModules = new Map();
  for (const f of files) {
    if (f.endsWith('.py')) {
      const norm = f.replace(/\.py$/, '');
      const parts = norm.split('/');
      if (parts[parts.length - 1] === '__init__') parts.pop();
      pyModules.set(parts.join('.'), f);
    }
  }

  const defsByFile = new Map();
  const outEdges = new Map();
  let defsTotal = 0;
  let edges = 0;
  const addEdge = (from, to) => {
    if (!to || to === from) return;
    if (!outEdges.has(from)) outEdges.set(from, new Set());
    if (!outEdges.get(from).has(to)) { outEdges.get(from).add(to); edges += 1; }
  };

  for (const f of files) {
    const text = readSafe(root, f);
    if (!text) { defsByFile.set(f, []); continue; }
    const defs = f.endsWith('.py') ? parsePythonDefs(text) : parseJsDefs(text);
    defsByFile.set(f, defs);
    defsTotal += defs.length;
    if (f.endsWith('.py')) {
      const pkg = pythonPackageOf(f);
      for (const ref of parsePythonImports(text)) addEdge(f, resolvePythonRef(ref, pkg, pyModules));
    } else {
      for (const spec of parseJsImports(text)) addEdge(f, resolveJsSpecifier(spec, f, fileSet));
    }
  }

  const { rank, indegree } = computeRanks(files, outEdges);
  const inEdges = new Map(files.map((f) => [f, []]));
  for (const [from, set] of outEdges) {
    for (const to of set) inEdges.get(to).push(from);
  }
  const byRank = (a, z) => (rank.get(z) - rank.get(a)) || (indegree.get(z) - indegree.get(a)) || a.localeCompare(z);

  const filesOut = {};
  for (const f of files) {
    filesOut[f] = {
      defs: defsByFile.get(f),
      out: [...(outEdges.get(f) || [])].sort(byRank),
      in: inEdges.get(f).sort(byRank),
      rank: Math.round(rank.get(f) * 1e6) / 1e6,
      indegree: indegree.get(f) || 0,
    };
  }
  return {
    filesAnalyzed: files.length,
    defsTotal,
    edges,
    files: filesOut,
    rankedFiles: [...files].sort(byRank),
  };
}

// ---- skeletons --------------------------------------------------------------
// skeleton markdown body (the emitter adds frontmatter): one section per
// file in rank order — signature lines with their source line numbers, doc
// lines as comments, NO bodies. files are accumulated until maxBytes; the
// rest are named in a truncation note (they remain reachable through
// repo-graph.json). deterministic for a given graph.
const SKELETON_MAX_BYTES = 200 * 1024;

function skeletonSection(graph, rel) {
  const info = graph.files[rel];
  const L = [`### ${rel}`, ''];
  if (!info.defs.length) {
    L.push('(no top-level definitions extracted)');
  }
  for (const def of info.defs) {
    const indent = def.kind === 'method' ? '    ' : '';
    L.push(`${indent}${def.signature}  # line ${def.line}`);
    if (def.doc) L.push(`${indent}${rel.endsWith('.py') ? '# ' : '// '}${def.doc}`);
  }
  L.push('');
  return L.join('\n');
}

function buildSkeletonText(graph, maxBytes = SKELETON_MAX_BYTES) {
  const L = [
    '# Skeletons',
    '',
    'Definition skeletons for every source file, most-depended-upon first:',
    'signatures and doc lines only — no bodies. Localize here, then open the',
    'one file you need (or query `scripts/repo-graph.js`).',
    '',
  ];
  let used = L.join('\n').length;
  let omitted = 0;
  for (const rel of graph.rankedFiles) {
    const section = skeletonSection(graph, rel);
    if (omitted === 0 && used + section.length > maxBytes && L.length > 6) {
      omitted = 1;
      continue;
    }
    if (omitted) { omitted += 1; continue; }
    L.push(section);
    used += section.length;
  }
  if (omitted) {
    L.push(`> Truncated: ${omitted} lower-ranked file(s) omitted to keep this index under ~${Math.round(maxBytes / 1024)}KB. Every file is still in \`repo-graph.json\`.`);
    L.push('');
  }
  return L.join('\n');
}

module.exports = {
  buildCodeGraph,
  buildSkeletonText,
  skeletonSection,
  SKELETON_MAX_BYTES,
  // exported for tests
  parsePythonDefs, parseJsDefs, computeRanks, subsystemOf,
};
