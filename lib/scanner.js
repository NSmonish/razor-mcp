// repo scanner — reads a project folder and figures out what stack it uses.
// detection is evidence-based: every fact records which file proved it, so the
// generated AGENTS.md isn't guessing. output keeps the original fields
// (language/framework/testing/orm/routes_folder) plus extras + an evidence map.
const fs = require('fs');
const path = require('path');

const IGNORE_DIRS = new Set([
  'node_modules', '.git', 'dist', 'build', 'out', '.next', '.cache',
  'coverage', 'venv', '.venv', '__pycache__', '.idea', '.vscode', 'target',
]);

const MAX_DEPTH = 4;      // don't dig forever
const MAX_FILES = 4000;   // safety cap on big repos
const MAX_READ_BYTES = 200 * 1024; // only read the first chunk of a file

// ---- .gitignore (minimal, root file only) --------------------------------
// comments/blanks skipped; `!` negations unsupported (a negated path stays
// included — the safe direction for a scanner); trailing `/` = dirs only;
// leading `/` anchors to the root; `*`/`?`/`**` globs supported.
function globToRegExp(glob, anchored) {
  let out = '';
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i];
    if (c === '*') {
      if (glob[i + 1] === '*') { out += '.*'; i++; } else out += '[^/]*';
    } else if (c === '?') out += '[^/]';
    else out += c.replace(/[.+^${}()|[\]\\]/g, '\\$&');
  }
  return new RegExp(`^${anchored ? '' : '(.*/)?'}${out}(/.*)?$`);
}

function makeIgnoreMatcher(root) {
  let text = '';
  try { text = fs.readFileSync(path.join(root, '.gitignore'), 'utf8'); } catch (_) { return () => false; }
  const rules = [];
  for (const raw of text.split('\n')) {
    let line = raw.trim();
    if (!line || line.startsWith('#') || line.startsWith('!')) continue;
    const dirOnly = line.endsWith('/');
    if (dirOnly) line = line.slice(0, -1);
    const anchored = line.startsWith('/');
    if (anchored) line = line.slice(1);
    if (line) rules.push({ re: globToRegExp(line, anchored), dirOnly });
  }
  if (!rules.length) return () => false;
  return (rel, isDir) => {
    const p = rel.split(path.sep).join('/');
    for (const r of rules) {
      if (r.dirOnly && !isDir) {
        const parent = p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '';
        if (parent && r.re.test(parent)) return true;
        continue;
      }
      if (r.re.test(p)) return true;
    }
    return false;
  };
}

// walk the tree, collecting relative file paths (skipping ignored dirs).
// traversal is name-sorted so the file order — and the cut when MAX_FILES
// truncates a huge repo — is deterministic, not OS readdir order.
function walk(root) {
  const files = [];
  const isIgnored = makeIgnoreMatcher(root);
  const stack = [{ dir: root, rel: '', depth: 0 }];
  while (stack.length && files.length < MAX_FILES) {
    const { dir, rel, depth } = stack.pop();
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch (_) {
      continue;
    }
    entries.sort((a, b) => a.name.localeCompare(b.name));
    for (const e of entries) {
      const full = path.join(dir, e.name);
      const childRel = rel ? `${rel}/${e.name}` : e.name;
      if (e.isDirectory()) {
        if (IGNORE_DIRS.has(e.name) || e.name.startsWith('.')) continue;
        if (isIgnored(childRel, true)) continue;
        if (depth < MAX_DEPTH) stack.push({ dir: full, rel: childRel, depth: depth + 1 });
      } else if (e.isFile()) {
        if (isIgnored(childRel, false)) continue;
        files.push(childRel.split('/').join(path.sep));
        if (files.length >= MAX_FILES) break;
      }
    }
  }
  return files;
}

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

function parseJson(root, rel) {
  const txt = readSafe(root, rel);
  if (!txt) return null;
  try {
    return JSON.parse(txt);
  } catch (_) {
    return null;
  }
}

// does any file whose path matches `re` contain `needle`?
function codeContains(root, files, re, needle) {
  for (const f of files) {
    if (!re.test(f)) continue;
    const txt = readSafe(root, f);
    if (txt.includes(needle)) return f;
  }
  return null;
}

// ---- node detection -------------------------------------------------------

function detectNode(root, files, ev) {
  const pkg = parseJson(root, 'package.json');
  if (!pkg) return null;
  ev.language = 'package.json';

  const deps = { ...(pkg.dependencies || {}), ...(pkg.devDependencies || {}) };
  const has = (n) => Object.prototype.hasOwnProperty.call(deps, n);
  const jsRe = /\.(m?js|cjs|ts|tsx|jsx)$/;

  // framework — prefer code confirmation over a bare dep listing
  const fwTable = [
    ['next', 'Next.js', "next"],
    ['nuxt', 'Nuxt', "nuxt"],
    ['@angular/core', 'Angular', "@angular"],
    ['svelte', 'Svelte', "svelte"],
    ['express', 'Express.js', "express"],
    ['fastify', 'Fastify', "fastify"],
    ['@nestjs/core', 'NestJS', "@nestjs"],
    ['koa', 'Koa', "koa"],
    ['vue', 'Vue', "vue"],
    ['react', 'React', "react"],
    ['vite', 'Vite', "vite"],
  ];
  let framework = 'unknown';
  for (const [dep, name, needle] of fwTable) {
    if (!has(dep)) continue;
    const codeFile = codeContains(root, files, jsRe, needle);
    framework = name;
    ev.framework = codeFile ? `${codeFile} (imports ${needle})` : `package.json dep: ${dep}`;
    break;
  }
  // self-framework: the repo IS a framework (express scanning itself finds
  // no `express` dep — it doesn't depend on itself). the package's own name
  // is the tell.
  if (framework === 'unknown' && pkg.name) {
    const selfHit = fwTable.find(([dep]) => dep === pkg.name);
    if (selfHit) {
      framework = selfHit[1];
      ev.framework = `package.json name: ${pkg.name} (this repo is the framework itself)`;
    }
  }

  // testing — unit runners first, then e2e tools, then the dep-less
  // node:test runner (detected from the test script's body, not a dep).
  const testTable = [
    ['jest', 'Jest'], ['vitest', 'Vitest'], ['mocha', 'Mocha'],
    ['ava', 'Ava'], ['tap', 'Tap'], ['cypress', 'Cypress'],
    ['@playwright/test', 'Playwright'],
  ];
  let testing = 'unknown';
  for (const [dep, name] of testTable) {
    if (has(dep)) { testing = name; ev.testing = `package.json dep: ${dep}`; break; }
  }
  if (testing === 'unknown' && pkg.scripts && /\bnode\s+--test\b/.test(pkg.scripts.test || '')) {
    testing = 'node:test';
    ev.testing = 'package.json test script: node --test';
  }

  // orm
  const ormTable = [
    ['@prisma/client', 'Prisma'], ['prisma', 'Prisma'],
    ['sequelize', 'Sequelize'], ['typeorm', 'TypeORM'],
    ['mongoose', 'Mongoose'], ['drizzle-orm', 'Drizzle'],
  ];
  let orm = 'unknown';
  for (const [dep, name] of ormTable) {
    if (has(dep)) { orm = name; ev.orm = `package.json dep: ${dep}`; break; }
  }

  const language = files.some((f) => /\.tsx?$/.test(f)) ? 'TypeScript' : 'JavaScript';
  return { language, framework, testing, orm, _pkg: pkg };
}

// ---- python detection -----------------------------------------------------

function detectPython(root, files, ev) {
  const reqFiles = ['requirements.txt', 'pyproject.toml', 'Pipfile', 'setup.py']
    .filter((f) => files.includes(f));
  const hasPy = files.some((f) => f.endsWith('.py'));
  if (!reqFiles.length && !hasPy) return null;

  let text = '';
  for (const f of reqFiles) text += '\n' + readSafe(root, f).toLowerCase();
  if (reqFiles[0]) ev.language = reqFiles[0];
  else ev.language = 'has .py files';

  const has = (n) => text.includes(n);
  // word-boundary match so a stray substring (e.g. "flask" inside a doc extra)
  // doesn't win. used for framework names where precision matters.
  const hasWord = (n) => new RegExp(`(^|[^a-z0-9_])${n}([^a-z0-9_]|$)`).test(text);
  const where = reqFiles[0] || 'deps';

  // order: most specific / least ambiguous first. fastapi before flask because
  // fastapi projects sometimes mention flask in extras; django is distinct.
  let framework = 'unknown';
  if (hasWord('django')) { framework = 'Django'; ev.framework = `${where}: django`; }
  else if (hasWord('fastapi')) { framework = 'FastAPI'; ev.framework = `${where}: fastapi`; }
  else if (hasWord('flask')) { framework = 'Flask'; ev.framework = `${where}: flask`; }

  let testing = 'unknown';
  if (has('pytest')) { testing = 'PyTest'; ev.testing = `${where}: pytest`; }
  else if (files.some((f) => /test_.*\.py$/.test(f))) { testing = 'unittest/pytest'; ev.testing = 'test_*.py files'; }

  let orm = 'unknown';
  if (has('sqlalchemy')) { orm = 'SQLAlchemy'; ev.orm = `${where}: sqlalchemy`; }
  else if (has('django')) { orm = 'Django ORM'; ev.orm = `${where}: django`; }
  else if (has('peewee')) { orm = 'Peewee'; ev.orm = `${where}: peewee`; }

  return { language: 'Python', framework, testing, orm };
}

// ---- shared signals -------------------------------------------------------

function detectRoutesFolder(root, files, ev) {
  const candidates = [
    'src/api/routes', 'src/routes', 'app/routes', 'routes',
    'src/api', 'api', 'app/api', 'pages/api', 'src/controllers', 'controllers',
  ];
  for (const c of candidates) {
    const p = path.join(root, c);
    if (fs.existsSync(p) && fs.statSync(p).isDirectory()) {
      ev.routes_folder = `found dir: ${c}`;
      return '/' + c;
    }
  }
  return 'unknown';
}

function detectTestLocations(files) {
  const dirs = new Set();
  for (const f of files) {
    if (/\.(test|spec)\.[tj]sx?$/.test(f) || /(^|\/)test_.*\.py$/.test(f) || /(^|\/)tests?\//.test(f)) {
      dirs.add(path.dirname(f) === '.' ? '(root)' : path.dirname(f));
    }
  }
  return [...dirs].slice(0, 10);
}

function detectConfigFiles(files) {
  const known = [
    'tsconfig.json', 'jest.config.js', 'jest.config.ts', 'vitest.config.ts',
    '.eslintrc', '.eslintrc.json', '.eslintrc.js', 'pyproject.toml',
    'docker-compose.yml', 'Dockerfile', '.prettierrc',
  ];
  return files.filter((f) => known.some((k) => f === k || f.endsWith('/' + k)));
}

function detectPackageManager(files) {
  if (files.includes('pnpm-lock.yaml')) return 'pnpm';
  if (files.includes('yarn.lock')) return 'yarn';
  if (files.includes('package-lock.json')) return 'npm';
  if (files.includes('poetry.lock')) return 'poetry';
  if (files.includes('Pipfile.lock')) return 'pipenv';
  return 'unknown';
}

// how to run a package.json script. The script body alone isn't runnable: it
// relies on node_modules/.bin being on PATH, which only the package manager sets up.
function scriptInvocation(name, pm) {
  if (pm === 'yarn') return `yarn ${name}`;
  if (pm === 'pnpm') return `pnpm run ${name}`;
  return name === 'test' ? 'npm test' : `npm run ${name}`;
}

// pull the handful of commands an agent actually needs (build/test/dev/start/lint).
// scripts: role -> the command that runs it (`npm run db:migrate`, never a
// made-up `npm run migrate`). sources: role -> the real script name and body.
function extractScripts(pkg, pm) {
  if (!pkg || !pkg.scripts) return { scripts: {}, sources: {} };
  const all = pkg.scripts;
  const wanted = ['dev', 'start', 'build', 'test', 'lint', 'typecheck', 'format', 'migrate'];
  const names = {};
  for (const k of wanted) {
    if (all[k]) names[k] = k;
  }
  // migration scripts are commonly namespaced (db:migrate, migrate:up). if we
  // didn't find an exact `migrate` key, fall back to the first script whose
  // name contains "migrate". kept narrow on purpose: a broad fuzzy match would
  // misclassify e.g. test:dev as the dev script.
  if (!names.migrate) {
    const hit = Object.keys(all).find((name) => /(^|[:_-])migrate([:_-]|$)/i.test(name));
    if (hit) names.migrate = hit;
  }
  const scripts = {};
  const sources = {};
  for (const [role, name] of Object.entries(names)) {
    scripts[role] = scriptInvocation(name, pm);
    sources[role] = { name, body: all[name] };
  }
  return { scripts, sources };
}

// top-level dirs with a rough guess at what they hold
function extractStructure(root, files) {
  const topDirs = [...new Set(files.filter((f) => f.includes('/')).map((f) => f.split('/')[0]))]
    .filter((d) => !IGNORE_DIRS.has(d) && !d.startsWith('.'));
  const hint = (d) => {
    const m = {
      src: 'source code', app: 'application code', pages: 'routes/pages',
      components: 'UI components', routes: 'route handlers', api: 'api endpoints',
      controllers: 'controllers', models: 'data models', services: 'business logic',
      lib: 'shared libraries', utils: 'helpers', tests: 'tests', test: 'tests',
      public: 'static assets', prisma: 'prisma schema/migrations', migrations: 'db migrations',
      config: 'configuration', scripts: 'tooling scripts', docs: 'documentation',
      server: 'backend', client: 'frontend', frontend: 'frontend', backend: 'backend',
    };
    return m[d] || '';
  };
  return topDirs.slice(0, 20).map((d) => (hint(d) ? `${d}/ — ${hint(d)}` : `${d}/`));
}

// env var names from .env.example / .env.sample (names only, never values)
function extractEnvVars(root, files) {
  const envFile = ['.env.example', '.env.sample', '.env.template']
    .find((f) => files.includes(f));
  if (!envFile) return [];
  const txt = readSafe(root, envFile);
  const names = [];
  for (const line of txt.split('\n')) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=/);
    if (m) names.push(m[1]);
  }
  return names.slice(0, 30);
}

// key runtime deps (not dev) — gives the LLM the real libraries in play
function extractKeyDeps(pkg) {
  if (!pkg || !pkg.dependencies) return [];
  return Object.keys(pkg.dependencies).slice(0, 25);
}

// first non-empty heading/line of the readme as a one-liner description
function extractReadmeSummary(root, files) {
  const rf = files.find((f) => /^readme\.md$/i.test(f));
  if (!rf) return '';
  const txt = readSafe(root, rf);
  for (const line of txt.split('\n')) {
    const t = line.replace(/^#+\s*/, '').trim();
    if (t && !t.startsWith('![') && !t.startsWith('<')) return t.slice(0, 160);
  }
  return '';
}


// ---- main -----------------------------------------------------------------

function scanRepo(dir) {
  const root = path.resolve(dir);
  if (!fs.existsSync(root) || !fs.statSync(root).isDirectory()) {
    throw new Error(`not a directory: ${dir}`);
  }

  const files = walk(root);
  const ev = {};

  let stack = detectNode(root, files, ev);
  if (!stack) stack = detectPython(root, files, ev);

  // precedence fix: some Python projects carry JS tooling at the root
  // (package.json + Gruntfile, docs builders, etc). detectNode fires on that
  // package.json but finds no real framework. if there are strong Python
  // signals (pyproject/setup.py and lots of .py files), prefer Python so we
  // don't mislabel e.g. Django as "JavaScript".
  if (stack && stack.language !== 'Python' && stack.framework === 'unknown') {
    const hasPyManifest = ['pyproject.toml', 'setup.py', 'requirements.txt', 'Pipfile']
      .some((f) => files.includes(f));
    const pyCount = files.filter((f) => f.endsWith('.py')).length;
    if (hasPyManifest && pyCount >= 5) {
      const pyEv = {};
      const py = detectPython(root, files, pyEv);
      if (py && (py.framework !== 'unknown' || py.testing !== 'unknown')) {
        stack = py;
        Object.assign(ev, pyEv);
      }
    }
  }

  // nothing at root — look one level down for a package.json / python project
  // (common for monorepos: frontend/, backend/, client/, server/, api/)
  const subProjects = [];
  if (!stack || stack.framework === 'unknown') {
    const subDirs = [...new Set(files
      .filter((f) => f.includes('/'))
      .map((f) => f.split('/')[0]))]
      .filter((d) => !IGNORE_DIRS.has(d));
    for (const sub of subDirs) {
      const subRoot = path.join(root, sub);
      const subFiles = walk(subRoot);
      const subEv = {};
      let s = detectNode(subRoot, subFiles, subEv) || detectPython(subRoot, subFiles, subEv);
      if (s && s.framework !== 'unknown') {
        delete s._pkg;
        subProjects.push({ path: sub, ...s, evidence: subEv });
      }
    }
    // if root found nothing at all, promote the first sub-project up
    if ((!stack || stack.framework === 'unknown') && subProjects.length) {
      const main = subProjects[0];
      stack = { language: main.language, framework: main.framework, testing: main.testing, orm: main.orm };
      ev.framework = `${main.path}/: ${main.evidence.framework || main.framework}`;
    }
  }

  // static site fallback — html + no package.json/python
  if (!stack) {
    const hasHtml = files.some((f) => f.endsWith('.html'));
    if (hasHtml) {
      stack = { language: 'HTML/CSS/JS (static)', framework: 'Static site', testing: 'unknown', orm: 'unknown' };
      ev.framework = 'has .html, no package.json';
    } else {
      stack = { language: 'unknown', framework: 'unknown', testing: 'unknown', orm: 'unknown' };
    }
  }

  const entryPoint =
    (stack._pkg && stack._pkg.main) ||
    ['index.js', 'src/index.js', 'src/index.ts', 'main.py', 'app.py', 'manage.py', 'index.html']
      .find((f) => files.includes(f)) || 'unknown';
  const pkg = stack._pkg || null;
  delete stack._pkg;
  const packageManager = detectPackageManager(files);
  const { scripts, sources } = extractScripts(pkg, packageManager);

  return {
    language: stack.language,
    framework: stack.framework,
    testing: stack.testing,
    orm: stack.orm,
    routes_folder: detectRoutesFolder(root, files, ev),
    package_manager: packageManager,
    entry_point: entryPoint,
    scripts,
    script_sources: sources,
    package_scripts: Object.keys((pkg && pkg.scripts) || {}),
    key_dependencies: extractKeyDeps(pkg),
    env_vars: extractEnvVars(root, files),
    structure: extractStructure(root, files),
    readme_summary: extractReadmeSummary(root, files),
    test_locations: detectTestLocations(files),
    config_files: detectConfigFiles(files),
    sub_projects: subProjects.map((s) => ({ path: s.path, framework: s.framework, language: s.language })),
    file_count: files.length,
    files, // full relative-path list — used to validate discovered-skill evidence
    evidence: ev,
  };
}

module.exports = { scanRepo, walk };
