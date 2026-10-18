"""Provider requests are mocked: no network or paid inference."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from lucid import context, llm, orchestrator


class LLMTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"LUCID_PROVIDER": "api"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_cli_lane_limit_defaults_overrides_and_api_is_unchanged(self):
        with patch.dict(os.environ):
            os.environ.pop("CLI_MAX_CONCURRENT_LANES", None)
            for provider in ("kiro", "codex"):
                self.assertEqual(llm.lane_workers(6, provider), 2)
            for value in ("1", "2", "6", " 3 "):
                os.environ["CLI_MAX_CONCURRENT_LANES"] = value
                for provider in ("kiro", "codex"):
                    self.assertEqual(llm.lane_workers(6, provider), int(value))
                    self.assertEqual(llm.lane_workers(1, provider), 1)
                self.assertEqual(llm.lane_workers(6, "api"), 6)
            for value in ("", "0", "7", "-1", "1.5", "two", "²"):
                os.environ["CLI_MAX_CONCURRENT_LANES"] = value
                for provider in ("kiro", "codex"):
                    with self.subTest(value=value, provider=provider), self.assertRaisesRegex(ValueError, "CLI_MAX_CONCURRENT_LANES"):
                        llm.lane_workers(6, provider)
                self.assertEqual(llm.lane_workers(6, "api"), 6)
            os.environ.update(LUCID_PROVIDER="codex", CLI_MAX_CONCURRENT_LANES="1")
            self.assertEqual(llm.lane_workers(6), 1)

    def test_terminal_specialists_use_cli_lane_limit(self):
        from concurrent.futures import ThreadPoolExecutor

        for provider in ("kiro", "codex"):
            with self.subTest(provider=provider), patch.dict(os.environ, {
                "LUCID_PROVIDER": provider, "CLI_MAX_CONCURRENT_LANES": "1",
            }), patch("lucid.orchestrator.ThreadPoolExecutor", wraps=ThreadPoolExecutor) as executor, patch(
                "lucid.orchestrator.run_specialist_loop", return_value=[],
            ) as specialist, patch("lucid.orchestrator.find_bugs_loop", return_value=[]) as general, patch("builtins.print"):
                findings = orchestrator.run_specialists_parallel("source", "context", rounds=1)
                executor.assert_called_once_with(max_workers=1)
                self.assertEqual(specialist.call_count, 5)
                general.assert_called_once()
                self.assertEqual(len(findings), 6)

    def test_auto_provider_prefers_key_then_kiro_and_never_falls_back_on_failure(self):
        with patch.dict(os.environ, {"LUCID_PROVIDER": "auto", "OPENAI_API_KEY": "fake"}), patch("lucid.llm.kiro.executable", return_value="/fake/kiro-cli"), patch("lucid.llm._client", side_effect=RuntimeError("provider failed")), patch("lucid.llm.kiro.complete") as fallback:
            self.assertEqual(llm.provider_kind(), "api")
            with self.assertRaises(RuntimeError):
                llm.complete("system", "source")
            fallback.assert_not_called()
        with patch.dict(os.environ, {"LUCID_PROVIDER": "auto", "OPENAI_API_KEY": "", "OPENAI_MODEL": "stale/requesty"}), patch("lucid.llm.kiro.executable", return_value="/fake/kiro-cli"), patch("lucid.llm.kiro.complete", return_value="answer") as complete, patch("lucid.llm.kiro.list_models", return_value=[{"id": "kiro-model"}]):
            self.assertEqual(llm.provider_kind(), "kiro")
            self.assertTrue(llm.provider_ready())
            self.assertEqual(llm.default_model(), "kiro-default")
            self.assertEqual(llm.list_models(), [{"id": "kiro-model"}])
            self.assertEqual(llm.complete("system", "source", json_mode=False), "answer")
            complete.assert_called_once_with("system", "source", model="kiro-default", json_mode=False, reasoning_effort=None)
        with patch.dict(os.environ, {"LUCID_PROVIDER": "auto", "OPENAI_API_KEY": ""}), patch("lucid.llm.kiro.executable", return_value=None), patch("lucid.llm.codex.executable", return_value=None):
            self.assertFalse(llm.provider_ready())

    def test_codex_auto_selection_and_explicit_dispatch(self):
        with patch.dict(os.environ, {"LUCID_PROVIDER": "auto", "OPENAI_API_KEY": "", "OPENAI_MODEL": "stale/api", "LUCID_CODEX_MODEL": "codex-default"}), patch("lucid.llm.kiro.executable", return_value=None), patch("lucid.llm.codex.executable", return_value="/fake/codex"), patch("lucid.llm.codex.list_models", return_value=[{"id": "codex-model"}]), patch("lucid.llm.codex.complete", return_value="answer") as complete, patch("lucid.llm._client") as api:
            self.assertEqual(llm.provider_kind(), "codex")
            self.assertTrue(llm.provider_ready())
            self.assertEqual(llm.default_model(), "codex-default")
            self.assertEqual(llm.list_models(), [{"id": "codex-model"}])
            self.assertEqual(llm.complete("system", "source", provider="codex", reasoning_effort="high"), "answer")
            complete.assert_called_once_with("system", "source", model="codex-default", json_mode=True, reasoning_effort="high")
            api.assert_not_called()
            complete.side_effect = RuntimeError("failure")
            with self.assertRaises(RuntimeError):
                llm.complete("system", "source", provider="codex")
            api.assert_not_called()

    def test_explicit_provider_overrides_and_invalid_configuration(self):
        with patch.dict(os.environ, {"LUCID_PROVIDER": "kiro", "OPENAI_API_KEY": "fake", "LUCID_KIRO_MODEL": "kiro-model"}):
            self.assertEqual(llm.provider_kind(), "kiro")
            self.assertEqual(llm.default_model(), "kiro-model")
        with patch.dict(os.environ, {"LUCID_PROVIDER": "unknown"}):
            with self.assertRaises(ValueError):
                llm.provider_kind()

    def test_request_provider_override_does_not_mutate_environment(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake", "OPENAI_MODEL": "api/model", "LUCID_KIRO_MODEL": "kiro/model"}), patch("lucid.llm.kiro.executable", return_value="/fake/kiro-cli"), patch("lucid.llm.kiro.complete", return_value="Kiro answer") as complete, patch("lucid.llm._client") as api:
            before = dict(os.environ)
            self.assertEqual(llm.provider_kind("kiro"), "kiro")
            self.assertTrue(llm.provider_ready("kiro"))
            self.assertEqual(llm.default_model("kiro"), "kiro/model")
            self.assertEqual(llm.complete("system", "source", provider="kiro"), "Kiro answer")
            complete.assert_called_once_with("system", "source", model="kiro/model", json_mode=True, reasoning_effort=None)
            api.assert_not_called()
            self.assertEqual(dict(os.environ), before)
        with patch.dict(os.environ, {"LUCID_PROVIDER": "kiro", "OPENAI_API_KEY": ""}), patch("lucid.llm.kiro.complete") as fallback, patch("lucid.llm.OpenAI") as sdk:
            self.assertEqual(llm.provider_kind("api"), "api")
            self.assertFalse(llm.provider_ready("api"))
            with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
                llm.complete("system", "source", provider="api")
            with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
                llm.list_models(provider="api")
            fallback.assert_not_called()
            sdk.assert_not_called()

    def test_request_auto_keeps_configured_default_and_rejects_invalid_values(self):
        with patch.dict(os.environ, {"LUCID_PROVIDER": "kiro", "OPENAI_API_KEY": "fake"}):
            self.assertEqual(llm.provider_kind("auto"), "kiro")
        for provider in ("", "unknown", "API", " kiro "):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                llm.provider_kind(provider)

    def test_selected_catalogs_never_invoke_inference(self):
        client = self.client()
        client.models.list.return_value = SimpleNamespace(data=[SimpleNamespace(id="api/model")])
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch("lucid.llm._client", return_value=client), patch("lucid.llm.kiro.list_models", return_value=[{"id": "kiro/model"}]) as catalog, patch("lucid.llm.kiro.complete") as complete:
            self.assertEqual(llm.list_models(provider="kiro"), [{"id": "kiro/model"}])
            self.assertEqual(llm.list_models(provider="api"), [{"id": "api/model"}])
            catalog.assert_called_once_with()
            complete.assert_not_called()
            client.chat.completions.create.assert_not_called()

    def test_context_only_forwards_provider_when_requested(self):
        with patch("lucid.context.llm.complete", return_value="Context") as complete:
            context.build_context("source", provider="kiro")
            self.assertEqual(complete.call_args.kwargs, {"json_mode": False, "model": None, "reasoning_effort": None, "provider": "kiro"})

    def client(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.chat.completions.create.return_value = SimpleNamespace(choices=[
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"findings": []}')),
        ])
        return client

    def test_model_default_and_per_call_override(self):
        client = self.client()
        with patch.dict(os.environ, {"OPENAI_MODEL": "provider/default"}), patch("lucid.llm._client", return_value=client):
            llm.complete("system", "source")
            self.assertEqual(client.chat.completions.create.call_args.kwargs["model"], "provider/default")
            llm.complete("system", "source", model=" anthropic/selected ", json_mode=False)
            args = client.chat.completions.create.call_args.kwargs
            self.assertEqual(args["model"], "anthropic/selected")
            self.assertNotIn("response_format", args)
            self.assertNotIn("temperature", args)
            self.assertNotIn("reasoning_effort", args)
            self.assertEqual(llm.default_model(), "provider/default")

    def test_json_and_reasoning_opt_in(self):
        client = self.client()
        with patch("lucid.llm._client", return_value=client):
            llm.complete("system", "source", model="provider/model", reasoning_effort="high")
        args = client.chat.completions.create.call_args.kwargs
        self.assertEqual(args["response_format"], {"type": "json_object"})
        self.assertEqual(args["reasoning_effort"], "high")

    def test_invalid_ids(self):
        for model in ("", "  ", "a b", "a\nb", "a\x00b", "a" * 257):
            with self.assertRaises(ValueError):
                llm.validate_model(model)

    def test_model_list_only_returns_unique_valid_ids(self):
        client = self.client()
        client.models.list.return_value = SimpleNamespace(data=[
            SimpleNamespace(id="b/model", secret="private"),
            SimpleNamespace(id="a/model"), SimpleNamespace(id="b/model"),
            SimpleNamespace(id="bad model"),
        ])
        with patch("lucid.llm._client", return_value=client) as factory:
            self.assertEqual(llm.list_models(), [{"id": "a/model"}, {"id": "b/model"}])
        factory.assert_called_once_with(timeout=10.0)
        client.chat.completions.create.assert_not_called()

    def test_client_uses_backend_credentials_and_disables_retries(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake", "OPENAI_BASE_URL": "https://router.requesty.ai/v1"}), patch("lucid.llm.OpenAI") as sdk:
            llm._client(timeout=10.0)
        sdk.assert_called_once_with(api_key="fake", base_url="https://router.requesty.ai/v1", timeout=10.0, max_retries=0)

    def test_context_forwards_selected_model_without_json_mode(self):
        with patch("lucid.context.llm.complete", return_value="Context") as complete:
            self.assertEqual(context.build_context("source", model="provider/model", reasoning_effort="low"), "Context")
        self.assertEqual(complete.call_args.kwargs, {"json_mode": False, "model": "provider/model", "reasoning_effort": "low"})


if __name__ == "__main__":
    unittest.main()
