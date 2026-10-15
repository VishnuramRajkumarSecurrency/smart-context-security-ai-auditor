"""Synchronous, deny-by-default adapter for Kiro CLI 2.18 ACP (JSON lines).

References: https://kiro.dev/docs/cli/acp/ and
https://kiro.dev/docs/cli/2x-reference/#custom-agent-config .
Each operation starts a fresh CLI with an explicit temporary agent profile
(no tools, MCP servers, resources, or hooks) in both startup and session
working directories, which otherwise contain no project files. These are
configuration precautions, NOT an OS sandbox. HOME is
preserved for CLI authentication; global CLI configuration may still apply,
and Kiro can persist prompts/session history under ~/.kiro. Use a trusted CLI
and an externally sandboxed account if filesystem isolation is required.

Compatibility with a real 2.18 binary has not been verified. A standard ACP
session/new model catalog is required; missing catalogs fail closed rather
than guessing IDs. Profile discovery timing is also unverified, hence the
identical profiles in both working directories.

Only complete() invokes inference (and may consume the user's Kiro quota).
JSON mode is a prompt instruction, not a guaranteed structured-output API.
Provider errors, stderr, and protocol payloads never enter exception messages.
"""

import json
import os
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path


DEFAULT_MODEL = "kiro-default"
_COMPLETION_TIMEOUT = 600.0
_CATALOG_TIMEOUT = 15.0
_MAX_INPUT = 8 * 1024 * 1024
_MAX_OUTPUT = 16 * 1024 * 1024
_MAX_FRAME = 8 * 1024 * 1024
_MAX_STDERR = 64 * 1024
_CLEANUP_TIMEOUT = 1.0
_AGENT = "lucid-audit"


class KiroError(RuntimeError):
    """An app-controlled error; never contains raw CLI/provider diagnostics."""


def executable() -> str | None:
    """Resolve LUCID_KIRO_CLI (a path or command name), or find kiro-cli."""
    candidate = os.environ.get("LUCID_KIRO_CLI")
    if candidate is not None:
        candidate = os.path.expanduser(candidate.strip())
        if not candidate:
            return None
        # An override is an executable, never a shell command with arguments.
        found = shutil.which(candidate)
    else:
        found = shutil.which("kiro-cli")
    return str(Path(found).resolve()) if found else None


def _model_id(value):
    if (not isinstance(value, str) or not value or len(value) > 256
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise KiroError("Kiro returned an invalid model catalog.")
    return value


def _validate_requested_model(model):
    if not isinstance(model, str):
        raise KiroError("Invalid Kiro model ID.")
    model = model.strip()
    try:
        return _model_id(model)
    except KiroError:
        raise KiroError("Invalid Kiro model ID.") from None


def _select_model(model, ids, current):
    selected = current if model in (None, DEFAULT_MODEL) else model
    if selected not in ids:
        raise KiroError("The selected model is not available in Kiro.")
    return selected


class _Connection:
    def __init__(self, process, timeout):
        self.process = process
        self.deadline = time.monotonic() + timeout
        self.selector = selectors.DefaultSelector()
        self.messages = deque()
        self.buffer = bytearray()
        self.stderr = bytearray()
        self.received = 0
        self.next_id = 0
        self.session_id = None
        self.collecting = False
        self.chunks = []
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
            raise KiroError("Kiro operation timed out.")
        events = self.selector.select(remaining)
        if not events:
            raise KiroError("Kiro operation timed out.")
        writable = False
        for key, _ in events:
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
                        raise KiroError("Kiro returned invalid ACP framing.")
                continue
            self.received += len(data)
            if self.received > _MAX_OUTPUT:
                raise KiroError("Kiro output exceeded the safety limit.")
            if key.data == "stderr":
                # Drain continuously to prevent a full stderr pipe deadlocking
                # the CLI. Retain only a bounded private diagnostic buffer.
                self.stderr.extend(data[:max(0, _MAX_STDERR - len(self.stderr))])
                continue
            self.buffer.extend(data)
            while True:
                end = self.buffer.find(b"\n")
                if end < 0:
                    break
                if end > _MAX_FRAME:
                    raise KiroError("Kiro output exceeded the safety limit.")
                frame = bytes(self.buffer[:end])
                del self.buffer[:end + 1]
                try:
                    message = json.loads(frame)
                except (ValueError, UnicodeError, RecursionError):
                    raise KiroError("Kiro returned invalid ACP framing.") from None
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise KiroError("Kiro returned an invalid ACP message.")
                # Handle notifications immediately, including while writing
                # stdin and after a response in the same read. A tool attempt
                # must not hide behind a queued successful prompt response.
                if "method" in message and "id" not in message:
                    self._notification(message)
                else:
                    self.messages.append(message)
            if len(self.buffer) > _MAX_FRAME:
                raise KiroError("Kiro output exceeded the safety limit.")
        return writable

    def _send(self, message):
        wire = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
        if len(wire) > _MAX_INPUT:
            raise KiroError("Kiro input exceeded the safety limit.")
        self.selector.register(self.process.stdin, selectors.EVENT_WRITE, "stdin")
        try:
            offset = 0
            while offset < len(wire):
                if not self._poll():
                    continue
                try:
                    offset += os.write(self.process.stdin.fileno(), wire[offset:offset + 65536])
                except BlockingIOError:
                    continue
        finally:
            self.selector.unregister(self.process.stdin)

    def _notification(self, message):
        if message.get("method") != "session/update":
            return
        params = message.get("params")
        if not isinstance(params, dict):
            raise KiroError("Kiro returned an invalid session update.")
        update = params.get("update")
        if not isinstance(update, dict):
            raise KiroError("Kiro returned an invalid session update.")
        # No tool activity is valid anywhere in this dedicated subprocess,
        # even during catalog setup or under another session/subagent ID.
        if update.get("sessionUpdate") in ("tool_call", "tool_call_update"):
            raise KiroError("Kiro attempted tool activity despite the text-only configuration.")
        if not self.collecting or params.get("sessionId") != self.session_id:
            return
        if update.get("sessionUpdate") != "agent_message_chunk":
            return
        content = update.get("content")
        if not isinstance(content, dict):
            raise KiroError("Kiro returned an invalid session update.")
        if content.get("type") == "text":
            if not isinstance(content.get("text"), str):
                raise KiroError("Kiro returned an invalid session update.")
            self.chunks.append(content["text"])

    def rpc(self, method, params):
        request_id = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            if time.monotonic() >= self.deadline:
                raise KiroError("Kiro operation timed out.")
            if not self.messages:
                if not self.stdout_open:
                    raise KiroError("Kiro CLI exited before completing the operation. Check CLI authentication and configuration.")
                self._poll()
                continue
            message = self.messages.popleft()
            if "method" in message:
                if "id" not in message:
                    self._notification(message)
                elif message["method"] == "session/request_permission":
                    self._send({"jsonrpc": "2.0", "id": message["id"],
                                "result": {"outcome": {"outcome": "cancelled"}}})
                else:
                    # No client-side file, terminal, authentication, or extension
                    # methods are supported, even if the server requests them.
                    self._send({"jsonrpc": "2.0", "id": message["id"],
                                "error": {"code": -32601, "message": "Client method disabled."}})
                continue
            if type(message.get("id")) is not int or message["id"] != request_id:
                raise KiroError("Kiro returned an unexpected ACP response.")
            if "error" in message:
                raise KiroError("Kiro ACP request failed. Check CLI authentication, model access, and configuration.")
            result = message.get("result")
            if not isinstance(result, dict):
                raise KiroError("Kiro returned an invalid ACP response.")
            return result

    def start(self, cwd):
        initialized = self.rpc("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": "lucid", "version": "1"},
        })
        if type(initialized.get("protocolVersion")) is not int or initialized["protocolVersion"] != 1:
            raise KiroError("Kiro CLI uses an unsupported ACP protocol version.")
        session = self.rpc("session/new", {"cwd": cwd, "mcpServers": []})
        self.session_id = session.get("sessionId")
        if not isinstance(self.session_id, str) or not self.session_id:
            raise KiroError("Kiro returned an invalid ACP session.")
        models = session.get("models")
        if not isinstance(models, dict) or not isinstance(models.get("availableModels"), list):
            raise KiroError("Kiro did not provide an ACP model catalog.")
        ids = set()
        for item in models["availableModels"]:
            if not isinstance(item, dict):
                raise KiroError("Kiro returned an invalid model catalog.")
            ids.add(_model_id(item.get("modelId")))
        current = _model_id(models.get("currentModelId"))
        if current not in ids or DEFAULT_MODEL in ids:
            raise KiroError("Kiro returned an inconsistent model catalog.")
        return ids, current


def _cleanup(process, connection):
    # A separate process group also contains descendants (e.g. an unexpectedly
    # launched MCP process). Kill the group even if its leader already exited.
    if process is not None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except OSError:
                pass
            try:
                process.wait(timeout=_CLEANUP_TIMEOUT)
            except subprocess.TimeoutExpired:
                continue
            if sig == signal.SIGKILL:
                break
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    if connection is not None:
        connection.selector.close()
        connection.stderr.clear()
        connection.buffer.clear()
        connection.messages.clear()
        connection.chunks.clear()


@contextmanager
def _session(timeout):
    cli = executable()
    if cli is None:
        raise KiroError("Kiro CLI was not found. Set LUCID_KIRO_CLI to an executable path.")
    process = connection = None
    try:
        with tempfile.TemporaryDirectory(prefix="lucid-kiro-") as root:
            startup = Path(root) / "startup"
            workspace = Path(root) / "workspace"
            # Do not place system/user source in files or argv. An explicit
            # profile avoids inheriting the default agent's tools and hooks.
            profile = json.dumps({
                "name": _AGENT,
                "description": "Text-only Lucid audit adapter",
                "prompt": "Follow the supplied instructions. Respond with text only; never use tools.",
                "tools": [], "allowedTools": [], "toolsSettings": {},
                "mcpServers": {}, "resources": [], "hooks": {},
            })
            # Legacy docs specify .kiro/agents but not ACP discovery timing.
            # Provide identical local profiles for startup and session lookup.
            # Use only v2 fields, not v3 includeMcpJson/permissions switches.
            for cwd in (startup, workspace):
                agents = cwd / ".kiro" / "agents"
                agents.mkdir(parents=True)
                (agents / f"{_AGENT}.json").write_text(profile, encoding="utf-8")
            env = os.environ.copy()
            env["KIRO_LOG_LEVEL"] = "off"
            env["KIRO_CHAT_LOG_FILE"] = str(Path(root) / "kiro.log")
            try:
                process = subprocess.Popen(
                    [cli, "acp", "--agent", _AGENT], cwd=str(startup), env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    shell=False, bufsize=0, start_new_session=True,
                )
                connection = _Connection(process, timeout)
                ids, current = connection.start(str(workspace))
                yield connection, ids, current
            finally:
                _cleanup(process, connection)
    except KiroError:
        raise
    except (OSError, ValueError, TypeError, RecursionError, subprocess.SubprocessError):
        raise KiroError("Kiro CLI transport failed. Check CLI installation and configuration.") from None


def list_models() -> list[dict[str, str]]:
    """Return sorted unique ACP model IDs only; never send session/prompt."""
    with _session(_CATALOG_TIMEOUT) as (_, ids, _current):
        return [{"id": model} for model in sorted(ids)]


def resolve_model(model: str) -> str:
    """Resolve 'kiro-default' or validate an explicit ID using a fresh catalog.

    Only initialize and session/new are sent: no inference or set_model.
    The result reflects this session's catalog; a later completion revalidates
    it against its own fresh session. Invalid/unavailable IDs raise KiroError.
    """
    model = _validate_requested_model(model)
    with _session(_CATALOG_TIMEOUT) as (_, ids, current):
        return _select_model(model, ids, current)


def complete(
    system: str, user: str, model: str | None = None, json_mode: bool = True,
    reasoning_effort: str | None = None,
) -> str:
    """Return streamed assistant text from one fresh ACP session.

    None or 'kiro-default' selects session/new's currentModelId, without
    sending the application marker to session/set_model. Explicit models must
    be in availableModels. ACP has no system role: instructions and user text
    are separate text content blocks. JSON mode only requests JSON in text.
    Non-None reasoning_effort is unsupported and rejected before spawning.
    """
    if reasoning_effort is not None:
        raise KiroError("Kiro ACP reasoning effort is not supported.")
    if not isinstance(system, str) or not isinstance(user, str):
        raise KiroError("Kiro prompts must be text.")
    if model is not None:
        model = _validate_requested_model(model)
    instructions = "Instructions:\n" + system
    if json_mode:
        instructions += "\nReturn only a valid JSON object, without Markdown fences or commentary."
    with _session(_COMPLETION_TIMEOUT) as (connection, ids, current):
        selected = _select_model(model, ids, current)
        if selected != current:
            connection.rpc("session/set_model", {"sessionId": connection.session_id, "modelId": selected})
        connection.collecting = True
        result = connection.rpc("session/prompt", {
            "sessionId": connection.session_id,
            "prompt": [{"type": "text", "text": instructions}, {"type": "text", "text": user}],
        })
        if result.get("stopReason") != "end_turn":
            raise KiroError("Kiro did not complete the response (refused, cancelled, or truncated).")
        return "".join(connection.chunks)
