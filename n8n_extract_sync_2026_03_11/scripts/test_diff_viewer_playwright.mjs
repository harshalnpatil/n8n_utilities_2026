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
  before: { name: 'Before', nodes: [{ id: '1', name: 'Start', type: 'n8n-nodes-base.manualTrigger', position: [200, 300] }], connections: {} },
  after: { name: 'After', nodes: [{ id: '1', name: 'Start', type: 'n8n-nodes-base.manualTrigger', position: [200, 300] }, { id: '2', name: 'Set', type: 'n8n-nodes-base.set', position: [420, 300] }], connections: {} },
};

let approvePostedHash = null;

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
        } catch {
          approvePostedHash = null;
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

    await page.goto(`http://${HOST}:${PORT}/`, { waitUntil: 'networkidle', timeout: 60000 });

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
    await Promise.race([
      page.waitForSelector('.monaco-diff-editor', { timeout: 90000 }),
      page.waitForSelector('#jsonFallback.visible', { timeout: 90000 }),
    ]);

    await tabGraph.click();
    const meta = await page.locator('#meta').innerText();
    if (!meta.includes('Workflow=wf_test_123')) {
      throw new Error(`Unexpected meta text: ${meta}`);
    }
    if (!meta.includes('Local updatedAt=2026-03-18T09:40:00.000000Z')) {
      throw new Error(`Missing local updatedAt in meta text: ${meta}`);
    }

    const remoteHref = await page.locator('#meta a').getAttribute('href');
    if (remoteHref !== contextPayload.remoteWorkflowUrl) {
      throw new Error(`Unexpected remote link href: ${remoteHref}`);
    }

    const statusBefore = await page.locator('#status').innerText();
    if (!statusBefore.includes('Diff loaded')) {
      throw new Error(`Unexpected pre-approve status: ${statusBefore}`);
    }

    await page.click('#approveBtn');
    await page.waitForFunction(() => document.querySelector('#status')?.textContent?.includes('Diff loaded'), null, { timeout: 10000 });

    if (approvePostedHash !== contextPayload.beforeHash) {
      throw new Error(`expectedRemoteHash mismatch: ${approvePostedHash}`);
    }

    console.log('PASS: diff viewer smoke test succeeded');
    console.log(`Meta: ${meta}`);
    console.log(`Approve payload hash: ${approvePostedHash?.slice(0, 12)}...`);
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
