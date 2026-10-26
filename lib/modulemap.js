// module map — static import analysis: which files depend on which, grouped
// into subsystems (top-level package/dir), with the dependency edges between
// subsystems and each subsystem's most-depended-upon files.
//
// deterministic and dependency-free, in the same spirit as scanner.js and
// workflows.js: line-based import extraction (python + js/ts), no AST, no
// invented edges — an edge exists only if an import statement was actually
// read and resolved to a real file in the repo.
//
// the file universe is the scanner's own list (scan.files), so the scanner's
// caps (depth 4, 4000 files) bound this pass too; nothing is re-walked.
const fs = require('fs');
const path = require('path');

const MAX_READ_BYTES = 200 * 1024; // match scanner.js: only the first chunk
const CODE_EXTS = new Set(['.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs']);
const JS_EXTS = ['.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.json'];
const ROOT_SUBSYSTEM = '(root)';

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

// subsystem = first path segment; root-level files group together.
function subsystemOf(rel) {
  const norm = rel.split(path.sep).join('/');
  const i = norm.indexOf('/');
  return i === -1 ? ROOT_SUBSYSTEM : norm.slice(0, i);
}

// ---- python ---------------------------------------------------------------
// file -> dotted module name: a/b/c.py -> a.b.c, a/b/__init__.py -> a.b,
// root app.py -> app.
function pythonModuleName(rel) {
  const norm = rel.split(path.sep).join('/').replace(/\.py$/, '');
  const parts = norm.split('/');
  if (parts[parts.length - 1] === '__init__') parts.pop();
  return parts.join('.');
}

// the package a python file belongs to (for resolving relative imports):
// a/b/__init__.py IS package a.b; a/b/c.py lives IN package a.b.
function pythonPackageOf(rel) {
  const norm = rel.split(path.sep).join('/');
  const dir = path.posix.dirname(norm);
  if (path.posix.basename(norm) === '__init__.py') return dir === '.' ? '' : dir.split('/').join('.');
  return dir === '.' ? '' : dir.split('/').join('.');
}

// extract import references from python source. returns a list of:
//   { level: 0, from: 'a.b', names: ['c', ...] }   absolute from-import
//   { level: 0, from: null, names: ['a.b'] }      plain `import a.b`
//   { level: n, from: 'x.y'|'', names: [...] }     relative (n dots)
function parsePythonImports(text) {
  const refs = [];
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    let m = line.match(/^import\s+(.+)$/);
    if (m) {
      for (const part of m[1].split(',')) {
        const mod = part.trim().split(/\s+as\s+/)[0].trim();
        if (/^[\w.]+$/.test(mod)) refs.push({ level: 0, from: null, names: [mod] });
      }
      continue;
    }
    m = line.match(/^from\s+(\.*)([\w.]*)\s+import\s+(.+)$/);
    if (m) {
      const names = m[3].replace(/[()]/g, ' ')
        .split(',')
        .map((s) => s.trim().split(/\s+as\s+/)[0].trim())
        .filter((s) => /^[\w]+$/.test(s));
      refs.push({ level: m[1].length, from: m[2] || '', names });
    }
  }
  return refs;
}

// resolve one python import reference to a file in the module index, or null
// (external / stdlib / unresolvable — no edge is invented for those).
function resolvePythonRef(ref, importerPkg, modules) {
  let base = ref.from || '';
  if (ref.level > 0) {
    const pkgParts = importerPkg ? importerPkg.split('.') : [];
    // level 1 = the package itself, level 2 = its parent, ...
    const keep = pkgParts.slice(0, Math.max(0, pkgParts.length - (ref.level - 1)));
    base = [...keep, ...(base ? base.split('.') : [])].join('.');
  }
  const candidates = [];
  if (ref.from === null) {
    candidates.push(ref.names[0]); // plain `import a.b.c`
  } else {
    // `from base import name`: name may be a submodule of base, else the
    // imported symbol lives in base itself.
    for (const n of ref.names) candidates.push(base ? `${base}.${n}` : n);
    if (base) candidates.push(base);
  }
  for (const cand of candidates) {
    // longest-prefix match: importing a.b.c where only a.b is a real module
    // still lands on a.b's file.
    let c = cand;
    while (c) {
      if (modules.has(c)) return modules.get(c);
      const i = c.lastIndexOf('.');
      if (i === -1) break;
      c = c.slice(0, i);
    }
  }
  return null;
}

// ---- js / ts --------------------------------------------------------------
// specifiers from import/export-from/require/dynamic-import, in source order.
function parseJsImports(text) {
  const found = [];
  const collect = (re) => {
    for (const m of text.matchAll(re)) found.push({ spec: m[1], at: m.index });
  };
  collect(/^\s*import\s+(?:[^'";]*?\s+from\s+)?['"]([^'"]+)['"]/gm);
  collect(/^\s*export\s+[^'";]*?\s+from\s+['"]([^'"]+)['"]/gm);
  collect(/\brequire\(\s*['"]([^'"]+)['"]\s*\)/g);
  collect(/\bimport\(\s*['"]([^'"]+)['"]\s*\)/g);
  found.sort((a, z) => a.at - z.at);
  return [...new Set(found.map((f) => f.spec))];
}

// resolve a relative specifier against the file set (exact, +extension,
// +/index.<ext>). bare specifiers are external packages — not our graph.
function resolveJsSpecifier(spec, importerRel, fileSet) {
  if (!spec.startsWith('./') && !spec.startsWith('../')) return null;
  const dir = path.posix.dirname(importerRel.split(path.sep).join('/'));
  const base = path.posix.normalize(path.posix.join(dir === '.' ? '' : dir, spec));
  if (fileSet.has(base)) return base;
  for (const ext of JS_EXTS) {
    if (fileSet.has(base + ext)) return base + ext;
  }
  for (const ext of JS_EXTS) {
    if (fileSet.has(`${base}/index${ext}`)) return `${base}/index${ext}`;
  }
  return null;
}

// ---- main -----------------------------------------------------------------
// build the module map for a scanned repo. returns null when no intra-repo
// import resolved at all (nothing truthful to say), else:
// {
//   filesAnalyzed,          // code files read (.py/.js/.ts/...)
//   importsResolved,        // import statements resolved to a repo file
//   edges,                  // distinct cross-subsystem file->file pairs
//   subsystems: [{ name, fileCount, keyFiles: [{path, importedBy}],
//                  deps: [{to, count}] }]   // sorted, deterministic
// }
function buildModuleMap(root, scan) {
  const files = ((scan && scan.files) || [])
    .map((f) => f.split(path.sep).join('/'))
    .filter((f) => CODE_EXTS.has(path.posix.extname(f)));
  if (!files.length) return null;
  const fileSet = new Set(files);

  // python module index (module name -> file)
  const pyModules = new Map();
  for (const f of files) {
    if (f.endsWith('.py')) pyModules.set(pythonModuleName(f), f);
  }

  // file -> set of files it imports (deduped: one edge per file pair)
  const outEdges = new Map();
  let importsResolved = 0;
  const addEdge = (from, to) => {
    if (!to || to === from) return;
    if (!outEdges.has(from)) outEdges.set(from, new Set());
    if (!outEdges.get(from).has(to)) {
      outEdges.get(from).add(to);
      importsResolved += 1;
    }
  };

  for (const f of files) {
    const text = readSafe(root, f);
    if (!text) continue;
    if (f.endsWith('.py')) {
      const pkg = pythonPackageOf(f);
      for (const ref of parsePythonImports(text)) {
        addEdge(f, resolvePythonRef(ref, pkg, pyModules));
      }
    } else {
      for (const spec of parseJsImports(text)) {
        addEdge(f, resolveJsSpecifier(spec, f, fileSet));
      }
    }
  }
  if (!importsResolved) return null;

  // in-degree per file (distinct importers)
  const importedBy = new Map();
  for (const set of outEdges.values()) {
    for (const to of set) importedBy.set(to, (importedBy.get(to) || 0) + 1);
  }

  // group into subsystems + cross-subsystem edge counts
  const byName = new Map();
  const bucket = (name) => {
    if (!byName.has(name)) byName.set(name, { name, files: [], depCounts: new Map() });
    return byName.get(name);
  };
  for (const f of files) bucket(subsystemOf(f)).files.push(f);
  let crossEdges = 0;
  for (const [from, set] of outEdges) {
    const fromSub = subsystemOf(from);
    for (const to of set) {
      const toSub = subsystemOf(to);
      if (toSub === fromSub) continue;
      crossEdges += 1;
      const b = bucket(fromSub);
      b.depCounts.set(toSub, (b.depCounts.get(toSub) || 0) + 1);
    }
  }

  const subsystems = [...byName.values()]
    .map((b) => ({
      name: b.name,
      fileCount: b.files.length,
      keyFiles: b.files
        .filter((f) => (importedBy.get(f) || 0) > 0)
        .map((f) => ({ path: f, importedBy: importedBy.get(f) }))
        .sort((a, z) => z.importedBy - a.importedBy || a.path.localeCompare(z.path))
        .slice(0, 3),
      deps: [...b.depCounts.entries()]
        .map(([to, count]) => ({ to, count }))
        .sort((a, z) => z.count - a.count || a.to.localeCompare(z.to)),
    }))
    .sort((a, z) => a.name.localeCompare(z.name));

  return {
    filesAnalyzed: files.length,
    importsResolved,
    edges: crossEdges,
    subsystems,
  };
}

module.exports = {
  buildModuleMap,
  // exported for tests
  parsePythonImports, resolvePythonRef, pythonModuleName, pythonPackageOf,
  parseJsImports, resolveJsSpecifier, subsystemOf,
};
