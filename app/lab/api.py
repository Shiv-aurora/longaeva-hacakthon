"""Lab API: status, the analyst chat (whole and streamed), ready insights and the simulator.

`app/server.py` includes `router`. These endpoints are public, so every request is
bounded in size, each client has a token bucket, model calls have a daily ceiling,
and nothing a user typed is ever written to a log. The chat needs no language model:
without a key, past the daily ceiling or while the model is rate-limited, the rules
planner in `chat` answers and says so.
"""
from __future__ import annotations
from datetime import datetime, timezone
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
RATE_BURST = 4               # chat turns a client may start at once
RATE_REFILL_SECONDS = 15.0   # one more turn every 15 s
# The simulator routes call no model and a run takes about half a second, so a page
# with sliders may use them freely: 60 scenario runs and 12 grids a minute per client.
SCENARIO_BURST, SCENARIO_REFILL_SECONDS = 60, 1.0
GRID_BURST, GRID_REFILL_SECONDS = 12, 5.0
# Groq's free plan gives each model 1,000 requests and 200,000 tokens a day; a turn
# takes 2 to 4 model calls. Past this the rules planner answers. Override with LAB_DAILY_MODEL_CALLS.
DAILY_MODEL_CALLS = 600
MAX_TRACKED_CLIENTS = 5_000
FAILED = "The turn could not be finished. Try again."
AGENT_NOTES = {"model": "A language model plans the work and words the report from facts that code fills in. "
                        "The rules planner takes over when the model is rate-limited.",
               "rules": "Running in rules mode: no language model is being called. The rules planner reads the common "
                        "question shapes, and every figure still comes from the simulator and the committed outputs."}


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


_limiter = RateLimiter()  # /chat and /chat/stream, and the day's model calls
_scenario_limiter = RateLimiter(SCENARIO_BURST, SCENARIO_REFILL_SECONDS, daily_model_calls=0)
_grid_limiter = RateLimiter(GRID_BURST, GRID_REFILL_SECONDS, daily_model_calls=0)


class ChatTurn(BaseModel):
    model_config = {"extra": "forbid"}
    role: Literal["user", "assistant"]
    content: str


class ChatPayload(BaseModel):
    model_config = {"extra": "forbid"}
    message: str
    history: list[ChatTurn] = Field(default_factory=list)
    model: str | None = None


def _client(request: Request) -> str:
    # Vercel writes x-forwarded-for itself. Anywhere else a client could forge it, so it is ignored.
    if os.environ.get("VERCEL") == "1":
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded[:64]
    return request.client.host if request.client else "unknown"


def _admit(request: Request, limiter: RateLimiter | None = None) -> None:
    wait = (limiter or _limiter).admit(_client(request))
    if wait:
        raise HTTPException(status_code=429, detail="Too many requests. Wait a few seconds and try again.",
                            headers={"Retry-After": str(math.ceil(wait))})


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


async def _chat_request(request: Request) -> tuple[str, list[dict], str | None]:
    """Validate, then charge the client's bucket. A turn costs one token however many steps it takes."""
    try:
        body = ChatPayload.model_validate(await _read(request))
    except ValueError:
        raise HTTPException(status_code=422, detail="Expected {message, history: [{role, content}], model}") from None
    choices = [chat.RULES, *groq_client.model_chain()]
    if body.model is not None and body.model not in choices:
        raise HTTPException(status_code=422, detail="model must be one of: " + ", ".join(choices))
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Message is empty")
    if len(message) > MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=422, detail=f"Message is too long (maximum {MAX_MESSAGE_CHARS} characters)")
    if len(body.history) > MAX_HISTORY_MESSAGES or any(len(turn.content) > MAX_HISTORY_CHARS for turn in body.history):
        raise HTTPException(status_code=422, detail=f"History is too long (maximum {MAX_HISTORY_MESSAGES} messages "
                                                    f"of {MAX_HISTORY_CHARS} characters)")
    _admit(request)
    return message, [turn.model_dump() for turn in body.history], body.model


def _agent() -> str:
    return "model" if chat.agent_mode() == "model" and _limiter.model_calls_left() else "rules"


@router.get("/status")
def lab_status():
    """What is connected. `agent` is "model" or "rules": in rules mode the chat still answers,
    through the rules planner, so there is no "not configured" state for the page to handle.

    `models` is what a turn may be sent with as `model`: [{id, label, note, available}], the
    models in the order a turn falls through them on a rate limit and then {"id": "rules"},
    the rules planner alone. A model is unavailable with no key or while it is rate-limited
    (`models_resting`: seconds until it is free); a turn sent with one answers by the rules.
    `commands` is the console's slash commands: [{name, description, template}], where
    `template` is the question put in the box, with __ where a figure goes.
    `suggested` is questions that work as they are."""
    configured = groq_client.is_configured()
    agent = _agent()
    return {"ai_configured": configured, "agent": agent, "agent_note": AGENT_NOTES[agent],
            "model": groq_client.default_model() if configured else None, "models": chat.model_options(),
            "models_resting": groq_client.cooling(), "commands": [dict(command) for command in chat.COMMANDS], "suggested": list(chat.CAN_ASK),
            "data_files": chat.data_files(), "simulator": chat.scenario_connected(), "simulator_parts": chat.simulator_parts(),
            "limits": {"max_message_chars": MAX_MESSAGE_CHARS, "max_history_messages": MAX_HISTORY_MESSAGES,
                       "max_history_chars": MAX_HISTORY_CHARS, "max_tool_calls": chat.MAX_TOOL_CALLS,
                       "model_calls_left_today": _limiter.model_calls_left()}}


@router.post("/chat")
async def lab_chat(request: Request):
    """One analyst turn as a single JSON object.

    Request:  {"message": "...", "history": [{"role": "user" | "assistant", "content": "..."}], "model": null}
      `model` is optional: an id from /status `models`, or "rules" for the rules planner
      alone. Anything else is a 422.
    Response: the result documented on `chat.iter_chat` ({status, answer, verified, note,
    planned_by, written_by, report, facts, sources, follow_ups, steps, charts, evidence_ids,
    scenarios, grids, usage, ...}). There is always an answer: with no API key, a
    rate-limited model or a spent daily ceiling the rules planner plans and writes it
    (planned_by "rules"). A turn takes one token from the client's bucket however many
    steps it runs; each model call counts against the daily ceiling.
    Errors: 413, 422 and 429 (with Retry-After).
    """
    message, history, model = await _chat_request(request)
    return await run_in_threadpool(chat.run_chat, message, history, model=model, allow_call=_limiter.spend_model_call)


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _events(message: str, history: list[dict], model: str | None = None):
    yield ": open\n\n"
    try:
        for name, payload in chat.iter_chat(message, history, model=model, allow_call=_limiter.spend_model_call):
            yield _event(name, payload)
    except Exception as error:  # the stream has started, so a failure has to arrive as an event
        # The type only: an exception's text can quote what the user typed.
        log.warning("lab chat: turn failed: %s", type(error).__name__)
        yield _event("error", {"status": "error", "detail": FAILED})


@router.post("/chat/stream")
async def lab_chat_stream(request: Request):
    """The same turn as Server-Sent Events, for hosts that do not buffer responses.

    Same request and same pre-flight errors as /chat. Events, in order:
      event: step    data: {id, kind, status, label, meta, details, name, args, summary, elapsed_ms}
                     One state of one step, as it happens: a step that takes time arrives as
                     "running" and again, with the same id, as "done" or "failed".
                     kind is plan | tool | write | check (see `chat.iter_chat`).
      event: final   data: the /chat response; its `steps` is each step's last state   always last on success
      event: error   data: {status, detail}                                            instead of final if the turn breaks
    """
    return StreamingResponse(_events(*await _chat_request(request)), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@router.get("/insights")
async def lab_insights():
    """Ready findings for the console's empty state, each written by code from a built file
    or from one simulator run at today's fuel price (`chat.insights`):
      {"status": "ok", "items": [{"id", "text", "chart": {"file", "kind"} | null, "question", "source": {id, name, kind, detail, href}}]}
    An item whose file is missing is left out; with none the status is "not_computed".
    Kept in memory for a few minutes, so it costs at most one simulator run now and then."""
    return await run_in_threadpool(chat.insights)


@router.post("/scenario")
async def lab_scenario(request: Request):
    """Run the simulator once, without the model.

    Request (docs/lab_contract.md, "The simulation is the product"; app/lab/scenario.py
    owns the final shape), every key optional and an empty object the baseline:
      {"fuel_shock_pct": 25, "fuel_shock_months": 6, "capacity_change_pct": 0, "demand_shock_pct": 0,
       "delivery_delay": {"aircraft": 0, "months": 0}, "horizon_months": 12, "seed": 1}
      "fuel_usd_gal" may be given instead of "fuel_shock_pct". Any other key is a 422:
      the simulator has no such input.
    Its own limit, apart from the chat's: 60 runs a minute per client.
    Response: the simulator's result with the cleaned request added:
      {"status": "ok", "file": null, "request": {...}, "run_id": "...", "elapsed_ms": 0,
       "modes": {"costs_only": {...}, "management": {...}, "measured": {...}},
       "bridge_per_share": [...], "response_used": {...}, "assumptions": [...]}
    or {"status": "not_computed", "file": null, "request": {...}, "reason": "..."} until
    the simulator module or its measured response exists.
    """
    body = await _read(request)
    _admit(request, _scenario_limiter)
    return _simulator_reply(await run_in_threadpool(chat.run_scenario, body))


def _simulator_reply(result: dict) -> dict:
    if result.get("status") == "invalid":
        # The agent's wrappers give one `reason`; the simulator's own results give an `errors` list.
        errors = result.get("errors") if isinstance(result.get("errors"), list) else []
        raise HTTPException(status_code=422, detail=result.get("reason") or "; ".join(str(item) for item in errors) or "Invalid request")
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
    {"status": "not_computed", "file": null, "reason": "..."} where run_grid is absent; 422 with its reasons for a request it refuses.
    Its own limit, apart from the chat's: 12 grids a minute per client.
    """
    body = await _read(request)
    _admit(request, _grid_limiter)
    return _simulator_reply(await run_in_threadpool(chat.run_grid, body))


@router.get("/today")
def lab_today():
    """The scenario request that matches the latest fuel data and what it rests on
    (app.lab.scenario.today_request): {"status": "ok", "file": null, "request": {...}, "basis": [...], "context": {...}},
    or {"status": "not_computed", ...} where that function is absent."""
    return _simulator_reply(chat.today_request())
