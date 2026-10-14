"""Text-only Codex app-server adapter (stdio JSONL, no jsonrpc header).

Protocol: https://developers.openai.com/codex/app-server/ . Launch flags and
wire fields checked against CLI 0.162.1 help/generated schemas; config keys
checked against openai/codex rust-v0.162.1/codex-rs/core/config.schema.json.
No live authentication or inference was used for that verification.

Only complete() starts inference, using locally managed `codex login` auth.
Lucid never reads/copies credentials, calls account APIs, or retries inference;
Codex itself may retry upstream requests. JSON mode is a prompt instruction,
not an arbitrary-object structured-output guarantee.

Each operation uses a fresh process group and private, empty temporary cwd.
Tools/features are disabled where supported, approvals are never granted, and
any observed tool activity fails closed. These precautions are NOT an OS
sandbox around app-server: its read-only, network-disabled sandbox applies to
agent commands, not the server or MCP processes. 0.162.1 app-server has no
--ignore-user-config (exec only), and its schema lacks restricted read roots.
HOME/CODEX_HOME must remain available for local auth: global instructions,
config, execpolicy, and managed requirements can still apply. Empty TOML maps
merge, so mcp_servers={} does NOT erase configured MCP servers. The orchestrator
MCP gate only disables orchestrator-owned tools. User MCP startup may therefore
occur before an event can be rejected; do not use this adapter with untrusted
Codex configuration. Use an externally sandboxed account for stronger isolation.
Ephemeral threads avoid rollouts, but CLI auth refresh/cache/log writes may
still occur. No source prompts are placed in argv or adapter-created files.
"""

import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

DEFAULT_MODEL = "codex-default"
_CATALOG_TIMEOUT = 15.0
_COMPLETION_TIMEOUT = 600.0
_CLEANUP_TIMEOUT = 1.0
_MAX_INPUT = 8 * 1024 * 1024
_MAX_OUTPUT = 16 * 1024 * 1024
_MAX_FRAME = 8 * 1024 * 1024
_MAX_PAGES = 100
_NO_TOOLS = "Respond using only the supplied text. Never use tools, skills, apps, shell commands, filesystem access, MCP, hooks, or subagents."
# Exact 0.162.1 advisories caused by our intentional feature restrictions.
# Configuration fallback, unknown features, and arbitrary warning text remain fatal.
_TEXT_ONLY_WARNINGS = frozenset({
    "Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; enable `features.code_mode_host` and install `codex-code-mode-host`.",
    "Code Mode is unavailable because code-mode host is disabled. Falling back to direct tools; enable `features.code_mode_host` and install `codex-code-mode-host`.",
})
_SKILL_DISCOVERY_WARNING = re.compile(
    r"Under-development features enabled: skip_host_skill_discovery\. "
    r"Under-development features are incomplete and may behave unpredictably\. "
    r"To suppress this warning, set `suppress_unstable_features_warning = true` in "
    r"[^\r\n]+[/\\]config\.toml\."
)
# No provider tokens, proxy credentials, Python injection, or endpoint overrides.
_ENV_KEYS = ("HOME", "CODEX_HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT")
_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "shell_snapshot", "shell_snapshot_v2",
    "apply_patch_freeform", "apps", "plugins", "hooks", "codex_hooks",
    "plugin_hooks", "multi_agent", "multi_agent_v2", "code_mode",
    "code_mode_host", "js_repl", "browser_use", "computer_use", "view_image",
    "image_generation", "imagegenext", "web_search", "web_search_cached",
    "web_search_request", "standalone_web_search", "search_tool", "skill_search",
    "skill_mcp_dependency_install", "skill_env_var_dependency_prompt",
    "memories", "memory_tool", "request_permissions", "request_permissions_tool",
    "tool_search", "tool_suggest", "remote_control", "sleep_tool",
)
_CONFIG = {
    "model_provider": "openai", "approval_policy": "never",
    "approvals_reviewer": "user", "sandbox_mode": "read-only",
    "web_search": "disabled", "allow_login_shell": False,
    "project_doc_max_bytes": 0, "project_doc_fallback_filenames": [],
    "developer_instructions": _NO_TOOLS,
    "skills.include_instructions": False, "skills.bundled.enabled": False,
    "cloud.skills.enabled": False, "orchestrator.mcp.enabled": False,
    "agents.enabled": False, "features.skip_host_skill_discovery": True,
    "tools.update_plan.enabled": False,
    "tools.experimental_request_user_input.enabled": False,
    "shell_environment_policy.inherit": "none",
    "include_apps_instructions": False, "include_environment_context": False,
    "notify": [], "history.persistence": "none", "analytics.enabled": False,
    "otel.exporter": "none", "otel.trace_exporter": "none",
    "otel.metrics_exporter": "none", "check_for_update_on_startup": False,
    **{f"features.{name}": False for name in _DISABLED_FEATURES},
}


class CodexError(RuntimeError):
    """App-controlled error: no raw stderr, protocol data, prompts or secrets."""


def executable() -> str | None:
    """Resolve LUCID_CODEX_CLI as one executable (not shell syntax), or codex."""
    candidate = os.environ.get("LUCID_CODEX_CLI", "codex")
    candidate = os.path.expanduser(candidate.strip())
    found = shutil.which(candidate) if candidate else None
    return str(Path(found).resolve()) if found else None


def _identifier(value):
    if (not isinstance(value, str) or not value or len(value) > 256
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise CodexError("Codex returned an invalid identifier or model catalog.")
    return value


def _requested_model(model):
    if not isinstance(model, str):
        raise CodexError("Invalid Codex model ID.")
    try:
        return _identifier(model.strip())
    except CodexError:
        raise CodexError("Invalid Codex model ID.") from None


def _wire(message):
    wire = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
    if len(wire) > _MAX_INPUT:
        raise CodexError("Codex input exceeded the safety limit.")
    return wire


class _Connection:
    def __init__(self, process, timeout):
        self.process = process
        self.deadline = time.monotonic() + timeout
        self.selector = selectors.DefaultSelector()
        self.messages = deque()
        self.events = deque()
        self.buffer = bytearray()
        self.received = 0
        self.next_id = 0
        self.stdout_open = True
        try:
            for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                os.set_blocking(stream.fileno(), False)
                self.selector.register(stream, selectors.EVENT_READ, name)
            os.set_blocking(process.stdin.fileno(), False)
        except BaseException:
            self.selector.close()
            raise

    def _poll(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise CodexError("Codex operation timed out.")
        ready = self.selector.select(remaining)
        if not ready:
            raise CodexError("Codex operation timed out.")
        writable = False
        for key, _ in ready:
            if key.data == "stdin":
                writable = True
                continue
            try:
                data = os.read(key.fd, 65536)
            except BlockingIOError:
                continue
            if not data:
                self.selector.unregister(key.fileobj)
                if key.data == "stdout":
                    self.stdout_open = False
                    if self.buffer:
                        raise CodexError("Codex returned invalid JSONL framing.")
                continue
            self.received += len(data)
            if self.received > _MAX_OUTPUT:
                raise CodexError("Codex output exceeded the safety limit.")
            if key.data == "stderr":
                # Drain, count, discard: never retain or expose CLI diagnostics.
                continue
            self.buffer.extend(data)
            while True:
                end = self.buffer.find(b"\n")
                if end < 0:
                    break
                if end > _MAX_FRAME:
                    raise CodexError("Codex frame exceeded the safety limit.")
                frame = bytes(self.buffer[:end])
                del self.buffer[:end + 1]
                try:
                    message = json.loads(frame)
                except (ValueError, UnicodeError, RecursionError):
                    raise CodexError("Codex returned invalid JSONL framing.") from None
                if not isinstance(message, dict) or "jsonrpc" in message:
                    raise CodexError("Codex returned an invalid app-server message.")
                # Inspect every event immediately, including events preceding
                # a turn/start response or following success in the same read.
                if "method" in message and "id" not in message:
                    self._notification(message)
                else:
                    self.messages.append(message)
            if len(self.buffer) > _MAX_FRAME:
                raise CodexError("Codex frame exceeded the safety limit.")
        return writable

    def _send(self, message):
        wire = _wire(message)
        self.selector.register(self.process.stdin, selectors.EVENT_WRITE, "stdin")
        try:
            offset = 0
            while offset < len(wire):
                if self._poll():
                    try:
                        offset += os.write(self.process.stdin.fileno(), wire[offset:offset + 65536])
                    except BlockingIOError:
                        pass
        finally:
            self.selector.unregister(self.process.stdin)

    def _notification(self, message):
        method, params = message.get("method"), message.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            raise CodexError("Codex returned an invalid notification.")
        if method == "warning":
            text = params.get("message")
            if isinstance(text, str) and (text in _TEXT_ONLY_WARNINGS or _SKILL_DISCOVERY_WARNING.fullmatch(text)):
                return
            raise CodexError("Codex reported an unrecognized runtime warning. Lucid stopped rather than ignoring a possible safety or model-configuration issue. Check CLI configuration and version.")
        if method == "configWarning":
            raise CodexError("Codex reported a configuration warning. Check for invalid or ignored settings and managed-policy overrides; Lucid cannot safely use fallback configuration.")
        if (method.startswith(("hook/", "mcpServer/startupStatus/", "item/commandExecution/",
                               "item/fileChange/", "item/mcpToolCall/", "item/tool/"))
                or method in ("turn/diff/updated", "turn/plan/updated", "model/rerouted",
                              "command/exec/outputDelta", "process/outputDelta", "process/exited",
                              "mcpServer/event/stream/notification")):
            if method.startswith("mcpServer/"):
                category = "MCP server activity; review inherited Codex MCP configuration"
            elif method.startswith("hook/"):
                category = "hook activity; review inherited Codex hook configuration"
            elif method == "model/rerouted":
                category = "model rerouting; the requested model was not kept pinned"
            elif method == "turn/plan/updated":
                category = "plan-tool activity"
            elif method == "turn/diff/updated":
                category = "file-diff activity"
            elif method.startswith(("item/commandExecution/", "command/exec/", "process/")):
                category = "command or process activity"
            elif method.startswith("item/fileChange/"):
                category = "file-change activity"
            else:
                category = "tool activity"
            raise CodexError(f"Codex attempted activity forbidden by the text-only configuration ({category}). No permissions were granted.")
        if method in ("item/started", "item/completed"):
            self._check_item(params.get("item"))
        if method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict) or not isinstance(turn.get("items", []), list):
                raise CodexError("Codex returned an invalid completed turn.")
            for item in turn.get("items", []):
                self._check_item(item)
        if method in ("item/completed", "turn/completed", "error"):
            self.events.append(message)

    @staticmethod
    def _check_item(item):
        if not isinstance(item, dict):
            raise CodexError("Codex returned an invalid item.")
        kind = item.get("type")
        # Codex emits this lifecycle item during automatic context management;
        # it is not a tool call and must never be collected as an audit answer.
        if kind == "contextCompaction":
            _identifier(item.get("id"))
            return
        if kind not in ("agentMessage", "userMessage", "reasoning"):
            categories = {
                "commandExecution": "command execution", "fileChange": "file change",
                "mcpToolCall": "MCP tool call", "dynamicToolCall": "client tool call",
                "collabToolCall": "subagent activity", "collabAgentToolCall": "subagent activity",
                "subAgentActivity": "subagent activity", "webSearch": "web search",
                "imageView": "image access", "imageGeneration": "image generation",
                "plan": "plan-tool activity",
                "hookPrompt": "hook activity", "sleep": "sleep tool",
            }
            category = categories.get(kind, "unrecognized item") if isinstance(kind, str) else "invalid item type"
            raise CodexError(f"Codex attempted activity forbidden by the text-only configuration ({category}). No permissions were granted.")

    def _deny(self, message):
        method = message["method"]
        response = {"id": message["id"]}
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            response["result"] = {"decision": "decline"}
        elif method == "item/permissions/requestApproval":
            response["result"] = {"permissions": {}, "scope": "turn"}
        elif method == "mcpServer/elicitation/request":
            response["result"] = {"action": "decline", "content": None}
        else:
            response["error"] = {"code": -32601, "message": "Client method disabled."}
        self._send(response)
        raise CodexError("Codex requested forbidden client activity; no permissions were granted.")

    def _responses(self):
        # All server requests are refused, even requests for other threads.
        while self.messages:
            message = self.messages.popleft()
            if "method" in message:
                self._deny(message)
            yield message

    def rpc(self, method, params):
        request_id = self.next_id
        self.next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        while True:
            if time.monotonic() >= self.deadline:
                raise CodexError("Codex operation timed out.")
            responses = list(self._responses())
            if responses:
                if len(responses) != 1:
                    raise CodexError("Codex returned unexpected app-server responses.")
                message = responses[0]
                if type(message.get("id")) is not int or message["id"] != request_id:
                    raise CodexError("Codex returned an unexpected app-server response.")
                if "error" in message:
                    raise CodexError("Codex request failed. Check CLI login, model access, and configuration.")
                if not isinstance(message.get("result"), dict):
                    raise CodexError("Codex returned an invalid app-server response.")
                return message["result"]
            self._read_more()

    def _read_more(self):
        if not self.stdout_open:
            raise CodexError("Codex CLI exited before completing the operation. Check CLI login and configuration.")
        self._poll()

    def catalog(self):
        self.rpc("initialize", {"clientInfo": {"name": "lucid_auditor", "title": "Lucid", "version": "0.1.0"}})
        self._send({"method": "initialized", "params": {}})
        models, cursors = {}, set()
        cursor = None
        for _ in range(_MAX_PAGES):
            params = {"limit": 100, "includeHidden": False}
            if cursor is not None:
                params["cursor"] = cursor
            result = self.rpc("model/list", params)
            data = result.get("data")
            if not isinstance(data, list):
                raise CodexError("Codex returned an invalid model catalog.")
            for entry in data:
                if not isinstance(entry, dict):
                    raise CodexError("Codex returned an invalid model catalog.")
                actual = _identifier(entry.get("model"))
                _identifier(entry.get("id"))
                if type(entry.get("isDefault")) is not bool:
                    raise CodexError("Codex returned an invalid model catalog.")
                if actual in models and models[actual] != entry:
                    raise CodexError("Codex returned an inconsistent model catalog.")
                models[actual] = entry
            cursor = result.get("nextCursor")
            if cursor is None:
                if not models:
                    raise CodexError("Codex returned an empty model catalog.")
                return models
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise CodexError("Codex returned invalid model pagination.")
            cursors.add(cursor)
        raise CodexError("Codex model pagination exceeded the safety limit.")

    def answer(self, thread_id, turn_id):
        answers = {}
        while True:
            if time.monotonic() >= self.deadline:
                raise CodexError("Codex operation timed out.")
            if list(self._responses()):
                raise CodexError("Codex returned an unexpected app-server response.")
            while self.events:
                event = self.events.popleft()
                method, params = event["method"], event["params"]
                turn = params.get("turn", {})
                event_turn = turn.get("id") if method == "turn/completed" else params.get("turnId")
                if params.get("threadId") != thread_id or event_turn != turn_id:
                    continue
                if method == "error":
                    if params.get("willRetry") is True:
                        # Codex owns the retry; Lucid sends no additional turn.
                        # Keep the original deadline and require terminal success.
                        continue
                    raise CodexError("Codex reported a turn error; no partial answer was returned.")
                if method == "item/completed":
                    item = params["item"]
                    if item["type"] == "agentMessage" and item.get("phase") in (None, "final_answer"):
                        item_id = _identifier(item.get("id"))
                        if not isinstance(item.get("text"), str):
                            raise CodexError("Codex returned an invalid agent message.")
                        if item_id in answers and answers[item_id] != item["text"]:
                            raise CodexError("Codex returned inconsistent completed messages.")
                        answers[item_id] = item["text"]
                elif method == "turn/completed":
                    for item in turn.get("items", []):
                        self._check_item(item)
                    if turn.get("status") != "completed" or turn.get("error") is not None:
                        raise CodexError("Codex did not successfully complete the turn.")
                    answer = "\n".join(answers.values())
                    if not answer.strip():
                        raise CodexError("Codex completed without a final agent message.")
                    return answer
            self._read_more()


def _cleanup(process, connection):
    if process is not None:
        # Kill descendants even if the app-server leader exited already.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except OSError:
                pass
            try:
                process.wait(timeout=_CLEANUP_TIMEOUT)
            except subprocess.TimeoutExpired:
                continue
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    if connection is not None:
        connection.selector.close()
        connection.buffer.clear()
        connection.messages.clear()
        connection.events.clear()


@contextmanager
def _session(timeout):
    if os.name != "posix":
        raise CodexError("The Codex adapter currently requires POSIX process and pipe support.")
    cli = executable()
    if cli is None:
        raise CodexError("Codex CLI was not found. Set LUCID_CODEX_CLI to an executable path.")
    process = connection = None
    try:
        with tempfile.TemporaryDirectory(prefix="lucid-codex-") as root:
            cwd = str(Path(root).resolve())
            env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
            env["TMPDIR"] = cwd
            args = [cli, "app-server", "--listen", "stdio://"]
            for key, value in _CONFIG.items():
                # These values are simple TOML scalars/arrays, also valid JSON.
                args.extend(["-c", key + "=" + json.dumps(value)])
            try:
                process = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           shell=False, bufsize=0, start_new_session=True)
                connection = _Connection(process, timeout)
                yield connection, cwd
            finally:
                _cleanup(process, connection)
    except CodexError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, subprocess.SubprocessError):
        raise CodexError("Codex CLI transport failed. Check CLI installation and configuration.") from None


def _select_model(model, models):
    if model in (None, DEFAULT_MODEL):
        defaults = [actual for actual, entry in models.items() if entry["isDefault"]]
        if len(defaults) != 1:
            raise CodexError("Codex did not advertise exactly one default model.")
        return defaults[0]
    if model not in models:
        raise CodexError("The selected model is not available in Codex.")
    return model


def list_models() -> list[dict[str, str]]:
    """Return sorted unique execution IDs, not picker IDs; no thread or turn."""
    with _session(_CATALOG_TIMEOUT) as (connection, _cwd):
        return [{"id": actual} for actual in sorted(connection.catalog())]


def resolve_model(model: str) -> str:
    """Resolve codex-default or validate a pinned execution ID without inference."""
    model = _requested_model(model)
    with _session(_CATALOG_TIMEOUT) as (connection, _cwd):
        return _select_model(model, connection.catalog())


def complete(system: str, user: str, model: str | None = None,
             json_mode: bool = True, reasoning_effort: str | None = None) -> str:
    """One ephemeral thread/turn, authoritative final text, successful turn only.

    Explicit effort is limited to low/medium/high and checked against advertised
    supportedReasoningEfforts before starting a thread or invoking inference.
    Omitted effort uses Codex's default. Unknown IDs never initiate inference.
    """
    if not isinstance(system, str) or not isinstance(user, str):
        raise CodexError("Codex prompts must be text.")
    if type(json_mode) is not bool:
        raise CodexError("Codex json_mode must be a boolean.")
    if reasoning_effort is not None and (not isinstance(reasoning_effort, str)
                                        or reasoning_effort not in ("low", "medium", "high")):
        raise CodexError("Codex reasoning effort must be low, medium, or high.")
    if model is not None:
        model = _requested_model(model)
    instructions = system + "\n\n" + _NO_TOOLS
    if json_mode:
        instructions += "\nReturn only a valid JSON object, without Markdown fences or commentary."
    # Bound escaped wire input before launching, not just Python character count.
    _wire({"system": instructions, "user": user})
    with _session(_COMPLETION_TIMEOUT) as (connection, cwd):
        models = connection.catalog()
        selected = _select_model(model, models)
        if reasoning_effort is not None:
            supported = models[selected].get("supportedReasoningEfforts")
            if (not isinstance(supported, list)
                    or not all(isinstance(option, dict) for option in supported)
                    or reasoning_effort not in [option.get("reasoningEffort") for option in supported]):
                raise CodexError("The selected Codex model does not advertise support for that reasoning effort.")
        result = connection.rpc("thread/start", {
            "model": selected, "modelProvider": "openai", "ephemeral": True,
            "cwd": cwd, "approvalPolicy": "never", "approvalsReviewer": "user",
            "sandbox": "read-only", "developerInstructions": instructions,
            "config": _CONFIG,
        })
        thread = result.get("thread", {})
        thread_id = _identifier(thread.get("id"))
        sandbox = result.get("sandbox", {})
        # Fail before turn/start if requirements/config weakened the requested
        # policy, loaded file instructions, changed providers, or repinned model.
        if (result.get("model") != selected or result.get("modelProvider") != "openai"
                or result.get("approvalPolicy") != "never" or result.get("approvalsReviewer") != "user"
                or result.get("cwd") != cwd or sandbox.get("type") != "readOnly"
                or sandbox.get("networkAccess", False) is not False
                or thread.get("ephemeral") is not True or result.get("instructionSources")):
            raise CodexError("Codex did not honor the isolated text-only thread configuration.")
        params = {"threadId": thread_id, "input": [{"type": "text", "text": user}],
                  "model": selected, "cwd": cwd, "approvalPolicy": "never",
                  "approvalsReviewer": "user", "sandboxPolicy": {"type": "readOnly", "networkAccess": False}}
        if reasoning_effort is not None:
            params["effort"] = reasoning_effort
        result = connection.rpc("turn/start", params)
        turn_id = _identifier(result.get("turn", {}).get("id"))
        return connection.answer(thread_id, turn_id)
