"""Offline backend tests; provider calls are always mocked."""

import json
import os
import re
import time
import unittest
from threading import Event
from unittest.mock import patch

import httpx
import openai

from fastapi import HTTPException
from fastapi.testclient import TestClient

from lucid.web import (
    AuditRequest,
    AuditStore,
    SourceFile,
    app,
    parse_findings,
    provider_error,
    uploaded_codebase,
)

HEADERS = {"X-Lucid-Request": "local-ui"}
FINDING = {
    "title": "Example", "severity": "high", "location": "Token.sol:1",
    "description": "Example logic", "impact": "Example impact", "exploit_steps": ["Example step"],
}


class WebTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"LUCID_PROVIDER": "api"})
        environment.start()
        self.addCleanup(environment.stop)
        self.client_context = TestClient(app, base_url="http://localhost:8765")
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)

    def wait(self, job_id):
        for _ in range(100):
            job = self.client.get(f"/api/audits/{job_id}").json()
            if job["status"] in ("completed", "failed"):
                return job
            time.sleep(0.01)
        self.fail("Mock audit did not finish")

    def test_config_and_removed_example_routes(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "OPENAI_BASE_URL": "https://user:secret@example.com/v1"}):
            config = self.client.get("/api/config").json()
        self.assertEqual(config["provider"], "example.com")
        self.assertNotIn("secret", json.dumps(config))
        self.assertEqual(self.client.get("/api/examples").status_code, 404)
        self.assertEqual(self.client.get("/api/examples/run-1").status_code, 404)

    def test_keyless_kiro_config_catalog_and_pinned_audit(self):
        with patch.dict(os.environ, {"LUCID_PROVIDER": "auto", "OPENAI_API_KEY": "", "OPENAI_MODEL": "stale/requesty"}), patch("lucid.kiro.executable", return_value="/fake/kiro-cli"), patch("lucid.kiro.list_models", return_value=[{"id": "kiro-model"}]), patch("lucid.kiro.resolve_model", return_value="kiro-model") as resolve, patch("lucid.kiro.complete", return_value='{"findings": []}') as complete:
            config = self.client.get("/api/config").json()
            self.assertEqual(config["provider_kind"], "kiro")
            self.assertFalse(config["key_configured"])
            self.assertTrue(config["provider_ready"])
            self.assertEqual(config["model"], "kiro-default")
            self.assertEqual(self.client.get("/api/models").json(), {"models": [{"id": "kiro-model"}]})
            complete.assert_not_called()
            response = self.client.post("/api/audits", headers=HEADERS, json={"confirmed_paid": True, "mode": "single", "reasoning_effort": "high"})
            self.assertEqual(response.status_code, 400)
            resolve.assert_not_called()
            complete.assert_not_called()
            response = self.client.post("/api/audits", headers=HEADERS, json={"confirmed_paid": True, "mode": "single"})
            self.assertEqual(response.status_code, 202)
            job = self.wait(response.json()["id"])
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["model"], "kiro-model")
            self.assertEqual(job["provider_kind"], "kiro")
            self.assertEqual(complete.call_args.kwargs["model"], "kiro-model")
            resolve.assert_called_once_with("kiro-default")

    def test_explicit_kiro_with_api_key_uses_selected_metadata_and_preflight(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake", "OPENAI_MODEL": "stale/api", "LUCID_KIRO_MODEL": "kiro-default"}), patch("lucid.kiro.executable", return_value="/fake/kiro-cli"), patch("lucid.kiro.list_models", return_value=[{"id": "kiro/model"}]) as catalog, patch("lucid.kiro.resolve_model", return_value="kiro/model") as resolve, patch("lucid.kiro.complete", return_value='{"findings": []}') as complete, patch("lucid.llm._client") as api:
            before = dict(os.environ)
            config = self.client.get("/api/config?provider=kiro").json()
            self.assertEqual(config["provider_kind"], "kiro")
            self.assertEqual(config["model"], "kiro-default")
            self.assertEqual(config["provider"], "Kiro CLI (ACP)")
            self.assertTrue(config["key_configured"])
            self.assertTrue(config["provider_ready"])
            self.assertFalse(config["reasoning_supported"])
            catalog.assert_not_called()
            resolve.assert_not_called()
            self.assertEqual(self.client.get("/api/models?provider=kiro").json(), {"models": [{"id": "kiro/model"}]})
            complete.assert_not_called()
            body = {"confirmed_paid": True, "provider": "kiro"}
            rejected = self.client.post("/api/audits", headers=HEADERS, json=body | {"reasoning_effort": "high"})
            self.assertEqual(rejected.status_code, 400)
            resolve.assert_not_called()
            complete.assert_not_called()
            started = self.client.post("/api/audits", headers=HEADERS, json=body)
            self.assertEqual(started.status_code, 202)
            job = self.wait(started.json()["id"])
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["provider_kind"], "kiro")
            self.assertEqual(job["model"], "kiro/model")
            resolve.assert_called_once_with("kiro-default")
            api.assert_not_called()
            self.assertEqual(self.client.get("/api/config").json()["provider_kind"], "api")
            self.assertEqual(dict(os.environ), before)

    def test_codex_config_catalog_and_pinned_context_audit(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "", "LUCID_CODEX_MODEL": "codex-default"}), patch("lucid.codex.executable", return_value="/fake/codex"), patch("lucid.codex.list_models", return_value=[{"id": "codex-model"}]), patch("lucid.codex.resolve_model", return_value="codex-model") as resolve, patch("lucid.codex.complete", side_effect=["Protocol context", '{"findings": []}']) as complete, patch("lucid.llm._client") as api:
            config = self.client.get("/api/config?provider=codex").json()
            self.assertEqual(config["provider_kind"], "codex")
            self.assertEqual(config["provider"], "Codex CLI")
            self.assertEqual(config["model"], "codex-default")
            self.assertTrue(config["provider_ready"])
            self.assertTrue(config["reasoning_supported"])
            self.assertFalse(config["key_configured"])
            self.assertEqual(self.client.get("/api/models?provider=codex").json(), {"models": [{"id": "codex-model"}]})
            complete.assert_not_called()
            response = self.client.post("/api/audits", headers=HEADERS, json={"confirmed_paid": True, "provider": "codex", "mode": "context", "reasoning_effort": "high"})
            self.assertEqual(response.status_code, 202)
            job = self.wait(response.json()["id"])
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["provider_kind"], "codex")
            self.assertEqual(job["model"], "codex-model")
            resolve.assert_called_once_with("codex-default")
            self.assertEqual(complete.call_count, 2)
            for call in complete.call_args_list:
                self.assertEqual(call.kwargs["model"], "codex-model")
                self.assertEqual(call.kwargs["reasoning_effort"], "high")
            api.assert_not_called()

    def test_codex_catalog_preflight_errors_do_not_start_inference(self):
        from lucid.codex import CodexError
        with patch("lucid.codex.executable", return_value="/fake/codex"), patch("lucid.codex.list_models", side_effect=CodexError("Codex operation timed out.")), patch("lucid.codex.resolve_model", side_effect=CodexError("The selected model is not available in Codex.")), patch("lucid.codex.complete") as complete:
            response = self.client.get("/api/models?provider=codex")
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.json()["provider_error_code"], "codex")
            response = self.client.post("/api/audits", headers=HEADERS, json={"provider": "codex", "confirmed_paid": True})
            self.assertEqual(response.status_code, 400)
            complete.assert_not_called()

    def test_explicit_api_without_key_never_falls_back_to_kiro(self):
        with patch.dict(os.environ, {"LUCID_PROVIDER": "kiro", "OPENAI_API_KEY": "", "OPENAI_MODEL": "api/default"}), patch("lucid.kiro.executable", return_value="/fake/kiro-cli"), patch("lucid.kiro.list_models") as catalog, patch("lucid.kiro.resolve_model") as resolve, patch("lucid.kiro.complete") as complete, patch("lucid.llm._client") as api:
            config = self.client.get("/api/config?provider=api").json()
            self.assertEqual(config["provider_kind"], "api")
            self.assertFalse(config["provider_ready"])
            self.assertEqual(config["model"], "api/default")
            self.assertTrue(config["reasoning_supported"])
            self.assertEqual(self.client.get("/api/models?provider=api").status_code, 400)
            self.assertEqual(self.client.post("/api/audits", headers=HEADERS, json={"confirmed_paid": True, "provider": "api"}).status_code, 400)
            catalog.assert_not_called()
            resolve.assert_not_called()
            complete.assert_not_called()
            api.assert_not_called()

    def test_provider_queries_validate_and_dispatch_without_inference(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch("lucid.web.llm.list_models", return_value=[{"id": "api/model"}]) as catalog, patch("lucid.web.llm.complete") as complete:
            for selection in ("api", "auto"):
                self.assertEqual(self.client.get(f"/api/models?provider={selection}").json(), {"models": [{"id": "api/model"}]})
                self.assertEqual(catalog.call_args.kwargs, {"provider": "api"})
            for invalid in ("unknown", "API", ""):
                self.assertEqual(self.client.get(f"/api/config?provider={invalid}").status_code, 422)
                self.assertEqual(self.client.get(f"/api/models?provider={invalid}").status_code, 422)
                self.assertEqual(self.client.post("/api/audits", headers=HEADERS, json={"confirmed_paid": True, "provider": invalid}).status_code, 422)
            self.assertEqual(catalog.call_count, 2)
            complete.assert_not_called()

    def test_kiro_login_failure_is_safe_and_never_starts_inference(self):
        from lucid.kiro import KiroError
        with patch.dict(os.environ, {"LUCID_PROVIDER": "kiro", "OPENAI_API_KEY": ""}), patch("lucid.kiro.executable", return_value="/fake/kiro-cli"), patch("lucid.kiro.resolve_model", side_effect=KiroError("Kiro ACP request failed. Check CLI authentication, model access, and configuration.")), patch("lucid.kiro.list_models", side_effect=KiroError("Kiro operation timed out.")), patch("lucid.kiro.complete") as complete:
            catalog = self.client.get("/api/models")
            self.assertEqual(catalog.status_code, 502)
            self.assertEqual(catalog.json()["provider_error_code"], "kiro")
            response = self.client.post("/api/audits", headers=HEADERS, json={"confirmed_paid": True})
            self.assertEqual(response.status_code, 400)
            self.assertIn("authentication", response.json()["detail"])
            complete.assert_not_called()

    def test_request_guards(self):
        body = {"confirmed_paid": True}
        self.assertEqual(self.client.post("/api/audits", json=body).status_code, 403)
        headers = HEADERS | {"Origin": "https://evil.example"}
        self.assertEqual(self.client.post("/api/audits", json=body, headers=headers).status_code, 403)
        self.assertEqual(self.client.get("/api/config", headers={"Host": "evil.example"}).status_code, 400)
        self.assertEqual(self.client.post("/api/audits", json={}, headers=HEADERS).status_code, 400)
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(self.client.post("/api/audits", json=body, headers=HEADERS).status_code, 400)
        self.assertEqual(self.client.post("/api/audits", content=b"x" * 2_000_001, headers=HEADERS).status_code, 413)

    def test_upload_validation(self):
        code = uploaded_codebase([SourceFile(path="B.sol", content="b"), SourceFile(path="a/A.sol", content="a")])
        self.assertEqual(code, "// FILE: B.sol\n1: b\n\n// FILE: a/A.sol\n1: a\n")
        unicode_source = uploaded_codebase([SourceFile(path="A.sol", content="// comment\u2028separator\r\ncontract A {}\n")])
        self.assertIn("1: // comment\u2028separator\n2: contract A {}", unicode_source)
        for name in ("../A.sol", "/A.sol", "A.txt", "a\nA.sol", "C:/A.sol"):
            with self.assertRaises(ValueError):
                uploaded_codebase([SourceFile(path=name, content="a")])
        with self.assertRaises(ValueError):
            uploaded_codebase([SourceFile(path="A.sol", content="a")] * 2)
        with self.assertRaises(ValueError):
            uploaded_codebase([SourceFile(path="A.sol", content="x" * 400_000)])

    def test_bad_model_output_is_not_clean(self):
        for raw in ("not json", "[]", '{"findings": null}', '{"findings": [{}]}'):
            with self.assertRaises(ValueError):
                parse_findings(raw)
        findings, warnings = parse_findings(json.dumps({"findings": [FINDING, {}]}))
        self.assertEqual(len(findings), 1)
        self.assertEqual(len(warnings), 1)

    def test_loop_progress_and_exclusion(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch("lucid.web.context.build_context", return_value="Protocol context"), patch("lucid.web.llm.complete", side_effect=[json.dumps({"findings": [FINDING]}), '{"findings": []}']) as complete:
            response = self.client.post("/api/audits", headers=HEADERS, json={"confirmed_paid": True, "mode": "loop", "rounds": 2})
            self.assertEqual(response.status_code, 202)
            job = self.wait(response.json()["id"])
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["completed_rounds"], 2)
            self.assertEqual(job["context"], "Protocol context")
            self.assertEqual(len(job["findings"]), 1)
            self.assertIn("Already found", complete.call_args_list[1].args[1])
        self.assertEqual(len(self.client.get("/api/audits").json()), 1)

    def test_provider_failure_redacted(self):
        for mode, target, error in (
            ("single", "lucid.web.llm.complete", RuntimeError("secret-token")),
            ("single", "lucid.web.llm.complete", ValueError("secret-token")),
            ("context", "lucid.web.context.build_context", ValueError("secret-token")),
        ):
            with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch(target, side_effect=error):
                response = self.client.post("/api/audits", headers=HEADERS, json={"confirmed_paid": True, "mode": mode})
                job = self.wait(response.json()["id"])
            self.assertEqual(job["status"], "failed")
            self.assertNotIn("secret-token", job["error"])

    def test_provider_status_categories_never_expose_raw_details(self):
        for status in (400, 401, 402, 403, 404, 413, 429, 500, 503, 418):
            with self.subTest(status=status):
                error = openai.APIStatusError(
                    "secret-token private-source", body={"secret": "secret-token"},
                    response=httpx.Response(status, request=httpx.Request("GET", "https://provider.example/v1/models")),
                )
                message = provider_error(error)
                self.assertNotIn("secret-token", message)
                self.assertNotIn("private-source", message)
                if status < 500 and status != 418:
                    self.assertIn(f"HTTP {status}", message)
                elif status >= 500:
                    self.assertIn("HTTP 5xx", message)
                with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch("lucid.web.llm.list_models", side_effect=error):
                    response = self.client.get("/api/models")
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json()["provider_status"], status)
                self.assertNotIn("secret-token", response.text)
                self.assertNotIn("private-source", response.text)
        request = httpx.Request("GET", "https://provider.example")
        self.assertIn("timed out", provider_error(openai.APITimeoutError(request=request)))
        self.assertIn("connect", provider_error(openai.APIConnectionError(request=request, message="secret-token")))

    def test_provider_status_reaches_audit_context_lane_and_judge(self):
        from lucid import judge
        error = openai.PermissionDeniedError(
            "secret-token private-source", body="secret-token",
            response=httpx.Response(403, request=httpx.Request("POST", "https://provider.example/v1/chat/completions")),
        )
        for phase in ("context", "lane", "judge"):
            with self.subTest(phase=phase):
                def complete(system, *args, **kwargs):
                    if phase == "lane" or system == judge.JUDGE_SYSTEM_PROMPT:
                        raise error
                    return json.dumps({"findings": [FINDING]})
                with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch(
                    "lucid.web.context.build_context", side_effect=error if phase == "context" else None, return_value="Context",
                ), patch("lucid.web.llm.complete", side_effect=complete):
                    response = self.client.post("/api/audits", headers=HEADERS, json={"confirmed_paid": True, "mode": "specialists", "rounds": 1})
                    job = self.wait(response.json()["id"])
                self.assertEqual(job["status"], "failed")
                self.assertIn("HTTP 403", job["error"])
                self.assertNotIn("secret-token", json.dumps(job))
                self.assertNotIn("private-source", json.dumps(job))
                if phase == "lane":
                    failed = [lane for lane in job["lanes"] if lane["status"] == "failed"]
                    self.assertTrue(failed)
                    self.assertTrue(all("HTTP 403" in lane["error"] for lane in failed))
                if phase == "judge":
                    self.assertEqual(job["judge_status"], "failed")

    def test_shutdown_stops_additional_calls(self):
        store = AuditStore()
        entered, release = Event(), Event()
        def complete(*args, **kwargs):
            entered.set()
            release.wait(2)
            return '{"findings": []}'
        with patch("lucid.web.context.build_context", return_value="Context"), patch("lucid.web.llm.complete", side_effect=complete) as mocked:
            try:
                job = store.start(AuditRequest(mode="loop", rounds=2), "contract A {}")
                self.assertTrue(entered.wait(2))
                with self.assertRaises(HTTPException) as conflict:
                    store.start(AuditRequest(), "contract A {}")
                self.assertEqual(conflict.exception.status_code, 409)
                store.close()
            finally:
                release.set()
                store.executor.shutdown(wait=True)
            self.assertEqual(mocked.call_count, 1)
            self.assertEqual(store.get(job["id"])["status"], "failed")

    def test_single_and_parse_failure(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch("lucid.web.llm.complete", return_value="bad JSON"):
            response = self.client.post("/api/audits", headers=HEADERS, json={"confirmed_paid": True, "target": "upload", "files": [{"path": "A.sol", "content": "contract A {}"}]})
            self.assertEqual(self.wait(response.json()["id"])["status"], "failed")

    def test_github_preview_and_pinned_audit(self):
        snapshot = {
            "repository_url": "https://github.com/owner/repo", "repository": "owner/repo",
            "ref": "main", "commit": "a" * 40, "subdirectory": "src",
            "files": [{"path": "src/A.sol", "content": "contract A {}"}],
            "file_count": 1, "total_chars": 13,
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), patch("lucid.web.fetch_repository", return_value=snapshot) as fetch, patch("lucid.web.llm.complete") as complete:
            response = self.client.post("/api/github/preview", headers=HEADERS, json={"url": snapshot["repository_url"], "subdirectory": "src"})
            self.assertEqual(response.status_code, 200)
            preview = response.json()
            self.assertEqual(preview["commit"], "a" * 40)
            complete.assert_not_called()
            self.assertEqual(fetch.call_count, 1)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch("lucid.web.fetch_repository") as fetch, patch("lucid.web.llm.complete", return_value='{"findings": []}') as complete:
            response = self.client.post("/api/audits", headers=HEADERS, json={"target": "github", "github_preview_id": preview["id"], "confirmed_paid": True})
            self.assertEqual(response.status_code, 202)
            job = self.wait(response.json()["id"])
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["source"]["commit"], preview["commit"])
            self.assertEqual(job["target"], "owner/repo")
            self.assertIn("// FILE: src/A.sol", complete.call_args.args[1])
            fetch.assert_not_called()

    def test_github_scope_is_validated_pinned_and_used_in_every_phase(self):
        from lucid import judge
        files = [
            {"path": "src/A.sol", "content": "contract Included {}"},
            {"path": "src/B.sol", "content": "contract Excluded {}"},
        ]
        snapshot = {
            "repository_url": "https://github.com/owner/repo", "repository": "owner/repo",
            "ref": "main", "commit": "a" * 40, "subdirectory": "src",
            "files": files, "file_count": 2, "total_chars": sum(len(file["content"]) for file in files),
        }
        with patch("lucid.web.fetch_repository", return_value=snapshot):
            preview = self.client.post("/api/github/preview", headers=HEADERS, json={"url": snapshot["repository_url"]}).json()
        body = {"target": "github", "github_preview_id": preview["id"], "confirmed_paid": True, "mode": "specialists", "rounds": 1}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch("lucid.web.context.build_context", return_value="Context") as build, patch("lucid.web.llm.complete", return_value=json.dumps({"findings": [FINDING]})) as complete, patch("lucid.web.fetch_repository") as fetch:
            for paths in ([], ["src/Unknown.sol"], ["../A.sol"], ["src/A.sol", "src/A.sol"]):
                response = self.client.post("/api/audits", headers=HEADERS, json=body | {"github_paths": paths})
                self.assertEqual(response.status_code, 400)
            build.assert_not_called()
            complete.assert_not_called()
            response = self.client.post("/api/audits", headers=HEADERS, json=body | {"github_paths": ["src/A.sol"], "files": [{"path": "src/A.sol", "content": "tampered"}]})
            self.assertEqual(response.status_code, 202)
            job = self.wait(response.json()["id"])
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["source"]["scope_paths"], ["src/A.sol"])
            self.assertEqual(job["source"]["file_count"], 1)
            self.assertEqual(job["source"]["total_chars"], len(files[0]["content"]))
            self.assertIn("Included", build.call_args.args[0])
            self.assertNotIn("Excluded", build.call_args.args[0])
            self.assertEqual(complete.call_count, 7)
            self.assertEqual(complete.call_args.args[0], judge.JUDGE_SYSTEM_PROMPT)
            for call in complete.call_args_list:
                self.assertIn("Included", call.args[1])
                self.assertNotIn("Excluded", call.args[1])
                self.assertNotIn("tampered", call.args[1])
            fetch.assert_not_called()
        # Selecting a subset does not mutate the cached snapshot.
        source, full = self.client.app.state.previews.get(preview["id"])
        self.assertEqual(source["scope_paths"], ["src/A.sol", "src/B.sol"])
        self.assertIn("Excluded", full)

    def test_private_github_preview_keeps_authentication_backend_only(self):
        snapshot = {
            "repository_url": "https://github.com/owner/private", "repository": "owner/private",
            "ref": "main", "commit": "a" * 40, "subdirectory": "",
            "files": [{"path": "A.sol", "content": "contract A {}"}],
            "file_count": 1, "total_chars": 13,
        }
        with patch.dict(os.environ, {"LUCID_GITHUB_TOKEN": "offline-secret"}), patch("lucid.web.fetch_repository", return_value=snapshot) as fetch, patch("lucid.web.llm.complete") as complete:
            response = self.client.post("/api/github/preview", headers=HEADERS, json={"url": snapshot["repository_url"], "authenticated": True})
            self.assertEqual(response.status_code, 200)
            fetch.assert_called_once_with(snapshot["repository_url"], "", "", authenticated=True)
            complete.assert_not_called()
            self.assertNotIn("offline-secret", response.text)
            source, codebase = self.client.app.state.previews.get(response.json()["id"])
            self.assertNotIn("offline-secret", json.dumps(source) + codebase)
        with patch.dict(os.environ, {"LUCID_GITHUB_TOKEN": ""}), patch("lucid.github.httpx.Client") as client:
            response = self.client.post("/api/github/preview", headers=HEADERS, json={"url": snapshot["repository_url"], "authenticated": True})
            self.assertEqual(response.status_code, 400)
            self.assertIn("LUCID_GITHUB_TOKEN", response.text)
            client.assert_not_called()

    def test_github_preview_errors_and_expiry(self):
        from lucid.github import GitHubSourceError
        with patch("lucid.web.fetch_repository", side_effect=GitHubSourceError("No Solidity files")):
            response = self.client.post("/api/github/preview", headers=HEADERS, json={"url": "https://github.com/owner/repo"})
            self.assertEqual(response.status_code, 400)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch("lucid.web.llm.complete") as complete:
            response = self.client.post("/api/audits", headers=HEADERS, json={"target": "github", "github_preview_id": "missing", "confirmed_paid": True})
            self.assertEqual(response.status_code, 400)
            complete.assert_not_called()
        store = app.state.previews
        snapshot = {"repository_url": "url", "repository": "owner/repo", "ref": "main", "commit": "a" * 40, "subdirectory": ""}
        for _ in range(12):
            store.add(snapshot, "code")
        self.assertEqual(len(store.snapshots), 10)
        with patch("lucid.web.time.monotonic", return_value=time.monotonic() + 901), self.assertRaises(HTTPException):
            store.get(next(iter(store.snapshots)))

    def test_model_catalog_is_explicit_and_redacts_errors(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), patch("lucid.web.llm.list_models") as catalog:
            self.assertEqual(self.client.get("/api/models").status_code, 400)
            catalog.assert_not_called()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch("lucid.web.llm.list_models", return_value=[{"id": "provider/model"}]) as catalog, patch("lucid.web.llm.complete") as complete:
            self.assertEqual(self.client.get("/api/models").json(), {"models": [{"id": "provider/model"}]})
            catalog.assert_called_once()
            complete.assert_not_called()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch("lucid.web.llm.list_models", side_effect=RuntimeError("secret-token")):
            response = self.client.get("/api/models")
            self.assertEqual(response.status_code, 502)
            self.assertNotIn("secret-token", response.text)

    def test_model_selection_is_pinned_for_context_and_every_round(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake", "OPENAI_MODEL": "provider/default"}), patch("lucid.web.context.build_context", return_value="Context") as build, patch("lucid.web.llm.complete", return_value='{"findings": []}') as complete:
            self.assertEqual(self.client.get("/api/config").json()["model"], "provider/default")
            response = self.client.post("/api/audits", headers=HEADERS, json={
                "confirmed_paid": True, "mode": "loop", "rounds": 2,
                "model": " provider/selected ", "json_mode": False, "reasoning_effort": "medium",
            })
            self.assertEqual(response.status_code, 202)
            job = self.wait(response.json()["id"])
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["model"], "provider/selected")
            self.assertFalse(job["json_mode"])
            self.assertEqual(job["reasoning_effort"], "medium")
            self.assertEqual(build.call_args.kwargs, {"model": "provider/selected", "reasoning_effort": "medium", "provider": "api"})
            self.assertEqual(complete.call_count, 2)
            for call in complete.call_args_list:
                self.assertEqual(call.kwargs, {"model": "provider/selected", "json_mode": False, "reasoning_effort": "medium", "provider": "api"})

    def test_invalid_model_settings_never_start_inference(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch("lucid.web.llm.complete") as complete:
            for settings in ({"model": "bad model"}, {"model": ""}, {"model": "x" * 257}, {"reasoning_effort": "unsupported"}):
                response = self.client.post("/api/audits", headers=HEADERS, json={"confirmed_paid": True} | settings)
                self.assertEqual(response.status_code, 422)
            with patch.dict(os.environ, {"OPENAI_MODEL": ""}):
                response = self.client.post("/api/audits", headers=HEADERS, json={"confirmed_paid": True})
                self.assertEqual(response.status_code, 400)
            complete.assert_not_called()

    def test_dashboard(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200, "Build the UI before running dashboard tests")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', response.text)
        self.assertGreaterEqual(len(assets), 2)
        for asset in assets:
            self.assertEqual(self.client.get(asset).status_code, 200)


if __name__ == "__main__":
    unittest.main()
