"""Offline ACP tests: only a local Python fixture is spawned, never Kiro."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from lucid import kiro


# This fixture exercises real pipes, selectors, framing, backpressure and
# cleanup. Its log exists only inside the test's own temporary directory.
_SERVER = r'''
import json, os, signal, sys, time
from pathlib import Path
scenario, logfile = sys.argv[1:]
def record(message):
    with open(logfile, "a") as log:
        log.write(json.dumps(message) + "\n")
def send(message):
    wire = (json.dumps(message, ensure_ascii=False) + "\n").encode()
    # Deliberately split UTF-8 and JSON framing across writes.
    for start in range(0, len(wire), 7):
        os.write(1, wire[start:start + 7])
def reply(request, result):
    send({"jsonrpc": "2.0", "id": request["id"], "result": result})
def update(kind, text, session="test-session"):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": session, "update": {"sessionUpdate": kind,
        "content": {"type": "text", "text": text}}}})
record({"startup": os.getcwd(), "pid": os.getpid(),
        "agent": json.loads(Path(".kiro/agents/lucid-audit.json").read_text()),
        "logfile": os.environ["KIRO_CHAT_LOG_FILE"],
        "root_mode": Path.cwd().parent.stat().st_mode & 0o777})
if scenario == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if scenario == "exit":
    os.write(2, b"SECRET provider credentials source")
    sys.exit(1)
if scenario == "no-read":
    time.sleep(60)
for line in sys.stdin.buffer:
    request = json.loads(line)
    record(request)
    method = request.get("method")
    if method is None:
        continue
    if scenario == "timeout":
        time.sleep(60)
    if scenario == "malformed":
        os.write(1, b"SECRET provider data\n")
        time.sleep(60)
    if scenario == "unfinished":
        os.write(1, b'{"secret":"SECRET"')
        sys.exit(1)
    if scenario == "invalid-message":
        os.write(1, b'[]\n')
        continue
    if scenario == "stdout-limit":
        os.write(1, b"x" * 100000)
        time.sleep(60)
    if scenario == "stderr-limit":
        os.write(2, b"SECRET" * 30000)
        time.sleep(60)
    if method == "initialize":
        if scenario == "auth":
            send({"jsonrpc": "2.0", "id": request["id"], "error": {
                "code": -32000, "message": "SECRET login/token", "data": {"source": "SECRET"}}})
            continue
        if scenario == "wrong-id":
            send({"jsonrpc": "2.0", "id": 999, "result": {}})
            continue
        if scenario == "bad-result":
            reply(request, ["SECRET"])
            continue
        if scenario == "catalog-tool":
            update("tool_call", "SECRET startup tool")
        reply(request, {"protocolVersion": 2 if scenario == "version" else 1})
    elif method == "session/new":
        workspace = Path(request["params"]["cwd"])
        profile_path = workspace / ".kiro/agents/lucid-audit.json"
        # Require successful discovery from session cwd, not just startup cwd.
        record({"workspace_agent": json.loads(profile_path.read_text()),
                "workspace_files": sorted(str(p.relative_to(workspace)) for p in workspace.rglob("*") if p.is_file()),
                "workspace_profile_symlink": profile_path.is_symlink(),
                "startup_profile_symlink": Path(".kiro/agents/lucid-audit.json").is_symlink()})
        models = {"currentModelId": "a/current", "availableModels": [
            {"modelId": "z/selected", "name": "SECRET", "description": "SECRET"},
            {"modelId": "a/current"}, {"modelId": "z/selected"}]}
        if scenario == "missing-models":
            reply(request, {"sessionId": "test-session"})
            continue
        if scenario == "invalid-model":
            models["availableModels"].append({"modelId": "bad model"})
        if scenario == "inconsistent":
            models["currentModelId"] = "not-listed"
        reply(request, {"sessionId": "test-session", "models": models})
    elif method == "session/set_model":
        if scenario == "set-error":
            send({"jsonrpc": "2.0", "id": request["id"], "error": {"message": "SECRET"}})
        else:
            reply(request, {})
    elif method == "session/prompt":
        assert "content" not in request["params"]
        assert isinstance(request["params"]["prompt"], list)
        if scenario == "permissions":
            for i, name in enumerate(["session/request_permission", "fs/read_text_file",
                                      "fs/write_text_file", "terminal/create", "terminal/output",
                                      "terminal/wait_for_exit", "terminal/kill", "terminal/release",
                                      "unknown/extension"]):
                send({"jsonrpc": "2.0", "id": "server-" + str(i), "method": name,
                      "params": {"sessionId": "test-session", "secret": "SECRET"}})
                record(json.loads(sys.stdin.buffer.readline()))
        if scenario == "stderr":
            os.write(2, b"SECRET" * 30000)
        if scenario == "prompt-error":
            update("agent_message_chunk", "SECRET partial")
            send({"jsonrpc": "2.0", "id": request["id"], "error": {"message": "SECRET"}})
            continue
        send({"jsonrpc": "2.0", "method": "_kiro.dev/commands/available", "params": {"secret": "SECRET"}})
        update("user_message_chunk", "SECRET user echo")
        update("agent_thought_chunk", "SECRET thoughts")
        if scenario in ("tool-call", "tool-update", "other-tool"):
            update("agent_message_chunk", "SECRET partial response")
            update("tool_call_update" if scenario == "tool-update" else "tool_call", "SECRET tool",
                   "other-session" if scenario == "other-tool" else "test-session")
        update("agent_message_chunk", "SECRET other session", "other-session")
        if scenario == "bad-update":
            send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "test-session", "update": {"sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": {"secret": "SECRET"}}}}})
        update("agent_message_chunk", '{"answer":')
        update("agent_message_chunk", '"héllo"}')
        stop = {"refusal": "refusal", "cancelled": "cancelled", "truncated": "max_tokens"}.get(scenario, "end_turn")
        reply(request, {"stopReason": stop})
'''


class KiroTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-kiro-")
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "requests.jsonl"
        self.processes = []
        self.calls = []
        self.real_popen = subprocess.Popen
        self.executable_patch = patch("lucid.kiro.executable", return_value="/offline/kiro-cli")
        self.executable_patch.start()
        self.addCleanup(self.executable_patch.stop)

    def fixture(self, scenario="normal"):
        def spawn(args, **kwargs):
            self.calls.append((args, kwargs))
            process = self.real_popen([sys.executable, "-u", "-c", _SERVER, scenario, str(self.log)], **kwargs)
            self.processes.append(process)
            return process
        return patch("lucid.kiro.subprocess.Popen", side_effect=spawn)

    def records(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def requests(self):
        return [record for record in self.records() if "method" in record]

    def assert_clean(self):
        for process in self.processes:
            self.assertIsNotNone(process.poll())
            self.assertTrue(process.stdin.closed)
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)
        for _, kwargs in self.calls:
            self.assertFalse(Path(kwargs["cwd"]).parent.exists())

    def test_executable_resolution(self):
        self.executable_patch.stop()
        with patch.dict(os.environ, {}, clear=True), patch("lucid.kiro.shutil.which", return_value="/bin/kiro-cli") as which:
            self.assertEqual(kiro.executable(), str(Path("/bin/kiro-cli").resolve()))
            which.assert_called_once_with("kiro-cli")
        with patch.dict(os.environ, {"LUCID_KIRO_CLI": " /custom/kiro "}), patch("lucid.kiro.shutil.which", return_value="/custom/kiro") as which:
            self.assertEqual(kiro.executable(), "/custom/kiro")
            which.assert_called_once_with("/custom/kiro")
        with patch.dict(os.environ, {"LUCID_KIRO_CLI": ""}), patch("lucid.kiro.shutil.which") as which:
            self.assertIsNone(kiro.executable())
            which.assert_not_called()
        with patch.dict(os.environ, {"LUCID_KIRO_CLI": "missing --argument"}), patch("lucid.kiro.shutil.which", return_value=None):
            self.assertIsNone(kiro.executable())

    def test_catalog_ids_only_no_inference(self):
        with self.fixture():
            self.assertEqual(kiro.list_models(), [{"id": "a/current"}, {"id": "z/selected"}])
        self.assertEqual([r["method"] for r in self.requests()], ["initialize", "session/new"])
        self.assert_clean()

    def test_resolve_model_catalog_only(self):
        for model, expected in (("kiro-default", "a/current"), ("a/current", "a/current"),
                                ("z/selected", "z/selected"), (" z/selected ", "z/selected")):
            with self.subTest(model=model), self.fixture():
                self.assertEqual(kiro.resolve_model(model), expected)
        self.assertEqual([r["method"] for r in self.requests()], ["initialize", "session/new"] * 4)
        self.assertEqual(len({p.pid for p in self.processes}), 4)
        for record in self.records():
            if "workspace_agent" in record:
                self.assertEqual(record["workspace_files"], [".kiro/agents/lucid-audit.json"])
        self.assert_clean()

    def test_resolve_unknown_model_catalog_only(self):
        with self.fixture(), self.assertRaisesRegex(kiro.KiroError, "not available"):
            kiro.resolve_model("SECRET-unavailable")
        self.assertEqual([r["method"] for r in self.requests()], ["initialize", "session/new"])
        self.assert_clean()

    def test_resolve_invalid_model_before_spawn(self):
        with patch("lucid.kiro.subprocess.Popen") as spawn:
            for model in (None, 42, "", "  ", "bad model", "a\x00b", "a" * 257):
                with self.subTest(model=model), self.assertRaisesRegex(kiro.KiroError, "Invalid Kiro model ID"):
                    kiro.resolve_model(model)
            spawn.assert_not_called()

    def test_resolve_catalog_failures_are_safe_and_clean(self):
        for scenario in ("auth", "exit", "missing-models", "invalid-model", "inconsistent"):
            with self.subTest(scenario=scenario), self.fixture(scenario):
                with self.assertRaises(kiro.KiroError) as raised:
                    kiro.resolve_model("kiro-default")
                self.assertNotIn("SECRET", str(raised.exception))
                self.assertFalse(any(r["method"] in ("session/prompt", "session/set_model") for r in self.requests()))
                self.assert_clean()
        with self.fixture("timeout"), patch("lucid.kiro._CATALOG_TIMEOUT", 0.2):
            with self.assertRaisesRegex(kiro.KiroError, "timed out"):
                kiro.resolve_model("kiro-default")
        self.assert_clean()

    def test_framing_notifications_prompt_and_isolation_precautions(self):
        source = "PRIVATE SOURCE ; $(touch bad)\nline\x00"
        with self.fixture():
            self.assertEqual(kiro.complete("SYSTEM", source, model="z/selected"), '{"answer":"héllo"}')
        requests = self.requests()
        self.assertEqual(requests[0]["params"]["clientCapabilities"], {
            "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False})
        self.assertEqual(requests[0]["params"]["protocolVersion"], 1)
        self.assertEqual(requests[1]["params"]["mcpServers"], [])
        self.assertEqual(requests[2]["method"], "session/set_model")
        self.assertEqual(requests[2]["params"], {"sessionId": "test-session", "modelId": "z/selected"})
        prompt = requests[3]["params"]
        self.assertEqual(prompt["sessionId"], "test-session")
        self.assertNotIn("content", prompt)
        self.assertEqual(prompt["prompt"][1], {"type": "text", "text": source})
        self.assertIn("SYSTEM", prompt["prompt"][0]["text"])
        self.assertIn("valid JSON object", prompt["prompt"][0]["text"])
        record = self.records()[0]
        for key, value in {"tools": [], "allowedTools": [], "resources": [], "hooks": {}, "mcpServers": {}}.items():
            self.assertEqual(record["agent"][key], value)
        self.assertNotIn("model", record["agent"])
        workspace = next(r for r in self.records() if "workspace_agent" in r)
        self.assertEqual(workspace["workspace_agent"], record["agent"])
        self.assertEqual(workspace["workspace_files"], [".kiro/agents/lucid-audit.json"])
        self.assertFalse(workspace["workspace_profile_symlink"])
        self.assertFalse(workspace["startup_profile_symlink"])
        self.assertFalse(set(record["agent"]) & {"includeMcpJson", "includePowers", "permissions", "excludedTools"})
        args, kwargs = self.calls[0]
        self.assertEqual(args, ["/offline/kiro-cli", "acp", "--agent", "lucid-audit"])
        self.assertNotIn(source, str(args))
        self.assertNotIn(source, json.dumps(record["agent"]))
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["start_new_session"])
        self.assertNotEqual(kwargs["cwd"], requests[1]["params"]["cwd"])
        self.assertEqual(kwargs["env"].get("HOME"), os.environ.get("HOME"))
        self.assert_clean()

    def test_default_marker_current_and_json_mode_false(self):
        for model in (None, "kiro-default", "a/current"):
            with self.subTest(model=model), self.fixture():
                self.assertEqual(kiro.complete("S", "U", model=model, json_mode=False), '{"answer":"héllo"}')
        self.assertFalse(any(r["method"] == "session/set_model" for r in self.requests()))
        prompts = [r for r in self.requests() if r["method"] == "session/prompt"]
        self.assertEqual(len(prompts), 3)
        self.assertEqual(prompts[0]["params"]["prompt"][0]["text"], "Instructions:\nS")
        self.assertEqual(len({p.pid for p in self.processes}), 3)
        self.assert_clean()

    def test_tool_activity_fails_closed_and_cleans_up(self):
        for scenario in ("tool-call", "tool-update", "other-tool", "catalog-tool"):
            with self.subTest(scenario=scenario), self.fixture(scenario):
                with self.assertRaisesRegex(kiro.KiroError, "attempted tool activity") as raised:
                    kiro.complete("PRIVATE SYSTEM", "PRIVATE SOURCE")
                self.assertNotIn("SECRET", str(raised.exception))
                self.assertNotIn("PRIVATE", str(raised.exception))
                self.assert_clean()
        with self.fixture("catalog-tool"):
            with self.assertRaisesRegex(kiro.KiroError, "attempted tool activity"):
                kiro.list_models()
        self.assert_clean()

    def test_tool_notification_after_response_in_same_read_fails_closed(self):
        from types import SimpleNamespace
        connection = kiro._Connection.__new__(kiro._Connection)
        connection.deadline = time.monotonic() + 1
        connection.selector = unittest.mock.MagicMock()
        connection.selector.select.return_value = [(SimpleNamespace(data="stdout", fd=123), 1)]
        connection.received = 0
        connection.buffer = bytearray()
        connection.messages = kiro.deque()
        connection.collecting = True
        connection.session_id = "test-session"
        response = {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "end_turn"}}
        tool = {"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "test-session", "update": {"sessionUpdate": "tool_call", "title": "SECRET"}}}
        wire = (json.dumps(response) + "\n" + json.dumps(tool) + "\n").encode()
        with patch("lucid.kiro.os.read", return_value=wire):
            with self.assertRaisesRegex(kiro.KiroError, "attempted tool activity"):
                connection._poll()

    def test_partial_connection_setup_closes_selector_and_process(self):
        selector = kiro.selectors.DefaultSelector()
        with self.fixture(), patch("lucid.kiro.selectors.DefaultSelector", return_value=selector), \
                patch.object(selector, "register", side_effect=OSError("SECRET failure")):
            with self.assertRaisesRegex(kiro.KiroError, "transport failed"):
                kiro.list_models()
        self.assertIsNone(selector.get_map())
        self.assert_clean()

    def test_temp_root_is_private_even_with_permissive_umask(self):
        previous = os.umask(0)
        try:
            with self.fixture():
                kiro.list_models()
        finally:
            os.umask(previous)
        self.assertEqual(self.records()[0]["root_mode"], 0o700)
        self.assert_clean()

    def test_permissions_denied_client_methods_rejected(self):
        with self.fixture("permissions"):
            kiro.complete("S", "U")
        responses = [r for r in self.records() if str(r.get("id", "")).startswith("server-") and "method" not in r]
        self.assertEqual(responses[0]["result"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual(len(responses), 9)
        for response in responses[1:]:
            self.assertEqual(response["error"]["code"], -32601)
        self.assert_clean()

    def test_unknown_model_never_prompts(self):
        with self.fixture(), self.assertRaisesRegex(kiro.KiroError, "not available"):
            kiro.complete("S", "U", model="unknown")
        self.assertEqual(len(self.requests()), 2)
        self.assert_clean()

    def test_validation_before_spawn(self):
        with patch("lucid.kiro.subprocess.Popen") as spawn:
            for effort in ("low", "high", "", False):
                with self.assertRaisesRegex(kiro.KiroError, "reasoning"):
                    kiro.complete("S", "U", reasoning_effort=effort)
            for model in ("", "bad model", "a\x00b", "a" * 257, 42):
                with self.assertRaises(kiro.KiroError):
                    kiro.complete("S", "U", model=model)
            with self.assertRaises(kiro.KiroError):
                kiro.complete(None, "U")
            spawn.assert_not_called()

    def test_safe_errors_and_cleanup(self):
        for scenario in ("auth", "exit", "malformed", "unfinished", "invalid-message", "version",
                         "wrong-id", "bad-result", "missing-models", "invalid-model", "inconsistent",
                         "set-error", "prompt-error", "bad-update", "refusal", "cancelled", "truncated"):
            with self.subTest(scenario=scenario), self.fixture(scenario):
                with self.assertRaises(kiro.KiroError) as raised:
                    kiro.complete("PRIVATE SYSTEM", "PRIVATE SOURCE", model="z/selected")
                self.assertNotIn("SECRET", str(raised.exception))
                self.assertNotIn("PRIVATE", str(raised.exception))
                self.assertNotIn("SECRET", repr(raised.exception))
                self.assert_clean()

    def test_timeout_and_input_backpressure(self):
        for scenario in ("timeout", "no-read"):
            with self.subTest(scenario=scenario), self.fixture(scenario), patch("lucid.kiro._COMPLETION_TIMEOUT", 0.2):
                start = time.monotonic()
                with self.assertRaisesRegex(kiro.KiroError, "timed out"):
                    kiro.complete("S", "U")
                self.assertLess(time.monotonic() - start, 4)
                self.assert_clean()

    def test_catalog_timeout(self):
        with self.fixture("timeout"), patch("lucid.kiro._CATALOG_TIMEOUT", 0.2):
            with self.assertRaisesRegex(kiro.KiroError, "timed out"):
                kiro.list_models()
        self.assert_clean()

    def test_cleanup_escalates_to_kill(self):
        with self.fixture("ignore-term"), patch("lucid.kiro._CLEANUP_TIMEOUT", 0.1):
            kiro.complete("S", "U")
        self.assertEqual(self.processes[0].returncode, -9)
        self.assert_clean()

    def test_large_prompt_stalled_writer_is_bounded(self):
        # Initialize succeeds, but the fake peer stops reading the large prompt.
        # Stop reading after session/new, before the client's large write.
        script = _SERVER.replace('reply(request, {"sessionId": "test-session", "models": models})',
                                'reply(request, {"sessionId": "test-session", "models": models})\n        time.sleep(60)')
        with patch(__name__ + "._SERVER", script), self.fixture(), patch("lucid.kiro._COMPLETION_TIMEOUT", 0.3):
            with self.assertRaisesRegex(kiro.KiroError, "timed out"):
                kiro.complete("S", "x" * 1000000)
        self.assert_clean()

    def test_stderr_drained_without_leaking(self):
        with self.fixture("stderr"), patch("lucid.kiro._MAX_STDERR", 32):
            self.assertEqual(kiro.complete("S", "U"), '{"answer":"héllo"}')
        self.assert_clean()

    def test_output_and_frame_limits(self):
        for scenario, limit in (("stdout-limit", "_MAX_OUTPUT"), ("stdout-limit", "_MAX_FRAME"), ("stderr-limit", "_MAX_OUTPUT")):
            with self.subTest(scenario=scenario, limit=limit), self.fixture(scenario), patch("lucid.kiro." + limit, 4096):
                with self.assertRaisesRegex(kiro.KiroError, "safety limit"):
                    kiro.complete("S", "U")
                self.assert_clean()

    def test_input_limit_and_spawn_failure_are_safe(self):
        with self.fixture(), patch("lucid.kiro._MAX_INPUT", 4096):
            with self.assertRaisesRegex(kiro.KiroError, "input exceeded"):
                kiro.complete("S", "x" * 10000)
        self.assert_clean()
        with patch("lucid.kiro.subprocess.Popen", side_effect=OSError("SECRET path/token")):
            with self.assertRaisesRegex(kiro.KiroError, "transport failed") as raised:
                kiro.complete("S", "U")
            self.assertNotIn("SECRET", str(raised.exception))
        with patch("lucid.kiro.executable", return_value=None), patch("lucid.kiro.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(kiro.KiroError, "not found"):
                kiro.list_models()
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
