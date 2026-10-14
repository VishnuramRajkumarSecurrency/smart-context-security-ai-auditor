"""Minimal LLM client.

This is the thinnest possible wrapper around a chat model. The whole point of
the course is that YOU own the harness, so we keep the provider layer tiny and
readable: one function to call the model, one helper to parse its reply.

Providers use the OpenAI-compatible API. Configure OPENAI_MODEL and
OPENAI_BASE_URL, or pass a model per call; credentials stay in the environment.
"""

import json
import os
import re

from openai import OpenAI

from . import codex, kiro

# Legacy course default. Availability is provider-dependent; prefer an explicit
# OPENAI_MODEL or a per-audit selection from the provider's current catalog.
MODEL = "openai/gpt-5.6-luna"


def provider_kind(provider: str | None = None) -> str:
    """Resolve a request override; auto uses the backend's configured default.

    Never fall back after a failed paid call. Omitted overrides preserve CLI
    configuration, while explicit providers override it without changing the environment.
    """
    if provider is not None and provider not in ("auto", "api", "kiro", "codex"):
        raise ValueError("Provider must be auto, api, kiro, or codex.")
    configured = (os.environ.get("LUCID_PROVIDER", "auto").strip().lower()
                  if provider in (None, "auto") else provider)
    if configured not in ("auto", "api", "kiro", "codex"):
        raise ValueError("LUCID_PROVIDER must be auto, api, kiro, or codex.")
    if configured != "auto":
        return configured
    if os.environ.get("OPENAI_API_KEY"):
        return "api"
    if kiro.executable():
        return "kiro"
    return "codex" if codex.executable() else "api"


def provider_ready(provider: str | None = None) -> bool:
    kind = provider_kind(provider)
    if kind in ("kiro", "codex"):
        return bool((kiro if kind == "kiro" else codex).executable())
    return bool(os.environ.get("OPENAI_API_KEY"))


def default_model(provider: str | None = None) -> str:
    """CLI defaults are separate so stale API model IDs aren't reused."""
    kind = provider_kind(provider)
    if kind == "codex":
        return validate_model(os.environ.get("LUCID_CODEX_MODEL", codex.DEFAULT_MODEL))
    if kind == "kiro":
        return validate_model(os.environ.get("LUCID_KIRO_MODEL", kiro.DEFAULT_MODEL))
    return validate_model(os.environ.get("OPENAI_MODEL", MODEL))


def validate_model(model: str) -> str:
    model = model.strip()
    if not model or len(model) > 256 or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in model):
        raise ValueError("Use a model ID of at most 256 characters without whitespace or control characters.")
    return model


def _client(*, timeout: float = 600.0) -> OpenAI:
    """Build the API client, reading the key from the environment.

    We never hard-code a key. It comes from OPENAI_API_KEY (see .env.example).

    If you point lucid at a non-OpenAI but OpenAI-compatible provider, set
    OPENAI_BASE_URL too and the client routes there instead of api.openai.com.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Copy .env.example to .env and add your "
            "key there — run.py loads .env automatically on startup."
        )
    base_url = os.environ.get("OPENAI_BASE_URL")
    if base_url:
        return OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
    return OpenAI(api_key=api_key, timeout=timeout, max_retries=0)


def list_models(provider: str | None = None) -> list[dict[str, str]]:
    """List IDs only, without forwarding provider metadata or invoking inference."""
    kind = provider_kind(provider)
    if kind in ("kiro", "codex"):
        return (kiro if kind == "kiro" else codex).list_models()
    with _client(timeout=10.0) as client:
        page = client.models.list()
    ids = set()
    for item in page.data:
        try:
            ids.add(validate_model(item.id))
        except (ValueError, AttributeError, TypeError):
            continue
    return [{"id": model} for model in sorted(ids)]


def complete(
    system: str, user: str, json_mode: bool = True, *,
    model: str | None = None, reasoning_effort: str | None = None,
    provider: str | None = None,
) -> str:
    """Send one system + one user message to the model and return the reply text.

    Args:
        system:    the standing instructions (who the model is, how to answer).
        user:      the actual request (here: the code to audit).
        json_mode: if True, ask the API to return strict JSON (JSON mode). This
                   makes parsing far more reliable than free-form text.
        model:     per-call model ID; defaults to OPENAI_MODEL or the course fallback.
        reasoning_effort: optional provider-specific reasoning control.
        provider: optional auto/api/kiro/codex override; credentials remain backend-only.

    Returns:
        The raw text of the model's reply. Parsing is a separate step so the
        caller can decide what to do with it.
    """
    kind = provider_kind(provider)
    if kind in ("kiro", "codex"):
        transport = kiro if kind == "kiro" else codex
        return transport.complete(system, user, model=model if model is not None else default_model(kind),
                             json_mode=json_mode, reasoning_effort=reasoning_effort)
    # Build the call arguments. JSON mode tells the API "the reply MUST be a
    # valid JSON object", which removes a whole class of parsing headaches. Not
    # every model supports it, so it stays optional — and we only add the
    # response_format key when we actually want it. Passing response_format=None
    # trips up some OpenAI-compatible gateways, so we leave it out entirely.
    kwargs = {
        "model": validate_model(model) if model is not None else default_model(kind),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        # Leave sampling and reasoning at provider defaults for portability.
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    if reasoning_effort is not None:
        if reasoning_effort not in ("low", "medium", "high"):
            raise ValueError("Unsupported reasoning effort.")
        kwargs["reasoning_effort"] = reasoning_effort

    with _client() as client:
        completion = client.chat.completions.create(**kwargs)

    # If the model hit its output-token limit, the reply is cut off mid-JSON and
    # will fail to parse. Say so plainly instead of letting a confusing parse
    # error surface downstream.
    if completion.choices[0].finish_reason == "length":
        raise RuntimeError(
            "The model's reply was truncated (it hit the output-token limit), "
            "so the JSON is incomplete. Try a smaller target or fewer files."
        )

    return completion.choices[0].message.content or ""


# A code fence looks like ```json ... ``` or ``` ... ```. We strip it if present.
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def extract_json(text: str) -> dict:
    """Parse a JSON object out of a model reply, tolerantly.

    Even in JSON mode a model can wrap its answer in a code fence or add a stray
    sentence before the braces. This helper handles the common cases:

      1. the reply is already clean JSON,
      2. the reply is wrapped in a ```json ... ``` fence,
      3. there is preamble/trailing text around a single {...} object.

    Returns the parsed dict, or raises ValueError if nothing parses.
    """
    text = text.strip()

    # Case 1: try it as-is first.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Case 2: strip a surrounding code fence and retry.
    unfenced = _FENCE.sub("", text).strip()
    try:
        return json.loads(unfenced)
    except json.JSONDecodeError:
        pass

    # Case 3: grab the outermost {...} span and parse that. This rescues replies
    # that have chatter before or after the object.
    start = unfenced.find("{")
    end = unfenced.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = unfenced[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse JSON from model reply:\n{text[:500]}")
