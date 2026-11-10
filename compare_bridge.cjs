// compare_bridge.cjs — spawn the vendored Python comparison loop
// (python/run_compare.py) once per razor_compare call and return its
// JSON result as a string. Same shape and env knobs as fix_bridge.cjs:
// the key reaches Python only through OPENAI_API_KEY, and the issue
// text and environment are never logged.
//
// Env knobs: RAZOR_FIX_PYTHON (default python3), RAZOR_FIX_TIMEOUT_S
// (default 1800; a pair runs back to back, so the bridge is the
// binding limit — raise it for very long comparisons).
'use strict';
const { execFile } = require('child_process');
const path = require('path');

const RUN_COMPARE = path.join(__dirname, 'python', 'run_compare.py');
const PYTHON = process.env.RAZOR_FIX_PYTHON || 'python3';
const TIMEOUT_MS = (Number(process.env.RAZOR_FIX_TIMEOUT_S) || 1800) * 1000;
const MAX_BUFFER = 64 * 1024 * 1024;

function runCompare({ repo_path, issue_text, failing_tests, test_command,
                      cost_cap_usd }) {
  return new Promise((resolve) => {
    const args = ['-u', RUN_COMPARE, '--repo', repo_path,
                  '--issue', issue_text];
    if (failing_tests && failing_tests.length) {
      args.push('--failing-tests', failing_tests.join(','));
    }
    if (test_command) args.push('--test-command', test_command);
    if (cost_cap_usd != null) args.push('--cost-cap', String(cost_cap_usd));
    execFile(PYTHON, args, {
      timeout: TIMEOUT_MS, maxBuffer: MAX_BUFFER, env: process.env,
    }, (err, stdout, stderr) => {
      if (err && err.killed) {
        return resolve(JSON.stringify({
          status: 'PARTIAL', naive: null, razor: null, comparison: null,
          summary: `compare run timed out after ${TIMEOUT_MS / 1000}s`,
        }));
      }
      const text = (stdout || '').trim();
      try {
        return resolve(JSON.stringify(JSON.parse(text)));
      } catch {
        const detail = ((stderr || '')
          || (err && err.message) || 'no output').slice(0, 500);
        return resolve(JSON.stringify({
          status: 'PARTIAL', naive: null, razor: null, comparison: null,
          summary: `compare runner failed: ${detail}`,
        }));
      }
    });
  });
}

module.exports = { runCompare };
