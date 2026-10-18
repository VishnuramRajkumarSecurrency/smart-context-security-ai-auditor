"""Specialist web audits: all source downloads and model calls are mocked."""

import json
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from lucid import agent, context, judge, specialists
from lucid.web import SPECIALIST_LANES, AuditRequest, AuditStore, app

HEADERS = {"X-Lucid-Request": "local-ui"}
KEYS = [lane["key"] for lane in SPECIALIST_LANES]
SYSTEM_KEYS = {specialists._PROMPTS[key][0]: key for key in KEYS[:-1]}
SYSTEM_KEYS[agent.SYSTEM_PROMPT] = "general_hunter"
FINDING = {
    "title": "Example", "severity": "high", "location": "A.sol:1",
    "description": "Example logic", "impact": "Example impact",
    "exploit_steps": ["Example step"],
}
EMPTY = '{"findings": []}'
OPTIONS = {"model": "provider/selected", "json_mode": False, "reasoning_effort": "high"}


def response(*findings):
    return json.dumps({"findings": list(findings)})


class SpecialistStoreTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"LUCID_PROVIDER": "api"})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = AuditStore()
        self.addCleanup(self.store.executor.shutdown, wait=True)
        self.addCleanup(self.store.close)

    def start(self, rounds=1, **settings):
        return self.store.start(AuditRequest(mode="specialists", rounds=rounds, **settings), "SOURCE")

    def wait(self, job_id, predicate=None):
        predicate = predicate or (lambda job: job["status"] in ("completed", "failed"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.store.get(job_id)
            if predicate(job):
                return job
            time.sleep(0.005)
        self.fail("Mock specialist audit did not reach the expected state")

    def test_cli_providers_limit_workers_and_run_all_lanes(self):
        for provider in ("kiro", "codex"):
            active, peak, calls = 0, 0, 0
            lock = Lock()

            def complete(*args, **kwargs):
                nonlocal active, peak, calls
                with lock:
                    active += 1
                    peak = max(peak, active)
                    calls += 1
                time.sleep(0.02)
                with lock:
                    active -= 1
                return EMPTY

            with self.subTest(provider=provider), patch.dict(os.environ, {"CLI_MAX_CONCURRENT_LANES": "2"}), patch(
                f"lucid.web.{provider}.resolve_model", return_value="cli-model",
            ), patch("lucid.web.context.build_context", return_value="CONTEXT"), patch(
                "lucid.web.llm.complete", side_effect=complete,
            ), patch("lucid.web.ThreadPoolExecutor", wraps=ThreadPoolExecutor) as executor:
                job = self.wait(self.start(provider=provider)["id"])
                executor.assert_called_once_with(max_workers=2, thread_name_prefix="lucid-lane")
            self.assertEqual(job["status"], "completed")
            self.assertEqual(calls, 6)
            self.assertLessEqual(peak, 2)
            self.assertTrue(all(lane["status"] == "completed" for lane in job["lanes"]))

    def test_invalid_cli_lane_limit_rejected_before_model_resolution(self):
        with patch.dict(os.environ, {"CLI_MAX_CONCURRENT_LANES": "0"}), patch(
            "lucid.web.codex.resolve_model",
        ) as resolve, self.assertRaises(HTTPException) as raised:
            self.start(provider="codex")
        self.assertEqual(raised.exception.status_code, 400)
        resolve.assert_not_called()

    def test_six_concurrent_sequential_lanes_exclusion_and_judge_contract(self):
        barriers = [Barrier(6), Barrier(6)]
        judge_entered, release_judge = Event(), Event()
        self.addCleanup(release_judge.set)
        calls, counts, lock = [], {}, Lock()

        def complete(system, prompt, **options):
            if system == judge.JUDGE_SYSTEM_PROMPT:
                judge_entered.set()
                if not release_judge.wait(5):
                    raise RuntimeError("Test judge timed out")
                return response(FINDING | {"title": "Judged only"}, {})
            key = SYSTEM_KEYS[system]
            with lock:
                index = counts.get(key, 0)
                counts[key] = index + 1
                calls.append((key, index, prompt, options))
            barriers[index].wait(5)  # Serial lane execution cannot pass either barrier.
            return response(FINDING | {"title": f"candidate::{key}"}, {}) if index == 0 else EMPTY

        with patch("lucid.web.context.build_context", return_value="SHARED CONTEXT") as build, patch("lucid.web.llm.complete", side_effect=complete) as mocked, patch("lucid.specialists.run_specialist_loop") as permissive, patch("lucid.agent.find_bugs_loop") as general, patch("lucid.judge.judge_findings") as upstream_judge:
            initial = self.start(rounds=2, **OPTIONS)
            self.assertTrue(judge_entered.wait(5))
            pending = self.store.get(initial["id"])
            self.assertEqual(pending["status"], "running")
            self.assertEqual(pending["judge_status"], "running")
            self.assertEqual(pending["findings"], [])
            self.assertEqual(pending["completed_rounds"], 12)
            self.assertEqual(pending["total_rounds"], 12)
            self.assertEqual(pending["max_model_calls"], 14)
            self.assertEqual(pending["rounds"], 2)
            self.assertEqual(set(pending["findings_by_lane"]), set(KEYS))
            for lane in pending["lanes"]:
                self.assertEqual(lane["status"], "completed")
                self.assertEqual(lane["completed_rounds"], 2)
                self.assertEqual(lane["rounds"], 2)
                self.assertEqual(lane["findings_count"], 1)
                self.assertIsNone(lane["error"])
            with self.assertRaises(HTTPException) as conflict:
                self.start()
            self.assertEqual(conflict.exception.status_code, 409)
            # Both full and history snapshots must be detached from stored mutable state.
            pending["lanes"][0]["status"] = "failed"
            history = self.store.list()
            self.assertNotIn("findings_by_lane", history[0])
            self.assertNotIn("findings", history[0])
            self.assertEqual(history[0]["findings_count"], 0)
            history[0]["lanes"][0]["status"] = "failed"
            self.assertEqual(self.store.get(initial["id"])["lanes"][0]["status"], "completed")
            release_judge.set()
            job = self.wait(initial["id"])
            build.assert_called_once_with("SOURCE", model=OPTIONS["model"], reasoning_effort="high", provider="api")
            permissive.assert_not_called()
            general.assert_not_called()
            upstream_judge.assert_not_called()
            self.assertEqual(mocked.call_count, 13)
            for call in mocked.call_args_list:
                self.assertEqual(call.kwargs, OPTIONS | {"provider": "api"})
            judge_prompt = mocked.call_args_list[-1].args[1]
            for key in KEYS:
                self.assertIn(f"candidate::{key}", judge_prompt)
            self.assertIn("SOURCE", judge_prompt)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["judge_status"], "completed")
        self.assertEqual(job["findings"], [FINDING | {"title": "Judged only"}])
        self.assertEqual(len(job["warnings"]), 7)
        for key, index, prompt, options in calls:
            self.assertIn("SOURCE", prompt)
            self.assertIn("SHARED CONTEXT", prompt)
            self.assertEqual(options, OPTIONS | {"provider": "api"})
            if index == 0:
                self.assertNotIn("candidate::", prompt)
            else:
                self.assertIn("Already found", prompt)
                self.assertIn(f"candidate::{key}", prompt)
                for other in set(KEYS) - {key}:
                    self.assertNotIn(f"candidate::{other}", prompt)

    def test_lane_failures_wait_for_inflight_retain_partial_and_redact(self):
        for bad in ("secret-source not json", "[]", '{"findings": null}', response({"title": "secret-source"}), RuntimeError("secret-source api-key"), ValueError("secret-source api-key")):
            with self.subTest(bad=type(bad).__name__):
                barriers = [Barrier(6), Barrier(6)]
                release = Event()
                counts, lock = {}, Lock()

                def complete(system, prompt, *, lock=lock, counts=counts, barriers=barriers, bad=bad, release=release, **options):
                    key = SYSTEM_KEYS[system]  # A judge call would fail this test.
                    with lock:
                        index = counts.get(key, 0)
                        counts[key] = index + 1
                    barriers[index].wait(5)
                    if index == 0:
                        return response(FINDING | {"title": key})
                    if key == KEYS[0]:
                        if isinstance(bad, Exception):
                            raise bad
                        return bad
                    if not release.wait(5):
                        raise RuntimeError("Test lane timed out")
                    return response(FINDING | {"title": f"late::{key}"})

                with patch("lucid.web.context.build_context", return_value="Context"), patch("lucid.web.llm.complete", side_effect=complete) as mocked:
                    try:
                        initial = self.start(rounds=3)
                        pending = self.wait(initial["id"], lambda job: any(lane["status"] == "failed" for lane in job["lanes"]))
                        self.assertEqual(pending["status"], "running")
                        self.assertEqual(pending["findings"], [])
                        with self.assertRaises(HTTPException):
                            self.start()
                    finally:
                        release.set()
                    job = self.wait(initial["id"])
                    self.assertEqual(mocked.call_count, 12)
                self.assertEqual(job["status"], "failed")
                self.assertEqual(job["judge_status"], "skipped")
                self.assertEqual(job["findings"], [])
                self.assertEqual(job["completed_rounds"], 11)
                self.assertEqual(job["lanes"][0]["status"], "failed")
                self.assertTrue(all(lane["status"] == "stopped" for lane in job["lanes"][1:]))
                self.assertEqual(len(job["findings_by_lane"][KEYS[0]]), 1)
                for key in KEYS[1:]:
                    self.assertEqual(len(job["findings_by_lane"][key]), 2)
                self.assertIn("unjudged", job["warnings"][-1])
                self.assertNotIn("secret-source", json.dumps(job))

    def test_judge_invalid_output_and_provider_failure_are_not_clean(self):
        for bad in ("secret-source", "[]", '{"findings": null}', response({}), RuntimeError("secret-source api-key"), ValueError("secret-source api-key")):
            with self.subTest(bad=type(bad).__name__):
                def complete(system, prompt, *, bad=bad, **options):
                    if system == judge.JUDGE_SYSTEM_PROMPT:
                        if isinstance(bad, Exception):
                            raise bad
                        return bad
                    return response(FINDING)
                with patch("lucid.web.context.build_context", return_value="Context"), patch("lucid.web.llm.complete", side_effect=complete):
                    job = self.wait(self.start()["id"])
                self.assertEqual(job["status"], "failed")
                self.assertEqual(job["judge_status"], "failed")
                self.assertEqual(job["findings"], [])
                self.assertEqual(job["completed_rounds"], 6)
                self.assertTrue(all(lane["status"] == "completed" for lane in job["lanes"]))
                self.assertTrue(all(len(pool) == 1 for pool in job["findings_by_lane"].values()))
                self.assertNotIn("secret-source", json.dumps(job))
                self.assertIn("incomplete", job["warnings"][-1])

    def test_shutdown_waits_for_inflight_lanes_without_later_rounds_or_judge(self):
        entered, release = Barrier(7), Event()
        self.addCleanup(release.set)
        def complete(*args, **kwargs):
            entered.wait(5)
            if not release.wait(5):
                raise RuntimeError("Test lane timed out")
            return response(FINDING)
        with patch("lucid.web.context.build_context", return_value="Context"), patch("lucid.web.llm.complete", side_effect=complete) as mocked:
            initial = self.start(rounds=2)
            entered.wait(5)
            self.store.close()
            self.assertEqual(self.store.get(initial["id"])["status"], "running")
            release.set()
            job = self.wait(initial["id"])
            self.assertEqual(mocked.call_count, 6)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["judge_status"], "skipped")
        self.assertEqual(job["completed_rounds"], 6)
        self.assertEqual(job["findings"], [])
        self.assertTrue(all(lane["status"] == "stopped" for lane in job["lanes"]))
        self.assertTrue(all(len(pool) == 1 for pool in job["findings_by_lane"].values()))

    def test_shutdown_after_final_lane_prevents_judge(self):
        barrier = Barrier(6)
        def complete(*args, **kwargs):
            barrier.wait(5)
            self.store.close()
            return response(FINDING)
        with patch("lucid.web.context.build_context", return_value="Context"), patch("lucid.web.llm.complete", side_effect=complete) as mocked:
            job = self.wait(self.start()["id"])
            self.assertEqual(mocked.call_count, 6)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["judge_status"], "skipped")
        self.assertEqual(job["completed_rounds"], 6)

    def test_shutdown_during_context_prevents_lanes(self):
        def build(*args, **kwargs):
            self.store.close()
            return "Context"
        with patch("lucid.web.context.build_context", side_effect=build), patch("lucid.web.llm.complete") as complete:
            job = self.wait(self.start()["id"])
            complete.assert_not_called()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["judge_status"], "skipped")
        self.assertTrue(all(lane["status"] == "stopped" for lane in job["lanes"]))

    def test_empty_successful_lanes_skip_judge_and_do_all_rounds(self):
        for rounds in (1, 10):
            with patch("lucid.web.context.build_context", return_value="Context") as build, patch("lucid.web.llm.complete", return_value=EMPTY) as complete:
                job = self.wait(self.start(rounds=rounds)["id"])
                build.assert_called_once()
                self.assertEqual(complete.call_count, 6 * rounds)
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["judge_status"], "skipped")
            self.assertEqual(job["findings"], [])
            self.assertEqual(job["warnings"], [])
            self.assertEqual(job["completed_rounds"], 6 * rounds)
            self.assertTrue(all(lane["status"] == "completed" for lane in job["lanes"]))

    def test_early_lane_failure_stops_queued_lanes(self):
        # Run the first lane to failure before the coordinator submits another lane.
        original = self.store.update_lane
        failed = Event()
        def publish(job_id, key, **values):
            original(job_id, key, **values)
            if values.get("status") == "failed":
                failed.set()

        class OrderedSubmission:
            def __init__(self, **kwargs):
                self.executor = ThreadPoolExecutor(**kwargs)

            def __enter__(self):
                self.executor.__enter__()
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return self.executor.__exit__(exc_type, exc_value, traceback)

            def submit(self, function, key):
                future = self.executor.submit(function, key)
                if key == KEYS[0] and not failed.wait(5):
                    raise RuntimeError("Test first lane did not fail")
                return future
        with patch("lucid.web.context.build_context", return_value="Context"), patch("lucid.web.ThreadPoolExecutor", OrderedSubmission), patch.object(self.store, "update_lane", side_effect=publish), patch("lucid.web.llm.complete", return_value="not json") as complete:
            job = self.wait(self.start(rounds=2)["id"])
            self.assertEqual(complete.call_count, 1)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["judge_status"], "skipped")
        self.assertEqual(job["completed_rounds"], 0)
        self.assertEqual(job["lanes"][0]["status"], "failed")
        self.assertTrue(all(lane["status"] == "stopped" for lane in job["lanes"][1:]))
        self.assertEqual(job["findings"], [])

    def test_queued_contract_and_valid_empty_judge_with_default_options(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        def build(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test context timed out")
            return "Context"
        def complete(system, prompt, **options):
            return EMPTY if system == judge.JUDGE_SYSTEM_PROMPT else response(FINDING)
        with patch.dict(os.environ, {"OPENAI_MODEL": "provider/pinned-default"}), patch("lucid.web.context.build_context", side_effect=build) as context_build, patch("lucid.web.llm.complete", side_effect=complete) as mocked:
            initial = self.start()
            self.assertTrue(entered.wait(5))
            pending = self.store.get(initial["id"])
            self.assertEqual(pending["judge_status"], "queued")
            self.assertEqual(pending["completed_rounds"], 0)
            self.assertEqual(pending["findings_by_lane"], {key: [] for key in KEYS})
            self.assertTrue(all(lane["status"] == "queued" for lane in pending["lanes"]))
            release.set()
            job = self.wait(initial["id"])
            context_build.assert_called_once_with("SOURCE", model="provider/pinned-default", reasoning_effort=None, provider="api")
            self.assertEqual(mocked.call_count, 7)
            for call in mocked.call_args_list:
                self.assertEqual(call.kwargs, {"model": "provider/pinned-default", "json_mode": True, "reasoning_effort": None, "provider": "api"})
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["judge_status"], "completed")
        self.assertEqual(job["findings"], [])
        self.assertEqual(job["warnings"], [])
        self.assertTrue(all(len(pool) == 1 for pool in job["findings_by_lane"].values()))

    def test_empty_or_failed_context_never_starts_lanes(self):
        for result in ("  \n", RuntimeError("secret-source"), ValueError("secret-source")):
            with self.subTest(result=result):
                if isinstance(result, Exception):
                    context_patch = patch("lucid.web.context.build_context", side_effect=result)
                else:
                    context_patch = patch("lucid.web.context.build_context", return_value=result)
                with context_patch, patch("lucid.web.llm.complete") as complete:
                    job = self.wait(self.start()["id"])
                    complete.assert_not_called()
                self.assertEqual(job["status"], "failed")
                self.assertEqual(job["judge_status"], "skipped")
                self.assertEqual(job["completed_rounds"], 0)
                self.assertTrue(all(lane["status"] == "stopped" for lane in job["lanes"]))
                self.assertNotIn("secret-source", json.dumps(job))


class SpecialistAPITests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"LUCID_PROVIDER": "api"})
        environment.start()
        self.addCleanup(environment.stop)
        client_context = TestClient(app, base_url="http://localhost:8765")
        self.client = client_context.__enter__()
        self.addCleanup(client_context.__exit__, None, None, None)

    def wait(self, job_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.client.get(f"/api/audits/{job_id}").json()
            if job["status"] in ("completed", "failed"):
                return job
            time.sleep(0.005)
        self.fail("Mock API audit did not finish")

    def test_upload_and_pinned_github_use_selected_model_for_every_actual_call(self):
        snapshot = {
            "repository_url": "https://github.com/owner/repo", "repository": "owner/repo",
            "ref": "main", "commit": "a" * 40, "subdirectory": "src",
            "files": [{"path": "src/Pinned.sol", "content": "contract Pinned {}"}],
            "file_count": 1, "total_chars": 18,
        }
        with patch("lucid.web.fetch_repository", return_value=snapshot) as fetch, patch("lucid.web.llm.complete") as complete:
            preview = self.client.post("/api/github/preview", headers=HEADERS, json={"url": snapshot["repository_url"]})
            self.assertEqual(preview.status_code, 200)
            complete.assert_not_called()
            fetch.assert_called_once()
        snapshot["files"][0]["content"] = "MUTATED AFTER PREVIEW"
        targets = [
            ({"target": "upload", "files": [{"path": "A.sol", "content": "contract A {}"}]}, "// FILE: A.sol\n1: contract A {}"),
            ({"target": "github", "github_preview_id": preview.json()["id"]}, "// FILE: src/Pinned.sol\n1: contract Pinned {}"),
        ]
        def complete(system, prompt, **options):
            return "Shared protocol context" if system == context.CONTEXT_SYSTEM_PROMPT else response(FINDING)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake", "OPENAI_MODEL": "provider/default"}), patch("lucid.web.fetch_repository") as fetch, patch("lucid.web.llm.complete", side_effect=complete) as mocked:
            for target, source in targets:
                mocked.reset_mock()
                result = self.client.post("/api/audits", headers=HEADERS, json={
                    "mode": "specialists", "rounds": 1, "confirmed_paid": True,
                    **OPTIONS, **target,
                })
                self.assertEqual(result.status_code, 202)
                job = self.wait(result.json()["id"])
                self.assertEqual(job["status"], "completed")
                self.assertEqual(job["mode"], "specialists")
                self.assertEqual(job["total_rounds"], 6)
                self.assertEqual(job["max_model_calls"], 8)
                self.assertEqual(job["model"], OPTIONS["model"])
                self.assertEqual(job["json_mode"], OPTIONS["json_mode"])
                self.assertEqual(job["reasoning_effort"], OPTIONS["reasoning_effort"])
                self.assertEqual(mocked.call_count, 8)
                self.assertEqual(mocked.call_args_list[0].args[0], context.CONTEXT_SYSTEM_PROMPT)
                self.assertEqual(mocked.call_args_list[-1].args[0], judge.JUDGE_SYSTEM_PROMPT)
                for call in mocked.call_args_list:
                    self.assertIn(source, call.args[1])
                    self.assertNotIn("MUTATED AFTER PREVIEW", call.args[1])
                    self.assertEqual(call.kwargs, OPTIONS | {"provider": "api"})
                if target["target"] == "github":
                    self.assertEqual(job["source"]["commit"], "a" * 40)
                    self.assertEqual(job["target"], "owner/repo")
            fetch.assert_not_called()
        history = self.client.get("/api/audits").json()
        self.assertTrue(all("findings_by_lane" not in item for item in history))
        self.assertTrue(all(item["findings_count"] == 1 for item in history))

    def test_provider_is_pinned_for_context_all_lanes_and_judge(self):
        for selection in (None, "auto", "api", "kiro"):
            with self.subTest(selection=selection):
                entered, release = Event(), Event()
                self.addCleanup(release.set)
                def complete(system, prompt, *, entered=entered, release=release, **options):
                    if system == context.CONTEXT_SYSTEM_PROMPT:
                        entered.set()
                        if not release.wait(5):
                            raise RuntimeError("Test context timed out")
                        return "Context"
                    return response(FINDING)
                kind = "kiro" if selection == "kiro" else "api"
                with patch.dict(os.environ, {"LUCID_PROVIDER": "auto", "OPENAI_API_KEY": "fake", "OPENAI_MODEL": "api/default", "LUCID_KIRO_MODEL": "kiro-default"}), patch("lucid.kiro.executable", return_value="/fake/kiro-cli"), patch("lucid.kiro.resolve_model", return_value="kiro/pinned") as resolve, patch("lucid.web.llm.complete", side_effect=complete) as mocked:
                    settings = {} if selection is None else {"provider": selection}
                    result = self.client.post("/api/audits", headers=HEADERS, json={
                        "confirmed_paid": True, "mode": "specialists", "rounds": 2,
                        "target": "upload", "files": [{"path": "A.sol", "content": "contract A {}"}],
                        **settings,
                    })
                    self.assertEqual(result.status_code, 202)
                    self.assertTrue(entered.wait(5))
                    # A different dropdown query and changed defaults cannot reroute a job.
                    other = "api" if kind == "kiro" else "kiro"
                    self.assertEqual(self.client.get(f"/api/config?provider={other}").json()["provider_kind"], other)
                    with patch.dict(os.environ, {"LUCID_PROVIDER": other, "OPENAI_MODEL": "changed/default", "LUCID_KIRO_MODEL": "changed/default"}):
                        release.set()
                        job = self.wait(result.json()["id"])
                    self.assertEqual(job["status"], "completed")
                    self.assertEqual(job["provider_kind"], kind)
                    self.assertEqual(job["model"], "kiro/pinned" if kind == "kiro" else "api/default")
                    self.assertEqual(mocked.call_count, 14)
                    self.assertEqual(mocked.call_args_list[0].args[0], context.CONTEXT_SYSTEM_PROMPT)
                    self.assertEqual(mocked.call_args_list[-1].args[0], judge.JUDGE_SYSTEM_PROMPT)
                    for call in mocked.call_args_list:
                        self.assertEqual(call.kwargs["provider"], kind)
                        self.assertEqual(call.kwargs["model"], job["model"])
                    if kind == "kiro":
                        resolve.assert_called_once_with("kiro-default")
                    else:
                        resolve.assert_not_called()

    def test_round_bounds_and_request_default(self):
        self.assertEqual(AuditRequest(mode="specialists").rounds, 5)
        for rounds in (1, 10):
            self.assertEqual(AuditRequest(mode="specialists", rounds=rounds).rounds, rounds)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake"}), patch("lucid.web.context.build_context") as build, patch("lucid.web.llm.complete") as complete:
            for rounds in (0, -1, 11):
                with self.assertRaises(ValidationError):
                    AuditRequest(mode="specialists", rounds=rounds)
                result = self.client.post("/api/audits", headers=HEADERS, json={
                    "mode": "specialists", "rounds": rounds, "confirmed_paid": True,
                })
                self.assertEqual(result.status_code, 422)
            build.assert_not_called()
            complete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
