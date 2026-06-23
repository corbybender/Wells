"""Configuration: loads credentials/settings from the environment and exposes an LLM client."""

import os
import time
from functools import lru_cache

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage


def _configure_ca_bundle() -> None:
    """Point OpenSSL at a usable CA bundle.

    uv's standalone CPython builds ship their own OpenSSL which, on some Linux
    systems, has no default CA file (``ssl.get_default_verify_paths().cafile``
    is None), causing ``CERTIFICATE_VERIFY_FAILED`` on every HTTPS call. We fix
    this by exporting ``SSL_CERT_FILE``/``SSL_CERT_DIR`` to the first bundle we
    can find (system bundle, then certifi's). A user-provided value wins.
    """
    if os.environ.get("SSL_CERT_FILE"):
        return

    candidates = [
        "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu/WSL
        "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL/Fedora
        "/etc/ssl/cert.pem",  # Alpine/macOS
        "/etc/ssl/certs",  # capath fallback
    ]
    for path in candidates:
        if os.path.exists(path):
            os.environ["SSL_CERT_FILE"] = path
            os.environ.setdefault("REQUESTS_CA_BUNDLE", path)
            os.environ.setdefault("CURL_CA_BUNDLE", path)
            return

    try:
        import certifi

        os.environ["SSL_CERT_FILE"] = certifi.where()
        os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
        os.environ.setdefault("CURL_CA_BUNDLE", certifi.where())
    except Exception:
        pass


_configure_ca_bundle()
load_dotenv()

from coding_harness.tokens import TokenBudget

ZAI_API_KEY: str = os.getenv("ZAI_API_KEY", "").strip()
ZAI_ENDPOINT: str = os.getenv("ZAI_ENDPOINT", "https://api.z.ai/api/paas/v4/").strip()
ZAI_MODEL: str = os.getenv("ZAI_MODEL", "glm-5.2").strip()
# Optional cheaper/faster model for low-stakes subtasks (summarization, log
# compression, classification). Defaults to the main model when unset.
ZAI_MODEL_CHEAP: str = os.getenv("ZAI_MODEL_CHEAP", "").strip()

MAX_ITERATIONS: int = int(os.getenv("MAX_ITERATIONS", "3"))

# Retry tuning for transient network / rate-limit blips.
LLM_TIMEOUT: float = float(os.getenv("LLM_TIMEOUT", "180"))
LLM_MAX_RETRIES: int = int(os.getenv("LLM_MAX_RETRIES", "5"))
LLM_BACKOFF_BASE: float = float(os.getenv("LLM_BACKOFF_BASE", "2.0"))

# --- Token optimization configuration -------------------------------------
BUDGET = TokenBudget(
    max_input_tokens=int(os.getenv("TOKEN_BUDGET_MAX_INPUT", "24000")),
    reserved_output_tokens=int(os.getenv("TOKEN_BUDGET_RESERVED_OUTPUT", "4000")),
)
SMALL_BUDGET = TokenBudget(
    max_input_tokens=int(os.getenv("TOKEN_BUDGET_SMALL_INPUT", "8000")),
    reserved_output_tokens=int(os.getenv("TOKEN_BUDGET_RESERVED_OUTPUT", "4000")),
)
# Replace verbatim plan/architecture with a summary on loop iterations when the
# durable context exceeds this many (estimated) tokens. Set 0 to disable.
SUMMARIZE_ON_LOOP: bool = os.getenv("SUMMARIZE_ON_LOOP", "1") not in ("0", "false", "")
SUMMARIZE_THRESHOLD: int = int(os.getenv("SUMMARIZE_THRESHOLD", "1500"))

# Task types routed to the cheaper model (Phase 5: model router).
CHEAP_TASKS = {"summarization", "compression", "classification", "validation", "query_rewrite"}


def model_name_for_task(task_type: str) -> str:
    """Pick the model for a task type. Cheap subtasks use ZAI_MODEL_CHEAP if set."""
    if task_type in CHEAP_TASKS and ZAI_MODEL_CHEAP:
        return ZAI_MODEL_CHEAP
    return ZAI_MODEL


@lru_cache(maxsize=8)
def _client_for(model: str, temperature: float):
    from langchain_openai import ChatOpenAI

    if not ZAI_API_KEY:
        raise RuntimeError(
            "ZAI_API_KEY is not set. Copy .env.example to .env and add your Z.ai API key."
        )

    return ChatOpenAI(
        model=model,
        api_key=ZAI_API_KEY,
        base_url=ZAI_ENDPOINT,
        temperature=temperature,
        timeout=LLM_TIMEOUT,
        max_retries=0,
    )


def get_llm(temperature: float = 0.3):
    """Cached ChatOpenAI client for the main model."""
    return _client_for(ZAI_MODEL, temperature)


def get_llm_for_task(task_type: str, temperature: float = 0.3):
    """Cached ChatOpenAI client selected by the model router for ``task_type``."""
    return _client_for(model_name_for_task(task_type), temperature)


def _is_transient(err: Exception) -> bool:
    """True for errors worth retrying (timeouts, connection issues, 429, 5xx)."""
    try:
        import openai

        if isinstance(
            err,
            (
                openai.APIConnectionError,
                openai.APITimeoutError,
                openai.RateLimitError,
                openai.InternalServerError,
            ),
        ):
            return True
    except Exception:
        pass
    return False


def _invoke_with_retry(llm, messages):
    """Invoke ``llm`` with ``messages``, retrying transient failures.

    OpenAI-level retries are disabled on the client (max_retries=0); we run this
    backoff loop so progress is logged and transient TLS/429 errors are survived.
    Raises the last error if every retry fails.
    """
    last_err: Exception | None = None
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            return llm.invoke(messages)
        except Exception as err:
            last_err = err
            if not _is_transient(err) or attempt == LLM_MAX_RETRIES:
                break
            backoff = min(LLM_BACKOFF_BASE ** attempt, 30.0)
            print(
                f"[llm] transient {type(err).__name__} on attempt {attempt}/"
                f"{LLM_MAX_RETRIES}; retrying in {backoff:.1f}s ..."
            )
            time.sleep(backoff)
    assert last_err is not None
    raise last_err


def ask_llm(prompt: str, temperature: float = 0.3) -> str:
    """Legacy convenience wrapper: one human message -> response text.

    New agent code uses :func:`coding_harness.runtime.run_step` instead, which
    accounts for tokens. This is kept for ad-hoc / external use.
    """
    try:
        resp = _invoke_with_retry(get_llm(temperature), [HumanMessage(content=prompt)])
        return (resp.content or "").strip()
    except Exception as err:
        msg = f"[LLM call failed after {LLM_MAX_RETRIES} attempts: {type(err).__name__}: {str(err)[:200]}]"
        print(f"[llm] giving up: {msg}")
        return msg
