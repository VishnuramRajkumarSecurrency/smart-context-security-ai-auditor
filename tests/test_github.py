"""GitHub preview tests: every request uses an offline mock transport."""

import gzip
import json
import unittest
from unittest.mock import patch

import httpx

from lucid import github

SHA = "a" * 40
URL = "https://github.com/owner/repo"
API = "https://api.github.com/repos/owner/repo"
CLIENT = httpx.Client
TOKEN = "offline-test-token"


def blob(path: object = "src/A.sol", content=b"contract A {}", **overrides):
    return {"path": path, "type": "blob", "mode": "100644", "size": len(content), **overrides}


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.reads = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    def close(self):
        self.closed = True


class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch("lucid.github.os.environ", {})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def fetch(self, *, entries=None, sources=None, handler=None, url=URL, ref="", subdirectory="",
              authenticated=False, token=TOKEN, private=False):
        self.requests = []
        entries = [blob()] if entries is None else entries
        sources = {"src/A.sol": b"contract A {}"} if sources is None else sources

        def respond(request):
            self.requests.append(request)
            if authenticated:
                self.assertEqual(request.url.host, "api.github.com")
                self.assertEqual(request.headers["authorization"], f"Bearer {token}")
            else:
                self.assertNotIn("authorization", request.headers)
            self.assertNotIn("cookie", request.headers)
            self.assertEqual(request.headers["accept-encoding"], "identity")
            self.assertEqual(request.url.scheme, "https")
            expected_accept = "application/vnd.github+json"
            if "/commits/" in request.url.path:
                expected_accept = "application/vnd.github.sha"
            elif authenticated and "/contents/" in request.url.path:
                expected_accept = "application/vnd.github.raw+json"
            self.assertEqual(request.headers["accept"], expected_accept)
            if handler:
                response = handler(request)
                if response is not None:
                    return response
            if str(request.url) == API:
                return httpx.Response(200, json={"default_branch": "main", "private": private})
            if request.url.path.startswith("/repos/owner/repo/commits/"):
                return httpx.Response(200, content=SHA.encode("ascii"))
            if str(request.url) == f"{API}/git/trees/{SHA}?recursive=1":
                return httpx.Response(200, json={"truncated": False, "tree": entries})
            contents_prefix = "/repos/owner/repo/contents/"
            if authenticated and request.url.path.startswith(contents_prefix):
                self.assertEqual(request.url.params, httpx.QueryParams({"ref": SHA}))
                return httpx.Response(200, content=sources[request.url.path[len(contents_prefix):]])
            prefix = f"/owner/repo/{SHA}/"
            if request.url.host == "raw.githubusercontent.com" and request.url.path.startswith(prefix):
                return httpx.Response(200, content=sources[request.url.path[len(prefix):]])
            self.fail(f"Unexpected request: {request.url}")

        def client(**kwargs):
            self.assertFalse(kwargs["follow_redirects"])
            self.assertFalse(kwargs["trust_env"])
            self.assertLessEqual(kwargs["timeout"], 10)
            return CLIENT(transport=httpx.MockTransport(respond), **kwargs)

        environment = {} if token is None else {"LUCID_GITHUB_TOKEN": token}
        with patch.dict("os.environ", environment, clear=True), patch("lucid.github.httpx.Client", side_effect=client):
            return github.fetch_repository(url, ref, subdirectory, authenticated=authenticated)

    def test_authenticated_default_branch_and_private_repository(self):
        result = self.fetch(authenticated=True, private=True)
        self.assertEqual([str(request.url) for request in self.requests], [
            API, f"{API}/commits/main", f"{API}/git/trees/{SHA}?recursive=1",
            f"{API}/contents/src/A.sol?ref={SHA}",
        ])
        self.assertEqual(result["files"], [{"path": "src/A.sol", "content": "contract A {}"}])
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertNotIn("token", result)
        self.assertNotIn("authenticated", result)
        public_result = self.fetch(authenticated=True, private=False)
        self.assertEqual(public_result, result)

    def test_authenticated_slash_ref_and_quoted_source_are_pinned(self):
        path, source = "src/space name-λ.sol", "// λ".encode()
        result = self.fetch(authenticated=True, ref="feature/a-b", entries=[blob(path, source)],
                            sources={path: source}, subdirectory="src")
        self.assertEqual(result["ref"], "feature/a-b")
        self.assertEqual(self.requests[0].url.raw_path, b"/repos/owner/repo/commits/feature%2Fa-b")
        self.assertEqual(self.requests[-1].url.raw_path,
                         b"/repos/owner/repo/contents/src/space%20name-%CE%BB.sol?ref=" + SHA.encode())
        self.assertEqual(len(self.requests), 3)
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_authenticated_missing_and_invalid_tokens_before_network(self):
        invalid = [None, "", " ", " token", "token ", "to ken", "to\tken", "to\r\nken",
                   "token\x00", "token\x1f", "token\x7f", "tokenλ", "token\u00a0", "x" * 4097]
        for token in invalid:
            with self.subTest(token=token), patch("lucid.github.httpx.Client") as client:
                with self.assertRaisesRegex(github.GitHubSourceError, "LUCID_GITHUB_TOKEN"):
                    self.fetch(authenticated=True, token=token)
                client.assert_not_called()
                self.assertEqual(self.requests, [])
        with patch.dict("os.environ", {"GITHUB_TOKEN": TOKEN, "GH_TOKEN": TOKEN}, clear=True), \
                patch("lucid.github.httpx.Client") as client:
            with self.assertRaises(github.GitHubSourceError):
                github.fetch_repository(URL, authenticated=True)
            client.assert_not_called()

    def test_authenticated_token_boundaries(self):
        for token in ("!", "~", "x" * 4096, "!#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"):
            with self.subTest(length=len(token)):
                self.fetch(authenticated=True, token=token)

    def test_anonymous_never_reads_environment_token(self):
        class UnreadableEnvironment(dict):
            def get(self, *args, **kwargs):
                raise AssertionError("Environment read")

        with patch("lucid.github.os.environ", UnreadableEnvironment()):
            self.fetch(token=TOKEN)
            self.fetch(token="invalid token\n")

    def test_repository_visibility_requires_boolean_in_both_modes(self):
        for authenticated in (False, True):
            for value in (None, 0, 1, "false", "true", [], {}):
                with self.subTest(authenticated=authenticated, value=value):
                    with self.assertRaisesRegex(github.GitHubSourceError, "invalid repository visibility"):
                        self.fetch(authenticated=authenticated, private=value)
                    self.assertEqual(len(self.requests), 1)
            with self.assertRaisesRegex(github.GitHubSourceError, "invalid repository visibility"):
                self.fetch(authenticated=authenticated, handler=lambda request:
                           httpx.Response(200, json={"default_branch": "main"}) if str(request.url) == API else None)

    def test_authenticated_http_errors_and_redirects_are_redacted(self):
        cases = [(401, {}, "authentication failed"), (404, {}, "token's repository access"),
                 (403, {}, "permissions.*SSO"), (429, {}, "rate limit"),
                 (403, {"x-ratelimit-remaining": "0"}, "rate limit"),
                 (403, {"retry-after": "60"}, "rate limit"),
                 (302, {"location": f"https://raw.githubusercontent.com/{TOKEN}"}, "redirect"),
                 (307, {"location": f"{API}/contents/{TOKEN}"}, "redirect"), (500, {}, "retry later")]
        for stage in ("metadata", "commit", "tree", "source"):
            for status, headers, message in cases:
                def handler(request, stage=stage, status=status, headers=headers):
                    target = {"metadata": str(request.url) == API, "commit": "/commits/" in request.url.path,
                              "tree": "/git/trees/" in request.url.path, "source": "/contents/" in request.url.path}[stage]
                    return httpx.Response(status, headers=headers, content=TOKEN.encode()) if target else None
                with self.subTest(stage=stage, status=status), self.assertRaisesRegex(github.GitHubSourceError, message) as caught:
                    self.fetch(authenticated=True, handler=handler)
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertEqual(len(self.requests), ("metadata", "commit", "tree", "source").index(stage) + 1)

    def test_authenticated_transport_errors_are_redacted(self):
        for error, message in ((httpx.ReadTimeout(TOKEN), "timed out"),
                               (httpx.ConnectError(TOKEN), "network connection")):
            def handler(request, error=error):
                if "/contents/" in request.url.path:
                    raise error
            with self.subTest(error=error), self.assertRaisesRegex(github.GitHubSourceError, message) as caught:
                self.fetch(authenticated=True, handler=handler)
            self.assertNotIn(TOKEN, str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    def test_authenticated_source_bounds_and_validation(self):
        for content, message in ((b"short", "does not match"), (b"\xff" * 13, "UTF-8")):
            with self.subTest(message=message), self.assertRaisesRegex(github.GitHubSourceError, message):
                self.fetch(authenticated=True, sources={"src/A.sol": content})
        stream = Chunks([b"x" * 400_000, b"x", b"not read"])
        with self.assertRaisesRegex(github.GitHubSourceError, "400,000"):
            self.fetch(authenticated=True, entries=[blob(size=400_000)], handler=lambda request:
                       httpx.Response(200, stream=stream) if "/contents/" in request.url.path else None)
        self.assertTrue(stream.closed)
        self.assertEqual(stream.reads, 2)
        with patch.object(github, "MAX_SOURCE_BYTES", 20), self.assertRaisesRegex(github.GitHubSourceError, "7-byte"):
            self.fetch(authenticated=True, entries=[blob("A.sol"), blob("B.sol", b"")],
                       sources={"A.sol": b"contract A {}", "B.sol": b"x" * 8})
        for entries, message in (([blob(size=400_001)], "400,000"),
                                 ([blob(f"{index}.sol", size=400_000) for index in range(5)], "1,600,000"),
                                 ([blob(f"{index}.sol", b"") for index in range(201)], "200 Solidity")):
            with self.subTest(message=message), self.assertRaisesRegex(github.GitHubSourceError, message):
                self.fetch(authenticated=True, entries=entries)
            self.assertEqual(len(self.requests), 3)

    def test_default_branch_and_commit_pinning(self):
        result = self.fetch(url=URL + ".git/")
        self.assertEqual(result, {
            "repository_url": URL, "repository": "owner/repo", "ref": "main",
            "commit": SHA, "subdirectory": "", "files": [{"path": "src/A.sol", "content": "contract A {}"}],
            "file_count": 1, "total_chars": 13,
        })
        self.assertEqual([str(request.url) for request in self.requests], [
            API, f"{API}/commits/main", f"{API}/git/trees/{SHA}?recursive=1",
            f"https://raw.githubusercontent.com/owner/repo/{SHA}/src/A.sol",
        ])

    def test_explicit_slash_ref_skips_metadata(self):
        result = self.fetch(ref="feature/a-b")
        self.assertEqual(result["ref"], "feature/a-b")
        self.assertEqual(self.requests[0].url.raw_path, b"/repos/owner/repo/commits/feature%2Fa-b")
        self.assertEqual(len(self.requests), 3)

    def test_sha_only_response_strips_ascii_whitespace_and_normalizes(self):
        result = self.fetch(handler=lambda request: httpx.Response(200, content=b" \t" + SHA.upper().encode("ascii") + b"\r\n")
                            if "/commits/" in request.url.path else None)
        self.assertEqual(result["commit"], SHA)
        self.assertEqual(self.requests[1].headers["accept"], "application/vnd.github.sha")

    def test_sha_only_response_rejects_malformed_and_non_ascii(self):
        for body in (b"", b"a" * 39, b"a" * 41, b"g" * 40, b"a" * 20 + b" " + b"a" * 20,
                     b'{"sha":"' + SHA.encode() + b'"}', b"\xc2\xa0" + SHA.encode(), b"\xff" * 40):
            with self.subTest(body=body), self.assertRaisesRegex(github.GitHubSourceError, "invalid commit SHA"):
                self.fetch(handler=lambda request, body=body: httpx.Response(200, content=body)
                           if "/commits/" in request.url.path else None)
            self.assertEqual(len(self.requests), 2)

    def test_sha_only_response_byte_limit(self):
        stream = Chunks([b"a" * 128, b"a"])
        with self.assertRaisesRegex(github.GitHubSourceError, "128-byte"):
            self.fetch(handler=lambda request: httpx.Response(200, stream=stream)
                       if "/commits/" in request.url.path else None)
        self.assertTrue(stream.closed)

    def test_repository_parser(self):
        for suffix in ("", "/", ".git", ".git/"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.fetch(url=URL + suffix)["repository"], "owner/repo")
        invalid = [
            "http://github.com/owner/repo", "https://github.com.evil/owner/repo",
            "https://gitlab.com/owner/repo", "https://api.github.com/owner/repo",
            "https://user@github.com/owner/repo", "https://user:pass@github.com/owner/repo",
            "https://github.com:443/owner/repo", URL + "?", URL + "#", URL + "?ref=main",
            URL + "/tree/main", URL + "/blob/main/A.sol", URL + "//", URL + "/../evil",
            "https://github.com/owner/..", "https://github.com/../repo", "https://github.com/owner/.git",
            "https://github.com/owner/%2e%2e", "https://github.com/owner/repo%2f..",
            "https://github.com/owner/%252e%252e", "https://github.com/owner/repo\\evil",
            "https://github.com/owner/repo\n", " " + URL, URL + " ", "https://github.com//repo",
            "https://github.com/-owner/repo", "https://github.com/owner-_/repo", None, 123,
        ]
        with patch("lucid.github.httpx.Client") as client:
            for url in invalid:
                with self.subTest(url=url), self.assertRaises(github.GitHubSourceError):
                    github.fetch_repository(url)
            client.assert_not_called()

    def test_invalid_refs_rejected_before_network(self):
        invalid = [None, 3, "a" * 256, "../main", "a/../b", "/main", "a//b", "main/", "a.lock", "a/.b",
                   "a..b", "a@{b", "a%2Fb", "a%252Fb", "main?x", "main#x", "main\n", "main\x7f", "a b", "a\\b"]
        with patch("lucid.github.httpx.Client") as client:
            for ref in invalid:
                with self.subTest(ref=ref), self.assertRaises(github.GitHubSourceError):
                    github.fetch_repository(URL, ref)
            client.assert_not_called()

    def test_invalid_subdirectories_rejected_before_network(self):
        invalid = [None, 3, "/src", "../src", "src/../other", "src/", "src//a", ".", "src/.",
                   "src\\a", "C:/src", "%2e%2e", "src/%252e%252e", "src?x", "src#x", "a\n", "a\x7f", "a\u202e", "a" * 241]
        with patch("lucid.github.httpx.Client") as client:
            for directory in invalid:
                with self.subTest(directory=directory), self.assertRaises(github.GitHubSourceError):
                    github.fetch_repository(URL, subdirectory=directory)
            client.assert_not_called()

    def test_subdirectory_exact_prefix_and_symlink_submodule_omission(self):
        entries = [blob("src2/Wrong.sol"), blob("src.sol"), blob("src/Z.sol", b"z", mode="100755"),
                   blob("src/A.sol", b"a"), blob("src/Link.sol", mode="120000"),
                   blob("src/Vendor.sol", type="commit", mode="160000"), blob("src/no.txt"),
                   {"path": "src/nested", "type": "tree", "mode": "040000"}]
        result = self.fetch(entries=entries, sources={"src/A.sol": b"a", "src/Z.sol": b"z"}, subdirectory="src")
        self.assertEqual(result["files"], [{"path": "src/A.sol", "content": "a"}, {"path": "src/Z.sol", "content": "z"}])
        self.assertEqual(result["subdirectory"], "src")
        self.assertEqual(len(self.requests), 5)

    def test_only_selected_solidity_paths_are_validated(self):
        unrelated = [blob("docs/why?.md"), blob("docs/" + "a" * 300 + ".md"),
                     blob("src/notes?.txt"), blob("src/" + "a" * 300 + ".txt"),
                     blob("src2/bad?.sol", size=400_001), blob("other/" + "a" * 300 + ".sol"),
                     blob(None), blob(123)]
        result = self.fetch(entries=[blob(), *unrelated], subdirectory="src")
        self.assertEqual(result["files"], [{"path": "src/A.sol", "content": "contract A {}"}])
        self.assertEqual(len(self.requests), 4)
        result = self.fetch(entries=[blob(), *unrelated[:4]])
        self.assertEqual(result["file_count"], 1)

    def test_selected_subdirectory_paths_still_reject_unsafe_names(self):
        for path in ("src/bad?.sol", "src/" + "a" * 300 + ".sol", "src/../A.sol", "src/%2e%2e/A.sol"):
            with self.subTest(path=path), self.assertRaises(github.GitHubSourceError):
                self.fetch(entries=[blob(path)], subdirectory="src")
            self.assertEqual(len(self.requests), 3)

    def test_utf8_counts_characters_not_bytes_and_quotes_paths(self):
        path, source = "src/space name-λ.sol", "// λ".encode()
        result = self.fetch(entries=[blob(path, source)], sources={path: source})
        self.assertEqual(result["total_chars"], 4)
        self.assertEqual(result["files"][0]["path"], path)
        self.assertIn(b"space%20name-%CE%BB.sol", self.requests[-1].url.raw_path)

    def test_truncated_or_missing_truncation_flag(self):
        for tree in ({"truncated": True, "tree": [blob()]}, {"tree": [blob()]}, {"truncated": "false", "tree": []}):
            with self.subTest(tree=tree), self.assertRaisesRegex(github.GitHubSourceError, "truncated"):
                self.fetch(handler=lambda request, tree=tree: httpx.Response(200, json=tree) if "/git/trees/" in request.url.path else None)
            self.assertEqual(len(self.requests), 3)

    def test_tree_and_metadata_response_byte_limits(self):
        for match in ("/git/trees/", "/commits/", "/repos/owner/repo"):
            def handler(request, match=match):
                if (match == request.url.path) or (match != "/repos/owner/repo" and match in request.url.path):
                    return httpx.Response(200, headers={"content-length": "8000001"}, stream=Chunks([b"never read"]))
            with self.subTest(match=match), self.assertRaisesRegex(github.GitHubSourceError, "byte preview limit"):
                self.fetch(handler=handler)
        stream = Chunks([b"x" * 20, b"y" * 20])
        with patch.object(github, "MAX_TREE_BYTES", 32), self.assertRaisesRegex(github.GitHubSourceError, "32-byte"):
            self.fetch(handler=lambda request: httpx.Response(200, stream=stream) if "/git/trees/" in request.url.path else None)
        self.assertTrue(stream.closed)

    def test_file_count_limit_and_boundary(self):
        entries = [blob(f"{index}.sol", b"") for index in range(201)]
        with self.assertRaisesRegex(github.GitHubSourceError, "200 Solidity"):
            self.fetch(entries=entries)
        self.assertEqual(len(self.requests), 3)
        result = self.fetch(entries=entries[:200], sources={f"{index}.sol": b"" for index in range(200)})
        self.assertEqual(result["file_count"], 200)

    def test_advertised_source_limits_before_downloads(self):
        for entries, message in (([blob(size=400_001)], "400,000"),
                                 ([blob(f"{index}.sol", size=400_000) for index in range(5)], "1,600,000")):
            with self.subTest(message=message), self.assertRaisesRegex(github.GitHubSourceError, message):
                self.fetch(entries=entries)
            self.assertEqual(len(self.requests), 3)

    def test_exact_source_byte_boundaries(self):
        source = b"x" * 400_000
        result = self.fetch(entries=[blob(f"{index}.sol", source) for index in range(4)],
                            sources={f"{index}.sol": source for index in range(4)})
        self.assertEqual(result["total_chars"], 1_600_000)

    def test_actual_stream_size_limit_and_closure(self):
        stream = Chunks([b"x" * 400_000, b"x", b"not read"])
        with self.assertRaisesRegex(github.GitHubSourceError, "400,000"):
            self.fetch(entries=[blob(size=400_000)], handler=lambda request: httpx.Response(200, stream=stream) if request.url.host == "raw.githubusercontent.com" else None)
        self.assertTrue(stream.closed)
        self.assertEqual(stream.reads, 2)
        with self.assertRaisesRegex(github.GitHubSourceError, "400,000"):
            self.fetch(handler=lambda request: httpx.Response(200, headers={"content-length": "400001"}, stream=Chunks([])) if request.url.host == "raw.githubusercontent.com" else None)

    def test_compressed_response_rejected_before_decompression(self):
        with self.assertRaisesRegex(github.GitHubSourceError, "compressed response"):
            self.fetch(handler=lambda request: httpx.Response(200, headers={"content-encoding": "gzip"},
                       stream=Chunks([gzip.compress(b"x" * 400_001)])) if request.url.host == "raw.githubusercontent.com" else None)

    def test_source_size_mismatch_and_invalid_utf8(self):
        for content, message in ((b"short", "does not match"), (b"\xff" * 13, "UTF-8")):
            with self.subTest(message=message), self.assertRaisesRegex(github.GitHubSourceError, message):
                self.fetch(sources={"src/A.sol": content})

    def test_actual_cumulative_download_limit(self):
        # Keep metadata under the budget; downloaded sources are still bounded.
        with patch.object(github, "MAX_SOURCE_BYTES", 20), self.assertRaisesRegex(github.GitHubSourceError, "7-byte"):
            self.fetch(entries=[blob("A.sol"), blob("B.sol", b"")], sources={"A.sol": b"contract A {}", "B.sol": b"x" * 8})

    def test_no_solidity(self):
        for entries in ([], [blob("a.txt")], [blob(mode="120000")]):
            with self.subTest(entries=entries), self.assertRaisesRegex(github.GitHubSourceError, "No regular .sol"):
                self.fetch(entries=entries)
        with self.assertRaisesRegex(github.GitHubSourceError, "No regular .sol"):
            self.fetch(subdirectory="src2")

    def test_malicious_tree_paths_and_invalid_sizes(self):
        for path in ("../A.sol", "/A.sol", "a/../A.sol", "a/%2e%2e/A.sol", "a/%252e%252e/A.sol", "a\\A.sol", "a\nA.sol", "a//A.sol", "a?x.sol", "a#x.sol"):
            with self.subTest(path=path), self.assertRaises(github.GitHubSourceError):
                self.fetch(entries=[blob(path)])
        for size in (-1, True, None, "13"):
            with self.subTest(size=size), self.assertRaisesRegex(github.GitHubSourceError, "invalid source file size"):
                self.fetch(entries=[blob(size=size)])
        with self.assertRaisesRegex(github.GitHubSourceError, "duplicate"):
            self.fetch(entries=[blob(), blob()])

    def test_http_errors_are_actionable_and_do_not_leak_response(self):
        cases = [(429, {}, "rate limit"), (403, {"x-ratelimit-remaining": "0"}, "rate limit"),
                 (403, {"retry-after": "60"}, "rate limit"), (403, {}, "denied"),
                 (401, {}, "authentication failed"), (404, {}, "token's repository access"),
                 (302, {"location": "https://evil.example/secret"}, "redirect"), (500, {}, "retry later")]
        for stage in ("metadata", "commit", "tree", "raw"):
            for status, headers, message in cases:
                def handler(request, stage=stage, status=status, headers=headers):
                    target = {"metadata": str(request.url) == API, "commit": "/commits/" in request.url.path,
                              "tree": "/git/trees/" in request.url.path, "raw": request.url.host == "raw.githubusercontent.com"}[stage]
                    return httpx.Response(status, headers=headers, content=b"secret-token") if target else None
                with self.subTest(stage=stage, status=status), self.assertRaisesRegex(github.GitHubSourceError, message) as caught:
                    self.fetch(handler=handler)
                self.assertNotIn("secret", str(caught.exception))
                self.assertTrue(all(request.url.host != "evil.example" for request in self.requests))

    def test_transport_errors_are_redacted(self):
        for error, message in ((httpx.ReadTimeout("secret-token"), "timed out"),
                               (httpx.ConnectError("secret-token"), "network connection")):
            def handler(request, error=error):
                raise error
            with self.subTest(error=error), self.assertRaisesRegex(github.GitHubSourceError, message) as caught:
                self.fetch(handler=handler)
            self.assertNotIn("secret", str(caught.exception))

    def test_invalid_api_responses(self):
        cases = [(API, b"bad", "malformed JSON"), (API, b"[]", "invalid API"),
                 (API, json.dumps({"private": True, "default_branch": "main"}).encode(), "Only public"),
                 (API, json.dumps({"private": False, "default_branch": "../main"}).encode(), "valid branch"),
                 (f"{API}/commits/main", b'{"sha":"../evil"}', "commit SHA"),
                 (f"{API}/commits/main", b'{"sha":42}', "commit SHA"),
                 (f"{API}/git/trees/{SHA}?recursive=1", b'{"truncated":false,"tree":null}', "invalid repository tree"),
                 (f"{API}/git/trees/{SHA}?recursive=1", b'{"truncated":false,"tree":[null]}', "invalid tree entry")]
        for target, body, message in cases:
            with self.subTest(target=target, body=body), self.assertRaisesRegex(github.GitHubSourceError, message):
                self.fetch(handler=lambda request, body=body, target=target: httpx.Response(200, content=body) if str(request.url) == target else None)
        for length in ("bad", "-1"):
            with self.subTest(length=length), self.assertRaisesRegex(github.GitHubSourceError, "invalid response length"):
                self.fetch(handler=lambda request, length=length: httpx.Response(200, headers={"content-length": length}, stream=Chunks([])))

    def test_elapsed_budget_before_request_and_during_stream(self):
        with patch("lucid.github.time.monotonic", side_effect=[0, 61]), self.assertRaisesRegex(github.GitHubSourceError, "elapsed-time budget"):
            self.fetch()
        self.assertEqual(self.requests, [])
        stream = Chunks([b'{}'])
        clock = [0]

        def handler(request):
            clock[0] = 61
            return httpx.Response(200, stream=stream)

        with patch("lucid.github.time.monotonic", side_effect=lambda: clock[0]), self.assertRaisesRegex(github.GitHubSourceError, "elapsed-time budget"):
            self.fetch(handler=handler)
        self.assertTrue(stream.closed)

    def test_request_timeout_uses_remaining_budget(self):
        clock = [0]

        def handler(request):
            if str(request.url) == API:
                clock[0] = 55
            else:
                self.assertLessEqual(request.extensions["timeout"]["read"], 5)

        with patch("lucid.github.time.monotonic", side_effect=lambda: clock[0]):
            self.fetch(handler=handler)


if __name__ == "__main__":
    unittest.main()
