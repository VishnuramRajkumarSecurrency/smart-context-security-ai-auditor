"""Local-only web API and dashboard. Run with ``python -m lucid.web``."""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from threading import Event, Lock
from typing import Literal
from uuid import uuid4

import openai
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import agent, codex, context, judge, kiro, llm, specialists
from .codebase import MAX_CHARS, load_codebase
from .github import GitHubSourceError, fetch_repository
from .schema import Finding

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "target" / "src"
DIST = ROOT / "ui" / "dist"

MAX_REQUEST_BYTES = 2_000_000
PROVIDER_ERROR = "Provider request failed. Check your backend API key, base URL, model, credits, and network. Earlier rounds remain available; retries may incur additional charges."
SPECIALIST_LANES = [
    {"key": lane["key"], "name": lane["name"]} for lane in specialists.SPECIALISTS
] + [{"key": "general_hunter", "name": "General Hunter (broad, unfocused pass)"}]

ALLOWED_ORIGINS = {
    f"http://{host}:{port}"
    for host in ("localhost", "127.0.0.1")
    for port in (8765, 5173)
}


def provider_error(exc: Exception) -> str:
    """Expose only SDK error categories/status codes, never provider text or bodies."""
    if isinstance(exc, (kiro.KiroError, codex.CodexError)):
        return str(exc)
    if isinstance(exc, openai.APIStatusError):
        messages = {
            400: "Provider rejected the request (HTTP 400). Check model support, JSON/reasoning settings, and context size.",
            401: "Provider authentication failed (HTTP 401). Set a valid API key for the configured provider and restart the backend.",
            402: "Provider requires payment (HTTP 402). Check your provider credits and billing.",
            403: "Provider denied access (HTTP 403). Check whether your key is valid, active, and allowed to use this endpoint/model. For Requesty, use a Requesty API key—not a ChatGPT subscription credential.",
            404: "Provider endpoint or model was not found (HTTP 404). Check the base URL and select an available model ID.",
            413: "Provider rejected the request size (HTTP 413). Select a smaller source scope.",
            429: "Provider rate or quota limit reached (HTTP 429). Check credits, quotas, and concurrency limits before retrying.",
        }
        if exc.status_code in messages:
            return messages[exc.status_code]
        if exc.status_code >= 500:
            return "Provider service failed (HTTP 5xx). Check provider service status before retrying."
    if isinstance(exc, openai.APITimeoutError):
        return "Provider request timed out. Check connectivity and try a smaller target before retrying."
    if isinstance(exc, openai.APIConnectionError):
        return "Could not connect to the provider. Check network connectivity and the configured base URL."
    return PROVIDER_ERROR


class SourceFile(BaseModel):
    path: str = Field(min_length=1, max_length=240)
    content: str = Field(max_length=MAX_CHARS)


class AuditRequest(BaseModel):
    mode: Literal["single", "context", "loop", "specialists"] = "single"
    rounds: int = Field(default=5, ge=1, le=10)
    confirmed_paid: bool = False
    provider: Literal["auto", "api", "kiro", "codex"] = "auto"
    model: str | None = Field(default=None, max_length=256)
    json_mode: bool = True
    reasoning_effort: Literal["low", "medium", "high"] | None = None

    @field_validator("model")
    @classmethod
    def valid_model(cls, value: str | None) -> str | None:
        return llm.validate_model(value) if value is not None else None
    target: Literal["bundled", "upload", "github"] = "bundled"
    files: list[SourceFile] = Field(default_factory=list, max_length=200)
    github_preview_id: str = Field(default="", max_length=32)
    github_paths: list[str] | None = Field(default=None, max_length=200)


class GitHubPreviewRequest(BaseModel):
    url: str = Field(min_length=1, max_length=256)
    ref: str = Field(default="", max_length=255)
    subdirectory: str = Field(default="", max_length=240)
    authenticated: bool = False


def uploaded_codebase(files: list[SourceFile]) -> str:
    """Match the CLI's input format without writing user-supplied paths to disk."""
    if not files:
        raise ValueError("Select at least one Solidity file.")
    normalized: dict[str, str] = {}
    for file in files:
        name = file.path.replace("\\", "/")
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or any(part in ("", ".", "..") for part in name.split("/"))
            or ":" in name
            or any(ord(char) < 32 for char in name)
            or path.suffix != ".sol"
        ):
            raise ValueError("Upload paths must be relative .sol paths without traversal.")
        if name in normalized:
            raise ValueError(f"Duplicate source path: {name}")
        normalized[name] = file.content
    if not any(content.strip() for content in normalized.values()):
        raise ValueError("Selected source files are empty.")
    blocks = []
    for name, content in sorted(normalized.items()):
        # Match the source preview/editor: Unicode separators inside comments
        # are not new source lines; only LF (including CRLF) advances a line.
        lines = content.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        numbered = "\n".join(
            f"{index}: {line.removesuffix(chr(13))}" for index, line in enumerate(lines, 1)
        )
        blocks.append(f"// FILE: {name}\n{numbered}\n")
    codebase = "\n".join(blocks)
    if len(codebase) > MAX_CHARS:
        raise ValueError(f"Annotated source exceeds {MAX_CHARS:,} characters.")
    return codebase


class PreviewStore:
    """Bounded session snapshots so audits use precisely the previewed source."""

    def __init__(self):
        self.lock = Lock()
        self.download_lock = Lock()
        self.snapshots: dict[str, tuple[float, dict, str]] = {}

    def prune(self):
        now = time.monotonic()
        self.snapshots = {
            key: value for key, value in self.snapshots.items() if now - value[0] < 900
        }

    def add(self, preview: dict, codebase: str) -> dict:
        with self.lock:
            self.prune()
            while len(self.snapshots) >= 10:
                del self.snapshots[next(iter(self.snapshots))]
            preview = preview | {"id": uuid4().hex}
            self.snapshots[preview["id"]] = (time.monotonic(), preview, codebase)
            return preview

    def get(self, preview_id: str, paths: list[str] | None = None) -> tuple[dict, str]:
        with self.lock:
            self.prune()
            if preview_id not in self.snapshots:
                raise HTTPException(400, "GitHub preview expired or was removed. Preview the repository again; no paid call has started.")
            _, preview, codebase = self.snapshots[preview_id]
            source = {key: preview[key] for key in ("repository_url", "repository", "ref", "commit", "subdirectory")}
            files = preview["files"]
            if paths is not None:
                available = {file["path"] for file in files}
                if not paths:
                    raise ValueError("Select at least one Solidity file in the audit scope.")
                if len(paths) != len(set(paths)) or any(path not in available for path in paths):
                    raise ValueError("Audit scope must contain unique paths from the pinned GitHub preview.")
                selected = set(paths)
                files = [file for file in files if file["path"] in selected]
                codebase = uploaded_codebase([SourceFile.model_validate(file) for file in files])
            source.update(
                scope_paths=[file["path"] for file in files],
                file_count=len(files),
                total_chars=sum(len(file["content"]) for file in files),
            )
            return source, codebase


class AuditOutputError(ValueError):
    """A safe, application-generated message that may be displayed to the user."""


def parse_findings(raw: str) -> tuple[list[dict], list[str]]:
    """Do not represent an unusable model response as a successful clean audit."""
    try:
        data = llm.extract_json(raw)
    except ValueError as exc:
        raise AuditOutputError("Model returned unreadable JSON. Retry or check model compatibility.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        raise AuditOutputError("Model response must contain a findings array.")
    findings, warnings = [], []
    for index, item in enumerate(data["findings"], 1):
        try:
            findings.append(Finding.model_validate(item).model_dump())
        except ValidationError:
            warnings.append(f"Finding {index} was skipped because it did not match the schema.")
    if data["findings"] and not findings:
        raise AuditOutputError("All model findings failed validation; this is not a clean audit.")
    return findings, warnings


class AuditStore:
    def __init__(self):
        self.lock = Lock()
        self.stopping = Event()
        self.jobs: dict[str, dict] = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lucid-audit")

    def close(self):
        self.stopping.set()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def check_shutdown(self):
        if self.stopping.is_set():
            raise AuditOutputError("Server is shutting down; no further audit rounds will start.")

    def update(self, job_id: str, **values):
        with self.lock:
            self.jobs[job_id].update(values)

    def get(self, job_id: str) -> dict:
        with self.lock:
            if job_id not in self.jobs:
                raise HTTPException(404, "Audit not found. History resets when the server restarts.")
            return json.loads(json.dumps(self.jobs[job_id]))

    def list(self) -> list[dict]:
        with self.lock:
            return json.loads(json.dumps([
                {key: value for key, value in job.items() if key not in ("context", "findings", "findings_by_lane")}
                | {"findings_count": len(job["findings"])}
                for job in reversed(list(self.jobs.values()))
            ]))

    def start(self, request: AuditRequest, codebase: str, source: dict | None = None) -> dict:
        provider = llm.provider_kind(request.provider)
        model = request.model if request.model is not None else llm.default_model(provider)
        if request.mode == "specialists":
            try:
                llm.lane_workers(len(SPECIALIST_LANES), provider)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
        if provider in ("kiro", "codex"):
            if provider == "kiro" and request.reasoning_effort is not None:
                raise HTTPException(400, "Kiro ACP does not support the reasoning effort option. Use provider default; no audit prompt was sent.")
            try:
                model = (kiro if provider == "kiro" else codex).resolve_model(model)
            except (kiro.KiroError, codex.CodexError) as exc:
                raise HTTPException(400, str(exc)) from None
        with self.lock:
            if any(job["status"] in ("queued", "running") for job in self.jobs.values()):
                raise HTTPException(409, "An audit is already running. Wait for it to finish.")
            while len(self.jobs) >= 50:
                del self.jobs[next(iter(self.jobs))]
            job_id = uuid4().hex
            rounds = request.rounds if request.mode in ("loop", "specialists") else 1
            job = {
                "id": job_id, "status": "queued", "stage": "Queued",
                "mode": request.mode, "rounds": rounds, "completed_rounds": 0,
                "findings": [], "context": None, "error": None, "warnings": [],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "model": model, "provider_kind": provider,
                "json_mode": request.json_mode, "reasoning_effort": request.reasoning_effort,
                "target": source["repository"] if source else "SecondSwap" if request.target == "bundled" else "Uploaded contracts",
                "source": source,
            }
            if request.mode == "specialists":
                job.update(
                    total_rounds=len(SPECIALIST_LANES) * rounds,
                    max_model_calls=2 + len(SPECIALIST_LANES) * rounds,
                    lanes=[lane | {
                        "status": "queued", "completed_rounds": 0, "rounds": rounds,
                        "findings_count": 0, "error": None,
                    } for lane in SPECIALIST_LANES],
                    judge_status="queued",
                    findings_by_lane={lane["key"]: [] for lane in SPECIALIST_LANES},
                )
            self.jobs[job_id] = job
        self.executor.submit(
                    self.run, job_id, codebase, request.mode, rounds,
                    model, request.json_mode, request.reasoning_effort, provider=provider,
                )
        return self.get(job_id)

    def update_lane(self, job_id: str, key: str, *, findings=None, warnings=(), **values):
        """Publish a lane's progress and aggregate it atomically, without shared snapshots."""
        with self.lock:
            job = self.jobs[job_id]
            lane = next(lane for lane in job["lanes"] if lane["key"] == key)
            lane.update(values)
            if findings is not None:
                job["findings_by_lane"][key] = list(findings)
                lane["findings_count"] = len(findings)
            job["warnings"].extend(warnings)
            job["completed_rounds"] = sum(lane["completed_rounds"] for lane in job["lanes"])

    def fail_specialists(self, job_id: str):
        with self.lock:
            job = self.jobs[job_id]
            for lane in job["lanes"]:
                if lane["status"] in ("queued", "running"):
                    lane["status"] = "stopped"
            if job["judge_status"] in ("queued", "running"):
                job["judge_status"] = "skipped"
            job["warnings"].append(
                "Specialist audit is incomplete. Lane candidates are partial, unjudged results; "
                "empty final findings do not indicate a clean audit."
            )

    def run_specialists(
        self, job_id: str, codebase: str, protocol_context: str, rounds: int,
        model: str, json_mode: bool, reasoning_effort: str | None,
        *, provider: str | None = None,
    ):
        stop = Event()
        options = {"model": model, "json_mode": json_mode, "reasoning_effort": reasoning_effort}
        if provider is not None:
            options["provider"] = provider

        def run_lane(key: str):
            findings = []
            try:
                for index in range(rounds):
                    if stop.is_set() or self.stopping.is_set():
                        self.update_lane(job_id, key, status="stopped")
                        return
                    self.update_lane(job_id, key, status="running")
                    if key == "general_hunter":
                        system = agent.SYSTEM_PROMPT
                        plain = agent.CONTEXT_AWARE_USER_PROMPT_TEMPLATE
                        exclusion = agent.EXCLUSION_USER_PROMPT_TEMPLATE
                    else:
                        system, plain = specialists._PROMPTS[key]
                        _, exclusion = specialists._EXCLUSION_PROMPTS[key]
                    prior = "\n".join(f"- {f['title']}: {f['description']}" for f in findings)
                    prompt = (exclusion if prior else plain).format(
                        codebase=codebase, context=protocol_context, already_found=prior,
                    )
                    # Best effort: do not cancel calls already in flight.
                    if stop.is_set() or self.stopping.is_set():
                        self.update_lane(job_id, key, status="stopped")
                        return
                    new, notices = parse_findings(llm.complete(system, prompt, **options))
                    findings.extend(new)
                    self.update_lane(
                        job_id, key, findings=findings, completed_rounds=index + 1,
                        warnings=[f"{key} · round {index + 1}: {notice}" for notice in notices],
                    )
                self.update_lane(job_id, key, status="completed")
            except Exception as exc:  # noqa: BLE001 - redact all provider failures before snapshots or exports.
                stop.set()
                error = str(exc) if isinstance(exc, AuditOutputError) else provider_error(exc)
                self.update_lane(job_id, key, status="failed", error=error)

        workers = llm.lane_workers(len(SPECIALIST_LANES), provider)
        self.update(job_id, stage=f"Auditing · six lanes · up to {workers} concurrent")
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lucid-lane") as executor:
            futures = [executor.submit(run_lane, lane["key"]) for lane in SPECIALIST_LANES]
            for future in futures:
                future.result()
        # All in-flight results have been published before the job can finish or judge.
        self.check_shutdown()
        snapshot = self.get(job_id)
        if stop.is_set():
            error = next(lane["error"] for lane in snapshot["lanes"] if lane["status"] == "failed")
            raise AuditOutputError(error)
        if not any(snapshot["findings_by_lane"].values()):
            self.update(job_id, judge_status="skipped")
            return
        pool = {
            key: [Finding.model_validate(finding) for finding in findings]
            for key, findings in snapshot["findings_by_lane"].items()
        }
        prompt = judge.JUDGE_USER_PROMPT_TEMPLATE.format(
            pooled_findings=judge._format_pool(pool), codebase=codebase,
        )
        self.check_shutdown()
        self.update(job_id, stage="Judging pooled candidates", judge_status="running")
        try:
            self.check_shutdown()
            findings, notices = parse_findings(llm.complete(judge.JUDGE_SYSTEM_PROMPT, prompt, **options))
        except Exception:
            self.update(job_id, judge_status="failed")
            raise
        self.update(
            job_id, findings=findings, judge_status="completed",
            warnings=snapshot["warnings"] + [f"Judge: {notice}" for notice in notices],
        )

    def run(
        self, job_id: str, codebase: str, mode: str, rounds: int,
        model: str, json_mode: bool, reasoning_effort: str | None,
        *, provider: str | None = None,
    ):
        options = {"provider": provider} if provider is not None else {}
        try:
            self.check_shutdown()
            self.update(job_id, status="running", stage="Preparing audit")
            protocol_context = ""
            if mode != "single":
                self.update(job_id, stage="Building protocol context")
                self.check_shutdown()
                protocol_context = context.build_context(
                                    codebase, model=model, reasoning_effort=reasoning_effort, **options,
                                )
                if not protocol_context.strip():
                    raise AuditOutputError("Model returned an empty protocol context. Retry the audit.")
                self.update(job_id, context=protocol_context)
            if mode == "specialists":
                self.run_specialists(
                    job_id, codebase, protocol_context, rounds, model, json_mode, reasoning_effort,
                    **options,
                )
                self.update(job_id, status="completed", stage="Audit complete")
                return
            findings, warnings = [], []
            for index in range(rounds):
                self.update(job_id, stage=f"Auditing · round {index + 1} of {rounds}")
                if mode == "single":
                    prompt = agent.USER_PROMPT_TEMPLATE.format(codebase=codebase)
                elif findings:
                    prior = "\n".join(f"- {f['title']}: {f['description']}" for f in findings)
                    prompt = agent.EXCLUSION_USER_PROMPT_TEMPLATE.format(
                        codebase=codebase, context=protocol_context, already_found=prior
                    )
                else:
                    prompt = agent.CONTEXT_AWARE_USER_PROMPT_TEMPLATE.format(
                        codebase=codebase, context=protocol_context
                    )
                self.check_shutdown()
                new, notices = parse_findings(llm.complete(
                                    agent.SYSTEM_PROMPT, prompt, json_mode=json_mode,
                                    model=model, reasoning_effort=reasoning_effort, **options,
                                ))
                findings.extend(new)
                warnings.extend(f"Round {index + 1}: {notice}" for notice in notices)
                self.update(job_id, findings=list(findings), warnings=list(warnings), completed_rounds=index + 1)
            self.update(job_id, status="completed", stage="Audit complete")
        except AuditOutputError as exc:
            if mode == "specialists":
                self.fail_specialists(job_id)
            self.update(job_id, status="failed", stage="Audit failed", error=str(exc))
        except Exception as exc:  # Provider failures must be redacted at this boundary.
            # Provider exception text can contain credentials or private source.
            if mode == "specialists":
                self.fail_specialists(job_id)
            self.update(
                job_id, status="failed", stage="Audit failed",
                error=provider_error(exc),
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.audits = AuditStore()
    app.state.previews = PreviewStore()
    yield
    app.state.audits.close()


app = FastAPI(title="Lucid local auditor", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])


@app.middleware("http")
async def local_requests(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        return JSONResponse({"detail": "Requests from this origin are not allowed."}, status_code=403)
    if request.method == "POST":
        if request.headers.get("x-lucid-request") != "local-ui":
            return JSONResponse({"detail": "Missing local UI request header."}, status_code=403)
        try:
            size = int(request.headers.get("content-length", "-1"))
        except ValueError:
            size = -1
        if size < 0 or size > MAX_REQUEST_BYTES:
            return JSONResponse({"detail": "Request requires Content-Length and must be below 2 MB."}, status_code=413)
        chunks, total = [], 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_REQUEST_BYTES:
                return JSONResponse({"detail": "Upload exceeds 2 MB."}, status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    return response


@app.get("/api/config")
def config(provider: Literal["auto", "api", "kiro", "codex"] = "auto"):
    from urllib.parse import urlparse
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")
    try:
        kind = llm.provider_kind(provider)
        ready = llm.provider_ready(kind)
    except ValueError:
        raise HTTPException(400, "LUCID_PROVIDER must be auto, api, kiro, or codex.") from None
    return {
        "model": llm.default_model(kind),
        "provider": "Codex CLI" if kind == "codex" else "Kiro CLI (ACP)" if kind == "kiro" else urlparse(base).hostname or "custom provider",
        "provider_kind": kind, "provider_ready": ready,
        "key_configured": bool(os.environ.get("OPENAI_API_KEY")),
        "reasoning_supported": kind != "kiro",
        "default_target": "SecondSwap", "default_file_count": len(list(TARGET.rglob("*.sol"))),
        "max_chars": MAX_CHARS,
    }


@app.get("/api/models")
def models(provider: Literal["auto", "api", "kiro", "codex"] = "auto"):
    try:
        kind = llm.provider_kind(provider)
    except ValueError:
        raise HTTPException(400, "LUCID_PROVIDER must be auto, api, kiro, or codex.") from None
    if not llm.provider_ready(kind):
        raise HTTPException(400, "Configure OPENAI_API_KEY or install and sign in to Kiro CLI or Codex CLI on the backend.")
    try:
        return {"models": llm.list_models(provider=kind)}
    except Exception as exc:  # Never expose provider exception details.
        return JSONResponse(status_code=502, content={
            "detail": f"Could not load provider models. {provider_error(exc)} No inference was requested.",
            "provider_status": exc.status_code if isinstance(exc, openai.APIStatusError) else None,
            "provider_error_code": "codex" if isinstance(exc, codex.CodexError) else "kiro" if isinstance(exc, kiro.KiroError) else None,
        })



@app.get("/api/audits")
def audits(request: Request):
    return request.app.state.audits.list()


@app.get("/api/audits/{job_id}")
def audit(job_id: str, request: Request):
    return request.app.state.audits.get(job_id)


@app.post("/api/github/preview")
def preview_github(body: GitHubPreviewRequest, request: Request):
    store = request.app.state.previews
    if not store.download_lock.acquire(blocking=False):
        raise HTTPException(409, "A GitHub preview is downloading. Wait for it to finish and retry.")
    try:
        preview = fetch_repository(body.url, body.ref, body.subdirectory, authenticated=body.authenticated)
        codebase = uploaded_codebase([SourceFile.model_validate(file) for file in preview["files"]])
        return store.add(preview, codebase)
    except GitHubSourceError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, "GitHub source exceeds the annotated source limit or contains unsupported paths. Choose a smaller contracts subdirectory.") from exc
    finally:
        store.download_lock.release()


@app.post("/api/audits", status_code=202)
def create_audit(body: AuditRequest, request: Request):
    if not body.confirmed_paid:
        raise HTTPException(400, "Confirm paid API usage and source sharing before starting.")
    try:
        kind = llm.provider_kind(body.provider)
    except ValueError:
        raise HTTPException(400, "LUCID_PROVIDER must be auto, api, kiro, or codex.") from None
    if not llm.provider_ready(kind):
        raise HTTPException(400, "Configure OPENAI_API_KEY or install and sign in to Kiro CLI or Codex CLI on the backend, then restart.")
    source = None
    try:
        if body.target == "github":
            source, codebase = request.app.state.previews.get(body.github_preview_id, body.github_paths)
        else:
            codebase = load_codebase(str(TARGET)) if body.target == "bundled" else uploaded_codebase(body.files)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        return request.app.state.audits.start(body, codebase, source)
    except ValueError as exc:
        raise HTTPException(400, "Configure a valid OPENAI_MODEL or select a model ID before starting. No paid call has started.") from exc


@app.get("/")
def dashboard():
    if not (DIST / "index.html").exists():
        raise HTTPException(503, "Frontend not built. Run npm install and npm run build inside ui/.")
    return FileResponse(DIST / "index.html")


if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


def main():
    import uvicorn
    uvicorn.run("lucid.web:app", host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
