import { useEffect, useMemo, useRef, useState } from 'react';
import { MODES, SEVERITIES, callCount, charCount, filterFindings, isActive, modelIds, parseRoute, request, requestRounds, routeHref, validateModel, validateFiles, validateGithubPreview, validateGithubUrl } from './lib.js';

function Icon({ name, size = 20, ...props }) {
  const paths = {
    shield: <><path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z" /><path d="m8 12 3 3 5-6" /></>,
    plus: <path d="M12 5v14M5 12h14" />,
    arrow: <path d="M4 12h16m-6-6 6 6-6 6" />,
    file: <><path d="M14 2H5v20h14V7zM14 2v5h5M8 12h8M8 16h6" /></>,
    upload: <><path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5" /></>,
    layers: <><path d="m12 3 10 5-10 5L2 8zM2 12l10 5 10-5M2 16l10 5 10-5" /></>,
    specialists: <><circle cx="12" cy="12" r="3" /><path d="M12 2v4M12 18v4M2 12h4M18 12h4M5 5l3 3M16 16l3 3M5 19l3-3M16 8l3-3" /></>,
    loop: <><path d="M20 7a9 9 0 0 0-15-2L2 8m0-5v5h5M4 17a9 9 0 0 0 15 2l3-3m0 5v-5h-5" /></>,
    clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
    download: <><path d="M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4" /></>,
    search: <><circle cx="10" cy="10" r="6" /><path d="m15 15 6 6" /></>,
    check: <path d="m5 12 4 4L19 6" />,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>{paths[name] || paths.shield}</svg>;
}
function Notice({ children, tone = 'error', onRetry }) {
  return <div className={`notice ${tone}`} role={tone === 'error' ? 'alert' : 'status'}><span>{children}</span>{onRetry && <button type="button" className="text-button" onClick={onRetry}>Retry</button>}</div>;
}
function Status({ status }) {
  return <span className={`status ${status}`}><span className={isActive({ status }) ? 'status-dot pulse' : 'status-dot'} />{status || 'Unknown'}</span>;
}
function displayDate(value) {
  if (!value || Number.isNaN(new Date(value).getTime())) return 'Just started';
  return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(new Date(value));
}

export default function App() {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  const [config, setConfig] = useState(null);
  const [configError, setConfigError] = useState('');

  const [jobs, setJobs] = useState([]);
  const [historyError, setHistoryError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [result, setResult] = useState(null);
  const [resultError, setResultError] = useState('');
  const [resultLoading, setResultLoading] = useState(false);
  const [resultRetry, setResultRetry] = useState(0);
  const [target, setTarget] = useState('bundled');
  const [mode, setMode] = useState('context');
  const [rounds, setRounds] = useState(3);
  const [provider, setProvider] = useState('auto');
  const [model, setModel] = useState('');
  const [jsonMode, setJsonMode] = useState(true);
  const [reasoningEffort, setReasoningEffort] = useState(null);
  const [models, setModels] = useState([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState('');
  const modelsRequest = useRef(null);
  const modelsVersion = useRef(0);
  const [files, setFiles] = useState([]);
  const [fileError, setFileError] = useState('');
  const [reading, setReading] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState('');
  const [github, setGithub] = useState({ url: '', ref: '', subdirectory: '', authenticated: false });
  const [preview, setPreview] = useState(null);
  const [githubPaths, setGithubPaths] = useState([]);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState('');
  const previewRequest = useRef(null);
  const previewVersion = useRef(0);
  const submitLock = useRef(false);
  const uploadVersion = useRef(0);
  const fileInput = useRef(null);
  const folderInput = useRef(null);
  const pageTitle = useRef(null);
  const retryAll = () => setRefresh((value) => value + 1);
  const upsertJob = (job) => setJobs((current) => {
    const next = [job, ...current.filter((item) => item.id !== job.id)];
    return next.sort((a, b) => (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0));
  });

  useEffect(() => {
    const onHash = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  useEffect(() => { pageTitle.current?.focus(); }, [route.type, route.id]);
  useEffect(() => () => { previewVersion.current++; previewRequest.current?.abort(); modelsVersion.current++; modelsRequest.current?.abort(); }, []);
  useEffect(() => { setConfirmed(false); setModels([]); setModelsError(''); if (config?.provider_kind === 'kiro') setReasoningEffort(null); }, [config?.model, config?.provider, config?.key_configured, config?.provider_ready]);
  useEffect(() => {
    const controller = new AbortController();
    request(provider === 'auto' ? '/api/config' : `/api/config?provider=${provider}`, { signal: controller.signal }).then((data) => { if (!controller.signal.aborted) { setConfig(data); setConfigError(''); } }).catch((error) => {
      if (!controller.signal.aborted) { setConfig(null); setConfigError(error.message); }
    });

    return () => controller.abort();
  }, [refresh, provider]);
  useEffect(() => {
    if (['kiro', 'codex'].includes(provider) && config?.provider_kind === provider && config?.provider_ready) loadModels();
    return () => { modelsVersion.current++; modelsRequest.current?.abort(); };
  }, [provider, config?.provider_kind, config?.provider_ready]);
  useEffect(() => {
    const controller = new AbortController();
    let timer;
    async function load() {
      try {
        const data = await request('/api/audits', { signal: controller.signal });
        if (!controller.signal.aborted) { setJobs(data); setHistoryError(''); }
      } catch (error) { if (!controller.signal.aborted) setHistoryError(error.message); }
      if (!controller.signal.aborted) timer = setTimeout(load, 5000);
    }
    load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [refresh]);
  useEffect(() => {
    const controller = new AbortController();
    let timer;
    setResult(null);
    setResultError('');
    if (route.type === 'new') { setResultLoading(false); return () => controller.abort(); }
    setResultLoading(true);
    async function load() {
      let retry = true;
      try {
        const data = await request(`/api/audits/${encodeURIComponent(route.id)}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setResult(data);
        setResultError('');
        upsertJob(data); retry = isActive(data);
      } catch (error) { if (!controller.signal.aborted) setResultError(error.message); }
      finally { if (!controller.signal.aborted) setResultLoading(false); }
      if (retry && !controller.signal.aborted) timer = setTimeout(load, 2500);
    }
    load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [route.type, route.id, resultRetry]);

  const activeJobs = jobs.filter(isActive);
  const calls = callCount(mode, rounds);
  const totalChars = useMemo(() => files.reduce((sum, file) => sum + charCount(file.content), 0), [files]);
  const scopedFiles = useMemo(() => preview?.files.filter((file) => githubPaths.includes(file.path)) || [], [preview, githubPaths]);
  const validationError = target === 'upload' ? validateFiles(files, config?.max_chars) : target === 'github' ? preview ? validateGithubPreview(preview, config?.max_chars) || validateFiles(scopedFiles, config?.max_chars) : 'Preview and review the repository before starting a live audit.' : null;
  const selectedModel = model.trim() || config?.model;
  const modelError = validateModel(selectedModel);
  const providerReady = config?.provider_ready ?? config?.key_configured;
  const isKiro = config?.provider_kind === 'kiro';
  const isCodex = config?.provider_kind === 'codex';
  const isCli = isKiro || isCodex;
  const cliName = isCodex ? 'Codex' : 'Kiro';
  const canStart = providerReady === true && confirmed && !modelError && !validationError && !reading && !previewLoading && !submitting;
  function clearPreview() {
    previewVersion.current++;
    previewRequest.current?.abort(); previewRequest.current = null;
    setPreview(null); setGithubPaths([]); setPreviewLoading(false); setPreviewError(''); setConfirmed(false); setSubmitError('');
  }
  function editGithub(field, value) {
    clearPreview();
    setGithub((current) => ({ ...current, [field]: value }));
  }
  async function previewRepository() {
    if (submitLock.current) return;
    clearPreview();
    const urlError = validateGithubUrl(github.url);
    if (urlError) { setPreviewError(urlError); return; }
    const version = previewVersion.current;
    const controller = new AbortController();
    previewRequest.current = controller;
    setPreviewLoading(true);
    try {
      const data = await request('/api/github/preview', { method: 'POST', body: github, signal: controller.signal, timeoutMs: 75000 });
      if (controller.signal.aborted || version !== previewVersion.current) return;
      const error = validateGithubPreview(data, config?.max_chars);
      if (error) throw new Error(error);
      setPreview(data); setGithubPaths(data.files.map((file) => file.path)); setConfirmed(false);
    } catch (error) {
      if (!controller.signal.aborted && version === previewVersion.current) setPreviewError(error.message);
    } finally {
      if (version === previewVersion.current && !controller.signal.aborted) { setPreviewLoading(false); previewRequest.current = null; }
    }
  }
  function clearModelRequest() {
    modelsVersion.current++;
    modelsRequest.current?.abort(); modelsRequest.current = null;
    setModelsLoading(false); setModelsError('');
  }
  function selectProvider(value) {
    if (submitLock.current) return;
    clearModelRequest(); setModels([]); setModel(''); setReasoningEffort(null);
    setConfirmed(false); setSubmitError(''); setConfig(null); setConfigError('');
    setProvider(value);
  }
  async function loadModels() {
    if (submitLock.current) return;
    clearModelRequest();
    const version = modelsVersion.current;
    const controller = new AbortController();
    modelsRequest.current = controller;
    setModelsLoading(true);
    try {
      const data = await request(provider === 'auto' ? '/api/models' : `/api/models?provider=${provider}`, { signal: controller.signal });
      if (!controller.signal.aborted && version === modelsVersion.current) setModels(modelIds(data, config?.provider_kind));
    } catch (error) {
      if (!controller.signal.aborted && version === modelsVersion.current) setModelsError(error.message);
    } finally {
      if (!controller.signal.aborted && version === modelsVersion.current) { setModelsLoading(false); modelsRequest.current = null; }
    }
  }
  function reset() {
    if (submitLock.current) return;
    uploadVersion.current++;
    clearPreview(); setGithub({ url: '', ref: '', subdirectory: '', authenticated: false });
    clearModelRequest(); setModel(''); setJsonMode(true); setReasoningEffort(null);
    setTarget('bundled'); setMode('context'); setRounds(3); setFiles([]); setFileError('');
    setConfirmed(false); setSubmitError(''); setReading(false);
    window.location.hash = routeHref('new');
  }
  function changeSetting(fn) { fn(); setConfirmed(false); setSubmitError(''); }
  async function readSources(selected, folder = false) {
    const version = ++uploadVersion.current;
    setReading(true); setFileError(''); setFiles([]); setConfirmed(false);
    try {
      const all = Array.from(selected || []);
      const solidity = all.filter((file) => file.name.toLowerCase().endsWith('.sol'));
      if (!folder && solidity.length !== all.length) throw new Error('Only .sol files are accepted. Choose Solidity source files.');
      if (!solidity.length) throw new Error('No .sol files found. Choose Solidity files or a folder containing them.');
      // Avoid reading arbitrarily large local files into memory before checking the API limit.
      const byteLimit = config?.max_chars > 0 ? config.max_chars * 4 : 20_000_000;
      if (solidity.reduce((sum, file) => sum + file.size, 0) > byteLimit) throw new Error('Selection is too large. Choose fewer or smaller Solidity files.');
      const sources = await Promise.all(solidity.map(async (file) => ({ path: file.webkitRelativePath || file.name, content: await file.text() })));
      sources.sort((a, b) => a.path.localeCompare(b.path));
      const error = validateFiles(sources, config?.max_chars);
      if (error) throw new Error(error);
      if (version === uploadVersion.current) setFiles(sources);
    } catch (error) { if (version === uploadVersion.current) setFileError(error.message); }
    finally { if (version === uploadVersion.current) setReading(false); }
  }
  async function startAudit(event) {
    event.preventDefault();
    if (!canStart || submitLock.current) return;
    submitLock.current = true; setSubmitting(true); setSubmitError('');
    try {
      const job = await request('/api/audits', { method: 'POST', body: { ...(provider === 'auto' ? {} : { provider }), mode, rounds: requestRounds(mode, rounds), model: selectedModel, json_mode: jsonMode, reasoning_effort: reasoningEffort, confirmed_paid: true, target, files: target === 'upload' ? files : [], ...(target === 'github' ? { github_preview_id: preview.id, github_paths: githubPaths } : {}) } });
      if (!job.id) throw new Error('The API did not return a job ID. Refresh history before submitting again.');
      upsertJob(job); setConfirmed(false); window.location.hash = routeHref('audit', job.id);
      retryAll();
    } catch (error) {
      setSubmitError(`${error.message} A request may already have been accepted; check audit history before starting another paid scan.`);
      setConfirmed(false); retryAll();
    } finally { submitLock.current = false; setSubmitting(false); }
  }

  return <div className="app-shell">
    <a className="skip-link" href="#main-content" onClick={(event) => { event.preventDefault(); document.getElementById('main-content')?.focus(); }}>Skip to workspace</a>
    <aside className="sidebar" aria-label="Workspace navigation">
      <a href="#/new" className="brand"><span className="brand-mark"><Icon name="shield" size={25} /></span><span>lucid<span className="brand-caption">SECURITY WORKSPACE</span></span></a>
      <div className="workspace-label"><span className="status-dot" />Local workspace<span className="keyboard-hint">MAC</span></div>
      <button className="new-audit-button" onClick={reset} disabled={submitting}><Icon name="plus" />New audit</button>
      <div className="nav-section-title"><span>Audit history</span><button className="icon-button" onClick={retryAll} aria-label="Refresh history and configuration"><Icon name="loop" size={15} /></button></div>
      {historyError && <Notice onRetry={retryAll}>{historyError}</Notice>}
      {!jobs.length && !historyError && <p className="sidebar-empty">Your live audits will appear here. Jobs keep running when you switch views.</p>}
      <nav className="history-list" aria-label="Audit history">{jobs.map((job) => <a key={job.id} href={routeHref('audit', job.id)} className={`history-item ${route.type === 'audit' && route.id === job.id ? 'selected' : ''}`} aria-current={route.type === 'audit' && route.id === job.id ? 'page' : undefined}>
        <span className="history-icon"><Icon name={job.mode === 'specialists' ? 'specialists' : job.mode === 'loop' ? 'loop' : 'shield'} size={17} /></span><span className="history-copy"><strong>{MODES.find((item) => item.id === job.mode)?.title || 'Live audit'}</strong><small>{displayDate(job.created_at)}</small><Status status={job.status} /></span>
      </a>)}</nav>

      <div className="sidebar-footer"><Icon name="shield" size={18} /><div>Keys stay on your backend<small>Live scans use your AI provider.</small></div></div>
    </aside>
    <div className="main-shell">
      <header className="topbar"><div><span className="muted">Workspace</span><span className="breadcrumb-divider">/</span><span>{route.type === 'new' ? 'New audit' : 'Audit report'}</span></div><span className={`connection ${config ? 'online' : ''}`}><span className="status-dot" />{config ? 'Local API connected' : configError ? 'API unavailable' : 'Connecting to API'}</span></header>
      <main id="main-content" tabIndex={-1}>
        <div className="page-heading"><div><div className="eyebrow"><span />SMART CONTRACT INTELLIGENCE</div><h1 ref={pageTitle} tabIndex={-1}>{route.type === 'new' ? 'A clearer view of your contracts.' : 'Your audit, in focus.'}</h1><p>{route.type === 'new' ? 'Understand the protocol. Surface vulnerabilities. Follow the evidence.' : 'Review structured findings, trace their impact, and plan your next step.'}</p></div><div className="heading-emblem"><Icon name="shield" size={46} /></div></div>
        {configError && <Notice onRetry={retryAll}>{configError}</Notice>}
        {activeJobs.length > 0 && <div className="active-banner"><span className="status-dot pulse" /><span>{activeJobs.length} active audit{activeJobs.length > 1 ? 's' : ''}. Switching views does not cancel a scan.</span><a href={routeHref('audit', activeJobs[0].id)}>View active audit <span aria-hidden="true">↗</span></a></div>}
        {route.type === 'new' ? <>
          <div className="config-strip"><div><span className="meta-label">MODEL</span><strong>{selectedModel || (config ? 'No model selected' : 'Waiting for backend')}</strong></div><div><span className="meta-label">PROVIDER</span><strong>{config?.provider || '—'}</strong></div><div><span className="meta-label">{isCli ? `${cliName.toUpperCase()} CLI` : 'BACKEND KEY'}</span><strong className={providerReady ? 'accent' : 'amber'}>{config ? providerReady ? isCli ? 'Detected · login required' : 'Configured' : 'Not configured' : 'Unknown'}</strong></div></div>
          {config && !providerReady && <Notice tone="warning" onRetry={retryAll}>Live audits are locked. Configure a provider key or install and sign in to Kiro or Codex CLI on the backend, then refresh. You can still preview public GitHub repositories—no key or paid model calls needed.</Notice>}
          <form onSubmit={startAudit} className="audit-form">
            <section className="panel source-panel"><div className="section-heading"><span className="step-number">01</span><div><h2>Select your source</h2><p>Choose the contracts you want to investigate.</p></div><span className="section-tag">SOLIDITY</span></div>
              <fieldset disabled={submitting}><legend className="sr-only">Source selection</legend><div className="segmented-control">{[['bundled', 'Bundled target', 'layers'], ['upload', 'Upload contracts', 'upload'], ['github', 'GitHub repository', 'layers']].map(([value, title, icon]) => <label key={value} className={target === value ? 'active' : ''}><input type="radio" name="target" value={value} checked={target === value} onChange={() => changeSetting(() => { clearPreview(); setTarget(value); })} /><Icon name={icon} size={17} />{title}</label>)}</div></fieldset>
              {target === 'bundled' ? <div className="bundled-source"><span className="source-icon"><Icon name="layers" size={29} /></span><div><h3>{config?.default_target || 'Bundled Solidity target'}</h3><p>Read directly by the local backend. No upload required.</p><span className="file-count"><Icon name="file" size={13} />{config ? `${config.default_file_count} source files` : 'Loading source details'}</span></div><span className="source-check"><Icon name="check" size={19} /></span></div> : target === 'github' ? <div className="github-source">
                              <fieldset disabled={submitting} className="github-fields"><legend className="sr-only">GitHub repository settings</legend>
                                <label className="github-url" htmlFor="github-url">Repository URL<input id="github-url" type="url" value={github.url} placeholder="https://github.com/owner/repo" aria-describedby="github-help" onChange={(event) => editGithub('url', event.target.value)} /></label>
                                <label htmlFor="github-ref">Ref <span className="muted">(optional)</span><input id="github-ref" type="text" value={github.ref} placeholder="Default branch" onChange={(event) => editGithub('ref', event.target.value)} /></label>
                                <label htmlFor="github-subdirectory">Subdirectory <span className="muted">(optional)</span><input id="github-subdirectory" type="text" value={github.subdirectory} placeholder="contracts/src" onChange={(event) => editGithub('subdirectory', event.target.value)} /></label>
                              </fieldset>
                              <label className="github-auth" htmlFor="github-auth"><input id="github-auth" type="checkbox" checked={github.authenticated} disabled={submitting} onChange={(event) => editGithub('authenticated', event.target.checked)} /><span>Use backend GitHub token <small>Required for private repositories. Configure LUCID_GITHUB_TOKEN on the backend with read-only access to this repository; never paste it into the URL.</small></span></label>
                                                            <p id="github-help" className="github-help">Public and private GitHub repositories are supported. Use the root URL https://github.com/owner/repo, not a file or tree URL. Leave ref empty for the default branch. Preview fetches source over the network; it makes no model calls and needs no AI provider key. The GitHub token stays on the backend and is used only when the option above is checked. Auditing sends selected source, including private code, to your AI provider after your consent.</p>
                              <button type="button" className="secondary-button" disabled={submitting || previewLoading || !github.url} onClick={previewRepository}><Icon name="search" size={16} />{previewLoading ? 'Previewing repository…' : 'Preview repository'}</button>
                              {previewLoading && <p role="status" className="github-help">Fetching and pinning Solidity source. This may take up to 60 seconds. Editing any field cancels this preview.</p>}
                              {previewError && <Notice onRetry={previewRepository}>{previewError}</Notice>}
                              {preview && <GithubPreview preview={preview} paths={githubPaths} disabled={submitting} onScopeChange={(paths) => { if (submitLock.current) return; setGithubPaths(paths); setConfirmed(false); setSubmitError(''); }} />}
                                                            {preview && validationError && <Notice>{validationError}</Notice>}
                            </div> : <div className="upload-source">
                <Icon name="upload" size={29} /><h3>Bring your Solidity source</h3><p>Select multiple .sol files, or a folder to preserve relative paths.</p><div className="upload-actions"><button type="button" className="secondary-button" disabled={reading || submitting} onClick={() => fileInput.current?.click()}>Choose .sol files</button><button type="button" className="text-button" disabled={reading || submitting} onClick={() => folderInput.current?.click()}>Choose folder</button></div>
                <input ref={fileInput} type="file" disabled={reading || submitting} multiple accept=".sol" className="sr-only" tabIndex={-1} aria-label="Upload Solidity files" onChange={(event) => { readSources(event.target.files); event.target.value = ''; }} />
                <input ref={folderInput} type="file" disabled={reading || submitting} multiple webkitdirectory="" className="sr-only" tabIndex={-1} aria-label="Upload folder of Solidity files" onChange={(event) => { readSources(event.target.files, true); event.target.value = ''; }} />
                <span className="upload-limit">{config?.max_chars ? `${config.max_chars.toLocaleString()} character limit` : 'Backend source limit applies'} · Only .sol files are included</span>
              </div>}
              {reading && <p role="status" className="muted">Reading local source files…</p>}
              {fileError && <Notice>{fileError}</Notice>}
              {target === 'upload' && files.length > 0 && <div className="file-manifest"><div className="manifest-heading"><strong>{files.length} files selected <span className="muted">· {totalChars.toLocaleString()} characters</span></strong><button type="button" className="text-button" disabled={submitting} onClick={() => changeSetting(() => setFiles([]))}>Clear</button></div><ul>{files.map((file) => <li key={file.path}><Icon name="file" size={14} /><code>{file.path}</code><small>{charCount(file.content).toLocaleString()} chars</small></li>)}</ul></div>}
              {target === 'upload' && files.length > 0 && validationError && <Notice>{validationError}</Notice>}
            </section>
            <section className="panel"><div className="section-heading"><span className="step-number">02</span><div><h2>Choose an audit strategy</h2><p>Go from a focused pass to a deeper, iterative investigation.</p></div></div>
              <fieldset disabled={submitting}><legend className="sr-only">Audit mode</legend><div className="mode-grid">{MODES.map((item) => <label key={item.id} className={`mode-card ${mode === item.id ? 'active' : ''}`}><input type="radio" name="mode" value={item.id} checked={mode === item.id} onChange={() => changeSetting(() => { setMode(item.id); if (item.id === 'specialists') setRounds(1); })} /><div className="mode-card-top"><Icon name={item.id === 'single' ? 'shield' : item.id === 'context' ? 'layers' : item.id === 'specialists' ? 'specialists' : 'loop'} size={23} /><span className="radio-indicator" /></div><h3>{item.title}</h3><p>{item.description}</p><span className="mode-detail">{item.detail}</span>{item.id === 'context' && <span className="recommended">RECOMMENDED</span>}</label>)}</div>
              {['loop', 'specialists'].includes(mode) && <div className="rounds-control"><label htmlFor="rounds">{mode === 'specialists' ? 'Audit rounds per lane' : 'Audit rounds'} <small>{mode === 'specialists' ? `${rounds} rounds in each of 6 parallel lanes (${6 * rounds} lane rounds total).` : `One context call, followed by ${rounds} finding rounds.`}</small></label><input id="rounds" type="range" min="1" max="10" step="1" value={rounds} onChange={(event) => changeSetting(() => setRounds(Number(event.target.value)))} /><output htmlFor="rounds">{rounds}</output></div>}</fieldset>
              {mode === 'specialists' && <p className="model-help">Six lanes run concurrently: provider rate limits may cause failures, and more rounds increase cost. The model judge is not human verification.</p>}
            </section>
            <section className="panel model-panel"><div className="section-heading"><span className="step-number">03</span><div><h2>Choose a model</h2><p>{isCodex ? 'Use the backend default or select a Codex catalog model for this audit.' : 'Use the backend default or enter a model ID for this audit.'}</p></div></div>
              <fieldset className="model-controls" disabled={submitting}><legend className="sr-only">Model settings</legend>
                <div className="model-options"><label htmlFor="audit-provider">Model provider<select id="audit-provider" value={provider} onChange={(event) => selectProvider(event.target.value)}><option value="auto">Automatic (backend configuration)</option><option value="api">API / Requesty</option><option value="kiro">Kiro CLI</option><option value="codex">Codex CLI</option></select></label>
                {isCli && <label htmlFor={`${config.provider_kind}-model`}>Available {cliName} models<select id={`${config.provider_kind}-model`} value={models.includes(model) ? model : ''} disabled={modelsLoading || !models.length} onChange={(event) => changeSetting(() => setModel(event.target.value))}><option value="">{modelsLoading ? `Loading ${cliName} models…` : `${cliName} default model`}</option>{models.map((id) => <option key={id} value={id}>{id}</option>)}</select></label>}</div>
                <div className="model-picker">{!isCodex && <label htmlFor="audit-model"><span>Model ID <span className="muted">(blank uses backend default)</span></span><input id="audit-model" type="text" list="available-models" value={model} placeholder={config?.model || 'Backend default'} aria-describedby="model-help" aria-invalid={!!modelError} onChange={(event) => changeSetting(() => setModel(event.target.value))} /></label>}<button type="button" className="secondary-button" disabled={submitting || modelsLoading} onClick={loadModels}><Icon name="search" size={16} />{modelsLoading ? 'Loading models…' : 'Load models'}</button></div>
                <datalist id="available-models">{models.map((id) => <option key={id} value={id} />)}</datalist>
                <div className="model-options"><label htmlFor="json-mode">JSON mode<select id="json-mode" value={String(jsonMode)} onChange={(event) => changeSetting(() => setJsonMode(event.target.value === 'true'))}><option value="true">Enabled (default)</option><option value="false">Disabled</option></select></label><label htmlFor="reasoning-effort">Reasoning effort<select id="reasoning-effort" disabled={isKiro} value={reasoningEffort || ''} onChange={(event) => changeSetting(() => setReasoningEffort(event.target.value || null))}><option value="">Provider default</option><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option></select></label></div>
              </fieldset>
              {isKiro && <p className="model-help">Using Kiro CLI via ACP. Selecting Kiro loads available models automatically; the reload button can refresh them. Run kiro-cli login on the backend, then Load models to verify access. Kiro subscription credits apply. JSON mode is a prompt instruction; reasoning effort overrides are unsupported. Audits use a no-tools agent profile, not an OS sandbox; Kiro may save prompts in its session history.</p>}
              {isCodex && <p className="model-help">Using Codex CLI. Run codex login on the backend, then Load models to verify access. Configuration and model listing request no inference. Audits use fresh ephemeral threads with built-in tools disabled and a read-only sandbox, not full OS isolation; inherited MCP servers and CLI configuration may still apply. JSON mode is a prompt instruction. Codex supports low, medium, or high reasoning effort; the backend validates support against the model catalog. CLI internal retries may consume usage.</p>}
              <p id="model-help" className="model-help">{isCodex ? 'Selecting Codex loads its catalog automatically; Load models refreshes it. Choose a catalog model ID or use the backend default. Model listing requests no inference.' : 'API model lists are fetched only when you click Load models; choosing Kiro loads its catalog automatically. The backend may contact your provider. Manual IDs work without loading the list.'} All context, finding rounds, and any specialist judge use the same selected model. JSON mode is optional; finding responses are still parsed and validated regardless.</p>
              <p className="model-help">Capabilities, JSON/reasoning support, pricing, and context limits vary by model and provider. No automatic paid compatibility retries are requested by this UI.</p>
              {modelsLoading && <p role="status" className="model-help">Loading provider model IDs… {isCodex ? 'Wait for the Codex catalog or use the backend default.' : 'You can still enter a model manually.'}</p>}
              {!modelsLoading && models.length > 0 && <p role="status" className="model-help">{models.length} model IDs available. {isCodex ? 'Select a Codex model above.' : 'Type to search suggestions.'}</p>}
              {modelsError && <Notice>{modelsError}</Notice>}
              {modelError && <Notice>{isCodex ? 'Select a valid Codex catalog model or configure a valid backend default.' : modelError}</Notice>}
            </section>
            <section className="panel launch-panel"><div className="section-heading"><span className="step-number">04</span><div><h2>Review & launch</h2><p>You’re always in control of paid model calls.</p></div></div><div className="cost-notice"><Icon name="clock" size={22} /><div><strong>{mode === 'specialists' ? `Up to ${calls} paid model calls` : `${calls} paid model call${calls === 1 ? '' : 's'} planned`}</strong><p>{mode === 'single' ? 'One vulnerability scan.' : mode === 'context' ? 'One protocol-context call + one vulnerability scan.' : mode === 'specialists' ? `One protocol-context call + 6 parallel lanes × ${rounds} rounds per lane + up to 1 AI judge call (skipped if there are no candidates).` : `One protocol-context call + ${rounds} vulnerability scans.`} Provider usage charges apply; retries may add calls. This is not a price estimate.</p></div></div>
              <label className="confirmation"><input type="checkbox" checked={confirmed} id="paid-consent" disabled={submitting || !providerReady || !!modelError || (target === 'github' && (!preview || previewLoading || !!validationError))} onChange={(event) => setConfirmed(event.target.checked)} /><span>{isCodex ? 'I authorize this live audit and its Codex usage (subscription or API billing).' : isKiro ? 'I authorize this live audit and its Kiro subscription usage.' : 'I authorize this live audit and its paid API usage.'}<small>Selected source and generated context will be sent by the backend to {config?.provider || 'the configured AI provider'} using model {selectedModel || '—'}. Credentials stay on the backend.</small></span></label>
              {submitError && <Notice>{submitError}</Notice>}
              <div className="launch-footer"><span><Icon name="shield" size={16} />AI findings require human verification.</span><button type="submit" className="primary-button" disabled={!canStart}>{submitting ? 'Starting audit…' : 'Start live audit'}<Icon name="arrow" size={18} /></button></div>
              {!canStart && !submitting && <p className="launch-help">{!config ? 'Connect to the backend to enable live audits.' : !providerReady ? 'Configure a backend provider key or install and sign in to Kiro or Codex CLI for live audits.' : reading ? 'Wait for the source files to finish loading.' : (modelError ? isCodex ? 'Select a valid Codex catalog model or configure a valid backend default.' : modelError : null) || validationError || (isCodex ? 'Confirm Codex usage (subscription or API billing) above to start.' : 'Confirm paid API usage above to start.')}</p>}
            </section>
          </form>
        </> : <>
          {resultLoading && <div className="panel loading-state" role="status"><span className="loader" />Loading audit report…</div>}
          {resultError && <Notice onRetry={() => setResultRetry((value) => value + 1)}>{resultError}{result && ' Showing the last successfully received data.'}</Notice>}
          {result && <AuditResults key={`${route.type}:${route.id}`} data={result} id={route.id} onNew={reset} submitting={submitting} />}
        </>}
        <footer className="main-footer"><span>lucid <span className="muted">/ AI-assisted security, with evidence.</span></span><span>Local interface · Remote model calls for live audits</span></footer>
      </main>
    </div>
  </div>;
}

function RepositoryMetadata({ source }) {
  const safeUrl = typeof source.repository_url === 'string' && !validateGithubUrl(source.repository_url) ? source.repository_url.replace(/\/$/, '') : null;
  const commitUrl = safeUrl && /^[a-fA-F0-9]{40}$/.test(source.commit || '') ? `${safeUrl}/commit/${source.commit}` : null;
  return <><dl className="report-meta repository-meta">
    <div><dt>Repository</dt><dd>{safeUrl ? <a href={safeUrl} target="_blank" rel="noopener noreferrer">{source.repository || source.repository_url}</a> : source.repository || 'Not recorded'}</dd></div>
    <div><dt>Pinned commit</dt><dd>{commitUrl ? <a href={commitUrl} target="_blank" rel="noopener noreferrer"><code>{source.commit}</code></a> : source.commit || 'Not recorded'}</dd></div>
    <div><dt>Ref / subdirectory</dt><dd>{source.ref || 'Default branch'} · {source.subdirectory || 'Repository root'}</dd></div>
  </dl>{Array.isArray(source.scope_paths) && <details className="report-scope"><summary>Audit scope · {source.scope_paths.length} Solidity files</summary><ul>{source.scope_paths.map((path) => <li key={path}><code>{path}</code></li>)}</ul></details>}</>;
}
function GithubPreview({ preview, paths, disabled, onScopeChange }) {
  const selected = preview.files.filter((file) => paths.includes(file.path));
  const selectedChars = selected.reduce((sum, file) => sum + charCount(file.content), 0);
  return <div className="github-preview">
    <h3>Repository preview · pinned source snapshot</h3>
    <RepositoryMetadata source={preview} />
    <fieldset disabled={disabled} className="scope-selection">
      <legend>Contracts in audit scope</legend>
      <p className="github-help">Only checked Solidity files are sent to the model, including for protocol context and judging. Include relevant dependencies manually; imports are not added automatically. Changing scope clears paid consent.</p>
      <div className="scope-actions"><button type="button" className="secondary-button" onClick={() => onScopeChange(preview.files.map((file) => file.path))}>Select all</button><button type="button" className="secondary-button" onClick={() => onScopeChange([])}>Clear selection</button></div>
      <div className="file-manifest"><div className="manifest-heading"><strong>{selected.length} of {preview.file_count} files in scope <span className="muted">· {selectedChars.toLocaleString()} characters</span></strong></div><ul>{preview.files.map((file) => <li key={file.path}><label className="scope-file"><input type="checkbox" checked={paths.includes(file.path)} aria-label={`Include ${file.path} in audit scope`} onChange={(event) => onScopeChange(event.target.checked ? [...paths, file.path] : paths.filter((path) => path !== file.path))} /><code>{file.path}</code></label><small>{charCount(file.content).toLocaleString()} chars</small></li>)}</ul></div>
    </fieldset>
    <p className="github-help">Review the source below before authorizing paid usage. Unchecked files are out of scope. The audit uses this pinned preview, not a moving branch.</p>
    <div className="source-review">{preview.files.map((file) => <details key={file.path}><summary><Icon name="file" size={14} /><code>{file.path}</code><span className="accordion-chevron" aria-hidden="true">⌄</span></summary><pre aria-label={`Source of ${file.path}`}><code>{file.content.split('\n').map((line, index) => <span className="source-line" key={index}><span className="line-number" aria-hidden="true">{index + 1}</span><span>{line || '\u00a0'}</span></span>)}</code></pre></details>)}</div>
  </div>;
}

function SpecialistStages({ data }) {
  const lanes = Array.isArray(data.lanes) ? data.lanes : [];
  return <section className="panel specialist-stages" aria-labelledby="specialist-stages-heading">
    <h2 id="specialist-stages-heading">Specialist lanes & AI judge</h2>
    {lanes.length > 0 ? <div className="lane-grid">{lanes.map((lane) => <div className="lane-progress" key={lane.key}>
      <div className="lane-heading"><h3>{lane.name || lane.key}</h3><Status status={lane.status} /></div>
      <p>{lane.completed_rounds} / {lane.rounds} rounds · {lane.findings_count} raw candidates</p>
      <progress aria-label={`${lane.name || lane.key} completed rounds`} max={Math.max(1, lane.rounds)} value={lane.completed_rounds} />
      {lane.error && <Notice>{lane.error}</Notice>}
    </div>)}</div> : <p className="model-help">Lane progress has not been returned yet.</p>}
    <div className="judge-stage"><h3>AI judge stage</h3>{data.judge_status ? <Status status={data.judge_status} /> : <span className="muted">Status not recorded</span>}</div>
    <p className="model-help">{data.judge_status === 'skipped' ? data.status === 'failed' ? 'Judge skipped: the audit was incomplete; any raw candidates remain unjudged. ' : 'Judge skipped: no candidates to review. ' : data.judge_status === 'failed' ? 'Judge failed: raw candidates remain unjudged. ' : ''}The model judge is not human verification.</p>
  </section>;
}

function LaneCandidates({ data }) {
  const candidatesByLane = data.findings_by_lane && typeof data.findings_by_lane === 'object' ? data.findings_by_lane : {};
  const lanes = Array.isArray(data.lanes) ? data.lanes : [];
  const keys = [...new Set([...lanes.map((lane) => lane.key), ...Object.keys(candidatesByLane)])];
  return <section className="panel lane-candidates" aria-labelledby="lane-candidates-heading">
    <h2 id="lane-candidates-heading">Raw lane candidates · Unjudged</h2>
    <p className="model-help">Original model claims, not the AI-judged report and not verified vulnerabilities. Candidates may overlap or contain duplicates across lanes and rounds, or be rejected by the judge. On failure, they remain unjudged; do not treat them as accepted findings.</p>
    {!keys.length && <p className="model-help">No lane candidates were included in this output.</p>}
    {keys.map((key) => {
      const candidates = Array.isArray(candidatesByLane[key]) ? candidatesByLane[key] : [];
      const name = lanes.find((lane) => lane.key === key)?.name || key;
      return <details className="candidate-lane" key={key}><summary><strong>{name}</strong><span>{candidates.length} raw candidates · Unjudged</span><span className="accordion-chevron" aria-hidden="true">⌄</span></summary>
        {candidates.length ? candidates.map((candidate, index) => <div className="candidate-evidence" key={index}><h3>Candidate {index + 1}{candidate?.title ? ` · ${candidate.title}` : ''}</h3><pre aria-label={`${name} candidate ${index + 1} plain text evidence`}>{JSON.stringify(candidate, null, 2)}</pre></div>) : <p className="model-help">No candidates returned for this lane.</p>}
      </details>;
    })}
  </section>;
}

function AuditResults({ data, id, onNew, submitting }) {
  const [severity, setSeverity] = useState('all');
  const [query, setQuery] = useState('');
  const [exportError, setExportError] = useState('');
  const specialists = data.mode === 'specialists';
  const findings = Array.isArray(data.findings) ? data.findings : [];
  const visible = filterFindings(findings, severity, query);
  const active = isActive(data);
  const totalRounds = Math.max(1, specialists ? data.total_rounds || 6 * (data.rounds || 1) : data.rounds || 1);
  const completedRounds = Math.min(totalRounds, Math.max(0, data.completed_rounds || 0));
  const finished = data.status === 'completed';
  function exportJson() {
    try {
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
      const anchor = document.createElement('a');
      anchor.href = url; anchor.download = `lucid-audit-${id.replace(/[^a-zA-Z0-9_-]/g, '_')}.json`;
      document.body.appendChild(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      setExportError('');
    } catch { setExportError('Could not export the report. Check browser download permissions and try again.'); }
  }
  return <div className="results">

    <section className="panel report-overview"><div className="report-heading"><div><div className="eyebrow">LIVE AUDIT</div><h2>{MODES.find((item) => item.id === data.mode)?.title || 'Audit report'}</h2><code className="report-id">{id}</code></div><div className="report-actions"><Status status={data.status} /><button className="secondary-button" onClick={exportJson}><Icon name="download" size={16} />Export JSON</button><button className="text-button" onClick={onNew} disabled={submitting}>New audit</button></div></div>
      <dl className="report-meta"><div><dt>Model</dt><dd>{data.model || 'Not recorded'}</dd></div><div><dt>Target</dt><dd>{data.target || 'Not recorded'}</dd></div><div><dt>Started</dt><dd>{displayDate(data.created_at)}</dd></div></dl>
      {data.source && <RepositoryMetadata source={data.source} />}
      {active && <div className="progress-block"><div className="progress-label" role="status"><span><span className="status-dot pulse" />{data.stage ? data.stage.replaceAll('_', ' ') : data.status === 'queued' ? 'Waiting in the queue' : 'Audit in progress'}</span><strong>{completedRounds} / {totalRounds} {specialists ? 'lane rounds' : 'rounds'}</strong></div><progress aria-label="Completed audit rounds" max={totalRounds} value={completedRounds} /><p>Refreshes automatically every 2.5 seconds. You can leave this view and return through audit history. There is no cancel endpoint.</p></div>}
      {data.status === 'failed' && <Notice>Audit failed: {data.error || 'No error details returned.'} Check backend logs, the provider configuration, and source limits. {specialists ? 'Raw lane candidates below are unjudged and may contain duplicates; they are not the judged report.' : 'Review any partial findings below before authorizing a new scan.'}</Notice>}
      {data.error && data.status !== 'failed' && <Notice>{data.error}</Notice>}
      {data.warnings?.length > 0 && <div className="notice warning"><div><strong>Backend warnings</strong><ul>{data.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div></div>}
      {exportError && <Notice>{exportError}</Notice>}

    </section>
    {specialists && <SpecialistStages data={data} />}
    <div className="severity-summary"><div className="summary-total"><span>{findings.length}</span><div>{specialists ? 'AI-judged findings' : 'Findings'}<small>{active ? 'Partial results · audit running' : finished ? 'Ready for review' : 'Partial audit output'}</small></div></div>{SEVERITIES.map((level) => <button type="button" key={level} className={`summary-severity ${level} ${severity === level ? 'selected' : ''}`} aria-pressed={severity === level} onClick={() => setSeverity(severity === level ? 'all' : level)}><span>{findings.filter((finding) => finding.severity === level).length}</span><small><span className="severity-dot" />{level}</small></button>)}</div>
    {data.context ? <details className="panel context-panel"><summary><span className="context-icon"><Icon name="layers" /></span><span><strong>Protocol context</strong><small>Actors, invariants, and dependencies used to inform this audit.</small></span><span className="accordion-chevron" aria-hidden="true">⌄</span></summary><pre className="context-content">{data.context}</pre></details> : <div className="context-unavailable"><Icon name="layers" size={16} />{active ? 'Protocol context will appear here when available.' : 'No protocol context was included in this report.'}</div>}
    <section className="findings-section" aria-labelledby="findings-heading"><div className="findings-heading"><div><h2 id="findings-heading">{specialists ? 'AI-judged findings · Not verified' : 'Security findings'} <span className="count-pill">{findings.length}</span></h2><p>{specialists ? 'Only the AI judge’s report appears here, not raw lane candidates. The model judge is not human verification.' : active ? 'Findings may change until the audit is completed.' : 'Model-generated claims, not verified vulnerabilities.'}</p></div><span className="tiny-label">EVIDENCE FIRST</span></div>
      <div className="filter-bar"><label className="search-field"><Icon name="search" size={18} /><span className="sr-only">Search findings including exploit steps</span><input type="search" placeholder="Search title, location, or evidence…" value={query} onChange={(event) => setQuery(event.target.value)} /></label><label className="severity-select"><span className="sr-only">Filter by severity</span><select value={severity} onChange={(event) => setSeverity(event.target.value)}><option value="all">All severities</option>{SEVERITIES.map((level) => <option key={level} value={level}>{level[0].toUpperCase() + level.slice(1)}</option>)}</select></label></div>
      <p className="filter-count" role="status">Showing {visible.length} of {findings.length} findings{(query || severity !== 'all') && <button className="text-button" onClick={() => { setQuery(''); setSeverity('all'); }}>Clear filters</button>}</p>
      {!visible.length && <div className="panel empty-findings"><Icon name={active ? 'clock' : 'shield'} size={34} /><h3>{findings.length ? 'No matching findings' : specialists ? 'No AI-judged findings returned' : active ? 'Investigation in progress' : 'No findings returned'}</h3><p>{findings.length ? 'Try a different search or clear the severity filter.' : specialists ? 'Raw candidates are separate below and may be unjudged or duplicated, especially if a lane or the judge failed. An empty judged report is not proof of security.' : active ? 'Structured findings will appear as the backend returns them.' : 'An empty report is not proof of security. Review the source and audit warnings independently.'}</p></div>}
      <div className="finding-list">{visible.map((finding, index) => <details key={`${finding.title}:${finding.location}:${index}`} className="finding-card"><summary><span className={`severity-badge ${finding.severity}`}>{finding.severity}</span><span className="finding-title"><strong>{finding.title}</strong><code>{finding.location}</code></span><span className="accordion-chevron" aria-hidden="true">⌄</span></summary><div className="finding-body"><section><h3>Description</h3><p>{finding.description}</p></section><section><h3>Impact</h3><p>{finding.impact}</p></section><section><h3>Exploit steps</h3><ol>{(finding.exploit_steps || []).map((step, stepIndex) => <li key={stepIndex}>{step}</li>)}</ol></section></div></details>)}</div>
    </section>
    {specialists && <LaneCandidates data={data} />}
  </div>;
}
