# AI for Web3 Security: Zero to Hero

A hands-on course that takes you from *"how do I start with AI in web3 security?"*
to building, measuring, and improving your own AI bug-hunting harness around a
frontier model.

You don't get a finished tool. You become someone who builds and improves these
systems — and who keeps going long after the last module.

## The arc

Same target codebase (SecondSwap) scanned across all four modules. The detection
curve climbs from *a few* bugs to *most*.

- **Module 1 — your first harness.** One reasoning agent → structured findings.
  Honest baseline on the target: catches a few.
- **Module 2 — context + loop.** A protocol-context pass plus an exclusion
  loop that tells each round what earlier rounds already found.
- **Module 3 — an orchestrated harness.** Five specialist agents plus the
  general hunter, run in parallel, and a judge that checks every finding
  against the code. Catches more than half.
- **Module 4 — a measured harness + the launchpad** _(coming next)_. Recall against ground truth,
  honestly, plus the self-improvement loop handed over as a transferable method.

## The tool you build: `lucid`

One Python package that grows a module at a time. Built on the raw model API —
for pedagogy (learn the primitives), determinism (code doesn't improvise), and
ownership (you own the wrapper).

## Layout

- `lucid/` — the harness you build, module by module.
- `target/` — the SecondSwap contracts under test (constant across modules).
- `modules/` — the per-module build guides.

## Setup

```bash
python3 -m venv .venv          # some systems alias this as `python`
source .venv/bin/activate      # macOS / Linux
# .venv\Scripts\activate       # Windows (PowerShell or cmd)
pip install -r requirements.txt
cp .env.example .env           # then paste your API key (the gateway URL is pre-filled)
```

Get an API key from [Requesty](https://requesty.ai) (the OpenAI-compatible
gateway the base URL is pre-filled for) and paste it into `OPENAI_API_KEY`.

Examples use Python and an OpenAI-compatible API. Set `OPENAI_MODEL` to a
currently available model ID from your provider; the legacy course fallback in
`lucid/llm.py` is not an availability guarantee. With Requesty, use its model
library or the dashboard's **Load models** button rather than guessing model
versions. `.env.example` supplies the gateway URL. To use another compatible
provider, change `OPENAI_BASE_URL`, the backend key, and the model ID.
`python -m lucid.check` confirms connectivity by making a real, paid model call.

## Run it

Run these from the repo root, so `lucid` resolves as a package:

```bash
python -m lucid.run                    # Module 1: one agent, one pass over the bundled SecondSwap
python -m lucid.run path/to/contracts  # same, on a directory you choose
python -m lucid.run --context          # Module 2: protocol context + one context-aware pass
python -m lucid.run --loop             # Module 2: context + 5-round exclusion loop (--loop 3 for fewer)
python -m lucid.run --specialists      # Module 3: 5 specialists + hunter, 10 rounds each, + judge (--specialists 5 = 5 rounds per lane)
```

Every run makes real, paid API calls. Upstream measured the bundled target at
about $0.13 for `--loop` and $1.30/~35 minutes for `--specialists`. These are
historical measurements, not estimates for your selected model or settings.

## Local web dashboard

The React dashboard runs with a FastAPI backend on your Mac. Python 3.10+
(and Node.js 22.12+ to build the frontend) are required.

```bash
# From the repository root, with the virtual environment activated:
pip install -r requirements.txt
npm --prefix ui ci
npm --prefix ui run build
python -m lucid.web
```

Open **http://127.0.0.1:8765**. The frontend has already been built on this Mac;
you can start it now with `.venv/bin/python -m lucid.web`.
If Homebrew's Node 24 executable is not on your PATH, prefix npm commands with
`PATH=/opt/homebrew/opt/node@24/bin:/opt/homebrew/bin:/usr/bin:/bin`.


- Scan bundled SecondSwap, upload `.sol` files/a contract folder, or preview a
  public GitHub repository.
- Choose a single pass, context-aware pass, 1–10 exclusion rounds, or a specialist audit.
- Follow progress, filter findings, inspect evidence/context, and export JSON.
- Live scans require explicit consent: source and context are sent to the
  configured remote AI provider and incur usage charges. API keys stay on the
  backend; configure `.env` as above, then restart the server. Select a model
  per audit in the UI, or leave the field blank to use `OPENAI_MODEL`.

### Specialist audits in the dashboard

Choose **Specialist audit** after selecting bundled, uploaded, or previewed
GitHub source. One shared protocol-context pass feeds five focused specialist
lanes (state/lifecycle, trust boundaries, arithmetic, temporal/staleness, and
edge cases) plus the general auditor, running concurrently. Each lane excludes
its own earlier findings; an AI judge reconciles their pooled candidates.
Your selected model and reasoning setting apply to context, all lanes, and the
judge; JSON mode applies to lane and judge responses.

The UI defaults to **one round per lane** and permits 1–10. Maximum planned
calls are `2 + 6 × rounds`: **8** at one round, **62** at ten. The judge is
skipped when successful lanes return no candidates. Concurrent calls can hit
provider rate limits; spending and context limits depend on your model.

Reports show each lane's progress, candidate count, and judge status. Raw lane
candidates remain separately labeled and exportable; only the judge's final
findings appear in the main report. A lane failure, unreadable output, or judge
failure marks the audit incomplete rather than implying a clean result. New
calls stop best-effort after a lane failure or shutdown, but calls already in
flight cannot be cancelled. There is still no user cancellation endpoint.
An AI judge is not human verification, and an empty report is not proof of
security. CLI specialist behavior remains unchanged.

### Requesty and model selection

Set these values in your backend `.env` (never in frontend configuration):

```dotenv
OPENAI_API_KEY=your-requesty-api-key
OPENAI_BASE_URL=https://router.requesty.ai/v1
# OPENAI_MODEL=provider/model-id-from-your-requesty-model-library
```

The key variable is named `OPENAI_API_KEY` because the app uses the OpenAI SDK;
its value is your **Requesty** key. Uncomment and set `OPENAI_MODEL` if you want
a default for the UI and CLI, then restart the server. Otherwise, enter an
available model ID in the UI before starting each audit.

In **Choose your model**, click **Load models** to request the provider's current
model IDs through the backend, then type to search suggestions. Loading the
catalog sends no contract source and requests no inference. It is explicit,
not automatic, and uses your backend key; Requesty lists models allowed for
that key. Manual entry also supports gateway aliases or routing policy IDs.
A listed model is not a guarantee of compatibility or adequate context size.

The chosen model is pinned to the job and used for protocol context and every
finding round. Reports/history and JSON exports record the requested model,
JSON mode, and reasoning setting. If using a Requesty routing policy, that ID
is the requested route, not necessarily the actual underlying model.
Changing model settings clears paid consent. Sampling and reasoning are left
at provider defaults unless you explicitly select a reasoning effort. Disable
JSON mode for models that reject `response_format`; findings must still pass
JSON parsing and schema validation. Context generation always uses text mode.
No automatic SDK retries or paid parameter-fallback calls are made. These
provider-default settings also apply to CLI calls, replacing the earlier
hardcoded temperature/high-reasoning settings.

This is model-agnostic within the **OpenAI-compatible chat API**, not support
for every provider's native API. JSON/reasoning support, context limits, quality,
and cost vary. Source is shared with Requesty and its routed provider during
paid scans; configure spending limits in Requesty if needed.

### Kiro CLI without an API key

Lucid automatically uses the OpenAI-compatible API when `OPENAI_API_KEY` is
nonempty. If no key is configured and `kiro-cli` is on the backend's `PATH`, it
uses **Kiro CLI via ACP** instead. It never switches providers after a failed
request. An API key loaded from `.env` also takes precedence in automatic mode.

On the machine running Lucid, install Kiro CLI and authenticate separately:

```sh
kiro-cli login
kiro-cli acp --help
python -m lucid.web
```

No Requesty/OpenAI key is needed for Kiro. In the dashboard's **Model provider**
selector, choose **Automatic**, **API / Requesty**, **Kiro CLI**, or **Codex CLI**. Choosing Kiro
checks the selected backend configuration and automatically loads its catalog
into **Available Kiro models**, without sending an audit prompt. **Load models**
refreshes the list manually; API catalogs remain explicit-only. Changing providers
clears the selected model and paid consent, cancels pending catalog requests, and
leaves your source scope intact. Each audit pins its resolved provider and model.
CLI detection alone does not verify login.
The implementation targets CLI v2 ACP, including the 2.18.0 interface; live
compatibility with your installation must be checked locally. V3 is not assumed
compatible. A model catalog in `session/new` is required.

Optional backend environment settings:

- `LUCID_PROVIDER=auto` (default), `api`, `kiro`, or `codex` to force a provider.
- `LUCID_KIRO_CLI=/absolute/path/to/kiro-cli` if the executable isn't on `PATH`.
- `LUCID_KIRO_MODEL=<Kiro model ID>` for a Kiro-specific default. Otherwise
  `kiro-default` resolves to the session's current model before an audit starts;
  the actual model ID is recorded and used by all subsequent rounds/lanes.
  `OPENAI_MODEL` is deliberately not reused for Kiro.

Kiro audits consume subscription credits and send the selected source to Kiro's
service. Explicit consent remains required. Each context, lane, and judge prompt
uses a fresh ACP session; six specialist lanes can open six processes in
parallel and may encounter account concurrency limits. No automatic inference
retries are made. JSON mode is a prompt instruction, not a structured-output
API guarantee; findings still undergo strict schema validation. Reasoning effort
overrides are unsupported and rejected before an audit prompt is sent.

Lucid creates a restrictive temporary agent profile with no tools, resources,
MCP servers, or hooks, denies ACP permission requests, and exposes no client
filesystem/terminal methods. **This is not an OS sandbox.** The trusted Kiro
executable retains access to its normal login/configuration, global settings may
still apply, and Kiro can persist prompts in its session history. Do not use
`--trust-all-tools`; review your Kiro configuration and use an external sandbox
if OS-level isolation is required. The adapter currently requires macOS/Linux
(or a suitable Linux environment such as WSL); its process transport uses POSIX
pipes/process groups.

### Codex CLI with your existing login

Install Codex CLI on the **machine running Lucid**, then authenticate there:

```sh
codex login
codex --version
python -m lucid.web
```

Choose **Codex CLI** in the dashboard's **Model provider** selector. Lucid
loads **Available Codex models** automatically using app-server `model/list`;
**Load models** refreshes the catalog. Catalog requests do not start inference,
but may contact Codex services. CLI detection and catalog availability alone do
not prove authentication or permission to run a model. Lucid never reads your
Codex auth file or accepts ChatGPT subscription credentials in an API-key field.
Your existing Codex login is managed by the CLI. An eligible ChatGPT plan can
provide Codex usage without a separate API key; API-key-based Codex login may
instead incur API billing. Account limits and model access still apply.

Optional settings:

- `LUCID_PROVIDER=codex` to make Codex the backend default.
- `LUCID_CODEX_CLI=/absolute/path/to/codex` when it is not on `PATH`.
- `LUCID_CODEX_MODEL=<Codex execution model ID>` to select a backend default.
  Otherwise `codex-default` resolves to the catalog's default model before the
  audit starts. API/Requesty model IDs are not reused. Every audit phase uses
  the pinned Codex model, including all specialist lanes and the judge.

Automatic selection prefers a nonempty `OPENAI_API_KEY`, then installed Kiro,
then installed Codex. Explicit UI selection overrides this without changing the
environment. There is no fallback after a failed request.

The adapter's protocol and configuration were checked against **Codex CLI
0.162.1**; authenticated inference was not tested. Other versions may require
updates. It uses app-server stdio, a fresh process and ephemeral thread for each
completion, and sends prompts through stdin rather than arguments or files.
JSON mode is a prompt instruction; the normal finding parser still validates
responses. Low/medium/high reasoning overrides are checked against the selected
model's advertised capabilities before inference. Lucid starts no inference
retries, but Codex's internal retries may consume additional usage. Specialist
mode launches concurrent CLI processes and can hit account concurrency limits.

For both **Kiro and Codex**, `CLI_MAX_CONCURRENT_LANES` limits active specialist
lanes per audit in the dashboard and terminal runner. The default is **2**;
valid values are **1–6**. All six lanes still run, with excess lanes queued, and
each lane's rounds remain sequential. API/Requesty keeps six concurrent lanes.
This controls concurrency, not the total model-call count or subscription usage;
separate Lucid processes have separate limits. For example, before starting Lucid:

```sh
export CLI_MAX_CONCURRENT_LANES=1
python -m lucid.web
```

Use `6` to restore full CLI fan-out. Invalid values reject dashboard specialist
audits before inference and terminal specialist runs before lane execution (the
terminal runner may already have built context). Restart the backend after
changing its environment.

Built-in tools/features are disabled where supported; the agent sandbox is
read-only with network disabled, approval requests are never granted, and
observed tool activity is rejected. Known 0.162.1 informational notices caused
by disabling Code Mode and host skill discovery are allowed; unknown runtime
warnings and configuration-fallback warnings still stop the operation. A
successful final turn is required even after an informational notice.
**This is not full OS/config isolation.**
The CLI retains access to its normal auth/config location. Version 0.162.1
app-server cannot ignore user config or restrict all read roots. Before creating
an audit thread, Lucid queries the effective configuration (without config layers)
and disables each inherited MCP server using thread-local `enabled=false`
overrides. It does not edit your normal Codex configuration or forward server
commands, URLs, or credentials into the thread overrides. If discovery fails or
MCP activity still occurs (for example, because managed policy overrides the
restriction), the audit stops; no tool permissions are granted. Configuration
changes between discovery and thread creation can still allow startup before
Lucid rejects an event. Use only trusted Codex configuration, review existing MCP
settings, and use an external sandbox/account
if stronger isolation is needed. Ephemeral threads avoid persisted rollouts,
but CLI auth refreshes, caches, and logs may still be written. Provider/GitHub
credential environment variables are not forwarded to Codex. The adapter
currently requires macOS/Linux POSIX pipes/process groups (or suitable WSL).

### GitHub targets

Select **GitHub repository**, enter a repository root URL such as
`https://github.com/code-423n4/2024-12-secondswap`, and optionally choose a branch,
tag, or commit and a subdirectory such as `contracts`. Leave the ref blank to
use the default branch. File/tree/pull-request URLs are not supported.

Click **Preview repository** to download and review the Solidity manifest and
line-numbered source without an AI key or model charges. This contacts GitHub,
so normal network access and GitHub rate limits apply. Public repositories use
anonymous requests by default, even when a backend token is configured.

For **private repositories**, configure `LUCID_GITHUB_TOKEN` in the backend's
environment (or its local `.env`), restart Lucid, and check **Use backend GitHub
token** before previewing. Use a fine-grained GitHub personal access token limited
to the repositories you need, with **Contents: read-only** permission (repository
metadata read access is included). Organization approval and SSO authorization
may also be required. Do not commit the token, paste it into the repository URL,
or share it with the browser. No token-entry field is exposed by the UI.

Authenticated previews send the token only to `api.github.com`, use commit-pinned
API source downloads, and never follow redirects. The token is not stored in
previews, audit history, exports, or model prompts. Private source itself is
returned to your local browser for review and is sent to the selected AI provider
only when you authorize an audit. Use this local dashboard only on a trusted
machine; GitHub access is separate from API/Kiro authentication.

After previewing, use **Contracts in audit scope** to check the Solidity files
that should be audited. All files start selected; **Select all** and **Clear
selection** make bulk changes easy. At least one file is required. Only selected
files are sent for protocol context, audit lanes/rounds, and judging; imported
dependencies are not automatically included, so select relevant dependencies
and cross-contract logic yourself. Scope changes clear paid consent. Reports
and JSON exports record the exact selected file paths.

The preview resolves to an immutable commit. A paid scan uses that exact
in-memory source snapshot, not a fresh download of the branch. Repository URL,
commit, ref, and scope are included in the report/JSON export. Changing the
repository selection clears the preview and paid consent. Previews expire
in 15 minutes; only the latest 10 are kept, and restarting clears them.

Only regular `.sol` files in the selected directory are downloaded, with at most
200 files, bounded download sizes, and the existing 400,000-character annotated
source limit. No repository scripts run, dependencies are installed, or symlinks
or submodules followed. Missing external dependencies are not fetched; upload
additional source separately if needed. Large or incomplete trees are rejected
rather than silently auditing a partial target. Smaller scopes can omit relevant
cross-contract logic, so review the manifest before starting.

The server binds only to loopback. Do not expose it on your network: it is a
local tool, not an authenticated multi-user service. Uploaded source is kept
in memory and never written to a user-selected filesystem path. Audit history
is kept in memory (latest 50 jobs) and resets on restart; export reports you
want to keep. Only one live audit runs at a time. There is no cancellation:
leaving the page does not stop a paid scan. Model findings and generated context
require human verification; the exclusion loop is not guaranteed to deduplicate.

Offline validation:

```bash
.venv/bin/python -m unittest discover -s tests -v
npm --prefix ui test
```

For frontend development, run `npm --prefix ui run dev` alongside the backend;
Vite serves on port 5173 and proxies `/api` to port 8765. See `ui/README.md`.
