import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { transformWithEsbuild } from 'vite';

const source = await readFile(new URL('./App.jsx', import.meta.url), 'utf8');
const transformed = await transformWithEsbuild(source, 'App.jsx', { jsx: 'automatic', loader: 'jsx' });
const code = transformed.code.replace(/from "([^"]+)"|from '([^']+)'/g, (_match, double, single) => {
  const specifier = double || single;
  return `from ${JSON.stringify(specifier.startsWith('.') ? new URL(specifier, import.meta.url).href : import.meta.resolve(specifier))}`;
});
const { default: App } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);

test('Codex CLI loads its catalog on selection, submits model IDs and reasoning, and ignores stale models and errors', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/#/new' });
  const originalFetch = globalThis.fetch;
  const originals = new Map();
  for (const [key, value] of Object.entries({ window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true })) {
    originals.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { value, configurable: true, writable: true });
  }
  const calls = [];
  const pending = [];
  let ready = true;
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options });
    if (path === '/api/config' || path === '/api/config?provider=codex') return Response.json({ model: 'codex-default', provider: 'Codex CLI', provider_kind: 'codex', provider_ready: ready, key_configured: false, max_chars: 1000 });
    if (path === '/api/config?provider=api') return Response.json({ model: 'api-model', provider: 'Requesty', provider_kind: 'api', provider_ready: true, key_configured: true });
    if (path === '/api/models' || path === '/api/models?provider=codex') return new Promise((resolve, reject) => pending.push({ resolve, reject, signal: options.signal }));
    if (path === '/api/audits' && options.method !== 'POST') return Response.json([]);
    return Response.json({ id: 'codex-job', status: 'completed', findings: [], provider_kind: 'codex', model: 'codex-catalog-id' });
  };
  const root = createRoot(document.getElementById('root'));
  const flush = async (fn = () => {}) => act(async () => { fn(); await new Promise((resolve) => setTimeout(resolve, 30)); });
  const button = (text) => [...document.querySelectorAll('button')].find((element) => element.textContent.includes(text));
  const edit = async (id, value) => flush(() => {
    const input = document.getElementById(id);
    const propsKey = Object.keys(input).find((key) => key.startsWith('__reactProps$'));
    input[propsKey].onChange({ target: { value } });
  });
  const catalog = () => Response.json({ models: [{ id: 'codex-catalog-id', name: 'Friendly display name' }, { id: 'other-codex-id' }] });
  try {
    await flush(() => root.render(React.createElement(App)));
    assert.equal(calls.some((call) => call.path.startsWith('/api/models')), false, 'automatic backend configuration does not fetch a catalog');
    assert.match(document.body.textContent, /CODEX CLI.*Detected · login required/);
    assert.equal(document.getElementById('paid-consent').disabled, false, 'CLI readiness does not require an API key');
    assert.equal(document.getElementById('audit-model'), null, 'Codex does not suggest manual API model IDs');
    assert.doesNotMatch(document.body.textContent, /Manual IDs|enter a model.*manually/);
    for (const copy of [/codex login on the backend/, /no inference/, /fresh ephemeral threads/, /built-in tools disabled/, /read-only sandbox, not full OS isolation/, /inherited MCP/, /CLI internal retries may consume usage/, /backend validates support against the model catalog/]) assert.match(document.body.textContent, copy);
    assert.equal(document.getElementById('reasoning-effort').disabled, false);
    assert.deepEqual([...document.getElementById('reasoning-effort').options].map((option) => option.value), ['', 'low', 'medium', 'high']);
    await flush(() => document.getElementById('paid-consent').click());
    await edit('audit-provider', 'codex');
    assert.equal(document.getElementById('paid-consent').checked, false);
    assert.equal(pending.length, 1, 'explicit Codex selection automatically loads the catalog');
    assert.equal(document.getElementById('codex-model').disabled, true);
    await edit('audit-provider', 'api');
    assert.equal(pending[0].signal.aborted, true);
    await flush(() => pending[0].resolve(catalog()));
    assert.equal(document.querySelector('#available-models option'), null, 'stale success cannot populate another provider');
    await edit('audit-provider', 'codex');
    const staleError = pending[1];
    await edit('audit-provider', 'api');
    assert.equal(staleError.signal.aborted, true);
    await flush(() => staleError.reject(new Error('Authorization: Bearer sk-stale-secret')));
    assert.doesNotMatch(document.body.textContent, /sk-stale-secret|Could not load Codex/);
    await edit('audit-provider', 'codex');
    await flush(() => pending[2].resolve(Response.json({ provider_error_code: 'codex', provider_status: 403, detail: 'Authorization: Bearer sk-secret https://private.example/token' }, { status: 502 })));
    assert.match(document.body.textContent, /Could not load Codex models.*codex login on the backend/);
    assert.doesNotMatch(document.body.textContent, /Bearer|sk-secret|private\.example|manually/);
    await flush(() => button('Load models').click());
    await flush(() => pending[3].resolve(catalog()));
    assert.equal(document.querySelector('#codex-model option[value="codex-catalog-id"]').textContent, 'codex-catalog-id');
    await edit('codex-model', 'codex-catalog-id');
    await edit('reasoning-effort', 'high');
    await flush(() => document.getElementById('paid-consent').click());
    await edit('audit-provider', 'api');
    assert.equal(document.getElementById('paid-consent').checked, false);
    assert.equal(document.getElementById('audit-model').value, '');
    assert.equal(document.getElementById('reasoning-effort').value, '');
    ready = false;
    await edit('audit-provider', 'codex');
    assert.equal(document.getElementById('paid-consent').disabled, true);
    assert.match(document.body.textContent, /Kiro or Codex CLI/);
    assert.equal(pending.length, 4, 'unavailable CLI does not load models');
    ready = true;
    await flush(() => document.querySelector('[aria-label="Refresh history and configuration"]').click());
    await flush(() => pending[4].resolve(catalog()));
    await edit('codex-model', 'codex-catalog-id');
    await edit('reasoning-effort', 'high');
    assert.match(document.querySelector('.confirmation').textContent, /Codex usage \(subscription or API billing\)/);
    await flush(() => document.getElementById('paid-consent').click());
    assert.equal(button('Start live audit').disabled, false);
    await flush(() => button('Start live audit').click());
    const body = JSON.parse(calls.find((call) => call.path === '/api/audits' && call.options.method === 'POST').options.body);
    assert.equal(body.provider, 'codex');
    assert.equal(body.model, 'codex-catalog-id');
    assert.equal(body.reasoning_effort, 'high');
    assert.equal(body.confirmed_paid, true);
  } finally {
    await act(async () => root.unmount());
    dom.window.close(); globalThis.fetch = originalFetch;
    for (const [key, descriptor] of originals) { if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key]; }
  }
});

test('Kiro CLI enables keyless audits, uses its catalog, and disables unsupported reasoning', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/#/new' });
  const originalFetch = globalThis.fetch;
  const originals = new Map();
  for (const [key, value] of Object.entries({ window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true })) {
    originals.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { value, configurable: true, writable: true });
  }
  const calls = [];
  let pendingKiro;
  let deferKiro = false;
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options });
    if (path === '/api/models?provider=kiro' && deferKiro) return new Promise((resolve) => { pendingKiro = resolve; });
    if (path === '/api/config' || path === '/api/config?provider=kiro') return Response.json({ model: 'kiro-default', provider: 'Kiro CLI (ACP)', provider_kind: 'kiro', provider_ready: true, key_configured: false, max_chars: 1000 });
    if (path === '/api/config?provider=api') return Response.json({ model: 'api-model', provider: 'api-provider', provider_kind: 'api', provider_ready: true, key_configured: true, max_chars: 1000 });
    if (path === '/api/models' || path === '/api/models?provider=kiro') return Response.json({ models: [{ id: 'kiro-model' }] });
    if (path === '/api/audits' && options.method !== 'POST') return Response.json([]);
    return Response.json({ id: 'kiro-job', status: 'completed', findings: [], provider_kind: 'kiro', model: 'kiro-model' });
  };
  const root = createRoot(document.getElementById('root'));
  const flush = async (fn = () => {}) => act(async () => { fn(); await new Promise((resolve) => setTimeout(resolve, 30)); });
  const button = (text) => [...document.querySelectorAll('button')].find((element) => element.textContent.includes(text));
  try {
    await flush(() => root.render(React.createElement(App)));
    assert.match(document.body.textContent, /Detected · login required/);
    assert.match(document.body.textContent, /Kiro subscription credits/);
    assert.equal(document.getElementById('reasoning-effort').disabled, true);
    assert.equal(document.getElementById('paid-consent').disabled, false);
    assert.equal(calls.some((call) => call.path === '/api/models'), false);
    await flush(() => button('Load models').click());
    assert.equal(document.querySelector('#available-models option').value, 'kiro-model');
    const edit = async (id, value) => flush(() => {
      const input = document.getElementById(id);
      const propsKey = Object.keys(input).find((key) => key.startsWith('__reactProps$'));
      input[propsKey].onChange({ target: { value } });
    });
    await flush(() => document.getElementById('paid-consent').click());
    await edit('audit-provider', 'api');
    assert.equal(document.getElementById('paid-consent').checked, false);
    assert.equal(document.getElementById('audit-model').value, '');
    deferKiro = true;
    await edit('audit-provider', 'kiro');
    assert.equal(document.getElementById('kiro-model').disabled, true);
    await edit('audit-provider', 'api');
    assert.equal(calls.findLast((call) => call.path === '/api/models?provider=kiro').options.signal.aborted, true);
    await flush(() => pendingKiro(Response.json({ models: [{ id: 'stale-kiro-model' }] })));
    assert.equal(document.querySelector('#available-models option'), null, 'stale Kiro catalog is ignored');
    deferKiro = false;
    await edit('audit-provider', 'kiro');
    assert.ok(calls.some((call) => call.path === '/api/models?provider=kiro'), 'Kiro selection automatically fetches models');
    assert.equal(document.querySelector('#kiro-model option[value="kiro-model"]').textContent, 'kiro-model');
    await edit('kiro-model', 'kiro-model');
    await flush(() => document.getElementById('paid-consent').click());
    assert.equal(button('Start live audit').disabled, false);
    await flush(() => button('Start live audit').click());
    const body = JSON.parse(calls.find((call) => call.path === '/api/audits' && call.options.method === 'POST').options.body);
    assert.equal(body.model, 'kiro-model');
    assert.equal(body.provider, 'kiro');
    assert.equal(body.reasoning_effort, null);
  } finally {
    await act(async () => root.unmount());
    dom.window.close(); globalThis.fetch = originalFetch;
    for (const [key, descriptor] of originals) { if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key]; }
  }
});

test('specialists forward all sources and settings, lock submission, and separate lane candidates from judged reports', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/#/new' });
  const originalFetch = globalThis.fetch;
  const originals = new Map();
  for (const [key, value] of Object.entries({ window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true })) {
    originals.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { value, configurable: true, writable: true });
  }
  const calls = [];
  const pending = [];
  const raw = { title: 'Raw duplicate', severity: 'high', location: 'Vault.sol:1', description: '<script>unsafe()</script>', impact: 'Unreviewed claim', exploit_steps: ['Raw evidence'], evidence: '<img src=x onerror=unsafe()>' };
  const judged = { ...raw, title: 'Judged claim', description: 'AI reviewed claim' };
  const snapshot = { id: 'pinned-preview', repository_url: 'https://github.com/owner/repo', repository: 'owner/repo', commit: 'a'.repeat(40), ref: 'main', subdirectory: '', file_count: 1, total_chars: 17, files: [{ path: 'Vault.sol', content: 'contract Vault {}' }] };
  let job = {
    id: 'specialist-job', mode: 'specialists', status: 'running', stage: 'specialist_lanes', rounds: 2, total_rounds: 12, completed_rounds: 5, judge_status: 'queued', findings: [],
    lanes: ['queued', 'running', 'completed', 'failed', 'stopped', 'completed'].map((status, index) => ({ key: `lane-${index}`, name: `Lens ${index}`, status, completed_rounds: index === 0 ? 0 : 1, rounds: 2, findings_count: index === 0 ? 0 : 1, error: status === 'failed' ? 'Safe lane failure' : null })),
    findings_by_lane: { 'lane-1': [raw], 'lane-2': [raw] },
  };
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options });
    if (path === '/api/config') return Response.json({ model: 'default-model', provider: 'test-provider', key_configured: true, max_chars: 1000 });

    if (path === '/api/github/preview') return Response.json(snapshot);
    if (path === '/api/audits' && options.method === 'POST') return new Promise((resolve) => pending.push(resolve));
    if (path === '/api/audits') return Response.json([job]);
    if (path === '/api/audits/specialist-job') return Response.json(job);
    throw new Error(`Unexpected request: ${path}`);
  };
  const root = createRoot(document.getElementById('root'));
  const flush = async (fn = () => {}) => act(async () => { fn(); await new Promise((resolve) => setTimeout(resolve, 30)); });
  const button = (text) => [...document.querySelectorAll('button')].find((element) => element.textContent.includes(text));
  const consent = () => document.getElementById('paid-consent');
  const edit = async (id, value) => flush(() => {
    const input = document.getElementById(id);
    const propsKey = Object.keys(input).find((key) => key.startsWith('__reactProps$'));
    input[propsKey].onChange({ target: { value } });
  });
  const navigate = async (hash) => flush(() => { window.location.hash = hash; window.dispatchEvent(new dom.window.HashChangeEvent('hashchange')); });
  const posts = () => calls.filter((call) => call.path === '/api/audits' && call.options.method === 'POST');
  try {
    await flush(() => root.render(React.createElement(App)));
    assert.equal(document.querySelectorAll('.mode-card').length, 4);
    assert.match(document.querySelector('.history-item').textContent, /Specialist audit/);
    assert.ok(document.querySelector('.history-icon circle'), 'specialist history uses its own icon');
    await flush(() => document.querySelector('input[value="loop"]').click());
    assert.equal(document.getElementById('rounds').value, '3');
    await edit('rounds', '10');
    await flush(() => consent().click());
    await flush(() => document.querySelector('input[value="specialists"]').click());
    assert.equal(consent().checked, false);
    assert.equal(document.getElementById('rounds').value, '1', 'specialists never inherit loop rounds');
    assert.match(document.body.textContent, /Up to 8 paid model calls/);
    assert.match(document.body.textContent, /Audit rounds per lane/);
    assert.match(document.body.textContent, /6 parallel lanes × 1 rounds per lane.*up to 1 AI judge call.*skipped if there are no candidates/);
    assert.match(document.body.textContent, /rate limits.*failures.*cost.*not human verification/);
    for (const target of ['bundled', 'upload', 'github']) {
      if (target !== 'bundled') {
        await flush(() => consent().click());
        await flush(() => document.querySelector(`input[value="${target}"]`).click());
        assert.equal(consent().checked, false, 'source changes reset consent');
        await flush(() => document.querySelector('input[value="specialists"]').click());
        assert.equal(document.getElementById('rounds').value, '1');
      }
      if (target === 'upload') {
        const input = document.querySelector('input[aria-label="Upload folder of Solidity files"]');
        Object.defineProperty(input, 'files', { configurable: true, value: [{ name: 'Vault.sol', webkitRelativePath: 'contracts/Vault.sol', size: 17, text: async () => 'contract Vault {}' }, { name: 'ignore.txt', size: 1, text: async () => 'x' }] });
        await flush(() => input.dispatchEvent(new dom.window.Event('change', { bubbles: true })));
      }
      if (target === 'github') {
        await edit('github-url', snapshot.repository_url);
        await flush(() => button('Preview repository').click());
      }
      for (const [id, value] of [['rounds', '2'], ['audit-model', ' provider/specialist '], ['json-mode', 'false'], ['reasoning-effort', 'high']]) {
        await flush(() => consent().click());
        await edit(id, value);
        assert.equal(consent().checked, false, `${id} resets consent`);
      }
      assert.match(document.body.textContent, /Up to 14 paid model calls/);
      await flush(() => consent().click());
      await flush(() => button('Start live audit').click());
      assert.deepEqual(JSON.parse(posts().at(-1).options.body), { mode: 'specialists', rounds: 2, model: 'provider/specialist', json_mode: false, reasoning_effort: 'high', confirmed_paid: true, target, files: target === 'upload' ? [{ path: 'contracts/Vault.sol', content: 'contract Vault {}' }] : [], ...(target === 'github' ? { github_preview_id: 'pinned-preview', github_paths: snapshot.files.map((file) => file.path) } : {}) });
      for (const input of document.querySelectorAll('form input, form select')) assert.ok(input.matches(':disabled'), `${input.id || input.name} is locked`);
      assert.ok(button('New audit').disabled);
      await flush(() => document.querySelector('form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true })));
      assert.equal(posts().length, ['bundled', 'upload', 'github'].indexOf(target) + 1, 'duplicate submits are locked');
      await flush(() => pending.shift()(Response.json(job)));
      await navigate('#/audit/specialist-job');
      assert.match(document.body.textContent, /5 \/ 12 lane rounds/);
      assert.equal(document.querySelector('progress[aria-label="Completed audit rounds"]').max, 12);
      assert.equal(document.querySelectorAll('.lane-progress').length, 6);
      assert.match(document.querySelector('.lane-progress').textContent, /Lens 0queued0 \/ 2 rounds · 0 raw candidates/);
      for (const status of ['queued', 'running', 'completed', 'failed', 'stopped']) assert.ok(document.querySelector(`.lane-progress .status.${status}`));
      assert.match(document.querySelector('.judge-stage').textContent, /AI judge stagequeued/);
      assert.match(document.querySelector('.specialist-stages').textContent, /Safe lane failure/);
      assert.match(document.getElementById('findings-heading').textContent, /AI-judged findings · Not verified/);
      assert.equal(document.querySelectorAll('.findings-section .finding-card').length, 0);
      assert.match(document.querySelector('.lane-candidates').textContent, /Unjudged.*duplicates/s);
      assert.equal(document.querySelector('.candidate-lane').open, false);
      assert.match(document.querySelector('.candidate-evidence pre').textContent, /<script>unsafe\(\)<\/script>/);
      assert.equal(document.querySelectorAll('.lane-candidates script, .lane-candidates img').length, 0);
      await flush(() => button('New audit').click());
    }
    job = { ...job, stage: 'judging', judge_status: 'running', completed_rounds: 12 };
    await navigate('#/audit/specialist-job');
    assert.match(document.querySelector('.judge-stage').textContent, /AI judge stagerunning/);
    assert.match(document.querySelector('.progress-label').textContent, /judging12 \/ 12 lane rounds/);
    await navigate('#/new');
    job = { ...job, status: 'failed', judge_status: 'failed', error: 'Safe judge failure' };
    await navigate('#/audit/specialist-job');
    assert.match(document.querySelector('.report-overview').textContent, /Audit failed.*Raw lane candidates.*unjudged.*duplicates/s);
    assert.match(document.querySelector('.specialist-stages').textContent, /Judge failed: raw candidates remain unjudged/);
    assert.equal(document.querySelectorAll('.findings-section .finding-card').length, 0);
    job = { ...job, status: 'completed', error: null, judge_status: 'completed', findings: [judged] };
    await navigate('#/new');
    await navigate('#/audit/specialist-job');
    assert.equal(document.querySelectorAll('.findings-section .finding-card').length, 1);
    assert.match(document.querySelector('.findings-section').textContent, /Judged claim/);
    assert.doesNotMatch(document.querySelector('.findings-section').textContent, /Raw duplicate/);
    job = { ...job, judge_status: 'skipped', findings: [], findings_by_lane: {} };
    await navigate('#/new');
    await navigate('#/audit/specialist-job');
    assert.match(document.querySelector('.specialist-stages').textContent, /Judge skipped: no candidates/);
    assert.equal(posts().length, 3);
    assert.equal(calls.some((call) => call.path.startsWith('/api/examples')), false);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    globalThis.fetch = originalFetch;
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

test('GitHub previews are keyless, race-safe, reviewable, and require fresh consent for pinned audits', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/#/new' });
  const originalFetch = globalThis.fetch;
  const originals = new Map();
  for (const [key, value] of Object.entries({ window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true })) {
    originals.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { value, configurable: true, writable: true });
  }
  const calls = [];
  const pending = [];
  let keyConfigured = false;
  const snapshot = (id = 'preview-1') => {
    const content = 'contract Vault {}\n// <script>alert(1)</script>';
    return { id, repository_url: 'https://github.com/owner/repo', repository: 'owner/repo', ref: 'main', commit: 'a'.repeat(40), subdirectory: 'contracts/src', file_count: 1, total_chars: content.length, files: [{ path: 'contracts/src/Vault.sol', content }] };
  };
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options });
    if (path === '/api/config') return Response.json({ model: 'test-model', provider: 'test-provider', key_configured: keyConfigured, max_chars: 1000 });
    if (path === '/api/audits' && options.method !== 'POST') return Response.json([]);
    if (path === '/api/github/preview') return new Promise((resolve, reject) => pending.push({ resolve, reject, options }));
    if (path === '/api/audits' || path === '/api/audits/github-job') return Response.json({ id: 'github-job', status: 'completed', target: 'github', mode: 'context', findings: [], source: snapshot() });
    throw new Error(`Unexpected request: ${path}`);
  };
  const root = createRoot(document.getElementById('root'));
  const flush = async (fn = () => {}) => act(async () => { fn(); await new Promise((resolve) => setTimeout(resolve, 30)); });
  const button = (text) => [...document.querySelectorAll('button')].find((element) => element.textContent.includes(text));
  // React's onChange event plugin is initialized before this DOM exists. Its native
  // value tracker can dispatch controlled edits through the host input props in Node.
  const edit = async (id, value) => flush(() => {
    const input = document.getElementById(id);
    const propsKey = Object.keys(input).find((key) => key.startsWith('__reactProps$'));
    input[propsKey].onChange({ target: { value } });
  });
  const preview = async () => flush(() => button('Preview repository').click());
  const consent = () => document.getElementById('paid-consent');
  const auditPosts = () => calls.filter((call) => call.path === '/api/audits' && call.options.method === 'POST');
  try {
    await flush(() => root.render(React.createElement(App)));
    await flush(() => document.querySelector('input[value="github"]').click());
    await edit('github-url', 'https://github.com/owner/repo/tree/main');
    await preview();
    assert.match(document.body.textContent, /root URL/);
    assert.equal(pending.length, 0);
    await edit('github-url', 'https://github.com/owner/repo');
    await preview();
    assert.equal(pending.length, 1, 'preview needs no model key');
    assert.equal(button('Previewing repository').disabled, true);
    assert.deepEqual(JSON.parse(pending[0].options.body), { url: 'https://github.com/owner/repo', ref: '', subdirectory: '', authenticated: false });
    assert.equal(pending[0].options.headers['X-Lucid-Request'], 'local-ui');
    await flush(() => document.getElementById('github-auth').click());
    assert.equal(document.getElementById('github-auth').checked, true);
    assert.equal(pending[0].options.signal.aborted, true, 'changing authentication cancels a preview');
    await edit('github-ref', 'main');
    assert.equal(pending[0].options.signal.aborted, true);
    await preview();
    assert.equal(JSON.parse(pending[1].options.body).authenticated, true);
        assert.doesNotMatch(pending[1].options.body, /token/i);
        await flush(() => pending[1].resolve(Response.json(snapshot('new-preview'))));
    await flush(() => pending[0].resolve(Response.json(snapshot('stale-preview'))));
    assert.ok(document.querySelector('.github-preview'));
    assert.equal(auditPosts().length, 0);
    assert.equal(consent().disabled, true);
    assert.equal(button('Start live audit').disabled, true);
    assert.equal(document.querySelector('.repository-meta a[href$="/commit/' + 'a'.repeat(40) + '"]').textContent, 'a'.repeat(40));
    await flush(() => document.querySelector('.source-review summary').click());
    assert.equal(document.querySelector('.source-review details').open, true);
    assert.equal(document.querySelectorAll('.line-number').length, 2);
    assert.match(document.querySelector('.source-review pre').textContent, /<script>alert\(1\)<\/script>/);
    assert.equal(document.querySelector('.source-review script'), null);

    keyConfigured = true;
    await flush(() => document.querySelector('[aria-label="Refresh history and configuration"]').click());
    assert.equal(button('Start live audit').disabled, true, 'preview is not paid consent');
    await flush(() => consent().click());
    assert.equal(button('Start live audit').disabled, false);
    await flush(() => document.querySelector('.scope-file input').click());
    assert.equal(consent().checked, false, 'scope changes clear consent');
    assert.match(document.body.textContent, /0 of 1 files in scope/);
    assert.equal(button('Start live audit').disabled, true, 'empty scope cannot start');
    await flush(() => button('Select all').click());
    assert.equal(consent().checked, false);
    assert.match(document.body.textContent, /1 of 1 files in scope/);
    await flush(() => consent().click());
    await flush(() => button('Clear selection').click());
    assert.equal(consent().checked, false);
    assert.equal(button('Start live audit').disabled, true);
    await flush(() => document.querySelector('.scope-file input').click());
    await flush(() => consent().click());
    assert.equal(button('Start live audit').disabled, false);
    await flush(() => document.getElementById('github-auth').click());
        assert.equal(consent().checked, false, 'authentication changes clear consent');
        assert.equal(document.querySelector('.github-preview'), null);
        await preview();
        await flush(() => pending.at(-1).resolve(Response.json(snapshot())));
        await flush(() => consent().click());
        for (const [id, value] of [['github-url', 'https://github.com/owner/other'], ['github-ref', 'v2'], ['github-subdirectory', 'contracts/src']]) {
      await edit(id, value);
      assert.equal(consent().checked, false, `${id} resets consent`);
      assert.equal(document.querySelector('.github-preview'), null);
      assert.equal(button('Start live audit').disabled, true);
      await preview();
      await flush(() => pending.at(-1).resolve(Response.json(snapshot())));
      await flush(() => consent().click());
    }
    await preview();
    assert.equal(consent().checked, false, 're-preview resets consent');
    await flush(() => pending.at(-1).resolve(Response.json({ detail: 'Repository not found' }, { status: 404 })));
    assert.match(document.body.textContent, /Repository not found/);
    assert.equal(document.querySelector('.github-preview'), null);
    assert.equal(button('Start live audit').disabled, true);
    await preview();
    await flush(() => pending.at(-1).reject(new TypeError('Network unavailable')));
    assert.match(document.body.textContent, /Cannot reach the local API/);
    assert.equal(button('Preview repository').disabled, false);
    await preview();
    await flush(() => pending.at(-1).resolve(Response.json({ ...snapshot(), commit: 'main' })));
    assert.match(document.body.textContent, /invalid repository preview/);
    await preview();
    await flush(() => pending.at(-1).resolve(Response.json(snapshot('paid-preview'))));
    await flush(() => consent().click());
    await flush(() => button('Start live audit').click());
    assert.deepEqual(JSON.parse(auditPosts()[0].options.body), { mode: 'context', rounds: 1, model: 'test-model', json_mode: true, reasoning_effort: null, confirmed_paid: true, target: 'github', github_preview_id: 'paid-preview', github_paths: ['contracts/src/Vault.sol'], files: [] });
    assert.equal(auditPosts().length, 1);
    assert.equal(window.location.hash, '#/audit/github-job');
    assert.match(document.body.textContent, /owner\/repo/);
    assert.match(document.body.textContent, /contracts\/src/);
    assert.ok(document.querySelector('.repository-meta a[href$="/commit/' + 'a'.repeat(40) + '"]'));

    await flush(() => button('New audit').click());
    await flush(() => document.querySelector('input[value="github"]').click());
    assert.equal(document.getElementById('github-url').value, '');
    await edit('github-url', 'https://github.com/owner/repo');
    await preview();
    const resetRequest = pending.at(-1);
    await flush(() => button('New audit').click());
    assert.equal(resetRequest.options.signal.aborted, true);
    await flush(() => resetRequest.resolve(Response.json(snapshot('reset-stale'))));
    await flush(() => document.querySelector('input[value="github"]').click());
    assert.equal(document.querySelector('.github-preview'), null);
    await edit('github-url', 'https://github.com/owner/repo');
    await preview();
    const switchedRequest = pending.at(-1);
    await flush(() => document.querySelector('input[value="bundled"]').click());
    assert.equal(switchedRequest.options.signal.aborted, true);
    await flush(() => switchedRequest.reject(new Error('stale failure')));
    await flush(() => document.querySelector('input[value="github"]').click());
    assert.doesNotMatch(document.body.textContent, /stale failure/);
    await preview();
    const unmountedRequest = pending.at(-1);
    await act(async () => root.unmount());
    assert.equal(unmountedRequest.options.signal.aborted, true);
    await flush(() => unmountedRequest.resolve(Response.json(snapshot('unmounted-stale'))));
  } finally {
    await act(async () => root.unmount());
    dom.window.close(); globalThis.fetch = originalFetch;
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

test('model settings load explicitly, preserve manual entry, gate consent, and ignore cancelled loads', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/#/new' });
  const originalFetch = globalThis.fetch;
  const originals = new Map();
  for (const [key, value] of Object.entries({ window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true })) {
    originals.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { value, configurable: true, writable: true });
  }
  const calls = [];
  const pending = [];
  let defaultModel = 'backend-model';
  let resolveAudit;
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options });
    if (path === '/api/config') return Response.json({ model: defaultModel, provider: 'test-provider', key_configured: true });
    if (path === '/api/models') return new Promise((resolve, reject) => pending.push({ resolve, reject, options }));
    if (path === '/api/audits' && options.method === 'GET') return Response.json([]);
    if (path === '/api/audits' && options.method === 'POST') return new Promise((resolve) => { resolveAudit = resolve; });
    if (path === '/api/audits/model-job') return Response.json({ id: 'model-job', status: 'completed', findings: [] });
    throw new Error(`Unexpected request: ${path}`);
  };
  const root = createRoot(document.getElementById('root'));
  const flush = async (fn = () => {}) => act(async () => { fn(); await new Promise((resolve) => setTimeout(resolve, 30)); });
  const button = (text) => [...document.querySelectorAll('button')].find((element) => element.textContent.includes(text));
  const consent = () => document.getElementById('paid-consent');
  const edit = async (id, value) => flush(() => {
    const input = document.getElementById(id);
    const propsKey = Object.keys(input).find((key) => key.startsWith('__reactProps$'));
    input[propsKey].onChange({ target: { value } });
  });
  const load = async () => flush(() => button('Load models').click());
  const options = () => [...document.querySelectorAll('#available-models option')].map((option) => option.value);
  try {
    await flush(() => root.render(React.createElement(App)));
    assert.equal(pending.length, 0, 'mount does not load models');
    assert.match(document.querySelector('.config-strip').textContent, /backend-model/);
    assert.match(document.querySelector('.confirmation').textContent, /backend-model/);
    assert.equal(document.getElementById('json-mode').value, 'true');
    assert.equal(document.getElementById('reasoning-effort').value, '');
    await flush(() => consent().click());
    assert.equal(button('Start live audit').disabled, false);
    for (const [id, value] of [['audit-model', '  manual/model:version  '], ['json-mode', 'false'], ['reasoning-effort', 'high']]) {
      await edit(id, value);
      assert.equal(consent().checked, false, `${id} resets consent`);
      await flush(() => consent().click());
    }
    assert.match(document.querySelector('.config-strip').textContent, /manual\/model:version/);
    assert.match(document.querySelector('.confirmation').textContent, /manual\/model:version/);
    await edit('audit-model', 'bad model');
    assert.equal(consent().disabled, true);
    assert.equal(button('Start live audit').disabled, true);
    assert.equal(document.getElementById('audit-model').getAttribute('aria-invalid'), 'true');
    await edit('audit-model', '   ');
    assert.match(document.querySelector('.config-strip').textContent, /backend-model/);
    assert.equal(consent().disabled, false);
    await flush(() => consent().click());
    defaultModel = '';
    await flush(() => document.querySelector('[aria-label="Refresh history and configuration"]').click());
    assert.equal(consent().checked, false, 'default configuration changes reset consent');
    assert.equal(consent().disabled, true, 'blank resolved model is blocked');
    await edit('audit-model', 'manual-model');
    await load();
    assert.equal(pending.length, 1);
    assert.equal(button('Loading models').disabled, true);
    assert.equal(document.getElementById('audit-model').disabled, false, 'manual input works while loading');
    await edit('audit-model', '  manual/model:version  ');
    await flush(() => pending[0].resolve(Response.json({ models: [{ id: 'z-model' }, { id: 'a-model' }, { id: 'z-model' }] })));
    assert.deepEqual(options(), ['a-model', 'z-model']);
    assert.equal(document.getElementById('audit-model').value, '  manual/model:version  ', 'loading never changes selected model');
    await flush(() => consent().click());
    await load();
    await flush(() => pending[1].resolve(Response.json({ detail: 'Provider secret sk-never-display https://credentials.example' }, { status: 401 })));
    assert.match(document.body.textContent, /API 401.*details are hidden/);
    assert.doesNotMatch(document.body.textContent, /sk-never-display|credentials\.example/);
    assert.equal(document.getElementById('audit-model').disabled, false);
    assert.equal(consent().checked, true, 'loading suggestions does not change paid settings');
    await load();
    await flush(() => pending[2].resolve(Response.json({ models: [{ id: null }] })));
    assert.match(document.body.textContent, /invalid model list/);
    assert.deepEqual(options(), ['a-model', 'z-model']);
    await load();
    await flush(() => pending[3].reject(new Error('network secret sk-never-display')));
    assert.match(document.body.textContent, /Cannot reach the local API/);
    assert.doesNotMatch(document.body.textContent, /sk-never-display/);
    assert.equal(button('Start live audit').disabled, false, 'failed load does not prevent manual model audit');
    await flush(() => button('Start live audit').click());
    const post = calls.find((call) => call.path === '/api/audits' && call.options.method === 'POST');
    assert.deepEqual(JSON.parse(post.options.body), { mode: 'context', rounds: 1, model: 'manual/model:version', json_mode: false, reasoning_effort: 'high', confirmed_paid: true, target: 'bundled', files: [] });
    for (const id of ['audit-model', 'json-mode', 'reasoning-effort', 'paid-consent']) assert.equal(document.getElementById(id).matches(':disabled'), true, `${id} locked while submitting`);
    assert.equal(button('Load models').matches(':disabled'), true);
    assert.equal(button('New audit').disabled, true);
    await flush(() => resolveAudit(Response.json({ id: 'model-job', status: 'completed', findings: [] })));
    assert.equal(calls.filter((call) => call.path === '/api/audits' && call.options.method === 'POST').length, 1, 'no paid compatibility retry');
    await flush(() => button('New audit').click());
    assert.equal(document.getElementById('audit-model').value, '');
    assert.equal(document.getElementById('json-mode').value, 'true');
    assert.equal(document.getElementById('reasoning-effort').value, '');
    assert.equal(consent().checked, false);
    assert.equal(pending.length, 4, 'reset and config refresh do not load models');
    await load();
    const stale = pending.at(-1);
    await flush(() => button('New audit').click());
    assert.equal(stale.options.signal.aborted, true);
    await load();
    const fresh = pending.at(-1);
    await flush(() => fresh.resolve(Response.json({ models: [{ id: 'fresh-model' }] })));
    await flush(() => stale.resolve(Response.json({ models: [{ id: 'stale-model' }] })));
    assert.deepEqual(options(), ['fresh-model']);
    await load();
    const staleFailure = pending.at(-1);
    await flush(() => button('New audit').click());
    await flush(() => staleFailure.reject(new Error('stale failure')));
    assert.equal(document.querySelector('.model-panel [role="alert"]')?.textContent.includes('Cannot reach'), false);
    await load();
    const unmounted = pending.at(-1);
    await act(async () => root.unmount());
    assert.equal(unmounted.options.signal.aborted, true);
    await flush(() => unmounted.resolve(Response.json({ models: [{ id: 'unmounted-model' }] })));
  } finally {
    await act(async () => root.unmount());
    dom.window.close(); globalThis.fetch = originalFetch;
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

// A finite DOM integration test: no servers, provider calls, or browser binaries.
test('rendered workflow gates paid scans, ignores legacy example routes, uploads, submits, and preserves history after reset', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/#/example/old-link' });
  const originalFetch = globalThis.fetch;
  const originals = new Map();
  for (const [key, value] of Object.entries({ window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true })) {
    originals.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { value, configurable: true, writable: true });
  }
  const calls = [];
  let keyConfigured = false;
  let job = null;
  let failNextPoll = false;
  const finding = { title: 'Unchecked callback', severity: 'high', location: 'Vault.sol:12', description: 'External call before accounting.', impact: 'Funds can be drained.', exploit_steps: ['Deploy receiver.', 'Reenter withdraw.'] };
  globalThis.fetch = async (path, options) => {
    calls.push({ path, options });
    if (path === '/api/config') return Response.json({ model: 'test-model', provider: 'test-provider', key_configured: keyConfigured, default_target: 'target/src', default_file_count: 12, max_chars: 1000 });

    if (path === '/api/audits' && options.method === 'POST') {
      job = { id: 'job-1', status: 'running', stage: 'finding_bugs', mode: 'loop', rounds: 3, completed_rounds: 1, findings: [finding], context: 'Live protocol context', error: null, warnings: ['Verify every finding'], created_at: '2026-10-04T10:00:00Z', model: 'test-model', target: 'upload' };
      return Response.json({ ...job, status: 'queued' });
    }
    if (path === '/api/audits') return Response.json(job ? [job] : []);
    if (path === '/api/audits/job-1') {
      if (failNextPoll) { failNextPoll = false; return Response.json({ detail: 'Provider temporarily unavailable' }, { status: 503 }); }
      return Response.json(job);
    }
    throw new Error(`Unexpected request: ${path}`);
  };
  const root = createRoot(document.getElementById('root'));
  const flush = async (fn = () => {}) => { await act(async () => { fn(); await new Promise((resolve) => setTimeout(resolve, 30)); }); };
  const button = (text) => [...document.querySelectorAll('button')].find((element) => element.textContent.includes(text));
  const navigate = async (hash) => flush(() => { window.location.hash = hash; window.dispatchEvent(new dom.window.HashChangeEvent('hashchange')); });
  try {
    await flush(() => root.render(React.createElement(App)));
    assert.equal(button('Start live audit').disabled, true);
    assert.match(document.body.textContent, /Live audits are locked/);
    assert.ok(document.querySelector('.audit-form'), 'legacy direct links show the new-audit form');
    assert.deepEqual(calls.map((call) => call.path).sort(), ['/api/audits', '/api/config']);
    assert.equal(document.querySelector('[aria-label="Saved examples"], .example-list, .example-item'), null);
    assert.doesNotMatch(document.body.textContent, /Saved examples|Saved example|Saved output/);
    const requestsBeforeLegacy = calls.length;
    await navigate('#/example/saved-1');
    assert.ok(document.querySelector('.audit-form'), 'legacy hashes show the new-audit form');
    assert.match(document.querySelector('.topbar').textContent, /New audit/);
    assert.equal(document.querySelector('.results'), null);
    assert.equal(calls.length, requestsBeforeLegacy, 'legacy navigation makes no API request');
    assert.equal(calls.some((call) => call.path.startsWith('/api/examples')), false);
    assert.equal(calls.some((call) => call.options.method === 'POST'), false);

    keyConfigured = true;
    await flush(() => document.querySelector('[aria-label="Refresh history and configuration"]').click());
    await flush(() => button('New audit').click());
    await flush(() => document.querySelector('input[value="upload"]').click());
    const input = document.querySelector('input[type="file"][accept=".sol"]');
    Object.defineProperty(input, 'files', { configurable: true, value: [{ name: 'Vault.sol', size: 17, text: async () => 'contract Vault {}' }] });
    await flush(() => input.dispatchEvent(new dom.window.Event('change', { bubbles: true })));
    assert.match(document.body.textContent, /1 files selected/);
    const checkbox = document.getElementById('paid-consent');
    await flush(() => checkbox.click());
    assert.equal(button('Start live audit').disabled, false);
    await flush(() => document.querySelector('input[value="loop"]').click());
    assert.equal(checkbox.checked, false, 'changing strategy resets paid consent');
    assert.match(document.body.textContent, /4 paid model calls planned/);
    await flush(() => checkbox.click());
    await flush(() => button('Start live audit').click());
    const post = calls.find((call) => call.options.method === 'POST');
    assert.deepEqual(JSON.parse(post.options.body), { mode: 'loop', rounds: 3, model: 'test-model', json_mode: true, reasoning_effort: null, confirmed_paid: true, target: 'upload', files: [{ path: 'Vault.sol', content: 'contract Vault {}' }] });
    assert.equal(post.options.headers['X-Lucid-Request'], 'local-ui');
    assert.equal(window.location.hash, '#/audit/job-1');
    assert.match(document.body.textContent, /1 \/ 3 rounds/);
    assert.match(document.body.textContent, /Backend warnings/);
    assert.match(document.body.textContent, /Unchecked callback/);
    assert.equal(document.querySelector('progress').value, 1);
    await flush(() => document.querySelector('.skip-link').click());
    assert.equal(window.location.hash, '#/audit/job-1', 'skip link does not change the current route');
    assert.equal(document.activeElement.id, 'main-content');

    await flush(() => button('New audit').click());
    assert.equal(window.location.hash, '#/new');
    assert.equal(document.querySelector('input[type="checkbox"]').checked, false);
    assert.ok(document.querySelector('a[href="#/audit/job-1"]'), 'active job remains reachable after reset');
    failNextPoll = true;
    await navigate('#/audit/job-1');
    assert.match(document.body.textContent, /API 503.*backend logs/);
    job = { ...job, status: 'completed', completed_rounds: 3 };
    await flush(() => button('Retry').click());
    assert.match(document.body.textContent, /Ready for review/);
    assert.equal(document.querySelector('progress'), null);
    assert.equal(document.querySelector('[role="alert"]'), null);
    assert.ok(document.querySelector('.finding-card > summary'));
    assert.ok(document.querySelector('.context-panel > summary'));
    assert.equal(calls.some((call) => call.path.startsWith('/api/examples')), false);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    globalThis.fetch = originalFetch;
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});
