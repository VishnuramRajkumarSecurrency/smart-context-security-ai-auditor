"""CI/PR entrypoint tests: real git for diff scoping, mocked model for audits."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lucid import ci
from lucid.schema import Finding


def _run(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _init_repo(root: Path):
    _run(root, "init", "-b", "main")
    _run(root, "config", "user.email", "t@example.com")
    _run(root, "config", "user.name", "Test")
    _run(root, "config", "commit.gpgsign", "false")


def _write(root: Path, rel: str, text: str):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _finding(severity="high", title="Reentrancy"):
    return Finding(title=title, severity=severity, location="A.sol:10",
                   description="x", impact="y", exploit_steps=["a", "b"])


class ChangedFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        _init_repo(self.repo)
        _write(self.repo, "src/A.sol", "contract A {}\n")
        _write(self.repo, "src/keep.sol", "contract Keep {}\n")
        _write(self.repo, "README.md", "# repo\n")
        _run(self.repo, "add", ".")
        _run(self.repo, "commit", "-m", "base")
        _run(self.repo, "checkout", "-b", "feature")

    def test_detects_added_modified_renamed_skips_deleted_and_non_sol(self):
        _write(self.repo, "src/A.sol", "contract A { uint x; }\n")   # modified
        _write(self.repo, "src/New.sol", "contract New {}\n")        # added
        _write(self.repo, "docs/note.md", "hi\n")                    # non-sol
        _run(self.repo, "mv", "src/keep.sol", "src/Renamed.sol")     # rename
        _run(self.repo, "add", "-A")
        _run(self.repo, "commit", "-m", "changes")

        files = ci.changed_sol_files(self.repo, "main", "feature")
        self.assertEqual(files, ["src/A.sol", "src/New.sol", "src/Renamed.sol"])

    def test_deleted_sol_is_excluded(self):
        _run(self.repo, "rm", "src/A.sol")
        _run(self.repo, "commit", "-m", "remove A")
        self.assertEqual(ci.changed_sol_files(self.repo, "main", "feature"), [])

    def test_subdir_filter(self):
        _write(self.repo, "src/A.sol", "contract A { uint x; }\n")
        _write(self.repo, "other/B.sol", "contract B {}\n")
        _run(self.repo, "add", "-A")
        _run(self.repo, "commit", "-m", "two dirs")
        self.assertEqual(
            ci.changed_sol_files(self.repo, "main", "feature", subdir="src"),
            ["src/A.sol"])

    def test_bad_ref_raises_cierror(self):
        with self.assertRaises(ci.CIError):
            ci.changed_sol_files(self.repo, "no-such-ref", "feature")

    def test_unified_diff_bounded_and_scoped(self):
        _write(self.repo, "src/A.sol", "contract A { uint256 y; }\n")
        _run(self.repo, "add", "-A")
        _run(self.repo, "commit", "-m", "edit A")
        diff = ci.unified_diff(self.repo, "main", "feature", ["src/A.sol"])
        self.assertIn("src/A.sol", diff)
        self.assertIn("uint256 y", diff)
        with patch.object(ci, "_MAX_DIFF_CHARS", 20):
            self.assertIn("truncated", ci.unified_diff(self.repo, "main", "feature", ["src/A.sol"]))


class AnnotateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_annotation_format_and_line_numbers(self):
        _write(self.repo, "src/A.sol", "line one\nline two\n")
        blob = ci.annotate_files(self.repo, ["src/A.sol"])
        self.assertIn("// FILE: src/A.sol", blob)
        self.assertIn("1: line one", blob)
        self.assertIn("2: line two", blob)

    def test_missing_file_skipped(self):
        self.assertEqual(ci.annotate_files(self.repo, ["gone.sol"]), "")

    def test_max_chars_enforced(self):
        _write(self.repo, "big.sol", "x" * 1000)
        with patch.object(ci, "MAX_CHARS", 100):
            with self.assertRaises(ci.CIError):
                ci.annotate_files(self.repo, ["big.sol"])


class GateTests(unittest.TestCase):
    def test_thresholds(self):
        highs = [_finding("high")]
        self.assertTrue(ci.gate_severity(highs, "high"))
        self.assertTrue(ci.gate_severity(highs, "medium"))
        self.assertFalse(ci.gate_severity(highs, "critical"))
        self.assertFalse(ci.gate_severity(highs, "none"))
        self.assertFalse(ci.gate_severity([_finding("low")], "high"))
        self.assertFalse(ci.gate_severity([], "medium"))


class RunAuditDispatchTests(unittest.TestCase):
    """Each mode reaches the model via a single mock and parses findings."""

    FINDINGS_JSON = json.dumps({"findings": [
        {"title": "Bug", "severity": "high", "location": "A.sol:1",
         "description": "d", "impact": "i", "exploit_steps": ["s"]}]})

    def test_single_skips_context(self):
        with patch("lucid.llm.complete", return_value=self.FINDINGS_JSON) as m:
            out = ci.run_audit("CODE", mode="single", rounds=1,
                               context_source="CTXSRC", diff="DIFF")
        self.assertEqual(len(out), 1)
        self.assertEqual(m.call_count, 1)  # no context pass

    def test_context_injects_diff(self):
        with patch("lucid.llm.complete", return_value=self.FINDINGS_JSON) as m:
            ci.run_audit("CODE", mode="context", rounds=1,
                         context_source="CTXSRC", diff="MYDIFF")
        # Two calls: build_context, then the context-aware hunt. The hunt's
        # user prompt must carry the injected diff section.
        self.assertEqual(m.call_count, 2)
        hunt_user_prompt = m.call_args_list[1].args[1]
        self.assertIn("Changes under review", hunt_user_prompt)
        self.assertIn("MYDIFF", hunt_user_prompt)

    def test_loop_rounds(self):
        with patch("lucid.llm.complete", return_value=self.FINDINGS_JSON) as m:
            out = ci.run_audit("CODE", mode="loop", rounds=2,
                               context_source="CTXSRC", diff="")
        # build_context (1) + 2 loop rounds = 3 calls.
        self.assertEqual(m.call_count, 3)
        self.assertEqual(len(out), 2)


class MainTests(unittest.TestCase):
    def setUp(self):
        # main() would call load_dotenv() and set LUCID_* vars; stub the dotenv
        # load and fully snapshot/restore os.environ so nothing from this repo's
        # real .env (or the --provider/--model flags) leaks into sibling tests.
        import os
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        dotenv_patch = patch("lucid.ci.load_dotenv", lambda *a, **k: None)
        dotenv_patch.start()
        self.addCleanup(dotenv_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        _init_repo(self.repo)
        _write(self.repo, "src/A.sol", "contract A {}\n")
        _run(self.repo, "add", ".")
        _run(self.repo, "commit", "-m", "base")
        _run(self.repo, "checkout", "-b", "feature")
        _write(self.repo, "src/A.sol", "contract A { uint x; }\n")
        _run(self.repo, "add", "-A")
        _run(self.repo, "commit", "-m", "edit")
        self.report = self.repo / "out.json"

    def _args(self, *extra):
        return ["--repo", str(self.repo), "--base", "main", "--head", "feature",
                "--report", str(self.report), *extra]

    def test_no_changes_exits_clean(self):
        _run(self.repo, "checkout", "main")
        _run(self.repo, "checkout", "-b", "empty")  # no .sol changes vs main
        code = ci.main(["--repo", str(self.repo), "--base", "main", "--head", "empty"])
        self.assertEqual(code, ci._EXIT_CLEAN)

    def test_high_finding_trips_gate_and_writes_report(self):
        with patch.object(ci, "run_audit", return_value=[_finding("high")]):
            code = ci.main(self._args("--mode", "single", "--fail-on", "high"))
        self.assertEqual(code, ci._EXIT_GATE_TRIPPED)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["schema"], "lucid-pr-audit/1")
        self.assertEqual(report["changed_files"], ["src/A.sol"])
        self.assertTrue(report["gate_tripped"])
        self.assertEqual(report["finding_count"], 1)

    def test_medium_finding_below_gate_exits_clean(self):
        with patch.object(ci, "run_audit", return_value=[_finding("medium")]):
            code = ci.main(self._args("--mode", "single", "--fail-on", "high"))
        self.assertEqual(code, ci._EXIT_CLEAN)
        self.assertFalse(json.loads(self.report.read_text())["gate_tripped"])

    def test_fail_on_none_never_trips(self):
        with patch.object(ci, "run_audit", return_value=[_finding("critical")]):
            code = ci.main(self._args("--mode", "single", "--fail-on", "none"))
        self.assertEqual(code, ci._EXIT_CLEAN)

    def test_missing_base_errors(self):
        code = ci.main(["--repo", str(self.repo), "--head", "feature"])
        self.assertEqual(code, ci._EXIT_ERROR)

    def test_provider_model_flags_set_env(self):
        import os
        with patch.object(ci, "run_audit", return_value=[]), \
             patch("lucid.llm.provider_kind", return_value="kiro"), \
             patch("lucid.llm.default_model", return_value="claude-sonnet-4.5"):
            ci.main(self._args("--mode", "single", "--provider", "kiro",
                               "--model", "claude-sonnet-4.5"))
        self.assertEqual(os.environ.get("LUCID_PROVIDER"), "kiro")
        # Model routed to the Kiro-specific env var based on provider kind.
        self.assertEqual(os.environ.get("LUCID_KIRO_MODEL"), "claude-sonnet-4.5")


if __name__ == "__main__":
    unittest.main()
