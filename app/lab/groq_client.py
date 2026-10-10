"""Groq chat-completions client: one call, explicit timeouts, bounded retries.

The key comes from GROQ_API_KEY (process environment first, then .env.local and
.env in the project root). It is sent only in the Authorization header and never
appears in an exception, a log line or a return value.
"""
from __future__ import annotations
from pathlib import Path
import os
import re
import time
import httpx

ROOT = Path(__file__).resolve().parents[2]
ENV_FILES = (ROOT / ".env.local", ROOT / ".env")
API_URL = "https://api.groq.com/openai/v1/chat/completions"

# Model IDs checked on 2026-10-10 against Groq's documentation and this account's
# live model list (chat models on the account: these two and qwen/qwen3.8-27b, a preview):
#   https://console.groq.com/docs/models              both are production models, context 131,072
#   https://console.groq.com/docs/tool-use            tool calling and JSON mode: both; parallel tool calls: neither
#   https://console.groq.com/docs/structured-outputs  json_schema with strict: true: both
#   https://console.groq.com/docs/rate-limits         free plan, per model: 30 requests/min, 1,000 requests/day,
#                                                     8,000 tokens/min, 200,000 tokens/day
#   https://console.groq.com/docs/prompt-caching      both cache a repeated prompt prefix; cached tokens are not rate limited
# Groq does not accept json_schema output in the same request as tools, so ask for
# one or the other. The Llama production models are enterprise-only and are not on
# this account. Override the default with GROQ_MODEL.
# Seen in live calls the same day (openai/gpt-oss-120b): forced and "required" tool
# choice, tool results, json_object and strict json_schema replies all work; the
# response headers confirmed 1,000 requests/day and 8,000 tokens/min; a forced tool
# call occasionally comes back as HTTP 400 tool_use_failed; the max_completion_tokens
# asked for is counted against the tokens-per-minute limit before the call runs.
MODELS_DOC_URL = "https://console.groq.com/docs/models"
MODELS_CHECKED_ON = "2026-10-10"
DEFAULT_MODEL = "openai/gpt-oss-120b"
FALLBACK_MODEL = "openai/gpt-oss-20b"

TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (0.5, 1.5)
# A longer wait than this cannot fit inside a web request; fail and let the caller decide.
MAX_RETRY_AFTER_SECONDS = 8.0

_TRANSPORT: httpx.BaseTransport | None = None  # tests install an httpx.MockTransport
_sleep = time.sleep


class GroqNotConfigured(RuntimeError):
    """No GROQ_API_KEY in the environment or in the project's .env files."""


class GroqError(RuntimeError):
    """A Groq request failed. The message holds a status and an error code only:
    no key, no request text and no upstream prose. `detail` carries Groq's own
    message with the key removed, for local debugging; do not show it to users.
    `retry_after` is the wait in seconds Groq asked for on a 429, if it gave one."""

    def __init__(self, message: str, *, status: int | None = None, code: str = "", detail: str = "",
                 retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail
        self.retry_after = retry_after


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[name] = value
    return values


def _setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    for path in ENV_FILES:
        value = _read_env_file(path).get(name, "").strip()
        if value:
            return value
    return ""


def is_configured() -> bool:
    return bool(_setting("GROQ_API_KEY"))


def default_model() -> str:
    return _setting("GROQ_MODEL") or DEFAULT_MODEL


def supports_reasoning_effort(model: str) -> bool:
    # https://console.groq.com/docs/reasoning: low | medium | high on the GPT-OSS models.
    return model in (DEFAULT_MODEL, FALLBACK_MODEL)


def _candidates(model: str | None) -> list[str]:
    if model:
        return [model]
    first = default_model()
    return [first] + [name for name in (DEFAULT_MODEL, FALLBACK_MODEL) if name != first][:1]


def _scrub(text: str, key: str) -> str:
    return text.replace(key, "[redacted]") if key else text


def _failure(response: httpx.Response, key: str) -> GroqError:
    status = response.status_code
    code, detail = "", ""
    try:
        error = response.json().get("error") or {}
        code = re.sub(r"[^a-z0-9_]", "", str(error.get("code") or error.get("type") or "").lower())[:60]
        detail = _scrub(str(error.get("message") or ""), key)[:400]
    except (ValueError, AttributeError):
        pass
    if status == 401:
        return GroqError("Groq rejected the API key (HTTP 401). Check GROQ_API_KEY.", status=status, code=code)
    if status == 429:
        return GroqError("Groq rate limit reached (HTTP 429).", status=status, code=code, detail=detail,
                         retry_after=_asked_wait(response))
    return GroqError(f"Groq API error (HTTP {status}{', ' + code if code else ''}).", status=status, code=code, detail=detail)


def _asked_wait(response: httpx.Response) -> float | None:
    try:
        return max(0.0, float(response.headers.get("retry-after", "")))
    except ValueError:
        return None


def _retry_wait(response: httpx.Response, attempt: int) -> float | None:
    wait = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
    asked = _asked_wait(response) if response.status_code == 429 else None
    if asked is None:
        return wait
    return None if asked > MAX_RETRY_AFTER_SECONDS else max(wait, asked)


def _request(client: httpx.Client, key: str, payload: dict) -> dict:
    for attempt in range(MAX_ATTEMPTS):
        last = attempt == MAX_ATTEMPTS - 1
        failed = ""
        try:
            response = client.post(API_URL, json=payload, headers={"Authorization": "Bearer " + key})
        except httpx.TransportError as error:
            failed = type(error).__name__
            last = last or isinstance(error, httpx.ReadTimeout)
        if failed:
            # Raised outside the handler: the httpx exception holds the request, and
            # with it the Authorization header, so it must not ride along as context.
            if last:
                raise GroqError("Groq request failed: " + failed + ".")
            _sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
            continue
        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError:
                data = None
            if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
                raise GroqError("Groq returned no usable completion.", status=200)
            return data
        if not last and (response.status_code == 429 or response.status_code >= 500):
            wait = _retry_wait(response, attempt)
            if wait is not None:
                _sleep(wait)
                continue
        raise _failure(response, key)
    raise GroqError("Groq request failed.")


def _try_next_model(error: GroqError) -> bool:
    # Rate limits are per model, so the fallback has its own budget.
    status = error.status or 0
    return status in (404, 429) or status >= 500 or "model" in error.code


def chat_completion(messages, *, tools=None, tool_choice=None, response_format=None, model=None,
                    temperature=0, max_tokens=None, reasoning_effort=None) -> dict:
    """POST one chat completion and return Groq's response (OpenAI chat-completions format).

    With `model=None` the request uses GROQ_MODEL or DEFAULT_MODEL and moves to the
    fallback once if that model is rate limited, unavailable or retired; read the
    model that answered from the response's "model". An explicit `model` is used as given.
    `max_tokens` is sent as max_completion_tokens (reasoning tokens count toward it).
    Raises GroqNotConfigured without a key and GroqError on any failure.
    """
    key = _setting("GROQ_API_KEY")
    if not key:
        raise GroqNotConfigured("GROQ_API_KEY is not set")
    payload: dict = {"messages": messages, "temperature": temperature}
    if tools:
        payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
    if response_format is not None:
        payload["response_format"] = response_format
    if max_tokens is not None:
        payload["max_completion_tokens"] = max_tokens
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort
    names = _candidates(model)
    with httpx.Client(timeout=TIMEOUT, transport=_TRANSPORT) as client:
        for index, name in enumerate(names):
            try:
                return _request(client, key, {**payload, "model": name})
            except GroqError as error:
                if index + 1 == len(names) or not _try_next_model(error):
                    raise
    raise GroqError("Groq request failed.")
