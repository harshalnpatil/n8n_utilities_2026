import { spawn } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { chromium } from 'playwright';

const HOST = process.env.DIFF_REAL_HOST || '127.0.0.1';
const PORT = Number(process.env.DIFF_REAL_PORT || 8765);
const REQUESTED_INSTANCE = process.env.DIFF_REAL_INSTANCE || 'primary';
const INSTANCE_EXPLICIT = Object.prototype.hasOwnProperty.call(process.env, 'DIFF_REAL_INSTANCE');
const SKIP_BROWSER = /^(1|true|yes)$/i.test(process.env.DIFF_REAL_SKIP_BROWSER || '');
const OUTPUT_DIR = process.env.DIFF_REAL_OUTPUT_DIR || detectOutputDir();
const DOTENV = process.env.DIFF_REAL_DOTENV || 'secrets/.env.n8n';

function detectOutputDir() {
  const cwd = process.cwd();
  const parent = path.resolve(cwd, '..');
  const candidates = [cwd, parent];
  for (const candidate of candidates) {
    const hasState = existsSync(path.join(candidate, '.n8n_sync', 'state.json'));
    const hasWorkflows = existsSync(path.join(candidate, 'workflows'));
    if (hasState && hasWorkflows) {
      return candidate;
    }
  }
  for (const candidate of candidates) {
    const hasState = existsSync(path.join(candidate, '.n8n_sync', 'state.json'));
    if (hasState) {
      return candidate;
    }
  }
  return cwd;
}

function readStateRecords(outputDir) {
  const statePath = path.resolve(outputDir, '.n8n_sync/state.json');
  if (!existsSync(statePath)) {
    throw new Error(`Missing state file: ${statePath}. Run backup first.`);
  }

  const raw = readFileSync(statePath, 'utf8');
  const state = JSON.parse(raw);
  const records = state?.records;
  if (!records || typeof records !== 'object') {
    throw new Error('Invalid .n8n_sync/state.json: records missing or not an object.');
  }

  return Object.values(records).filter((rec) => rec && typeof rec === 'object');
}

function normalizeStateInstanceToCli(stateInstance) {
  if (stateInstance === 'secondary') return 'secondary';
  if (stateInstance === 'primary') return 'primary';
  return stateInstance;
}

function candidateStateInstancesForRequest(requestedInstanceAlias) {
  if (requestedInstanceAlias === 'secondary') return ['secondary'];
  if (requestedInstanceAlias === 'primary') return ['primary'];
  return [requestedInstanceAlias];
}

function resolveInstanceAndWorkflow(outputDir, requestedInstanceAlias, strictInstance = false) {
  const all = readStateRecords(outputDir);
  const requestedCandidates = candidateStateInstancesForRequest(requestedInstanceAlias);
  const requestedMatches = all.filter((rec) => requestedCandidates.includes(rec.instance));
  let chosen = requestedMatches;

  if (chosen.length === 0) {
    if (strictInstance) {
      throw new Error(
        `No tracked workflow found for explicitly requested instance '${requestedInstanceAlias}'`
      );
    }
    const available = [...new Set(all.map((rec) => rec.instance).filter(Boolean))];
    if (available.length === 0) {
      throw new Error('No tracked workflows found in .n8n_sync/state.json');
    }
    chosen = all.filter((rec) => rec.instance === available[0]);
    console.warn(
      `WARN: No tracked workflow for instance '${requestedInstanceAlias}'. Falling back to '${available[0]}'.`
    );
  }

  const entries = chosen
    .filter((rec) => typeof rec.workflowId === 'string' && rec.workflowId.trim());

  if (entries.length === 0) {
    throw new Error(
      `No tracked workflow with workflowId found for selected instance in .n8n_sync/state.json`
    );
  }

  const stateInstance = String(entries[0].instance);
  const cliInstance = normalizeStateInstanceToCli(stateInstance);
  const workflowId = String(entries[0].workflowId);
  return { stateInstance, cliInstance, workflowId };
}

function startDiffServer({ instance, workflowId, host, port, dotenv }) {
  const args = [
    'scripts/workflow_diff_server.py',
    '--instance',
    instance,
    '--workflow-id',
    workflowId,
    '--host',
    host,
    '--port',
    String(port),
    '--dotenv',
    dotenv,
    '--output-dir',
    OUTPUT_DIR,
  ];

  const child = spawn('python3', args, {
    cwd: process.cwd(),
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  let stdoutBuf = '';
  let stderrBuf = '';

  child.stdout.on('data', (chunk) => {
    stdoutBuf += chunk.toString();
  });
  child.stderr.on('data', (chunk) => {
    stderrBuf += chunk.toString();
  });

  return { child, getLogs: () => ({ stdout: stdoutBuf, stderr: stderrBuf }) };
}

async function waitForServerReady({ host, port, child, getLogs, maxWaitMs = 20000 }) {
  const url = `http://${host}:${port}/api/context`;
  const deadline = Date.now() + maxWaitMs;
  let lastError = null;

  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      const logs = getLogs();
      const errText = logs.stderr.trim() || logs.stdout.trim() || 'server exited unexpectedly';
      throw new Error(`Server exited before ready: ${errText}`);
    }
    try {
      const res = await fetch(url);
      const json = await res.json();
      if (!res.ok) {
        throw new Error(json?.error || `HTTP ${res.status}`);
      }
      return json;
    } catch (err) {
      lastError = err;
      await sleep(500);
    }
  }

  throw new Error(`Server not ready in ${maxWaitMs}ms. Last error: ${lastError?.message || 'unknown'}`);
}

async function runBrowserChecks({ host, port, context }) {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    await page.goto(`http://${host}:${port}/`, { waitUntil: 'networkidle', timeout: 30000 });

    await page.waitForSelector('#meta', { timeout: 15000 });
    const meta = await page.locator('#meta').innerText();
    if (!meta.includes(context.workflowId)) {
      throw new Error(`Meta does not include workflow id. Meta='${meta}'`);
    }
    if (!meta.includes(context.localUpdatedAt || '?')) {
      throw new Error(`Meta does not include local updatedAt. Meta='${meta}'`);
    }

    const remoteHref = await page.locator('#meta a').getAttribute('href');
    if (remoteHref !== context.remoteWorkflowUrl) {
      throw new Error(`Unexpected remote link href: '${remoteHref}'`);
    }

    await page.waitForFunction(() => {
      const el = document.querySelector('#status');
      if (!el) return false;
      const text = (el.textContent || '').trim();
      return text && text !== 'Loading review context...';
    }, null, { timeout: 15000 });

    const status = await page.locator('#status').innerText();
    const allowed = [
      'Diff loaded. Review and approve when ready.',
      'No semantic diff between remote and local workflow.',
      'Graph diff: n8n preview iframe failed to load (blocked or timeout). Use Side-by-side or JSON diff tabs.',
    ];

    if (!allowed.includes(status)) {
      throw new Error(`Unexpected status text: '${status}'`);
    }

    await page.click('#tab-json');
    await page.waitForSelector('#jsonDiffBody tr', { timeout: 15000 });
    const jsonSummary = await page.locator('#jsonDiffSummary').innerText();
    if (!jsonSummary) {
      throw new Error('JSON diff summary is empty');
    }

    console.log('PASS: real server browser checks succeeded');
    console.log(`Meta: ${meta}`);
    console.log(`Status: ${status}`);
  } finally {
    await browser.close();
  }
}

async function stopProcess(child) {
  if (child.killed || child.exitCode !== null) return;
  child.kill('SIGTERM');
  const deadline = Date.now() + 3000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) return;
    await sleep(100);
  }
  child.kill('SIGKILL');
}

async function main() {
  const resolved = resolveInstanceAndWorkflow(OUTPUT_DIR, REQUESTED_INSTANCE, INSTANCE_EXPLICIT);
  const workflowId = process.env.DIFF_REAL_WORKFLOW_ID || resolved.workflowId;
  const cliInstance = resolved.cliInstance;
  console.log(
    `Using outputDir='${OUTPUT_DIR}', instance='${cliInstance}' (state='${resolved.stateInstance}'), workflowId='${workflowId}', host='${HOST}', port=${PORT}`
  );

  const { child, getLogs } = startDiffServer({
    instance: cliInstance,
    workflowId,
    host: HOST,
    port: PORT,
    dotenv: DOTENV,
  });

  try {
    const context = await waitForServerReady({
      host: HOST,
      port: PORT,
      child,
      getLogs,
      maxWaitMs: 30000,
    });
    console.log('PASS: /api/context returned real workflow context');
    console.log(`Workflow: ${context.workflowId} | Instance: ${context.instance} | isDifferent: ${context.isDifferent}`);

    if (!SKIP_BROWSER) {
      await runBrowserChecks({ host: HOST, port: PORT, context });
    } else {
      console.log('SKIP: browser checks skipped (DIFF_REAL_SKIP_BROWSER=1)');
    }
  } finally {
    await stopProcess(child);
    const logs = getLogs();
    if (logs.stderr.trim()) {
      console.error('Server stderr:');
      console.error(logs.stderr.trim());
    }
  }
}

main().catch((err) => {
  if (String(err?.message || '').includes('libnspr4.so')) {
    console.error('Hint: install Linux browser deps: sudo npx playwright install-deps chromium');
  }
  console.error(`FAIL: ${err.message}`);
  process.exitCode = 1;
});
