// Dev-only smoke test: spawns server.js over stdio and calls all five tools.
// razor_fix runs against test-fixture, whose tests already pass, so it
// returns SETUP_FAIL without spending anything. A dummy key is set so the
// key check passes and the precheck path is exercised; no real API call
// is made. Not part of the shipped server.
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

// The MCP SDK's stdio transport inherits only a safe env allowlist, so
// the key must be passed explicitly in the transport params (same as the
// "env" block in a real .mcp.json). A dummy key is enough: the fixture's
// tests already pass, so the precheck stops the fix before any model
// call and no real API request is made.
const DUMMY_KEY = process.env.OPENAI_API_KEY || 'sk-test-dummy';

const repo = process.argv[2] || '/home/hatch/workspace/razor-cli-v2';
const transport = new StdioClientTransport({ command: 'node', args: ['server.js'], cwd: import.meta.dirname, env: { OPENAI_API_KEY: DUMMY_KEY } });
const client = new Client({ name: 'razor-smoke', version: '0.1.0' }, { capabilities: {} });
await client.connect(transport);

const tools = await client.listTools();
console.log('TOOLS:', tools.tools.map((t) => t.name).join(', '));

async function call(name, args) {
  const res = await client.callTool({ name, arguments: args });
  const text = res.content.map((c) => c.text).join('\n');
  console.log(`\n=== ${name} (isError=${!!res.isError}) ${text.length} chars ===`);
  console.log(text.slice(0, 1200));
  return res;
}

await call('map_repo', { repo_path: repo });
await call('locate', { repo_path: repo, issue_text: 'code graph pagerank ranking is wrong for javascript imports', top_k: 5 });
await call('skeleton', { repo_path: repo, file: 'codegraph.js' });
await call('module_brief', { repo_path: repo, path: '.' });
await call('locate', { repo_path: '/nonexistent-dir-xyz', issue_text: 'x' });

// razor_fix end-to-end through MCP: the fixture's tests already pass, so
// the precheck fails the fix before any model call ($0). The result must
// be a SETUP_FAIL JSON round-tripped through the bridge.
const fixture = `${import.meta.dirname}/test-fixture`;
const fix = await call('razor_fix', {
  repo_path: fixture,
  issue_text: 'add(2, 3) is wrong',
  failing_tests: ['tests/test_calc.py'],
  test_command: 'python3 tests/test_calc.py',
});
const parsed = JSON.parse(fix.content.map((c) => c.text).join('\n'));
console.log(`\n=== razor_fix status: ${parsed.status} ===`);
if (parsed.status !== 'SETUP_FAIL') {
  throw new Error(`expected razor_fix SETUP_FAIL, got ${parsed.status}`);
}
if (parsed.patch !== null && parsed.patch !== undefined) {
  throw new Error('expected no patch on SETUP_FAIL');
}
await client.close();
console.log('\nSMOKE DONE');
