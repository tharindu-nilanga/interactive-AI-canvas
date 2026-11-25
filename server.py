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

ENDPOINT = os.environ.get("ROAR_ENDPOINT", "https://api.roar-ai.com/v1/chat/completions")
API_KEY = os.environ.get("ROAR_API_KEY", "").strip()
MODEL = os.environ.get("ROAR_MODEL", "gpt-4o-mini").strip()
TIMEOUT = float(os.environ.get("ROAR_TIMEOUT", "45"))
TEMPERATURE = float(os.environ.get("ROAR_TEMPERATURE", "0.4"))
FORCE_MOCK = os.environ.get("MOCK", "").strip() in {"1", "true", "yes"}
PORT = int(os.environ.get("PORT", "8000"))
LIVE = bool(API_KEY and ENDPOINT) and not FORCE_MOCK


SYSTEM_PROMPT = """You are the AI co-pilot of CANVAS, a spatial study board used by a student.
The student sees a pannable 2D world (0..4000 on both axes) containing interactive blocks.

Reply with ONE JSON object and nothing else. No prose, no markdown, no code fences.

{
  "reply": "one or two conversational sentences to show in the sidebar",
  "actions": [
    {
      "kind": "graph" | "quiz" | "note" | "<custom_snake_case_kind>",
      "title": "short block header, max 6 words",
      "x": 56, "y": 56, "w": 268,
      "data": {}
    }
  ]
}

Registered kinds and their data:
- graph: { "equation": "x^2 - 4*x + 3" }   (one variable, x. Use * for multiply, ^ for power,
           sqrt/sin/cos/tan/ln/log/abs/exp allowed, pi and e allowed. No 'y=' prefix.)
- quiz:  { "question": "...", "answer": "..." }
- note:  { "text": "..." }

For anything else invent a concise snake_case kind and put the content in data.items (array of
strings) or data.steps, or plain key/value fields. The board renders unknown kinds as a formatted
list, so structured lists look better than prose blobs.

Rules:
- Prefer 1-4 actions. Never repeat a block that is already on the board with the same content.
- Place new blocks near the viewport given below, leaving a ~20px gap: x and y are world
  coordinates, blocks are ~268 wide and ~140-200 tall. Do not overlap current_blocks.
- Keep reply short: it may be spoken aloud.
- If the student's request is a question with no board change, use "actions": [].
"""


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


def build_user_message(command, board, viewport):
    return json.dumps(
        {
            "command": s(command, 1500),
            "viewport": viewport_summary(viewport),
            "current_blocks": board_summary(board),
        },
        ensure_ascii=False,
    )


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


def ask_model(command, board, viewport):
    """One call; if the JSON is unusable, one strict repair call. Returns sanitized dict."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_message(command, board, viewport)},
    ]
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

    equation = None
    fn_match = re.search(r"(sin|cos|tan|log|ln|sqrt|abs|exp)\s*\(\s*x\s*\)", command or "", re.I)
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
                },
            )
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

        if not LIVE:
            payload = mock_payload(command, board)
            payload["latency_ms"] = int((time.time() - started) * 1000)
            self.send_json(200, payload)
            return

        try:
            payload = ask_model(command, board, viewport)
        except Exception as exc:  # noqa: BLE001 - surface everything to the client
            sys.stderr.write(f"[canvas] model error: {exc}\n")
            self.send_json(502, {"error": f"{exc}"})
            return

        payload["latency_ms"] = int((time.time() - started) * 1000)
        self.send_json(200, payload)


def main():
    mode = "LIVE" if LIVE else "MOCK"
    print(f"[canvas] mode={mode} model={MODEL}", flush=True)
    print(f"[canvas] upstream={ENDPOINT}", flush=True)
    if not LIVE:
        print("[canvas] no ROAR_API_KEY -> keyword mock. Put the key in .env to go live.", flush=True)
    print(f"[canvas] http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[canvas] stopped")
