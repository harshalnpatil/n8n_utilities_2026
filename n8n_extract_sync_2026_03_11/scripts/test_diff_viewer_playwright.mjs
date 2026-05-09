import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';

const HOST = '127.0.0.1';
const PORT = Number(process.env.DIFF_VIEWER_TEST_PORT || 8876);

const contextPayload = {
  instance: 'primary',
  workflowId: 'wf_test_123',
  workflowName: 'Diff Viewer Smoke Test',
  localPath: 'workflows/primary/test/workflow.json',
  remoteUpdatedAt: '2026-03-18T09:30:00.000Z',
  localUpdatedAt: '2026-03-18T09:40:00.000000Z',
  remoteWorkflowUrl: 'https://n8n.example.com/workflow/wf_test_123',
  beforeHash: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  afterHash: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
  isDifferent: true,
  before: {
    name: 'Before',
    nodes: [{
      id: '1',
      name: 'Prepare Import Context',
      type: 'n8n-nodes-base.code',
      position: [176, 700],
      parameters: {
        jsCode: 'const total = items.reduce((sum, item) => sum + item.json.amount, 0);\\nreturn [{ json: { total } }];',
      },
    }],
    connections: {},
  },
  after: {
    name: 'Before',
    nodes: [{
      id: '1',
      name: 'Prepare Import Context',
      type: 'n8n-nodes-base.code',
      position: [176, 268],
      parameters: {
        jsCode:
          'const total = items.reduce((sum, item) => sum + item.json.amount, 0);\\n' +
          'const average = total / Math.max(items.length, 1);\\n' +
          'return [{ json: { total, average } }];',
      },
    }],
    connections: {},
  },
};

let approvePostedHash = null;
let approveForceFlags = [];
let approveCallCount = 0;

function json(res, code, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
  });
  res.end(body);
}

async function startServer() {
  const htmlPath = path.resolve('web/diff_review.html');
  const html = await readFile(htmlPath);

  const server = createServer((req, res) => {
    if (!req.url) {
      res.writeHead(400);
      res.end('bad request');
      return;
    }

    if (req.method === 'GET' && (req.url === '/' || req.url === '/index.html')) {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(html);
      return;
    }

    if (req.method === 'GET' && req.url === '/api/context') {
      json(res, 200, contextPayload);
      return;
    }

    if (req.method === 'POST' && req.url === '/api/approve') {
      let body = '';
      req.on('data', (chunk) => (body += chunk));
      req.on('end', () => {
        try {
          const payload = JSON.parse(body || '{}');
          approvePostedHash = payload.expectedRemoteHash || null;
          approveForceFlags.push(Boolean(payload.force));
        } catch {
          approvePostedHash = null;
          approveForceFlags.push(false);
        }
        approveCallCount += 1;
        if (!approveForceFlags[approveForceFlags.length - 1]) {
          json(res, 500, {
            ok: false,
            error:
              "Push command failed.\n" +
              "stdout:\nOK primary ok > primary | 1 workflows | push\n" +
              "CONFLICT * Workout energizer id=hBhXHT9qG2bE19bw 2026-05-04 03:56 <- workflows/primary/workout_energizer_hbhxht9qg2be19bw/workflow.json\n" +
              "stderr:\nworkspace root: C:\\Users\\harsh\\Documents\\n8n_workflows_2026_01_25\n" +
              "error: Refusing to push workflow 'Workout energizer' (id=hBhXHT9qG2bE19bw) because the remote changed since the last local sync. Run status/diff, then backup or resolve the conflict before pushing (or rerun with --force).",
          });
          return;
        }
        json(res, 200, { ok: true, stdout: 'push ok', stderr: '' });
      });
      return;
    }

    res.writeHead(404);
    res.end('not found');
  });

  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(PORT, HOST, resolve);
  });
  return server;
}

async function run() {
  let server = null;
  let browser = null;

  try {
    server = await startServer();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();

    page.on('dialog', async (dialog) => {
      await dialog.accept();
    });

    await page.goto(`http://${HOST}:${PORT}/`, { waitUntil: 'domcontentloaded', timeout: 60000 });

    await page.waitForSelector('#meta', { timeout: 10000 });

    const tabGraph = page.locator('#tab-graph');
    const tabDual = page.locator('#tab-dual');
    const tabJson = page.locator('#tab-json');
    await tabGraph.waitFor({ state: 'visible', timeout: 5000 });
    await tabDual.waitFor({ state: 'visible', timeout: 5000 });
    await tabJson.waitFor({ state: 'visible', timeout: 5000 });
    const graphLabel = await tabGraph.textContent();
    if (!String(graphLabel || '').includes('Graph diff')) {
      throw new Error(`Unexpected Graph tab label: ${graphLabel}`);
    }

    await tabDual.click();
    await page.waitForSelector('#demoRemote', { timeout: 5000 });
    await page.waitForSelector('#demoLocal', { timeout: 5000 });

    await tabJson.click();
    await page.waitForSelector('#jsonDiffBody tr', { timeout: 15000 });
    await page.waitForSelector('#prevDiffBtn', { timeout: 5000 });
    await page.waitForSelector('#nextDiffBtn', { timeout: 5000 });
    await page.waitForSelector('#ignorePositionToggle', { timeout: 5000 });
    const jsonSummary = await page.locator('#jsonDiffSummary').innerText();
    if (!jsonSummary.includes('diff section')) {
      throw new Error(`Unexpected JSON diff summary: ${jsonSummary}`);
    }

    await page.click('#ignorePositionToggle');
    await page.waitForFunction(
      () => {
        const text = document.querySelector('#jsonDiffSummary')?.textContent || '';
        return text.includes('changed lines');
      },
      null,
      { timeout: 10000 }
    );
    const ignoredSummary = await page.locator('#jsonDiffSummary').innerText();
    if (!ignoredSummary.includes('changed lines')) {
      throw new Error(`Unexpected ignored JSON diff summary: ${ignoredSummary}`);
    }

    await page.click('#semanticDiffToggle');
    await page.waitForFunction(
      () => {
        const text = document.querySelector('#jsonDiffSummary')?.textContent || '';
        return text.includes('changed JSON paths');
      },
      null,
      { timeout: 10000 }
    );
    const semanticSummary = await page.locator('#jsonDiffSummary').innerText();
    if (!semanticSummary.includes('changed JSON paths')) {
      throw new Error(`Unexpected semantic JSON diff summary: ${semanticSummary}`);
    }
    const semanticBody = await page.locator('#jsonDiffBody').innerText();
    if (!semanticBody.includes('nodes[0].parameters.jsCode')) {
      throw new Error(`Semantic diff did not render jsCode path: ${semanticBody}`);
    }
    if (!semanticBody.includes('const average = total / Math.max(items.length, 1);')) {
      throw new Error(`Semantic diff did not render formatted JS change: ${semanticBody}`);
    }
    const semanticInsertCount = await page.locator('#jsonDiffBody .diff-inline-ins').count();
    if (semanticInsertCount < 1) {
      throw new Error('Semantic diff did not highlight inserted inline tokens');
    }

    await page.click('#tab-node-inspector');
    await page.waitForSelector('#nodeListContainer .node-item', { timeout: 10000 });
    await page.click('#nodeListContainer .node-item');
    await page.waitForFunction(
      () => {
        const text = document.querySelector('#nodeDiffSummary')?.textContent || '';
        return text.includes('changed JSON paths');
      },
      null,
      { timeout: 10000 }
    );
    const nodeSummary = await page.locator('#nodeDiffSummary').innerText();
    if (!nodeSummary.includes('changed JSON paths')) {
      throw new Error(`Unexpected node inspector semantic summary: ${nodeSummary}`);
    }
    const nodeInsertCount = await page.locator('#nodeDiffContent .diff-inline-ins').count();
    if (nodeInsertCount < 1) {
      throw new Error('Node inspector semantic diff did not highlight inserted inline tokens');
    }

    await tabGraph.click();
    const meta = await page.locator('#meta').innerText();
    if (!meta.includes('wf_test_123')) {
      throw new Error(`Unexpected meta text: ${meta}`);
    }
    if (!meta.includes('2026-03-18T09:40:00.000000Z')) {
      throw new Error(`Missing local updatedAt in meta text: ${meta}`);
    }

    const remoteHref = await page.locator('#meta a').getAttribute('href');
    if (remoteHref !== contextPayload.remoteWorkflowUrl) {
      throw new Error(`Unexpected remote link href: ${remoteHref}`);
    }

    await page.waitForFunction(() => {
      const text = document.querySelector('#statusBadge')?.textContent?.trim();
      return text && text !== 'Loading review context...';
    }, null, { timeout: 20000 });
    const statusBefore = await page.locator('#statusBadge').innerText();
    if (!statusBefore.includes('Diff loaded')) {
      throw new Error(`Unexpected pre-approve status: ${statusBefore}`);
    }

    await page.click('#approveBtn');
    await page.waitForSelector('#pushErrorModal.visible', { timeout: 10000 });
    const modalTitle = await page.locator('#pushErrorTitle').innerText();
    if (!modalTitle.includes('Conflict detected')) {
      throw new Error(`Unexpected push modal title: ${modalTitle}`);
    }
    const modalBody = await page.locator('#pushErrorBody').innerText();
    if (!modalBody.includes('Refusing to push workflow')) {
      throw new Error(`Missing conflict text in modal body: ${modalBody}`);
    }
    await page.waitForSelector('#pushErrorForceBtn:not([hidden])', { timeout: 5000 });
    await page.click('#pushErrorForceBtn');
    await page.waitForFunction(() => document.querySelector('#statusBadge')?.textContent?.includes('Diff loaded'), null, { timeout: 10000 });

    if (approvePostedHash !== contextPayload.beforeHash) {
      throw new Error(`expectedRemoteHash mismatch: ${approvePostedHash}`);
    }
    if (approveCallCount !== 2) {
      throw new Error(`Expected two approve calls, saw ${approveCallCount}`);
    }
    if (approveForceFlags[0] !== false || approveForceFlags[1] !== true) {
      throw new Error(`Unexpected force flags: ${approveForceFlags.join(',')}`);
    }

    console.log('PASS: diff viewer smoke test succeeded');
    console.log(`Meta: ${meta}`);
    console.log(`Approve payload hash: ${approvePostedHash?.slice(0, 12)}...`);
    console.log(`Force flags: ${approveForceFlags.join(',')}`);
  } finally {
    if (browser) {
      await browser.close();
    }
    if (server) {
      await new Promise((resolve) => server.close(resolve));
    }
  }
}

run().catch((err) => {
  if (String(err?.message || '').includes('libnspr4.so')) {
    console.error('Hint: missing Linux browser deps. In WSL run: sudo npx playwright install-deps chromium');
  }
  console.error(`FAIL: ${err.message}`);
  process.exitCode = 1;
});
