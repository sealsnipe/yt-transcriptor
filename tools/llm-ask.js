#!/usr/bin/env node
/* llm-ask — thin CLI bridge so this project's Python code can use the LLM
 * presets. It requires the stockstuff OAuth-preset backend, which owns the ONE
 * canonical token store — never duplicate it: two independent refreshers on the
 * same refresh_token could invalidate each other.
 *
 * Usage: node tools/llm-ask.js --role extraction [--system "..."] [--user "..."]
 *        echo "prompt" | node tools/llm-ask.js --role extraction
 * Prints the model's text to stdout; errors go to stderr with exit 1. */
const path = require('path');
const STOCKSTUFF = process.env.STOCKSTUFF_DIR || '/home/ma-agent1/projects/stockstuff';
const backend = require(path.join(STOCKSTUFF, 'server/agents/backend'));

function parseArgs(argv) {
  const args = { role: null, system: '', user: null };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--role') args.role = argv[++i];
    else if (a === '--system') args.system = argv[++i];
    else if (a === '--user') args.user = argv[++i];
    else if (a === '--help' || a === '-h') { args.help = true; }
    else { console.error(`unknown arg: ${a}`); process.exit(1); }
  }
  return args;
}

async function readStdin() {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  return Buffer.concat(chunks).toString('utf8');
}

(async () => {
  const args = parseArgs(process.argv);
  if (args.help || !args.role) {
    console.error('usage: llm-ask.js --role <orchestrator|extraction|sentiment|video-qa> [--system S] [--user U] (user falls back to stdin)');
    process.exit(args.help ? 0 : 1);
  }
  const user = args.user != null ? args.user : (await readStdin()).trim();
  if (!user) { console.error('empty user prompt'); process.exit(1); }
  try {
    const out = await backend.runRole(args.role, { system: args.system, user });
    if (!out) { console.error('empty model response'); process.exit(1); }
    process.stdout.write(out + '\n');
  } catch (e) {
    console.error(`llm-ask failed: ${e.message}`);
    process.exit(1);
  }
})();
