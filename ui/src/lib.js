export const SEVERITIES = ['critical', 'high', 'medium', 'low'];
export const MODES = [
  { id: 'single', title: 'Single pass', label: '01', description: 'A focused vulnerability scan directly over your Solidity source.', detail: '1 model call' },
  { id: 'context', title: 'Context-aware', label: '02', description: 'Build a protocol understanding, then audit with that context.', detail: '2 model calls' },
  { id: 'loop', title: 'Iterative loop', label: '03', description: 'Build context once, then explore new findings across multiple rounds.', detail: 'Context + N rounds' },
  { id: 'specialists', title: 'Specialist audit', label: '04', description: 'Five specialist lenses and one general lane, followed by an AI judge.', detail: 'Context + 6 parallel lanes + judge' },
];
export const isActive = (job) => ['queued', 'running'].includes(job?.status);
export const callCount = (mode, rounds) => mode === 'single' ? 1 : mode === 'context' ? 2 : mode === 'specialists' ? 2 + 6 * rounds : rounds + 1;
export const requestRounds = (mode, rounds) => ['loop', 'specialists'].includes(mode) ? rounds : 1;

export const charCount = (text) => { let count = 0; for (const _char of text) count++; return count; };
export function parseRoute(hash) {
  const match = /^#\/(audit)\/([^/]+)$/.exec(hash);
  if (!match) return { type: 'new', id: null };
  try { return { type: match[1], id: decodeURIComponent(match[2]) }; }
  catch { return { type: 'new', id: null }; }
}
export const routeHref = (type, id) => type === 'new' ? '#/new' : `#/${type}/${encodeURIComponent(id)}`;
export function filterFindings(findings, severity, query) {
  const needle = query.trim().toLowerCase();
  return findings.filter((finding) =>
    (severity === 'all' || finding.severity === severity) &&
    [finding.title, finding.location, finding.description, finding.impact, ...(finding.exploit_steps || [])]
      .join(' ').toLowerCase().includes(needle));
}
export function validateFiles(files, maxChars) {
  if (!files.length) return 'Choose at least one .sol file before starting an audit.';
  const seen = new Set();
  for (const file of files) {
    if (!file.path.toLowerCase().endsWith('.sol')) return 'Only Solidity (.sol) source files are accepted.';
    if (file.path.startsWith('/') || file.path.split(/[\\/]/).some((part) => part === '..')) return 'Source paths must be relative, with no parent-directory segments.';
    if (seen.has(file.path)) return `Duplicate path: ${file.path}. Select a folder to preserve relative paths.`;
    seen.add(file.path);
  }
  const total = files.reduce((sum, file) => sum + charCount(file.content), 0);
  if (!total) return 'The selected source files are empty. Choose files containing Solidity source.';
  if (maxChars > 0 && total > maxChars) return `Source contains ${total.toLocaleString()} characters; the backend limit is ${maxChars.toLocaleString()}. Select fewer or smaller files.`;
  return null;
}

export function validateGithubUrl(url) {
  return /^https:\/\/github\.com\/[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\/[a-zA-Z0-9_.-]+\/?$/.test(url) && !['.', '..'].includes(url.split('/')[4])
    ? null : 'Use a public repository root URL: https://github.com/owner/repo (not a file, tree, or pull request URL).';
}
export function validateGithubPreview(preview, maxChars) {
  if (!preview || typeof preview.id !== 'string' || !preview.id ||
      typeof preview.repository_url !== 'string' || validateGithubUrl(preview.repository_url) ||
      typeof preview.repository !== 'string' || !/^[^/]+\/[^/]+$/.test(preview.repository) ||
      typeof preview.commit !== 'string' || !/^[a-fA-F0-9]{40}$/.test(preview.commit) ||
      typeof preview.ref !== 'string' || typeof preview.subdirectory !== 'string' ||
      !Array.isArray(preview.files) || preview.files.some((file) => typeof file?.path !== 'string' || typeof file?.content !== 'string')) {
    return 'The API returned an invalid repository preview. Preview the repository again before scanning.';
  }
  const error = validateFiles(preview.files, maxChars);
  if (error) return error;
  if (preview.file_count !== preview.files.length || preview.total_chars !== preview.files.reduce((sum, file) => sum + charCount(file.content), 0)) {
    return 'The preview manifest does not match its source snapshot. Preview the repository again.';
  }
  return null;
}

export function validateModel(model) {
  return typeof model === 'string' && model.length > 0 && model.length <= 256 && !/[\s\u0000-\u001f\u007f]/u.test(model)
    ? null : 'Enter a valid model ID without whitespace or control characters, or leave blank to use a valid backend default.';
}
export function modelIds(data, providerKind) {
  if (!Array.isArray(data?.models) || data.models.some((model) => validateModel(model?.id))) {
    throw new Error(providerKind === 'codex' ? 'The API returned an invalid Codex model list. Reload the catalog or use the backend default. No inference was requested.' : 'The API returned an invalid model list. You can still enter a model ID manually.');
  }
  return [...new Set(data.models.map((model) => model.id))].sort();
}

export async function request(path, { signal, method = 'GET', body, timeoutMs = 20000 } = {}) {
  const timeout = AbortSignal.timeout(timeoutMs);
  const githubPreview = path === '/api/github/preview';
  const modelsRequest = path.split('?')[0] === '/api/models';
  const codexModels = modelsRequest && new URLSearchParams(path.split('?')[1]).get('provider') === 'codex';
  const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;
  let response;
  try {
    response = await fetch(path, {
      method, signal: combined, credentials: 'same-origin',
      headers: method === 'POST' ? { 'Content-Type': 'application/json', 'X-Lucid-Request': 'local-ui' } : { Accept: 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new Error(timeout.aborted
      ? modelsRequest ? codexModels ? 'Codex model loading timed out. Check the backend, Codex CLI installation, and codex login, then reload the catalog. No inference was requested.' : 'Model loading timed out. Check the backend and provider connectivity, or enter a model ID manually.' : githubPreview ? 'Repository preview timed out. Check the backend and GitHub connectivity, then preview again. No model calls were requested.' : 'The local API timed out. Check the backend on 127.0.0.1:8765 and refresh history before retrying a scan.'
      : 'Cannot reach the local API. Start the backend on 127.0.0.1:8765, then retry.');
  }
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : null; } catch { /* Handled below, without displaying proxy HTML. */ }
  if (!response.ok) {
    if (modelsRequest) {
      // Only allowlisted status codes are interpreted; provider text stays hidden.
      const advice = new Map([
        [400, 'Provider rejected the request (HTTP 400). Check the configured base URL.'],
        [401, 'Provider authentication failed (HTTP 401). Check the API key and restart the backend.'],
        [402, 'Provider requires payment (HTTP 402). Check provider credits and billing.'],
        [403, 'Provider denied access (HTTP 403). Check key validity and endpoint permissions. Requesty requires a Requesty API key, not a ChatGPT subscription credential.'],
        [404, 'Provider endpoint was not found (HTTP 404). Check the configured base URL.'],
        [429, 'Provider rate or quota limit reached (HTTP 429). Check provider credits and limits.'],
        [500, 'Provider service failed (HTTP 500). Check provider service status.'],
        [502, 'Provider service failed (HTTP 502). Check provider service status.'],
        [503, 'Provider service failed (HTTP 503). Check provider service status.'],
        [504, 'Provider service timed out (HTTP 504). Try loading models later.'],
      ]).get(data?.provider_status);
      if (data?.provider_error_code === 'codex' || codexModels) throw new Error(`API ${response.status}: Could not load Codex models. Check Codex CLI installation and run codex login on the backend, then reload the catalog. Provider error details are hidden. No inference was requested.`);
      if (data?.provider_error_code === 'kiro') throw new Error(`API ${response.status}: Could not load Kiro models. Check Kiro CLI installation and run kiro-cli login on the backend. The CLI must support ACP and expose a model catalog. No inference was requested.`);
      throw new Error(`API ${response.status}: Could not load models. ${advice || 'Check the backend and provider configuration, or enter a model ID manually. Provider error details are hidden.'} No inference was requested.`);
    }
    const detail = typeof data?.detail === 'string' ? data.detail : typeof data?.error === 'string' ? data.error : Array.isArray(data?.detail) ? data.detail.map((item) => item.msg).join('; ') : '';
    const action = response.status === 403 ? 'Use the local UI origin and check the backend CSRF configuration.'
      : githubPreview ? 'Check the public repository root URL, ref, and subdirectory, and the backend’s GitHub connectivity, then preview again. No model calls were requested.'
      : response.status === 401 ? 'Configure the provider key on the backend, then refresh configuration.'
      : response.status === 413 ? 'Select fewer or smaller source files.'
      : response.status === 404 ? 'Refresh audit history; this job may no longer exist.'
      : response.status >= 500 ? 'Check the backend logs and provider configuration, then retry.'
      : 'Review the source selection and audit settings, then retry.';
    throw new Error(`API ${response.status}${detail ? `: ${detail}` : ''}. ${action}`);
  }
  if (data === undefined || data === null) throw new Error('The API returned no JSON data. Check the backend and the /api proxy configuration.');
  return data;
}
