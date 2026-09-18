"""The chat agent — a real tool-use loop over backend/agent_tools.py's
toolset, for the dashboard's chat command bar and Q&A box.

Unlike backend/llm.py's ``narrate()`` (an optional one-shot narration
layer over data the code already computed, with a computed fallback if
the model is unavailable), this is the real thing: the model itself
decides what to look up and what to do, in a loop, using the same
capabilities a person would reach through the REST API — and every
action it takes goes through the exact same code path (TaskManager.
create_task, etc.), so it's bound by the same pre-execution eligibility
gate as anyone else. There is no computed fallback here — a live model
*is* the feature, so the chat endpoint just says plainly when it can't
run (no key configured) rather than pretending to answer.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from .agent_tools import TOOLS, execute_tool
from .llm import GROQ_MODEL, GROQ_URL, find_api_key
from .models import LogCategory

MAX_TOOL_ITERATIONS = 10
CHAT_MAX_TOKENS = 400
# A tool-use loop (possibly several round trips to Groq) needs more
# headroom than backend/llm.py's single one-shot narration call.
CHAT_TIMEOUT_SECONDS = 20

# Groq's free tier is a tight 8000 tokens/minute shared across whatever
# else is using the same key — a 429 there is routine, not exceptional,
# especially mid-conversation once history has built up. Rather than
# fail the whole turn, wait out the exact backoff Groq itself reports
# (capped, so a chat reply is never stuck for too long) and try once
# more before giving up.
RATE_LIMIT_MAX_RETRIES = 2
RATE_LIMIT_MAX_WAIT_SECONDS = 20
RATE_LIMIT_DEFAULT_WAIT_SECONDS = 5
_RETRY_AFTER_RE = re.compile(r"try again in\s+([\d.]+)\s*s", re.IGNORECASE)

SYSTEM_PROMPT = (
    "You are the operations AI for a warehouse digital twin. You have real "
    "tools to create/cancel tasks and to look up robots, boxes, agents, "
    "operators, statistics, and this project's own graded task history. "
    "Use tools whenever you need current information or need to actually "
    "do something — never guess at ids, names, statuses, or task outcomes. "
    "A request naming multiple steps ('do X then Y') should result in "
    "multiple tool calls, in order. After acting, always tell the user "
    "plainly what you did or found — task ids, verdicts, names — in a few "
    "short sentences or a brief bullet list. Never fabricate a task id, "
    "verdict, or any other fact a tool didn't actually return."
)


class ChatError(Exception):
    """Raised when the chat agent genuinely can't produce an answer — no
    key configured, an empty message, or Groq itself failing outright.
    The caller (the /api/agent/chat endpoint) turns this into a clean
    4xx/5xx, same as any other domain error in this app. Note this is
    NOT raised just because the tool-use loop ran out of steps or the
    model's final message came back empty — see _fallback_summary(),
    both of those degrade to a real (if less polished) reply instead,
    since real actions may already have been taken by that point."""


def _rate_limit_wait_seconds(exc: "urllib.error.HTTPError", detail: str) -> float:
    """How long Groq itself says to wait — its Retry-After header if
    present, else the "try again in N.NNs" it puts in the error message
    body — falling back to a fixed default if neither is parseable."""
    retry_after = None
    try:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
    except Exception:  # pragma: no cover - headers should always be a Message, but never trust a 3rd party
        retry_after = None
    if retry_after:
        try:
            return min(RATE_LIMIT_MAX_WAIT_SECONDS, max(0.0, float(retry_after)))
        except ValueError:
            pass
    match = _RETRY_AFTER_RE.search(detail)
    if match:
        try:
            return min(RATE_LIMIT_MAX_WAIT_SECONDS, max(0.0, float(match.group(1))))
        except ValueError:
            pass
    return RATE_LIMIT_DEFAULT_WAIT_SECONDS


def _fallback_summary(actions: List[Dict[str, Any]]) -> str:
    """A plain summary built directly from what the tools actually
    returned — used only when the model's own final message comes back
    with no tool calls AND no text. That does happen: a smaller/faster
    model under a long run of tool results (a many-step command) can
    end its turn with genuinely empty content rather than the summary
    the system prompt asks for. Never shown when the model *did* write
    something, and never fabricates anything beyond what's already in
    `actions` — every line traces back to a real tool result."""
    if not actions:
        return "I didn't do anything — try rephrasing the request."
    lines = []
    for action in actions:
        result = action.get("result") or {}
        if result.get("error"):
            lines.append(f"✗ {action.get('tool', '?')}: {result['error']}")
        elif result.get("task_id"):
            lines.append(
                f"✓ {result['task_id']} ({result.get('type', action.get('tool', '?'))}) "
                f"— {result.get('status', 'done')}"
            )
        else:
            lines.append(f"✓ {action.get('tool', '?')} completed")
    return "\n".join(lines)


def _post(messages: List[Dict[str, Any]], api_key: str) -> Dict[str, Any]:
    body = json.dumps({
        "model": GROQ_MODEL,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0.2,
        "max_tokens": CHAT_MAX_TOKENS,
    }).encode("utf-8")
    request = urllib.request.Request(
        GROQ_URL, data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            # See the matching comment in backend/llm.py — Groq's API is
            # behind Cloudflare, which blocks urllib's default User-Agent.
            "User-Agent": "Mozilla/5.0 (compatible; warehouse-digital-twin/1.0)",
        },
        method="POST",
    )
    for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=CHAT_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code == 429 and attempt < RATE_LIMIT_MAX_RETRIES:
                time.sleep(_rate_limit_wait_seconds(exc, detail))
                continue
            raise ChatError(f"Groq returned {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ChatError(f"Could not reach Groq: {exc}") from exc
        except (ValueError, KeyError) as exc:  # malformed JSON back from Groq
            raise ChatError(f"Unexpected response from Groq: {exc}") from exc
    raise ChatError("Groq rate-limited every retry attempt.")  # pragma: no cover - every branch above returns/raises first


def run_chat(
    twin: Any,
    message: str,
    history: Optional[List[Dict[str, str]]] = None,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Run one turn of the chat agent's tool-use loop.

    `history` is a list of {"role": "user"|"assistant", "content": str}
    from earlier turns of the same conversation (only the final
    user-visible exchange of each turn — tool calls themselves aren't
    replayed). Returns {"reply", "actions", "iterations"}. Raises
    ChatError if the feature isn't usable right now (no key) or Groq
    itself fails — the caller is expected to surface that as an error,
    not a degraded reply, since there's no computed fallback for "no AI".

    `on_progress`, if given, is called once per meaningful step —
    {"phase": "thinking", "iteration": N} before each call to Groq, and
    {"phase": "action", "tool", "args", "result"} right after each tool
    call completes — so a caller (the /api/agent/chat endpoint, over SSE)
    can show live progress on a long, many-step command instead of one
    static "thinking…" the whole time. Best-effort only: any exception
    it raises is swallowed, since a broken progress callback must never
    break the chat turn it's just narrating.
    """
    def _notify(event: Dict[str, Any]) -> None:
        if on_progress is None:
            return
        try:
            on_progress(event)
        except Exception:  # pragma: no cover - progress is cosmetic, never load-bearing
            pass

    api_key = find_api_key()
    if not api_key:
        raise ChatError(
            "No Groq API key configured — set GROQ_API_KEY or add it to evals/.env to use the chat agent."
        )
    if not message or not message.strip():
        raise ChatError("Say something first.")

    messages: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in (history or [])[-8:]:  # a handful of prior turns is plenty of context
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": str(turn["content"])})
    messages.append({"role": "user", "content": message.strip()})

    actions: List[Dict[str, Any]] = []
    for iteration in range(MAX_TOOL_ITERATIONS):
        _notify({"phase": "thinking", "iteration": iteration + 1})
        payload = _post(messages, api_key)
        try:
            choice_message = payload["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise ChatError(f"Unexpected response from Groq: {exc}") from exc

        tool_calls = choice_message.get("tool_calls")
        if not tool_calls:
            reply = (choice_message.get("content") or "").strip() or _fallback_summary(actions)
            twin.logger.info(
                LogCategory.TASK,
                f'[agent-chat] "{message.strip()[:80]}" → {len(actions)} action(s), {iteration + 1} step(s)',
            )
            return {"reply": reply, "actions": actions, "iterations": iteration + 1}

        messages.append(choice_message)
        for call in tool_calls:
            name = (call.get("function") or {}).get("name", "")
            raw_args = (call.get("function") or {}).get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if raw_args else {}
            except (TypeError, ValueError):
                args = {}
            if isinstance(args, dict):
                # Some models emit a dummy {"": ""} for a tool with no
                # real parameters (e.g. get_fleet_load) — harmless to the
                # handler either way, but keep the recorded action clean.
                args = {k: v for k, v in args.items() if k}
            else:
                args = {}
            result = execute_tool(twin, name, args)
            actions.append({"tool": name, "args": args, "result": result})
            _notify({"phase": "action", "tool": name, "args": args, "result": result})
            messages.append({
                "role": "tool", "tool_call_id": call.get("id", ""), "content": json.dumps(result, default=str),
            })

    # Ran out of steps without the model ever stopping on its own — a
    # very long command (many tasks in one message) can do this. Whatever
    # it actually did up to this point is real and already happened, so
    # report that plainly rather than discarding it behind a hard error.
    twin.logger.info(
        LogCategory.TASK,
        f'[agent-chat] "{message.strip()[:80]}" → hit the {MAX_TOOL_ITERATIONS}-step limit, {len(actions)} action(s) taken',
    )
    reply = _fallback_summary(actions) + f"\n\n(stopped after {MAX_TOOL_ITERATIONS} steps — try fewer things per message)"
    return {"reply": reply, "actions": actions, "iterations": MAX_TOOL_ITERATIONS}
