"""Offline JSONL tests: spawn Python fixtures only, never a real Codex server."""

import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from lucid import codex

_SERVER = r'''
import json, os, signal, sys, time
from pathlib import Path
scenario, logfile = sys.argv[1:]
def record(value):
    with open(logfile, "a") as log:
        log.write(json.dumps(value) + "\n")
def send(value):
    wire = (json.dumps(value, ensure_ascii=False) + "\n").encode()
    for offset in range(0, len(wire), 7):
        os.write(1, wire[offset:offset + 7])
def reply(request, result):
    send({"id": request["id"], "result": result})
def event(method, **params):
    send({"method": method, "params": params})
def item(kind="agentMessage", text='{"answer":"héllo"}', phase="final_answer", thread="thread", turn="turn", item_id="answer"):
    event("item/completed", threadId=thread, turnId=turn,
          item={"id": item_id, "type": kind, "text": text, "phase": phase})
def done(status="completed", thread="thread", turn="turn"):
    event("turn/completed", threadId=thread, turn={"id": turn, "status": status,
          "items": [], "error": {"message": "SECRET"} if status == "failed" else None})
record({"startup": os.getcwd(), "pid": os.getpid(), "files": os.listdir(),
        "mode": Path.cwd().stat().st_mode & 0o777, "env": dict(os.environ)})
if scenario == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if scenario == "exit":
    os.write(2, b"SECRET provider credentials"); sys.exit(1)
if scenario == "no-read":
    time.sleep(60)
for line in sys.stdin.buffer:
    request = json.loads(line)
    record(request)
    method = request.get("method")
    if not method:
        continue
    if scenario in ("timeout", "ignore-term"):
        time.sleep(60)
    if scenario == "malformed":
        os.write(1, b"SECRET invalid JSON\n"); time.sleep(60)
    if scenario == "unfinished":
        os.write(1, b'{"secret":"SECRET"'); sys.exit(1)
    if scenario == "bad-message":
        os.write(1, b'[]\n'); time.sleep(60)
    if scenario == "jsonrpc":
        send({"jsonrpc": "2.0", "id": request["id"], "result": {}}); continue
    if scenario == "frame-limit":
        os.write(1, b"x" * 100000); time.sleep(60)
    if scenario == "output-limit":
        os.write(2, b"SECRET" * 30000); time.sleep(60)
    if method == "initialize":
        if scenario == "rpc-error":
            send({"id": request["id"], "error": {"code": -32000, "message": "SECRET", "data": "SECRET"}})
        elif scenario == "wrong-id":
            send({"id": 999, "result": {}})
        elif scenario == "bad-result":
            reply(request, ["SECRET"])
        elif scenario == "catalog-tool":
            item("commandExecution", "SECRET", thread="other"); reply(request, {})
        else:
            reply(request, {"userAgent": "fixture"})
    elif method == "initialized":
        assert "id" not in request and "jsonrpc" not in request
    elif method == "model/list":
        if scenario == "catalog-timeout":
            time.sleep(60)
        entries = [
            {"id": "picker-z", "model": "z-real", "isDefault": False,
             "supportedReasoningEfforts": [{"reasoningEffort": "high"}]},
            {"id": "picker-a", "model": "a-real", "isDefault": True,
             "supportedReasoningEfforts": [{"reasoningEffort": effort} for effort in ["low", "medium", "high"]]}]
        cursor = request["params"].get("cursor")
        data = entries[:1] if cursor is None else entries[1:]
        next_cursor = "page2" if cursor is None else None
        if scenario == "repeat-cursor": next_cursor = "page2"
        if scenario == "bad-cursor": next_cursor = 7
        if scenario == "bad-model": data[0]["model"] = "bad model SECRET"
        if scenario == "missing-model": del data[0]["model"]
        if scenario == "empty-models": data, next_cursor = [], None
        if scenario == "missing-default":
            for row in data: row["isDefault"] = False
        if scenario == "two-defaults":
            for row in data: row["isDefault"] = True
        if scenario == "duplicate": data = entries + [entries[0]]; next_cursor = None
        if scenario == "inconsistent": data = entries + [dict(entries[0], isDefault=True)]; next_cursor = None
        if scenario == "missing-efforts":
            for row in data: row.pop("supportedReasoningEfforts")
        reply(request, {"data": data, "nextCursor": next_cursor})
    elif method == "config/read":
        config = {"mcp_servers": {}}
        if scenario in ("inherited-mcp", "inherited-mcp-ignored"):
            config["mcp_servers"] = {
                'server.with.dots"quoted': {"command": "SECRET", "env": {"TOKEN": "SECRET"}},
                "already-disabled": {"enabled": False, "url": "SECRET"}}
        if scenario == "bad-mcp-config": config["mcp_servers"] = ["SECRET"]
        if scenario == "bad-mcp-entry": config["mcp_servers"] = {"SECRET": "SECRET"}
        if scenario == "bad-mcp-name": config["mcp_servers"] = {"bad\nSECRET": {}}
        if scenario == "many-mcp-servers": config["mcp_servers"] = {str(i): {} for i in range(513)}
        if scenario == "missing-config":
            reply(request, {})
        elif scenario == "config-read-error":
            send({"id": request["id"], "error": {"message": "SECRET"}})
        else:
            reply(request, {"config": config})
    elif method == "thread/start":
        p = request["params"]
        result = {"thread": {"id": "thread", "ephemeral": True}, "model": p["model"],
                  "modelProvider": "openai", "cwd": os.getcwd(), "approvalPolicy": "never",
                  "approvalsReviewer": "user", "sandbox": {"type": "readOnly", "networkAccess": False},
                  "instructionSources": []}
        if scenario == "unsafe-sandbox": result["sandbox"] = {"type": "dangerFullAccess"}
        if scenario == "unsafe-network": result["sandbox"]["networkAccess"] = True
        if scenario == "unsafe-model": result["model"] = "different"
        if scenario == "unsafe-provider": result["modelProvider"] = "other"
        if scenario == "unsafe-policy": result["approvalPolicy"] = "on-request"
        if scenario == "unsafe-reviewer": result["approvalsReviewer"] = "auto_review"
        if scenario == "unsafe-cwd": result["cwd"] = "/other"
        if scenario == "persistent": result["thread"]["ephemeral"] = False
        if scenario == "instructions": result["instructionSources"] = ["SECRET"]
        if scenario in ("inherited-mcp", "inherited-mcp-ignored"):
            assert p["config"]["mcp_servers"] == {'server.with.dots"quoted': {"enabled": False}, "already-disabled": {"enabled": False}}
        if scenario in ("mcp-startup", "inherited-mcp-ignored"): event("mcpServer/startupStatus/updated", threadId="thread", name="SECRET")
        if scenario == "warning": event("configWarning", summary="SECRET")
        if scenario == "skill-advisory":
            event("warning", message="Under-development features enabled: skip_host_skill_discovery. Under-development features are incomplete and may behave unpredictably. To suppress this warning, set `suppress_unstable_features_warning = true` in /isolated/config.toml.")
        if scenario == "unknown-advisory": event("warning", message="SECRET unknown warning")
        reply(request, result)
        if scenario == "stall-turn-write": time.sleep(60)
    elif method == "turn/start":
        if scenario.startswith("request:"):
            name = scenario.split(":", 1)[1]
            send({"id": "server-1", "method": name, "params": {"threadId": "other", "secret": "SECRET"}})
            record(json.loads(sys.stdin.buffer.readline())); time.sleep(60)
        if scenario == "stderr": os.write(2, b"SECRET" * 30000)
        if scenario == "descendant":
            import subprocess
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            record({"child": child.pid})
        # Ignore user echoes, deltas, reasoning, commentary, unrelated scope.
        item("userMessage", "SECRET user")
        item("reasoning", "SECRET reasoning")
        item(text="SECRET commentary", phase="commentary", item_id="commentary")
        item(text="SECRET wrong-thread", thread="other")
        item(text="SECRET wrong-turn", turn="other")
        done("failed", thread="other")
        done("failed", turn="other")
        event("item/agentMessage/delta", threadId="thread", turnId="turn", delta="SECRET delta")
        if scenario.startswith("tool:"):
            item(scenario.split(":", 1)[1], "SECRET tool", thread="other")
        if scenario in ("code-mode-advisory", "direct-tools-advisory", "advisory-then-tool"):
            outcome = "Falling back to direct tools" if scenario == "direct-tools-advisory" else "Code mode will fail closed"
            event("warning", threadId="thread", message=f"Code Mode is unavailable because code-mode host is disabled. {outcome}; enable `features.code_mode_host` and install `codex-code-mode-host`.")
        if scenario == "advisory-then-tool": item("commandExecution", "SECRET tool")
        if scenario in ("compaction", "compaction-then-tool", "compaction-no-final", "compaction-failed"):
            event("item/started", threadId="thread", turnId="turn", item={"id":"compact", "type":"contextCompaction"})
            event("item/completed", threadId="thread", turnId="turn", item={"id":"compact", "type":"contextCompaction"})
        if scenario == "compaction-then-tool": item("commandExecution", "SECRET tool")
        if scenario == "hook": event("hook/started", threadId="thread", turnId="turn", run={"secret":"SECRET"})
        if scenario == "rerouted": event("model/rerouted", threadId="thread", turnId="turn", toModel="SECRET")
        if scenario.startswith("notification:"):
            _, name, scope = scenario.split(":", 2)
            params = {"secret": "SECRET"}
            if scope != "unscoped":
                params.update(threadId="thread" if scope == "active" else "other",
                              turnId="turn" if scope == "active" else "other")
            event(name, **params)
        if scenario == "turn-error": event("error", threadId="thread", turnId="turn", willRetry=False, error={"message":"SECRET"})
        if scenario.startswith("retry-"):
            event("error", threadId="thread", turnId="turn", willRetry=True, error={"message":"SECRET retry"})
        if scenario == "early":
            item(); done(); reply(request, {"turn": {"id": "turn", "status": "inProgress"}}); continue
        reply(request, {"turn": {"id": "turn", "status": "inProgress"}})
        if scenario == "turn-rpc-error":
            send({"id": request["id"], "error": {"message": "SECRET"}}); continue
        if scenario not in ("no-final", "commentary-only", "compaction-no-final"):
            item(phase=None if scenario == "null-phase" else "final_answer")
        if scenario == "duplicate-answer": item()
        if scenario == "inconsistent-answer": item(text="SECRET mismatch")
        if scenario == "empty-answer": item(text="")
        if scenario == "bad-text": item(text={"secret": "SECRET"})
        if scenario in ("turn-timeout", "retry-timeout"): time.sleep(60)
        if scenario == "retry-failed":
            done("failed")
        elif scenario == "retry-interrupted":
            done("interrupted")
        elif scenario == "retry-terminal-error":
            event("error", threadId="thread", turnId="turn", willRetry=False, error={"message":"SECRET terminal"})
            done()
        elif scenario == "compaction-failed":
            done("failed")
        elif scenario == "compaction":
            event("turn/completed", threadId="thread", turn={"id":"turn", "status":"completed", "items":[{"id":"compact", "type":"contextCompaction"}], "error":None})
        elif scenario in ("failed", "interrupted"):
            done(scenario)
        elif scenario == "late-tool":
            wire = [{"method":"turn/completed", "params":{"threadId":"thread", "turn":{"id":"turn", "status":"completed", "items":[], "error":None}}},
                    {"method":"item/started", "params":{"threadId":"other", "turnId":"other", "item":{"id":"tool", "type":"commandExecution"}}}]
            os.write(1, b"".join((json.dumps(m)+"\n").encode() for m in wire))
        elif scenario in ("embedded-tool", "other-embedded-tool"):
            event("turn/completed", threadId="other" if scenario == "other-embedded-tool" else "thread", turn={"id":"turn", "status":"completed", "items":[{"id":"tool", "type":"fileChange"}], "error":None})
        else:
            done()
'''


@unittest.skipUnless(os.name == "posix", "POSIX adapter")
class CodexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-codex-")
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "requests.jsonl"
        self.processes, self.calls = [], []
        self.real_popen = subprocess.Popen
        # Fake auth locations are empty; fixture does not read them at all.
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.codex_home = Path(self.temp.name) / "codex-home"
        self.codex_home.mkdir()
        p = patch.dict(os.environ, {"HOME": str(self.home), "CODEX_HOME": str(self.codex_home)})
        p.start()
        self.addCleanup(p.stop)
        p = patch("lucid.codex.executable", return_value="/offline/codex")
        p.start()
        self.addCleanup(p.stop)

    def fixture(self, scenario="normal"):
        def spawn(args, **kwargs):
            self.calls.append((args, kwargs))
            process = self.real_popen([sys.executable, "-u", "-c", _SERVER, scenario, str(self.log)], **kwargs)
            self.processes.append(process)
            return process
        return patch("lucid.codex.subprocess.Popen", side_effect=spawn)

    def records(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def requests(self):
        return [r for r in self.records() if "method" in r]

    def assert_clean(self):
        for process in self.processes:
            self.assertIsNotNone(process.poll())
            for stream in (process.stdin, process.stdout, process.stderr):
                self.assertTrue(stream.closed)
        for _, kwargs in self.calls:
            self.assertFalse(Path(kwargs["cwd"]).exists())

    def test_executable_resolution(self):
        # Exercise the original resolver without ever executing its result.
        spec = importlib.util.spec_from_file_location("offline_codex", codex.__file__)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.dict(os.environ, {"LUCID_CODEX_CLI": " ~/bin/codex "}), patch.object(module.shutil, "which", return_value="/fixture/codex") as which:
            self.assertEqual(module.executable(), "/fixture/codex")
            which.assert_called_once_with(str(self.home / "bin/codex"))
        with patch.dict(os.environ, {"LUCID_CODEX_CLI": " "}), patch.object(module.shutil, "which") as which:
            self.assertIsNone(module.executable())
            which.assert_not_called()
        with patch.dict(os.environ, {}, clear=True), patch.object(module.shutil, "which", return_value=None) as which:
            self.assertIsNone(module.executable())
            which.assert_called_once_with("codex")

    def test_catalog_pagination_execution_ids_only_no_inference(self):
        with self.fixture():
            self.assertEqual(codex.list_models(), [{"id": "a-real"}, {"id": "z-real"}])
        requests = self.requests()
        self.assertEqual([r["method"] for r in requests], ["initialize", "initialized", "model/list", "model/list"])
        self.assertEqual(requests[-1]["params"]["cursor"], "page2")
        self.assertFalse(any("jsonrpc" in r for r in requests))
        self.assert_clean()

    def test_resolve_default_and_pinned_catalog_only(self):
        for requested, actual in [(codex.DEFAULT_MODEL, "a-real"), (" z-real ", "z-real")]:
            with self.fixture():
                self.assertEqual(codex.resolve_model(requested), actual)
        self.assertFalse(any(r["method"] in ("thread/start", "turn/start") for r in self.requests()))
        self.assert_clean()

    def test_complete_wire_and_isolation(self):
        tokens = {name: "SECRET" for name in ["OPENAI_API_KEY", "OPENAI_BASE_URL", "LUCID_GITHUB_TOKEN", "ANTHROPIC_API_KEY", "AWS_SESSION_TOKEN", "GITHUB_TOKEN", "PYTHONPATH", "HTTPS_PROXY"]}
        with patch.dict(os.environ, tokens), self.fixture():
            self.assertEqual(codex.complete("SYSTEM source", "USER source", "z-real", reasoning_effort="high"), '{"answer":"héllo"}')
        args, kwargs = self.calls[0]
        self.assertEqual(args[:4], ["/offline/codex", "app-server", "--listen", "stdio://"])
        self.assertNotIn("--ignore-user-config", args)
        self.assertFalse(any("bypass" in arg for arg in args))
        self.assertNotIn("SYSTEM source", " ".join(args))
        self.assertNotIn("USER source", " ".join(args))
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["start_new_session"])
        self.assertFalse(set(tokens) & set(kwargs["env"]))
        self.assertEqual(kwargs["env"]["HOME"], str(self.home))
        self.assertEqual(kwargs["env"]["CODEX_HOME"], str(self.codex_home))
        startup = self.records()[0]
        self.assertEqual(startup["files"], [])
        self.assertEqual(startup["mode"], 0o700)
        requests = self.requests()
        thread = next(r["params"] for r in requests if r["method"] == "thread/start")
        turn = next(r["params"] for r in requests if r["method"] == "turn/start")
        self.assertTrue(thread["ephemeral"])
        self.assertEqual(thread["sandbox"], "read-only")
        self.assertEqual(thread["modelProvider"], "openai")
        self.assertIn("SYSTEM source", thread["developerInstructions"])
        self.assertNotIn("USER source", thread["developerInstructions"])
        self.assertIn("valid JSON object", thread["developerInstructions"])
        self.assertEqual(thread["config"], codex._CONFIG)
        self.assertFalse(thread["config"]["features.shell_tool"])
        self.assertFalse(thread["config"]["features.hooks"])
        self.assertEqual(turn["input"], [{"type": "text", "text": "USER source"}])
        self.assertEqual(turn["model"], "z-real")
        self.assertEqual(turn["effort"], "high")
        self.assertEqual(turn["sandboxPolicy"], {"type": "readOnly", "networkAccess": False})
        self.assertEqual(sum(r["method"] == "turn/start" for r in requests), 1)
        self.assert_clean()

    def test_early_notifications_null_phase_dedup_and_no_json_mode(self):
        for scenario in ("early", "null-phase", "duplicate-answer", "stderr"):
            with self.subTest(scenario=scenario), self.fixture(scenario):
                self.assertEqual(codex.complete("system", "user", json_mode=False), '{"answer":"héllo"}')
        threads = [r["params"] for r in self.requests() if r["method"] == "thread/start"]
        self.assertTrue(all(t["model"] == "a-real" for t in threads))
        self.assertTrue(all("valid JSON object" not in t["developerInstructions"] for t in threads))
        self.assertTrue(all("effort" not in r["params"] for r in self.requests() if r["method"] == "turn/start"))
        self.assert_clean()

    def test_unknown_models_and_unsupported_effort_do_not_start_thread(self):
        cases: list[tuple[str, dict[str, Any]]] = [("normal", {"model":"picker-z"}), ("normal", {"model":"unknown"}),
                                 ("normal", {"model":"z-real", "reasoning_effort":"low"}),
                                 ("missing-efforts", {"reasoning_effort":"high"})]
        for scenario, kwargs in cases:
            with self.subTest(scenario=scenario, kwargs=kwargs), self.fixture(scenario), self.assertRaises(codex.CodexError):
                codex.complete("s", "u", **kwargs)
        self.assertFalse(any(r["method"] in ("thread/start", "turn/start") for r in self.requests()))
        self.assert_clean()

    def test_validation_before_spawn(self):
        with patch("lucid.codex.subprocess.Popen") as spawn:
            cases: list[dict[str, Any]] = [{"system": None}, {"user": []}, {"model": "bad model"}, {"model": 3},
                           {"reasoning_effort": "xhigh"}, {"reasoning_effort": []}, {"json_mode": "yes"}]
            for kwargs in cases:
                with self.subTest(kwargs=kwargs), self.assertRaises(codex.CodexError):
                    params: dict[str, Any] = {"system": "s", "user": "u", **kwargs}
                    codex.complete(**params)
            invalid_models: tuple[Any, ...] = (None, "", "bad model", "\x00", 3)
            for model in invalid_models:
                with self.assertRaises(codex.CodexError): codex.resolve_model(model)
            with patch.object(codex, "_MAX_INPUT", 10), self.assertRaises(codex.CodexError):
                codex.complete("s", "u")
            spawn.assert_not_called()

    def test_safe_catalog_errors(self):
        for scenario in ("rpc-error", "wrong-id", "bad-result", "bad-model", "missing-model", "empty-models",
                         "repeat-cursor", "bad-cursor", "inconsistent", "catalog-tool"):
            with self.subTest(scenario=scenario), self.fixture(scenario), self.assertRaises(codex.CodexError) as raised:
                codex.resolve_model(codex.DEFAULT_MODEL)
            self.assertNotIn("SECRET", str(raised.exception))
        self.assert_clean()

    def test_ambiguous_default_and_duplicate_catalog(self):
        for scenario in ("missing-default", "two-defaults"):
            with self.fixture(scenario), self.assertRaises(codex.CodexError):
                codex.resolve_model(codex.DEFAULT_MODEL)
        with self.fixture("duplicate"):
            self.assertEqual(codex.list_models(), [{"id":"a-real"}, {"id":"z-real"}])
        self.assert_clean()

    def test_effective_thread_policy_checked_before_inference(self):
        for scenario in ("unsafe-sandbox", "unsafe-network", "unsafe-model", "unsafe-provider", "unsafe-policy",
                         "unsafe-reviewer", "unsafe-cwd", "persistent", "instructions", "mcp-startup", "warning"):
            with self.subTest(scenario=scenario), self.fixture(scenario), self.assertRaises(codex.CodexError) as raised:
                codex.complete("s", "u")
            self.assertNotIn("SECRET", str(raised.exception))
        self.assertFalse(any(r["method"] == "turn/start" for r in self.requests()))
        self.assert_clean()

    def test_intentional_restriction_advisories_allow_successful_text_turn(self):
        for scenario in ("skill-advisory", "code-mode-advisory", "direct-tools-advisory"):
            with self.subTest(scenario=scenario), self.fixture(scenario):
                self.assertEqual(codex.complete("system", "user"), '{"answer":"héllo"}')
        self.assertEqual(sum(r["method"] == "turn/start" for r in self.requests()), 3)
        self.assert_clean()

    def test_other_warnings_and_tools_after_advisory_still_fail_closed(self):
        for scenario in ("unknown-advisory", "advisory-then-tool"):
            with self.subTest(scenario=scenario), self.fixture(scenario), self.assertRaises(codex.CodexError) as raised:
                codex.complete("s", "u")
            self.assertNotIn("SECRET", str(raised.exception))
        self.assert_clean()

    def test_warning_allowlist_rejects_configuration_fallback_and_changed_features(self):
        connection = object.__new__(codex._Connection)
        harmless = "Under-development features enabled: skip_host_skill_discovery. Under-development features are incomplete and may behave unpredictably. To suppress this warning, set `suppress_unstable_features_warning = true` in /isolated/config.toml."
        connection._notification({"method": "warning", "params": {"message": harmless}})
        for text in (None, {}, "Invalid configuration; using defaults.",
                     harmless.replace("skip_host_skill_discovery", "skip_host_skill_discovery, code_mode"),
                     harmless + " SECRET", harmless.replace("/isolated/config.toml", "/isolated/\nconfig.toml")):
            with self.subTest(text=text), self.assertRaises(codex.CodexError):
                connection._notification({"method": "warning", "params": {"message": text}})
        with self.assertRaises(codex.CodexError):
            connection._notification({"method": "configWarning", "params": {"summary": harmless}})

    def test_inherited_mcp_disabled_before_thread_without_copying_secrets(self):
        with self.fixture("inherited-mcp"):
            self.assertEqual(codex.complete("s", "u"), '{"answer":"héllo"}')
        requests = self.requests()
        config_read = next(r for r in requests if r["method"] == "config/read")
        thread = next(r for r in requests if r["method"] == "thread/start")
        self.assertEqual(config_read["params"], {"includeLayers": False, "cwd": thread["params"]["cwd"]})
        self.assertLess(requests.index(config_read), requests.index(thread))
        self.assertEqual(thread["params"]["config"]["mcp_servers"], {
            'server.with.dots"quoted': {"enabled": False}, "already-disabled": {"enabled": False}})
        self.assertNotIn("SECRET", json.dumps(requests))
        self.assertNotIn("mcp_servers", codex._CONFIG)
        self.assertFalse(any(r["method"].startswith("config/") and r["method"] != "config/read" for r in requests))
        self.assert_clean()

    def test_invalid_mcp_discovery_or_ignored_override_fails_before_turn(self):
        for scenario in ("bad-mcp-config", "bad-mcp-entry", "bad-mcp-name", "many-mcp-servers",
                         "missing-config", "config-read-error", "inherited-mcp-ignored"):
            before = len(self.requests()) if self.log.exists() else 0
            with self.subTest(scenario=scenario), self.fixture(scenario), self.assertRaises(codex.CodexError) as raised:
                codex.complete("s", "u")
            self.assertNotIn("SECRET", str(raised.exception))
            new_requests = self.requests()[before:]
            self.assertFalse(any(r["method"] == "turn/start" for r in new_requests))
            if scenario != "inherited-mcp-ignored":
                self.assertFalse(any(r["method"] == "thread/start" for r in new_requests))
        self.assert_clean()

    def test_automatic_compaction_is_not_a_tool_or_final_answer(self):
        with self.fixture("compaction"):
            self.assertEqual(codex.complete("s", "u"), '{"answer":"héllo"}')
        for scenario in ("compaction-then-tool", "compaction-no-final", "compaction-failed"):
            with self.subTest(scenario=scenario), self.fixture(scenario), self.assertRaises(codex.CodexError) as raised:
                codex.complete("s", "u")
            self.assertNotIn("SECRET", str(raised.exception))
        for item in ({"type": "contextCompaction"}, {"type": "contextCompaction", "id": "bad id SECRET"}):
            with self.subTest(item=item), self.assertRaises(codex.CodexError):
                codex._Connection._check_item(item)
        self.assert_clean()

    def test_activity_categories_are_fixed_and_redacted(self):
        connection = object.__new__(codex._Connection)
        methods = {
            "mcpServer/startupStatus/updated": "MCP server activity",
            "hook/started": "hook activity",
            "model/rerouted": "model rerouting",
            "turn/plan/updated": "plan-tool activity",
            "turn/diff/updated": "file-diff activity",
            "process/exited": "command or process activity",
            "item/fileChange/outputDelta": "file-change activity",
            "item/tool/SECRET": "tool activity",
        }
        for method, category in methods.items():
            with self.subTest(method=method), self.assertRaises(codex.CodexError) as raised:
                connection._notification({"method": method, "params": {"name": "SECRET", "source": "SECRET"}})
            self.assertIn(category, str(raised.exception))
            self.assertNotIn("SECRET", str(raised.exception))
        for kind, category in (("commandExecution", "command execution"), ("mcpToolCall", "MCP tool call"),
                               ("SECRET", "unrecognized item"), (["SECRET"], "invalid item type")):
            with self.subTest(kind=kind), self.assertRaises(codex.CodexError) as raised:
                connection._check_item({"type": kind, "text": "SECRET"})
            self.assertIn(category, str(raised.exception))
            self.assertNotIn("SECRET", str(raised.exception))

    def test_tool_events_fail_closed_regardless_of_scope(self):
        kinds = ("commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall", "collabAgentToolCall",
                 "collabToolCall", "subAgentActivity", "webSearch", "imageView", "imageGeneration", "hookPrompt", "sleep", "newTool")
        for scenario in ["tool:" + kind for kind in kinds] + ["hook", "late-tool", "embedded-tool", "other-embedded-tool", "rerouted"]:
            with self.subTest(scenario=scenario), self.fixture(scenario), self.assertRaises(codex.CodexError) as raised:
                codex.complete("s", "u")
            self.assertNotIn("SECRET", str(raised.exception))
        self.assert_clean()

    def test_notification_only_tool_activity_fails_closed(self):
        methods = ("turn/plan/updated", "command/exec/outputDelta", "process/outputDelta",
                   "process/exited", "mcpServer/event/stream/notification")
        for method in methods:
            for scope in ("active", "other", "unscoped"):
                with self.subTest(method=method, scope=scope), self.fixture("notification:" + method + ":" + scope), self.assertRaises(codex.CodexError) as raised:
                    codex.complete("s", "u")
                self.assertNotIn("SECRET", str(raised.exception))
        self.assert_clean()

    def test_internal_retry_error_waits_for_success_without_new_turn(self):
        with self.fixture("retry-success"):
            self.assertEqual(codex.complete("s", "u"), '{"answer":"héllo"}')
        self.assertEqual([r["method"] for r in self.requests()],
                         ["initialize", "initialized", "model/list", "model/list", "config/read", "thread/start", "turn/start"])
        self.assert_clean()

    def test_internal_retry_still_requires_terminal_success(self):
        for scenario in ("retry-failed", "retry-interrupted", "retry-terminal-error", "retry-timeout"):
            before = len(self.requests()) if self.log.exists() else 0
            with self.subTest(scenario=scenario), self.fixture(scenario), patch.object(codex, "_COMPLETION_TIMEOUT", 0.35), self.assertRaises(codex.CodexError) as raised:
                codex.complete("s", "u")
            self.assertNotIn("SECRET", str(raised.exception))
            self.assertEqual(sum(r["method"] == "turn/start" for r in self.requests()[before:]), 1)
        self.assert_clean()

    def test_incoming_requests_denied_then_fail(self):
        methods = ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                   "item/permissions/requestApproval", "mcpServer/elicitation/request", "item/tool/call",
                   "item/tool/requestUserInput", "fs/readFile", "process/spawn", "account/chatgptAuthTokens/refresh", "unknown/extension")
        original_send = codex._Connection._send
        sent = []
        def capture(connection, message):
            original_send(connection, message)
            sent.append(message)
        for method in methods:
            with self.subTest(method=method), self.fixture("request:" + method), patch.object(codex._Connection, "_send", capture), self.assertRaises(codex.CodexError):
                codex.complete("s", "u")
            response = next(r for r in reversed(sent) if r.get("id") == "server-1")
            if method == "item/permissions/requestApproval":
                self.assertEqual(response["result"], {"permissions": {}, "scope": "turn"})
            elif method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                self.assertEqual(response["result"], {"decision":"decline"})
            elif method == "mcpServer/elicitation/request":
                self.assertEqual(response["result"], {"action":"decline", "content":None})
            else:
                self.assertEqual(response["error"]["code"], -32601)
        self.assert_clean()

    def test_no_partial_answers_on_failure(self):
        for scenario in ("failed", "interrupted", "turn-error", "no-final", "commentary-only", "empty-answer",
                         "bad-text", "inconsistent-answer", "turn-rpc-error"):
            with self.subTest(scenario=scenario), self.fixture(scenario), self.assertRaises(codex.CodexError) as raised:
                codex.complete("s", "u")
            self.assertNotIn("SECRET", str(raised.exception))
        self.assert_clean()

    def test_transport_failures_are_safe(self):
        for scenario in ("exit", "malformed", "unfinished", "bad-message", "jsonrpc"):
            with self.subTest(scenario=scenario), self.fixture(scenario), self.assertRaises(codex.CodexError) as raised:
                codex.complete("SECRET s", "SECRET u")
            self.assertNotIn("SECRET", str(raised.exception))
        with patch("lucid.codex.subprocess.Popen", side_effect=OSError("SECRET")), self.assertRaises(codex.CodexError) as raised:
            codex.list_models()
        self.assertNotIn("SECRET", str(raised.exception))
        with patch("lucid.codex.executable", return_value=None), self.assertRaises(codex.CodexError):
            codex.list_models()
        self.assert_clean()

    def test_timeouts_and_stalled_input_are_bounded(self):
        for scenario in ("timeout", "turn-timeout", "no-read", "stall-turn-write"):
            start = time.monotonic()
            with self.subTest(scenario=scenario), self.fixture(scenario), patch.object(codex, "_COMPLETION_TIMEOUT", 0.35), self.assertRaises(codex.CodexError):
                codex.complete("s", "u" * 500000)
            self.assertLess(time.monotonic() - start, 3)
        with self.fixture("catalog-timeout"), patch.object(codex, "_CATALOG_TIMEOUT", 0.2), self.assertRaises(codex.CodexError):
            codex.list_models()
        self.assert_clean()

    def test_output_frame_and_pagination_bounds(self):
        for scenario, constant, limit in [("frame-limit", "_MAX_FRAME", 1024), ("output-limit", "_MAX_OUTPUT", 1024),
                                          ("normal", "_MAX_PAGES", 1)]:
            with self.subTest(scenario=scenario), self.fixture(scenario), patch.object(codex, constant, limit), self.assertRaises(codex.CodexError):
                codex.list_models()
        self.assert_clean()

    def test_partial_setup_cleanup_and_kill_escalation(self):
        with self.fixture(), patch("lucid.codex.os.set_blocking", side_effect=OSError("SECRET")), self.assertRaises(codex.CodexError):
            codex.list_models()
        with self.fixture("ignore-term"), patch.object(codex, "_CATALOG_TIMEOUT", 0.2), patch.object(codex, "_CLEANUP_TIMEOUT", 0.1), self.assertRaises(codex.CodexError):
            codex.list_models()
        self.assertEqual(self.processes[-1].returncode, -signal.SIGKILL)
        self.assert_clean()

    def test_descendant_process_group_cleanup(self):
        original_killpg = os.killpg
        with self.fixture("descendant"), patch("lucid.codex.os.killpg", wraps=original_killpg) as killpg:
            codex.complete("s", "u")
        self.assertTrue(any("child" in r for r in self.records()))
        pid = self.processes[0].pid
        self.assertEqual([call.args for call in killpg.call_args_list],
                         [(pid, signal.SIGTERM), (pid, signal.SIGKILL)])
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                original_killpg(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            self.fail("Fixture process group survived cleanup")
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
