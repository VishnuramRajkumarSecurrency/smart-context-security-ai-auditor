"""Smoke test: prove your setup can reach a model before running a full scan.

    python -m lucid.check

It loads .env (via the package import), checks your key and base URL, shows
which model is configured, and makes one tiny real call. If anything is off it
says exactly what to fix in one line - not a traceback.
"""

from urllib.parse import urlparse

from . import llm  # importing the package runs load_dotenv() in __init__


def main() -> None:
    import os

    try:
        provider = llm.provider_kind()
        if not llm.provider_ready():
            print("Configure OPENAI_API_KEY or install and sign in to Kiro CLI or Codex CLI.")
            return
        model = llm.default_model()
    except ValueError:
        print("Check LUCID_PROVIDER and the configured model ID.")
        return

    print(f"provider: {provider}")
    if provider == "api":
        print(f"base: {urlparse(os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com')).hostname}")
        print("key: configured (hidden)")
    print(f"model: {model}")

    print("\nCalling the model (consumes provider credits/subscription usage)...")
    try:
        reply = llm.complete("You are a helpful assistant.", "Reply with exactly: ready", json_mode=False)
    except Exception:  # Raw SDK/CLI diagnostics may contain credentials or source.
        print("  Call failed. Check provider authentication, model access, credits, and connectivity.")
        return

    print(f"reply: {reply.strip()!r}")
    if "ready" in reply.lower():
        print("\nAll set - provider authentication and model resolve.")
    else:
        print("\nGot a reply, so the chain resolves - the exact text just varied.")


if __name__ == "__main__":
    main()
