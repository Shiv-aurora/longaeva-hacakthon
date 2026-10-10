"""Lab API: status, the analyst chat (whole and streamed) and the simulator.

`app/server.py` includes `router`. These endpoints are public, so every request is
bounded in size, each client has a token bucket, model calls have a daily ceiling,
and nothing a user typed is ever written to a log.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Literal
import json
import logging
import math
import os
import threading
import time
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from . import chat, groq_client

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/lab")

MAX_BODY_BYTES = 64_000
MAX_MESSAGE_CHARS = chat.MAX_MESSAGE_CHARS
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CHARS = 2_000
RATE_BURST = 4               # turns a client may start at once
RATE_REFILL_SECONDS = 15.0   # one more turn every 15 s
# Groq's free plan gives each model 1,000 requests and 200,000 tokens a day; a turn
# takes about 4 to 8 model calls. Override with LAB_DAILY_MODEL_CALLS.
DAILY_MODEL_CALLS = 600
MAX_TRACKED_CLIENTS = 5_000
UNAVAILABLE = "The AI service is busy or unavailable. Try again shortly."
BUDGET_USED = "Today's AI budget is used. The simulator and the data pages still work."


def _daily_cap() -> int:
    try:
        return max(0, int(os.environ.get("LAB_DAILY_MODEL_CALLS", "")))
    except ValueError:
        return DAILY_MODEL_CALLS


class RateLimiter:
    """A token bucket per client and one daily count of model calls, held in memory.

    Serverless instances do not share memory and are recycled at will, so each
    instance keeps its own counts. This is a brake on abuse and cost, not a guarantee.
    """

    def __init__(self, burst: int = RATE_BURST, refill_seconds: float = RATE_REFILL_SECONDS,
                 daily_model_calls: int | None = None, clock=time.monotonic, today=None):
        self.burst, self.refill_seconds = burst, refill_seconds
        self.daily_model_calls = _daily_cap() if daily_model_calls is None else daily_model_calls
        self._clock = clock
        self._today = today or (lambda: datetime.now(timezone.utc).date())
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}
        self._day, self._spent = None, 0

    def admit(self, client: str) -> float:
        """Take one turn from the client's bucket: 0.0 if admitted, else the seconds until one is free."""
        now = self._clock()
        with self._lock:
            if client not in self._buckets and len(self._buckets) >= MAX_TRACKED_CLIENTS:
                refilled = self.burst * self.refill_seconds
                self._buckets = {name: state for name, state in self._buckets.items() if now - state[1] < refilled}
                if len(self._buckets) >= MAX_TRACKED_CLIENTS:
                    self._buckets.clear()
            tokens, seen = self._buckets.get(client, (float(self.burst), now))
            tokens = min(float(self.burst), tokens + (now - seen) / self.refill_seconds)
            admitted = tokens >= 1
            self._buckets[client] = (tokens - 1 if admitted else tokens, now)
            return 0.0 if admitted else (1 - tokens) * self.refill_seconds

    def _roll(self) -> None:
        today = self._today()
        if today != self._day:
            self._day, self._spent = today, 0

    def model_calls_left(self) -> int:
        with self._lock:
            self._roll()
            return max(0, self.daily_model_calls - self._spent)

    def spend_model_call(self) -> bool:
        with self._lock:
            self._roll()
            if self._spent >= self.daily_model_calls:
                return False
            self._spent += 1
            return True


_limiter = RateLimiter()


class ChatTurn(BaseModel):
    model_config = {"extra": "forbid"}
    role: Literal["user", "assistant"]
    content: str


class ChatPayload(BaseModel):
    model_config = {"extra": "forbid"}
    message: str
    history: list[ChatTurn] = Field(default_factory=list)


def _client(request: Request) -> str:
    # Vercel writes x-forwarded-for itself. Anywhere else a client could forge it, so it is ignored.
    if os.environ.get("VERCEL") == "1":
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded[:64]
    return request.client.host if request.client else "unknown"


def _admit(request: Request) -> None:
    wait = _limiter.admit(_client(request))
    if wait:
        raise HTTPException(status_code=429, detail="Too many requests. Wait a few seconds and try again.",
                            headers={"Retry-After": str(math.ceil(wait))})


def _budget_error() -> HTTPException:
    now = datetime.now(timezone.utc)
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(days=1)
    return HTTPException(status_code=429, detail=BUDGET_USED, headers={"Retry-After": str(int((midnight - now).total_seconds()) + 1)})


async def _read(request: Request):
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request is too large")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request is too large")
    try:
        return json.loads(body)
    except ValueError:
        # `from None`: the parser's message quotes the body.
        raise HTTPException(status_code=422, detail="Request body is not valid JSON") from None


async def _chat_request(request: Request) -> tuple[str, list[dict]]:
    """Validate, then charge the client's bucket. A turn costs one token however many steps it takes."""
    try:
        body = ChatPayload.model_validate(await _read(request))
    except ValueError:
        raise HTTPException(status_code=422, detail="Expected {message, history: [{role, content}]}") from None
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Message is empty")
    if len(message) > MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=422, detail=f"Message is too long (maximum {MAX_MESSAGE_CHARS} characters)")
    if len(body.history) > MAX_HISTORY_MESSAGES or any(len(turn.content) > MAX_HISTORY_CHARS for turn in body.history):
        raise HTTPException(status_code=422, detail=f"History is too long (maximum {MAX_HISTORY_MESSAGES} messages "
                                                    f"of {MAX_HISTORY_CHARS} characters)")
    _admit(request)
    if not _limiter.model_calls_left():
        raise _budget_error()
    return message, [turn.model_dump() for turn in body.history]


@router.get("/status")
def lab_status():
    return {"ai_configured": groq_client.is_configured(), "model": groq_client.default_model(),
            "data_files": chat.data_files(), "simulator": chat.scenario_connected(), "simulator_parts": chat.simulator_parts(),
            "limits": {"max_message_chars": MAX_MESSAGE_CHARS, "max_history_messages": MAX_HISTORY_MESSAGES,
                       "max_history_chars": MAX_HISTORY_CHARS, "max_tool_calls": chat.MAX_TOOL_CALLS}}


@router.post("/chat")
async def lab_chat(request: Request):
    """One analyst turn as a single JSON object.

    Request:  {"message": "...", "history": [{"role": "user" | "assistant", "content": "..."}]}
    Response: the result documented on `chat.iter_chat` ({status, answer, verified,
    report, steps, charts, evidence_ids, scenarios, grids, ...}), or
    {"status": "not_configured"} when there is no API key. A turn takes one token
    from the client's bucket however many steps it runs; each model call counts
    against the daily ceiling. Errors: 413, 422, 429 (with Retry-After) and 503.
    """
    if not groq_client.is_configured():
        return {"status": "not_configured"}
    message, history = await _chat_request(request)
    try:
        return await run_in_threadpool(chat.run_chat, message, history, allow_call=_limiter.spend_model_call)
    except groq_client.GroqNotConfigured:
        return {"status": "not_configured"}
    except chat.BudgetExhausted:
        raise _budget_error() from None
    except groq_client.GroqError as error:
        # The message is a status and an error code: no key and nothing the user typed.
        log.warning("lab chat: upstream failure: %s", error)
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from error


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _events(message: str, history: list[dict]):
    yield ": open\n\n"
    try:
        for name, payload in chat.iter_chat(message, history, allow_call=_limiter.spend_model_call):
            yield _event(name, payload)
    except groq_client.GroqNotConfigured:
        yield _event("final", {"status": "not_configured"})
    except chat.BudgetExhausted:
        yield _event("error", {"status": "budget_used", "detail": BUDGET_USED})
    except groq_client.GroqError as error:
        log.warning("lab chat: upstream failure: %s", error)
        yield _event("error", {"status": "unavailable", "detail": UNAVAILABLE})


@router.post("/chat/stream")
async def lab_chat_stream(request: Request):
    """The same turn as Server-Sent Events, for hosts that do not buffer responses.

    Same request and same pre-flight errors as /chat. Events, in order:
      event: step    data: {kind, name, args, summary, elapsed_ms}   one per step, as it happens
      event: final   data: the /chat response                        always last on success
      event: error   data: {status, detail}                          instead of final if the model fails
    """
    if not groq_client.is_configured():
        events = iter([_event("final", {"status": "not_configured"})])
    else:
        events = _events(*await _chat_request(request))
    return StreamingResponse(events, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@router.post("/scenario")
async def lab_scenario(request: Request):
    """Run the simulator once, without the model.

    Request (docs/lab_contract.md, "The simulation is the product"; app/lab/scenario.py
    owns the final shape), every key optional and an empty object the baseline:
      {"fuel_shock_pct": 25, "fuel_shock_months": 6, "capacity_change_pct": 0, "demand_shock_pct": 0,
       "delivery_delay": {"aircraft": 0, "months": 0}, "horizon_months": 12, "seed": 1}
      "fuel_usd_gal" may be given instead of "fuel_shock_pct". Any other key is a 422:
      the simulator has no such input.
    Response: the simulator's result with the cleaned request added:
      {"status": "ok", "file": null, "request": {...}, "run_id": "...", "elapsed_ms": 0,
       "modes": {"costs_only": {...}, "management": {...}, "measured": {...}},
       "bridge_per_share": [...], "response_used": {...}, "assumptions": [...]}
    or {"status": "not_computed", "file": null, "request": {...}, "reason": "..."} until
    the simulator module or its measured response exists.
    """
    body = await _read(request)
    _admit(request)
    return _simulator_reply(await run_in_threadpool(chat.run_scenario, body))


def _simulator_reply(result: dict) -> dict:
    if result.get("status") == "invalid":
        raise HTTPException(status_code=422, detail=result.get("reason") or "Invalid request")
    if result.get("status") == "error":
        raise HTTPException(status_code=503, detail="The simulator failed on this request")
    return result


@router.post("/grid")
async def lab_grid(request: Request):
    """Run the simulator over a grid of two drivers against a threshold: "what breaks" and "what must be true".

    Request and response are app.lab.scenario.run_grid's (docs/lab_contract.md, POST /api/lab/grid):
      {"x": {"driver": "fuel_shock_pct", "values": [-20, 0, 20, 40]}, "y": {"driver": "demand_shock_pct", "values": [-6, -3, 0, 3]},
       "metric": "fcf_12m_musd", "threshold": 0, "direction": "below", "mode": "measured", "base": {"horizon_months": 12}}
      -> {"status": "ok", "file": null, "metric": ..., "cells": [{"x", "y", "prob", "p50"}], "boundary": [{"x", "y"}], "paths", "elapsed_ms"}
    {"status": "not_computed", "file": null, "reason": "..."} until run_grid exists; 422 with its reasons for a request it refuses.
    """
    body = await _read(request)
    _admit(request)
    return _simulator_reply(await run_in_threadpool(chat.run_grid, body))


@router.get("/today")
def lab_today():
    """The scenario request that matches the latest fuel data and what it rests on
    (app.lab.scenario.today_request), or {"status": "not_computed", ...} until that exists."""
    return _simulator_reply(chat.today_request())
