// fix_bridge.cjs — spawn the vendored Python fix loop (python/run_fix.py)
// once per razor_fix call and return its JSON result as a string.
//
// The user's OpenAI key reaches Python only through the OPENAI_API_KEY
// environment variable, which the MCP server process already carries.
// Never log arguments or environment here: the issue text is user data
// and the environment holds the key.
//
// Env knobs: RAZOR_FIX_PYTHON (default python3), RAZOR_FIX_TIMEOUT_S
// (default 1800; the loop's own deadline is 5400s, so the bridge is the
// binding limit — raise it for very long fixes).
'use strict';
const { execFile } = require('child_process');
const path = require('path');

const RUN_FIX = path.join(__dirname, 'python', 'run_fix.py');
const PYTHON = process.env.RAZOR_FIX_PYTHON || 'python3';
const TIMEOUT_MS = (Number(process.env.RAZOR_FIX_TIMEOUT_S) || 1800) * 1000;
const MAX_BUFFER = 64 * 1024 * 1024;

function runFix({ repo_path, issue_text, failing_tests, test_command,
                  cost_cap_usd }) {
  return new Promise((resolve) => {
    const args = ['-u', RUN_FIX, '--repo', repo_path, '--issue', issue_text];
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
          status: 'UNRESOLVED', patch: null,
          summary: `fix run timed out after ${TIMEOUT_MS / 1000}s`,
        }));
      }
      const text = (stdout || '').trim();
      try {
        return resolve(JSON.stringify(JSON.parse(text)));
      } catch {
        const detail = ((stderr || '')
          || (err && err.message) || 'no output').slice(0, 500);
        return resolve(JSON.stringify({
          status: 'UNRESOLVED', patch: null,
          summary: `fix runner failed: ${detail}`,
        }));
      }
    });
  });
}

module.exports = { runFix };
