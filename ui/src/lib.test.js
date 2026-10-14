import test from 'node:test';
import assert from 'node:assert/strict';
import { MODES, callCount, charCount, filterFindings, isActive, modelIds, parseRoute, request, requestRounds, routeHref, validateModel, validateFiles, validateGithubPreview, validateGithubUrl } from './lib.js';

test('model validation accepts provider IDs without requiring list membership', () => {
  for (const id of ['gpt-model', 'provider/model:free', 'model.v1_2']) assert.equal(validateModel(id), null);
  for (const id of ['', undefined, null, 123, 'bad model', 'bad\nmodel', 'bad\u0000model', 'bad\u007fmodel']) assert.ok(validateModel(id));
  assert.deepEqual(modelIds({ models: [{ id: 'z' }, { id: 'a' }, { id: 'a' }] }), ['a', 'z']);
  assert.deepEqual(modelIds({ models: [] }), []);
  assert.throws(() => modelIds({ models: [{ id: 'bad model' }] }, 'codex'), (error) => /invalid Codex model list/.test(error.message) && !/manually/.test(error.message));
  for (const data of [null, [], { models: null }, { models: [null] }, { models: [{ id: '' }] }, { models: [{ id: 'bad model' }] }]) assert.throws(() => modelIds(data), /invalid model list/);
});
test('model requests are bounded, cancellable, and hide provider error details', async () => {
  const originalFetch = globalThis.fetch;
  const originalTimeout = AbortSignal.timeout;
  const timeouts = [];
  const controller = new AbortController();
  try {
    AbortSignal.timeout = (ms) => { timeouts.push(ms); return new AbortController().signal; };
    globalThis.fetch = async (path, options) => {
      assert.equal(path, '/api/models');
      assert.equal(options.method, 'GET');
      assert.equal(options.headers.Accept, 'application/json');
      assert.ok(options.signal);
      return Response.json({ detail: 'Authorization: Bearer sk-secret', error: 'https://key.example' }, { status: 502 });
    };
    await assert.rejects(request('/api/models', { signal: controller.signal }), (error) => /API 502.*hidden/.test(error.message) && !/sk-secret|key\.example|Bearer/.test(error.message));
    assert.deepEqual(timeouts, [20000]);
    AbortSignal.timeout = () => { const timeout = new AbortController(); timeout.abort(); return timeout.signal; };
    globalThis.fetch = async (_path, { signal }) => signal.throwIfAborted();
    await assert.rejects(request('/api/models'), /Model loading timed out.*manually/);
    controller.abort();
    await assert.rejects(request('/api/models', { signal: controller.signal }), { name: 'AbortError' });
  } finally { globalThis.fetch = originalFetch; AbortSignal.timeout = originalTimeout; }
});
test('model errors show only allowlisted provider status guidance', async () => {
  const originalFetch = globalThis.fetch;
  try {
    for (const status of [400, 401, 402, 403, 404, 429, 500, 502, 503, 504]) {
      globalThis.fetch = async () => Response.json({ provider_status: status, detail: 'Authorization: Bearer sk-secret' }, { status: 502 });
      await assert.rejects(request('/api/models'), (error) => error.message.includes(`HTTP ${status}`) && error.message.includes('No inference') && !/Bearer|sk-secret/.test(error.message));
    }
    for (const status of ['403', 'sk-secret', null, 418, '__proto__', 'constructor']) {
      globalThis.fetch = async () => Response.json({ provider_status: status, detail: 'sk-secret' }, { status: 502 });
      await assert.rejects(request('/api/models'), (error) => /details are hidden/.test(error.message) && !/sk-secret/.test(error.message));
    }
  } finally { globalThis.fetch = originalFetch; }
});
test('provider-specific model requests preserve error redaction', async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => Response.json({ detail: 'sk-secret', provider_error_code: 'kiro' }, { status: 502 });
    await assert.rejects(request('/api/models?provider=kiro'), (error) => /kiro-cli login/.test(error.message) && !/sk-secret/.test(error.message));
    for (const path of ['/api/models', '/api/models?provider=codex', '/api/models?provider=codex&refresh=true']) {
      globalThis.fetch = async () => Response.json({ detail: 'Authorization: Bearer sk-secret https://private.example/token', provider_status: 403, provider_error_code: 'codex' }, { status: 502 });
      await assert.rejects(request(path), (error) => /codex login on the backend/.test(error.message) && /No inference was requested/.test(error.message) && !/sk-secret|Bearer|private\.example|Requesty|manually/.test(error.message));
    }
    globalThis.fetch = async () => Response.json({ detail: 'sk-secret', provider_status: 403 }, { status: 502 });
    await assert.rejects(request('/api/models?provider=api'), (error) => /HTTP 403/.test(error.message) && !/sk-secret/.test(error.message));
  } finally { globalThis.fetch = originalFetch; }
});
test('paid call counts include context only when used', () => {
  assert.equal(callCount('single', 10), 1);
  assert.equal(callCount('context', 10), 2);
  assert.equal(callCount('loop', 1), 2);
  assert.equal(callCount('loop', 10), 11);
  assert.equal(callCount('specialists', 1), 8);
  assert.equal(callCount('specialists', 10), 62);
});
test('specialist strategy requests per-lane rounds', () => {
  assert.equal(MODES.length, 4);
  assert.equal(MODES[3].id, 'specialists');
  for (const rounds of [1, 3, 10]) {
    for (const mode of ['loop', 'specialists']) assert.equal(requestRounds(mode, rounds), rounds);
    for (const mode of ['single', 'context']) assert.equal(requestRounds(mode, rounds), 1);
  }

});
test('audit routes encode IDs and unsupported or legacy routes fall back to new', () => {
  assert.deepEqual(parseRoute(routeHref('audit', 'job/a b')), { type: 'audit', id: 'job/a b' });
  for (const hash of ['#/example/saved-1', '#/example/%E0%A4', '#/unknown/item']) {
    assert.deepEqual(parseRoute(hash), { type: 'new', id: null });
  }
  assert.deepEqual(parseRoute('#/new'), { type: 'new', id: null });
  assert.deepEqual(parseRoute('#/audit/%E0%A4'), { type: 'new', id: null });
});
test('only queued and running jobs need polling', () => {
  assert.ok(isActive({ status: 'queued' }));
  assert.ok(isActive({ status: 'running' }));
  assert.equal(isActive({ status: 'completed' }), false);
  assert.equal(isActive({ status: 'failed' }), false);
  assert.equal(isActive(null), false);
});
test('source validation catches unsafe, duplicate, empty and oversized uploads', () => {
  const file = { path: 'src/Vault.sol', content: 'contract Vault {}' };
  assert.equal(validateFiles([file], 100), null);
  assert.match(validateFiles([], 100), /at least one/);
  assert.match(validateFiles([{ ...file, path: 'secret.txt' }], 100), /Only Solidity/);
  assert.match(validateFiles([{ ...file, path: '../Vault.sol' }], 100), /relative/);
  assert.match(validateFiles([{ ...file, path: 'src\\..\\Vault.sol' }], 100), /relative/);
  assert.match(validateFiles([{ ...file, path: '/Vault.sol' }], 100), /relative/);
  assert.match(validateFiles([file, file], 100), /Duplicate path/);
  assert.match(validateFiles([{ ...file, content: '' }], 100), /empty/);
  assert.match(validateFiles([file], 3), /backend limit/);
  assert.equal(charCount('x🛡'), 2);
  assert.equal(validateFiles([{ ...file, content: 'x🛡' }], 2), null);
});
test('GitHub URLs accept only public HTTPS repository roots without credentials', () => {
  for (const url of ['https://github.com/owner/repo', 'https://github.com/owner/repo/']) assert.equal(validateGithubUrl(url), null);
  for (const url of ['', 'http://github.com/owner/repo', 'https://evil.example/owner/repo', 'https://token@github.com/owner/repo', 'https://github.com/owner/repo/tree/main', 'https://github.com/owner/repo?ref=main', 'https://github.com/owner/..']) assert.ok(validateGithubUrl(url), url);
});
test('GitHub preview validation requires a pinned, consistent Solidity snapshot', () => {
  const preview = { id: 'preview-1', repository_url: 'https://github.com/owner/repo', repository: 'owner/repo', commit: 'a'.repeat(40), ref: 'main', subdirectory: '', file_count: 1, total_chars: 2, files: [{ path: 'Vault.sol', content: 'x🛡' }] };
  assert.equal(validateGithubPreview(preview, 1000), null);
  for (const change of [{ id: '' }, { commit: 'main' }, { files: null }, { files: [null] }, { file_count: 2 }, { total_chars: 3 }, { repository_url: 'javascript:alert(1)' }]) assert.ok(validateGithubPreview({ ...preview, ...change }, 1000));
  assert.match(validateGithubPreview(preview, 1), /backend limit/);
});
test('preview requests use the optional timeout and existing CSRF helper', async () => {
  const originalFetch = globalThis.fetch;
  const originalTimeout = AbortSignal.timeout;
  const timeouts = [];
  try {
    AbortSignal.timeout = (ms) => { timeouts.push(ms); return new AbortController().signal; };
    globalThis.fetch = async (path, options) => {
      assert.equal(path, '/api/github/preview');
      assert.equal(options.headers['X-Lucid-Request'], 'local-ui');
      assert.deepEqual(JSON.parse(options.body), { url: 'https://github.com/owner/repo', ref: '', subdirectory: '' });
      return Response.json({ id: 'preview-1' });
    };
    await request('/api/github/preview', { method: 'POST', body: { url: 'https://github.com/owner/repo', ref: '', subdirectory: '' }, timeoutMs: 75000 });
    await request('/api/github/preview', { method: 'POST', body: { url: 'https://github.com/owner/repo', ref: '', subdirectory: '' } });
    assert.deepEqual(timeouts, [75000, 20000]);
  } finally { globalThis.fetch = originalFetch; AbortSignal.timeout = originalTimeout; }
});
test('finding search includes all evidence fields and composes with severity', () => {
  const findings = [
    { title: 'Reentrancy', severity: 'high', location: 'Vault.sol:4', description: 'External call', impact: 'Drain', exploit_steps: ['Deploy a receiver'] },
    { title: 'Rounding', severity: 'medium', location: 'Pool.sol:9', description: 'Division', impact: 'Dust', exploit_steps: ['Deposit'] },
  ];
  assert.equal(filterFindings(findings, 'all', ' RECEIVER ').length, 1);
  assert.equal(filterFindings(findings, 'medium', 'receiver').length, 0);
  assert.equal(filterFindings(findings, 'all', 'pool.sol').length, 1);
  assert.equal(filterFindings(findings, 'high', '').length, 1);
});
test('POST requests enforce the CSRF header and carry confirmation', async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async (path, options) => {
      assert.equal(path, '/api/audits');
      assert.equal(options.headers['X-Lucid-Request'], 'local-ui');
      assert.equal(options.headers['Content-Type'], 'application/json');
      assert.equal(options.credentials, 'same-origin');
      assert.equal(JSON.parse(options.body).confirmed_paid, true);
      return new Response(JSON.stringify({ id: 'job-1', status: 'queued' }));
    };
    assert.equal((await request('/api/audits', { method: 'POST', body: { confirmed_paid: true } })).id, 'job-1');
  } finally { globalThis.fetch = original; }
});
test('API errors are actionable and do not display proxy HTML', async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'Invalid origin' }), { status: 403 });
    await assert.rejects(request('/api/audits'), /Invalid origin.*CSRF/);
    globalThis.fetch = async () => new Response('<html>proxy error</html>', { status: 502 });
    await assert.rejects(request('/api/config'), /API 502.*backend logs/);
    globalThis.fetch = async () => new Response('<html>not JSON</html>');
    await assert.rejects(request('/api/config'), /no JSON data/);
    globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
    await assert.rejects(request('/api/config'), /127\.0\.0\.1:8765/);
  } finally { globalThis.fetch = original; }
});
test('preview timeout and API errors do not ask for provider keys or paid retries', async () => {
  const originalFetch = globalThis.fetch;
  const originalTimeout = AbortSignal.timeout;
  try {
    globalThis.fetch = async () => Response.json({ detail: 'Repository not found' }, { status: 404 });
    await assert.rejects(request('/api/github/preview'), /Repository not found.*public repository.*No model calls/);
    AbortSignal.timeout = () => { const controller = new AbortController(); controller.abort(); return controller.signal; };
    globalThis.fetch = async (_path, { signal }) => signal.throwIfAborted();
    await assert.rejects(request('/api/github/preview', { timeoutMs: 75000 }), /preview timed out.*No model calls/);
  } finally { globalThis.fetch = originalFetch; AbortSignal.timeout = originalTimeout; }
});
test('aborted requests preserve cancellation instead of reporting connectivity errors', async () => {
  const original = globalThis.fetch;
  const controller = new AbortController();
  controller.abort();
  try {
    globalThis.fetch = async (_path, { signal }) => { signal.throwIfAborted(); };
    await assert.rejects(request('/api/config', { signal: controller.signal }), { name: 'AbortError' });
  } finally { globalThis.fetch = original; }
});
