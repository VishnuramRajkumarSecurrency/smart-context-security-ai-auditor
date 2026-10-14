"""Bounded GitHub Solidity previews with opt-in private repository access."""

import json
import os
import re
import time
import unicodedata
from urllib.parse import quote

import httpx

MAX_FILES = 200
MAX_FILE_BYTES = 400_000
MAX_SOURCE_BYTES = 1_600_000
MAX_TREE_BYTES = 8_000_000
# Elapsed budget checked between operations, not a hard wall-clock timeout.
TOTAL_TIMEOUT = 60.0
REQUEST_TIMEOUT = 10.0


class GitHubSourceError(ValueError):
    """An application-generated error safe to display to the user."""


def _repository(url: str) -> str:
    if not isinstance(url, str) or len(url) > 256:
        raise GitHubSourceError("Use a repository root URL: https://github.com/owner/repo.")
    match = re.fullmatch(
        r"https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/([A-Za-z0-9_.-]{1,104})/?",
        url,
    )
    if not match:
        raise GitHubSourceError("Use a repository root URL: https://github.com/owner/repo; supply the ref separately.")
    owner, repo = match.groups()
    repo = repo.removesuffix(".git")
    if not repo or repo in (".", "..") or len(repo) > 100:
        raise GitHubSourceError("The GitHub repository name is invalid.")
    return f"{owner}/{repo}"


def _ref(value: object) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,254}", value)
        or ".." in value
        or any(not part or part.startswith(".") or part.endswith((".", ".lock")) for part in value.split("/"))
    ):
        raise GitHubSourceError("Use a valid branch, tag, or commit ref of at most 255 characters; branch slashes are allowed.")
    return value


def _path(value: object, *, empty: bool = False) -> str:
    if isinstance(value, str) and empty and value == "":
        return value
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 240
        or any(char in value for char in "\\:%?#")
        or any(unicodedata.category(char).startswith("C") for char in value)
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise GitHubSourceError("Use a relative repository path without traversal, encoded characters, or control characters (maximum 240 characters).")
    return value


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GitHubSourceError("GitHub preview exceeded its elapsed-time budget, checked between operations. Retry with a smaller subdirectory.")
    return min(REQUEST_TIMEOUT, remaining)


def _download(
    client: httpx.Client, url: str, limit: int, deadline: float, label: str,
    *, accept: str = "application/vnd.github+json",
) -> bytes:
    timeout = _remaining(deadline)
    with client.stream("GET", url, timeout=timeout, headers={"Accept": accept}) as response:
        _remaining(deadline)
        status = response.status_code
        if status == 429 or (status == 403 and (
            response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers
        )):
            raise GitHubSourceError("GitHub's rate limit was reached. Wait and retry later; authenticated requests can also be rate limited.")
        if status == 401:
            raise GitHubSourceError("GitHub authentication failed. Check the token used for authenticated access.")
        if status == 404:
            raise GitHubSourceError("Repository, ref, or source was not found. Check the URL and ref, and the token's repository access for private repositories.")
        if status == 403:
            raise GitHubSourceError("GitHub denied this request. Check token permissions and organization SSO restrictions.")
        if 300 <= status < 400:
            raise GitHubSourceError("GitHub returned a redirect. Use the repository's current canonical URL; redirects are not followed.")
        if status != 200:
            raise GitHubSourceError("GitHub could not provide the preview. Check the repository and retry later.")
        # Avoid allocating an unbounded decompressed chunk before checking its size.
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise GitHubSourceError("GitHub returned an unexpectedly compressed response. Retry later; only uncompressed previews are supported.")
        length = response.headers.get("content-length")
        if length is not None:
            try:
                size = int(length)
            except ValueError:
                raise GitHubSourceError("GitHub returned an invalid response length.") from None
            if size < 0:
                raise GitHubSourceError("GitHub returned an invalid response length.")
            if size > limit:
                raise GitHubSourceError(f"{label} exceeds the {limit:,}-byte preview limit. Choose a smaller subdirectory or repository.")
        data = bytearray()
        chunks = response.iter_bytes()
        while True:
            _remaining(deadline)
            try:
                chunk = next(chunks)
            except StopIteration:
                break
            _remaining(deadline)
            if len(data) + len(chunk) > limit:
                raise GitHubSourceError(f"{label} exceeds the {limit:,}-byte preview limit. Choose a smaller subdirectory or repository.")
            data.extend(chunk)
        return bytes(data)


def _json(client: httpx.Client, url: str, limit: int, deadline: float, label: str) -> dict:
    try:
        data = json.loads(_download(client, url, limit, deadline, label))
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, GitHubSourceError):
            raise
        raise GitHubSourceError("GitHub returned malformed JSON. Retry later.") from None
    if not isinstance(data, dict):
        raise GitHubSourceError("GitHub returned an invalid API response. Retry later.")
    return data


def fetch_repository(url: str, ref: str = "", subdirectory: str = "", *, authenticated: bool = False) -> dict:
    """Fetch regular .sol files at one pinned commit, keeping repo-root paths.

    Uses GET /repos/{owner}/{repo} (only for a default ref),
    GET /repos/{owner}/{repo}/commits/{ref} with Accept: application/vnd.github.sha, and
    GET /repos/{owner}/{repo}/git/trees/{commit}?recursive=1 on api.github.com,
    then GET /{owner}/{repo}/{commit}/{path} on raw.githubusercontent.com anonymously.
    With authenticated=True, only LUCID_GITHUB_TOKEN is read and sent as a Bearer
    token to api.github.com; sources use GET /repos/{owner}/{repo}/contents/{path}
    with ref={commit} and Accept: application/vnd.github.raw+json instead.
    Nothing is written to disk or executed. Redirects are never followed.
    The 60-second elapsed budget is checked between operations, with per-request
    inactivity timeouts capped at 10 seconds or the remaining budget. This is
    not a hard wall-clock deadline: trickling headers or other blocking HTTP
    operations can delay the next elapsed-budget check.
    """
    repository = _repository(url)
    subdirectory = _path(subdirectory, empty=True)
    if not isinstance(ref, str):
        raise GitHubSourceError("The ref must be a branch, tag, or commit string.")
    if ref:
        ref = _ref(ref)
    headers = {"User-Agent": "lucid-public-repository-preview", "Accept": "application/vnd.github+json", "Accept-Encoding": "identity"}
    if authenticated:
        token = os.environ.get("LUCID_GITHUB_TOKEN")
        if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,4096}", token):
            raise GitHubSourceError("Authenticated GitHub access requires a valid LUCID_GITHUB_TOKEN (1–4096 printable ASCII characters without whitespace).")
        headers["Authorization"] = f"Bearer {token}"
    deadline = time.monotonic() + TOTAL_TIMEOUT
    api = f"https://api.github.com/repos/{repository}"
    try:
        # trust_env=False also prevents environment proxies and netrc credentials.
        with httpx.Client(
            follow_redirects=False, trust_env=False, timeout=REQUEST_TIMEOUT,
            headers=headers,
        ) as client:
            if not ref:
                metadata = _json(client, api, 64_000, deadline, "Repository metadata")
                private = metadata.get("private")
                if type(private) is not bool:
                    raise GitHubSourceError("GitHub returned an invalid repository visibility. Retry later.")
                if private and not authenticated:
                    raise GitHubSourceError("Only public GitHub repositories are supported without authenticated access.")
                ref = _ref(metadata.get("default_branch"))
            commit_bytes = _download(
                client, f"{api}/commits/{quote(ref, safe='')}", 128, deadline, "Commit SHA response",
                accept="application/vnd.github.sha",
            )
            try:
                commit = commit_bytes.decode("ascii").strip()
            except UnicodeDecodeError:
                raise GitHubSourceError("GitHub returned an invalid commit SHA. Retry later.") from None
            if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
                raise GitHubSourceError("GitHub returned an invalid commit SHA. Retry later.")
            commit = commit.lower()
            tree = _json(client, f"{api}/git/trees/{commit}?recursive=1", MAX_TREE_BYTES, deadline, "Repository tree")
            if tree.get("truncated") is not False:
                raise GitHubSourceError("GitHub returned a truncated or incomplete repository tree. Use a smaller repository; partial previews are not allowed.")
            if not isinstance(tree.get("tree"), list):
                raise GitHubSourceError("GitHub returned an invalid repository tree. Retry later.")
            selected = {}
            for entry in tree["tree"]:
                _remaining(deadline)
                if not isinstance(entry, dict):
                    raise GitHubSourceError("GitHub returned an invalid tree entry. Retry later.")
                if entry.get("type") != "blob" or entry.get("mode") not in ("100644", "100755"):
                    continue
                path = entry.get("path")
                if not isinstance(path, str) or not path.endswith(".sol"):
                    continue
                if subdirectory and not path.startswith(subdirectory + "/"):
                    continue
                path = _path(path)
                size = entry.get("size")
                if type(size) is not int or size < 0:
                    raise GitHubSourceError("GitHub returned an invalid source file size. Retry later.")
                if path in selected:
                    raise GitHubSourceError("GitHub returned duplicate source paths. Retry later.")
                if size > MAX_FILE_BYTES:
                    raise GitHubSourceError("A Solidity file exceeds the 400,000-byte preview limit. Choose a smaller subdirectory.")
                selected[path] = size
                if len(selected) > MAX_FILES:
                    raise GitHubSourceError("More than 200 Solidity files selected. Choose a smaller subdirectory.")
            if not selected:
                raise GitHubSourceError("No regular .sol files found at this ref and subdirectory. Check your selection.")
            if sum(selected.values()) > MAX_SOURCE_BYTES:
                raise GitHubSourceError("Selected Solidity sources exceed the 1,600,000-byte total limit. Choose a smaller subdirectory.")
            files, total_bytes = [], 0
            for path in sorted(selected):
                if authenticated:
                    source_url = f"{api}/contents/{quote(path, safe='/')}?ref={commit}"
                    accept = "application/vnd.github.raw+json"
                else:
                    source_url = f"https://raw.githubusercontent.com/{repository}/{commit}/{quote(path, safe='/')}"
                    accept = "application/vnd.github+json"
                content = _download(client, source_url, min(MAX_FILE_BYTES, MAX_SOURCE_BYTES - total_bytes), deadline, "Solidity source", accept=accept)
                total_bytes += len(content)
                if len(content) != selected[path]:
                    raise GitHubSourceError("Source size does not match the pinned tree. Retry later; partial previews are not allowed.")
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError:
                    raise GitHubSourceError("A Solidity source is not valid UTF-8. Select a subdirectory containing UTF-8 source files.") from None
                files.append({"path": path, "content": text})
            _remaining(deadline)
    except httpx.TimeoutException:
        raise GitHubSourceError("GitHub request timed out. Retry or choose a smaller subdirectory.") from None
    except httpx.HTTPError:
        raise GitHubSourceError("Unable to download from GitHub. Check your network connection and retry.") from None
    return {
        "repository_url": f"https://github.com/{repository}", "repository": repository,
        "ref": ref, "commit": commit, "subdirectory": subdirectory, "files": files,
        "file_count": len(files), "total_chars": sum(len(file["content"]) for file in files),
    }
