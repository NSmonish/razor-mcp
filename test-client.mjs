// Dev-only smoke test: spawns server.js over stdio and calls all four tools
// against a sample repo. Not part of the shipped server.
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const repo = process.argv[2] || '/home/hatch/workspace/razor-cli-v2';
const transport = new StdioClientTransport({ command: 'node', args: ['server.js'], cwd: import.meta.dirname });
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
await client.close();
console.log('\nSMOKE DONE');
