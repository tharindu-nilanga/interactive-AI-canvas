#!/usr/bin/env python3
"""
CANVAS proxy — keeps the Roar API key server-side and hardens the model's JSON.

    python3 server.py            # http://localhost:8000

Zero dependencies (stdlib only), so nothing to install at the venue.

Config comes from environment variables, or a .env file next to this script:

    ROAR_ENDPOINT   default https://api.roar-ai.com/v1/chat/completions
    ROAR_API_KEY    required for live mode; without it the server serves a
                    keyword-driven mock so the demo still works offline
    ROAR_MODEL      default gpt-4o-mini
    ROAR_TIMEOUT    seconds, default 45
    ROAR_TEMPERATURE default 0.4
    PORT            default 8000
    MOCK            1 = force mock mode even with a key

Endpoints:

    GET  /              index.html (+ static assets)
    GET  /api/health    { ok, mode: "live" | "mock", endpoint, model }
    POST /api/chat      { command, board: [...] } -> { reply, actions: [...] }
"""

import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest
from urllib import error as urlerror
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
MAX_BODY = 1 << 20          # 1 MB
MAX_ACTIONS = 8
WORLD_SIZE = 4000
DEFAULT_WIDTH = 268


def load_env(path):
    """Minimal .env reader: KEY=value, # comments, optional quotes."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(os.path.join(ROOT, ".env"))

# Hosted on Roar AI, the app gets ROAR_API_KEY and ROAR_BASE_URL (already ends in /v1)
# injected — no key to add. Locally, .env supplies both.
BASE_URL = os.environ.get("ROAR_BASE_URL", "").rstrip("/")
ENDPOINT = os.environ.get("ROAR_ENDPOINT") or (
    f"{BASE_URL}/chat/completions" if BASE_URL else "https://api.roar-ai.com/v1/chat/completions"
)
API_KEY = os.environ.get("ROAR_API_KEY", "").strip()
MODEL = os.environ.get("ROAR_MODEL", "gpt-4o-mini").strip()
TIMEOUT = float(os.environ.get("ROAR_TIMEOUT", "45"))
TEMPERATURE = float(os.environ.get("ROAR_TEMPERATURE", "0.4"))
FORCE_MOCK = os.environ.get("MOCK", "").strip() in {"1", "true", "yes"}
PORT = int(os.environ.get("PORT", "8000"))
# a hosted app must accept traffic from the platform's proxy, not just localhost
HOST = os.environ.get("HOST") or ("0.0.0.0" if BASE_URL or os.environ.get("APP_URL") else "127.0.0.1")
LIVE = bool(API_KEY and ENDPOINT) and not FORCE_MOCK


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
# One entry per kind the board can draw: (what it is, example payload, when to use it).
# The client sends the list of kinds it can actually render, so this guide is never
# advertising a widget the page cannot draw.
KIND_DOCS = {
    "graph": (
        "plots one function of x onto a canvas",
        '{"equation": "x^2 - 4*x + 3"}',
        "a shape, a slope, roots or an intersection. One curve per card.",
    ),
    "quiz": (
        "an answerable question, graded in the browser",
        '{"question": "...", "answer": "...", "choices": ["...", "..."]}',
        "checking understanding right after teaching something. `answer` must be short and "
        "exactly matchable (\"3x^2\", \"x = 1 and x = 3\", \"even\"); add `choices` when the "
        "answer is one of a few options.",
    ),
    "note": (
        "one short takeaway",
        '{"text": "..."}',
        "a single sentence or two (under ~40 words) that must be visible at a glance.",
    ),
    "card": (
        "free-form markdown: headings, lists, tables, code, quotes",
        '{"body": "## Heading\\n\\n- point\\n- point\\n\\n| a | b |\\n|---|---|\\n| 1 | 2 |"}',
        "explanations, worked examples, summaries — anything longer than a note. This is "
        "the richest kind; use it whenever a widget above does not fit.",
    ),
    "steps": (
        "a numbered derivation",
        '{"steps": ["start from ...", "then ...", "so ..."]}',
        "derivations the student follows line by line (3-6 steps, one operation each).",
    ),
    "table": (
        "a comparison with headers and rows",
        '{"headers": ["f", "parity", "period"], "rows": [["sin", "odd", "2pi"]]}',
        "comparing two or more things across two or more attributes.",
    ),
    "checklist": (
        "tickable items",
        '{"items": ["revise ...", "practise ..."]}',
        "a revision plan, a procedure to follow, or to-dos.",
    ),
    "formula": (
        "one highlighted expression",
        '{"expression": "x = (-b sqrt(b^2-4ac)) / 2a", "note": "quadratic formula"}',
        "pinning a single formula the student must remember.",
    ),
    "grid": (
        "a 2D matrix of cells (sudoku, truth table, matrix, pixel pattern)",
        '{"rows": ["53.7.....", "6..195..."], "note": "1-9 once per row, column and box"}',
        "anything genuinely two-dimensional. Rows may be equal-length strings or arrays.",
    ),
}

PROMPT_TEMPLATE = """You are the AI co-pilot of CANVAS, a spatial study board used by one student.
You answer by placing interactive blocks on a pannable 2D board.

Reply with ONE JSON object and nothing else. No prose, no markdown fence around it.

{
  "reply": "one or two conversational sentences for the sidebar",
  "actions": [
    {
      "kind": "__KINDS__",
      "title": "short header, 2-5 words, no kind name",
      "place": "right" | "below" | "new-row",
      "data": {}
    },
    { "op": "delete", "id": "el_123" }
  ]
}

`actions` may mix new blocks and operations on existing ones — see CHANGING WHAT IS ALREADY ON
THE BOARD below. A block that only describes a change is a bug: if the student says "remove X",
emit the delete op, do not make a card saying X was removed.

LAYOUT is automatic: the board packs your blocks into equal-width columns with even gaps,
starting at the top-left of the student's current view, in the order you list them. Do NOT
compute pixel coordinates. "place" is an optional hint relative to the previous block: "below"
stacks a quiz under its graph, "right" sits beside it, "new-row" starts a fresh line.

CHANGING WHAT IS ALREADY ON THE BOARD
An action may also operate on existing blocks. Target them with the "id" from current_blocks
(a "title" fallback works for delete and move):
  {"op": "delete", "id": "el_123"}                                   remove one block
  {"op": "update", "id": "el_123", "title": "new", "data": {...}}     rewrite a block in place
  {"op": "move",   "id": "el_123", "x": 120, "y": 80}                 reposition it
  {"op": "clear"}                                                    remove every block
  {"op": "spawn", "kind": "...", "title": "...", "data": {}}          same as omitting "op"
Rules: never delete, clear or move something the student did not ask to change. Prefer "update"
over delete+spawn when correcting or extending an existing block. If they got a quiz wrong, do not
erase the attempt — update the quiz to something easier, or spawn a card/steps block that explains
the step they missed.

WHAT THE STUDENT JUST DID
The request carries recent_events: quiz_wrong / quiz_correct / quiz_revealed / checklist_toggle /
block_closed, with block ids and the answers they gave. Use them when the student says "this",
"that one", "what did I miss", or asks for a hint. Never read them back as a system log.

KINDS YOU CAN EMIT
__KIND_GUIDE__
- any other concise snake_case kind: it renders as a list (data.items / data.steps) or a
  key/value card, so put structured fields in data rather than one prose blob. Inventing a
  kind is allowed, but a registered kind almost always looks better.

HOW TO COMPOSE
- A teaching answer is a cluster, not a pile: usually 1 explanation (card or note) + 1 concrete
  artefact (graph, table, grid, formula) + 1 quiz. Two or three blocks beat four.
- Vary the kinds. Four notes in a row is a bad answer; a graph, a formula and a quiz is a good one.
- If the student asks for a game, puzzle or interactive thing, use the kind that actually renders
  it (grid for a sudoku, checklist for steps, quiz for a guess-and-check) instead of describing it.
- Put the main artefact first, supporting text after it.
- Never repeat a block that is already on the board with the same content.
- When the student asks a pure question with nothing to show, use "actions": [].

CONTENT
- reply: 1-2 sentences, plain text (it may be spoken aloud), no markdown, no lists.
- Be concrete: real numbers, real steps, real equations — not "you could plot the function".
- In graph equations use * between terms (4*x, not 4x), ^ for powers, and x as the only variable.
- Keep each block self-contained: a student should understand it without the reply.
- If the board already holds related blocks, build on them instead of restarting the topic.
"""


def build_system_prompt(kinds=None):
    """Compose the prompt from the kinds this client can actually render."""
    requested = {k for k in (kinds or []) if isinstance(k, str)}
    available = [k for k in KIND_DOCS if not requested or k in requested]
    if not available:
        available = list(KIND_DOCS)

    guide = "\n".join(
        f'- {kind}: {KIND_DOCS[kind][1]}\n    {KIND_DOCS[kind][0]}. Use for: {KIND_DOCS[kind][2]}'
        for kind in available
    )
    return (
        PROMPT_TEMPLATE.replace("__KIND_GUIDE__", guide)
        .replace("__KINDS__", " | ".join(available + ["<snake_case_kind>"]))
    )


SYSTEM_PROMPT = build_system_prompt()


# --------------------------------------------------------------------------- #
# JSON hardening
# --------------------------------------------------------------------------- #

KIND_RE = re.compile(r"^[a-z][a-z0-9_\-]{0,23}$")


def strip_fences(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def first_json_object(text):
    """Return the first balanced {...} span, ignoring braces inside strings."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None


def loads_tolerant(raw):
    """json.loads, then a trailing-comma / smart-quote repair pass."""
    attempts = [raw]
    repaired = re.sub(r",\s*([}\]])", r"\1", raw)
    repaired = repaired.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")
    attempts.append(repaired)
    for candidate in attempts:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def parse_model_json(content):
    """Model text -> dict, or None. Handles fences, prose wrappers, truncation."""
    if isinstance(content, dict):
        return content
    text = strip_fences(content)
    if not text:
        return None
    span = first_json_object(text)
    if span:
        parsed = loads_tolerant(span)
        if isinstance(parsed, dict):
            return parsed
    parsed = loads_tolerant(text)
    if isinstance(parsed, dict):
        return parsed
    return None


def s(value, limit):
    if value is None:
        return ""
    if not isinstance(value, str):
        if isinstance(value, (int, float, bool)):
            value = str(value)
        elif isinstance(value, list):
            value = "\n".join(str(v) for v in value)
        else:
            value = json.dumps(value, ensure_ascii=False)
    return value.strip()[:limit]


def clamp_int(value, low, high):
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return max(low, min(high, number))


def sanitize_payload(payload):
    """Never trust the model: coerce types, drop unusable actions, cap sizes."""
    if not isinstance(payload, dict):
        raise ValueError("model payload is not an object")

    if "actions" not in payload and isinstance(payload.get("action"), dict):
        payload["actions"] = [payload["action"]]

    reply = s(payload.get("reply") or payload.get("message") or "", 400)
    actions = payload.get("actions")
    if not isinstance(actions, list):
        actions = []

    clean = []
    for raw in actions[:MAX_ACTIONS]:
        if not isinstance(raw, dict):
            continue

        # panel mutations: {op: delete|clear|update|move, id|title, ...}
        op = s(raw.get("op"), 12).lower()
        if op in {"delete", "remove", "clear", "update", "move"}:
            op = "delete" if op == "remove" else op
            item: dict = {"op": op}
            ident = s(raw.get("id"), 48)
            if ident:
                item["id"] = ident

            if op in {"delete", "move"}:
                # a title is a fallback way to point at a block
                if not ident:
                    title = s(raw.get("title"), 60)
                    if not title:
                        continue
                    item["title"] = title
            elif op == "update":
                if not ident:
                    continue  # updating without an id is ambiguous
                data = raw.get("data")
                if isinstance(data, dict):
                    item["data"] = {
                        str(k)[:32]: s(v, 800) if isinstance(v, str) else v
                        for k, v in list(data.items())[:12]
                    }
                kind = s(raw.get("kind"), 24).lower()
                if KIND_RE.match(kind):
                    item["kind"] = kind
                title = s(raw.get("title"), 60)
                if title:
                    item["title"] = title
                for key in ("w", "h"):
                    size = clamp_int(raw.get(key), 140, 760)
                    if size is not None:
                        item[key] = size
                if len(item) == 1:
                    continue  # nothing to change

            if op == "move":
                for axis in ("x", "y"):
                    pos = clamp_int(raw.get(axis), 0, WORLD_SIZE)
                    if pos is not None:
                        item[axis] = pos
                if not ({item.get("x"), item.get("y")} - {None}):
                    continue

            clean.append(item)
            continue

        kind = s(raw.get("kind") or raw.get("type") or "note", 24).lower()
        kind = re.sub(r"^add_", "", kind).replace(" ", "_")
        kind = re.sub(r"[^a-z0-9_\-]", "", kind)  # "QUIZ!!" -> "quiz"
        if not KIND_RE.match(kind):
            kind = "note"

        data = raw.get("data")
        if not isinstance(data, dict):
            data = {}
        if not data:
            data = {
                k: v
                for k, v in raw.items()
                if k not in {"kind", "type", "title", "x", "y", "w", "id", "data"}
            }

        if kind == "graph":
            equation = s(
                data.get("equation")
                or data.get("expression")
                or data.get("formula")
                or raw.get("equation"),
                200,
            )
            if not equation:
                continue
            data = {"equation": equation}
            title_default = equation
        elif kind == "quiz":
            question = s(data.get("question") or data.get("prompt"), 400)
            if not question:
                continue
            data = {"question": question, "answer": s(data.get("answer"), 800)}
            title_default = "QUIZ"
        elif kind == "note":
            text = s(data.get("text") or data.get("body") or data.get("content"), 2000)
            if not text:
                continue
            data = {"text": text}
            title_default = "NOTE"
        else:
            trimmed = {}
            for key, value in list(data.items())[:12]:
                trimmed[str(key)[:32]] = s(value, 600) if isinstance(value, str) else value
            data = trimmed
            title_default = kind.upper()

        item = {
            "kind": kind,
            "title": s(raw.get("title") or title_default, 60) or kind.upper(),
            "data": data,
        }

        # layout hints: the client arranges blocks, so pass structure through
        place = s(raw.get("place"), 12).lower()
        if place in {"right", "below", "new-row", "left", "auto"}:
            item["place"] = place
        col = clamp_int(raw.get("col"), 0, 3)
        if col is not None:
            item["col"] = col

        for axis in ("x", "y"):
            pos = clamp_int(raw.get(axis), 0, WORLD_SIZE)
            if pos is not None:
                item[axis] = pos
        width = clamp_int(raw.get("w"), 180, 520)
        if width is not None:
            item["w"] = width
        clean.append(item)

    return {"reply": reply, "actions": clean}


# --------------------------------------------------------------------------- #
# Upstream
# --------------------------------------------------------------------------- #

def board_summary(board):
    """Trim client board state to what the model needs, dropping junk."""
    out = []
    if not isinstance(board, list):
        return out
    for element in board[:24]:
        if not isinstance(element, dict):
            continue
        entry = {
            "kind": s(element.get("kind"), 24),
            "title": s(element.get("title"), 60),
            "x": clamp_int(element.get("x"), 0, WORLD_SIZE) or 0,
            "y": clamp_int(element.get("y"), 0, WORLD_SIZE) or 0,
        }
        data = element.get("data")
        if isinstance(data, dict):
            entry["data"] = {
                k: s(v, 300) for k, v in list(data.items())[:6] if isinstance(v, (str, int, float))
            }
        out.append(entry)
    return out


def viewport_summary(viewport):
    if not isinstance(viewport, dict):
        return "unknown (assume the top-left corner is visible)"
    parts = []
    for key in ("x", "y", "w", "h"):
        value = clamp_int(viewport.get(key), 0, WORLD_SIZE * 2)
        parts.append(f"{key}={value if value is not None else '?'}")
    return " ".join(parts)


def sanitize_events(events):
    """What the student just did on the board, trimmed to the fields that matter."""
    out = []
    if not isinstance(events, list):
        return out
    for event in events[-12:]:
        if not isinstance(event, dict):
            continue
        entry: dict = {"type": s(event.get("type"), 24)}
        for key in ("id", "title", "item", "question", "given", "expected"):
            if key in event:
                entry[key] = s(event.get(key), 120)
        if "correct" in event:
            entry["correct"] = bool(event.get("correct"))
        if "checked" in event:
            entry["checked"] = bool(event.get("checked"))
        out.append(entry)
    return out


def describe_events(events):
    """Plain-language activity lines placed above the JSON payload.

    Nested JSON gets skipped by models; a short sentence does not. This is the
    difference between "I can't see what you missed" and an actual follow-up.
    """
    clean = sanitize_events(events)
    if not clean:
        return ""
    lines = []
    for ev in clean:
        kind = ev.get("type", "")
        block = ev.get("id") or ev.get("title") or "a block"
        if kind in {"quiz_wrong", "quiz_correct"}:
            verdict = "correct" if ev.get("correct") else "wrong"
            lines.append(
                f'- quiz {verdict}: the student answered "{ev.get("given", "?")}" to '
                f'"{ev.get("question", "?")}" on block {block} (expected "{ev.get("expected", "?")}")'
            )
        elif kind == "quiz_revealed":
            lines.append(f'- they pressed reveal on block {block} ("{ev.get("question", "?")}")')
        elif kind == "checklist_toggle":
            state = "ticked" if ev.get("checked") else "un-ticked"
            lines.append(f'- they {state} "{ev.get("item", "?")}" on block {block}')
        elif kind == "block_closed":
            lines.append(f'- they closed the block "{ev.get("title", "?")}"')
        else:
            lines.append(f"- {kind} on {block}")
    return "What the student just did on the board (most recent last):\n" + "\n".join(lines)


def build_user_message(command, board, viewport, events=None):
    payload = json.dumps(
        {
            "command": s(command, 1500),
            "viewport": viewport_summary(viewport),
            "current_blocks": board_summary(board),
        },
        ensure_ascii=False,
    )
    activity = describe_events(events)
    return f"{activity}\n\n{payload}" if activity else payload


def call_upstream(messages):
    body = {
        "model": MODEL,
        "temperature": TEMPERATURE,
        "messages": messages,
    }
    req = urlrequest.Request(
        ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"upstream {exc.code}: {detail}") from exc
    except urlerror.URLError as exc:
        raise RuntimeError(f"upstream unreachable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("upstream timed out") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"upstream returned non-JSON: {raw[:200]}") from exc

    return extract_content(data)


def extract_content(data):
    """Accept OpenAI chat shape, /completions shape, or a bare {reply, actions}."""
    if isinstance(data, dict) and ("reply" in data or "actions" in data):
        return data

    choices = data.get("choices") if isinstance(data, dict) else None
    if isinstance(choices, list) and choices:
        first = choices[0] or {}
        message = first.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # multimodal parts
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(first.get("text"), str):
            return first["text"]

    for key in ("content", "text", "output", "answer"):
        if isinstance(data, dict) and isinstance(data.get(key), str) and data[key].strip():
            return data[key]

    raise RuntimeError("upstream response had no message content")


LAST_REQUEST: dict = {}


def ask_model(command, board, viewport, kinds=None, events=None):
    """One call; if the JSON is unusable, one strict repair call. Returns sanitized dict."""
    messages = [
        {"role": "system", "content": build_system_prompt(kinds)},
        {"role": "user", "content": build_user_message(command, board, viewport, events)},
    ]
    LAST_REQUEST.clear()
    LAST_REQUEST.update({"system": messages[0]["content"], "user": messages[1]["content"]})
    content = call_upstream(messages)
    payload = parse_model_json(content)

    if payload is None:
        messages += [
            {"role": "assistant", "content": s(content, 4000)},
            {
                "role": "user",
                "content": (
                    "That was not valid JSON. Convert it into the required object now. "
                    "Reply with JSON only: no prose, no code fences."
                ),
            },
        ]
        content = call_upstream(messages)
        payload = parse_model_json(content)
        if payload is None:
            raise RuntimeError("model did not return usable JSON after one repair attempt")

    return sanitize_payload(payload)


# --------------------------------------------------------------------------- #
# Mock (offline demo fallback, keyword driven)
# --------------------------------------------------------------------------- #

def mock_payload(command, board):
    text = (command or "").lower()
    reply = f'Mock co-pilot: understood "{s(command, 80)}".'
    actions = []

    # the mock can clear the board too, so the op path is demoable offline
    if re.search(r"\b(clear|wipe|empty|reset)\b.*\b(board|blocks?|canvas|everything)\b", text):
        return {
            "reply": "Mock mode: clearing the board.",
            "actions": [{"op": "clear"}],
            "mock": True,
        }

    equation = None
    fn_match = re.search(
        r"(sin|cos|tan|asin|acos|atan|sinh|cosh|tanh|sqrt|cbrt|abs|exp|ln|log10|log|floor|ceil|round|sign)"
        r"\s*\(\s*x\s*\)",
        command or "",
        re.I,
    )
    poly_match = re.search(r"x\s*\^\s*\d(\s*[-+]\s*\d*\s*\*?\s*x?)*", command or "", re.I)
    if fn_match:
        equation = re.sub(r"\s+", "", fn_match.group(0))
    elif poly_match:
        equation = re.sub(r"\s+", "", poly_match.group(0))

    if "=" in (command or ""):
        rhs = command.split("=", 1)[1]
        if re.search(r"\bx\b", rhs):
            equation = rhs.strip().rstrip(".,;").replace(" ", "")
    if equation is None and any(w in text for w in ("graph", "plot", "curve", "parabola", "sine", "function")):
        equation = "x^2 - 4*x + 3"

    if equation:
        actions.append(
            {
                "kind": "graph",
                "title": "PLOT",
                "data": {"equation": equation},
            }
        )

    if any(w in text for w in ("quiz", "test", "question", "practice", "check")):
        actions.append(
            {
                "kind": "quiz",
                "title": "ROOT CHECK",
                "data": {
                    "question": "Find the roots of x^2 - 4x + 3 = 0.",
                    "answer": "x = 1 and x = 3",
                },
            }
        )

    if any(w in text for w in ("step", "derive", "solve", "how")):
        actions.append(
            {
                "kind": "steps",
                "title": "WORKING",
                "data": {
                    "steps": [
                        "Start from the quadratic form ax^2 + bx + c = 0.",
                        "Here a=1, b=-4, c=3.",
                        "Factor as (x-1)(x-3) = 0.",
                        "So x = 1 or x = 3.",
                    ]
                },
            }
        )

    if not actions:
        actions.append(
            {
                "kind": "note",
                "title": "OFFLINE",
                "data": {
                    "text": (
                        "Proxy is in mock mode. Set ROAR_API_KEY in .env to get real answers.\n"
                        f"Your command was: {s(command, 200)}"
                    )
                },
            }
        )
        reply = "Mock mode: no API key configured on the server."

    return {"reply": reply, "actions": actions, "mock": True}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

# Only front-end assets are served. Anything else in the folder (server.py, .env,
# .git, .pyc, logs) must never be reachable over HTTP.
SERVABLE = {".html", ".js", ".css", ".json", ".svg", ".png", ".ico", ".woff2", ".txt"}


class Handler(BaseHTTPRequestHandler):
    server_version = "CanvasProxy/1.0"

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        sys.stderr.write("[canvas] %s\n" % (format % args))

    # -- helpers -----------------------------------------------------------
    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/api/health":
            self.send_json(
                200,
                {
                    "ok": True,
                    "mode": "live" if LIVE else "mock",
                    "endpoint": ENDPOINT,
                    "model": MODEL,
                    "kinds": list(KIND_DOCS),
                },
            )
            return

        # inspect exactly what the model is told (no key, no secrets in here)
        if path == "/api/prompt":
            self.send_json(200, {"prompt": build_system_prompt()})
            return

        # what the last request actually sent (system + user), for debugging
        if path == "/api/last":
            self.send_json(200, LAST_REQUEST or {"error": "no request yet"})
            return

        if path == "/":
            path = "/index.html"

        target = os.path.realpath(os.path.join(ROOT, path.lstrip("/")))
        ext = os.path.splitext(target)[1].lower()
        hidden = any(part.startswith(".") for part in target[len(ROOT):].split(os.sep) if part)
        if (
            not target.startswith(os.path.realpath(ROOT))
            or not os.path.isfile(target)
            or hidden
            or ext not in SERVABLE
        ):
            self.send_json(404, {"error": "not found"})
            return

        with open(target, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/chat":
            self.send_json(404, {"error": "not found"})
            return

        started = time.time()
        try:
            body = self.read_body()
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return

        command = s(body.get("command") or body.get("prompt"), 1500)
        if not command:
            self.send_json(400, {"error": "command is required"})
            return
        board = body.get("board")
        viewport = body.get("viewport")
        capabilities = body.get("capabilities") if isinstance(body.get("capabilities"), dict) else {}
        kinds = capabilities.get("kinds") if isinstance(capabilities.get("kinds"), list) else None
        events = body.get("recent_events")

        if not LIVE:
            payload = mock_payload(command, board)
            payload["latency_ms"] = int((time.time() - started) * 1000)
            self.send_json(200, payload)
            return

        try:
            payload = ask_model(command, board, viewport, kinds, events)
        except Exception as exc:  # noqa: BLE001 - surface everything to the client
            sys.stderr.write(f"[canvas] model error: {exc}\n")
            self.send_json(502, {"error": f"{exc}"})
            return

        payload["latency_ms"] = int((time.time() - started) * 1000)
        sys.stderr.write(
            "[canvas] chat: %d action(s), %d event(s), %dms\n"
            % (len(payload.get("actions", [])), len(sanitize_events(events)), payload["latency_ms"])
        )
        self.send_json(200, payload)


def main():
    mode = "LIVE" if LIVE else "MOCK"
    print(f"[canvas] mode={mode} model={MODEL}", flush=True)
    print(f"[canvas] upstream={ENDPOINT}", flush=True)
    if not LIVE:
        print("[canvas] no ROAR_API_KEY -> keyword mock. Put the key in .env to go live.", flush=True)
    print(f"[canvas] listening on {HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[canvas] stopped")
