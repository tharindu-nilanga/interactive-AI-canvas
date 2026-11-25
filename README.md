# CANVAS — spatial AI study workspace

Whiteboard-style study board where the student pans, sketches, and drags blocks, while an AI
co-pilot reads board state and answers with structured JSON actions that spawn interactive blocks.

Single file, no build step: `index.html`.

## Run

```bash
cp .env.example .env      # then paste ROAR_API_KEY into .env
python3 server.py         # http://localhost:8000
```

`server.py` is a stdlib-only proxy: it serves this page, holds the API key server-side, calls
Roar AI (OpenAI-compatible: `https://api.roar-ai.com/v1/chat/completions`), and sanitises the
model's JSON before it reaches the browser. No `npm install`, no venv.

Without a key the server runs a keyword mock, and if the proxy is down entirely the page falls
back to a client-side mock — the demo never shows a blank board.

```bash
python3 stub_upstream.py 8100                      # fake upstream that returns messy JSON
ROAR_ENDPOINT=http://127.0.0.1:8100/v1/chat/completions \
ROAR_API_KEY=test PORT=8010 python3 server.py      # exercise the repair path
```

## Controls

| Key | Action |
|-----|--------|
| `H` | toggle PAN mode (stage drag pans the 4000×4000 world) |
| `P` | toggle PEN (freehand ink, world coordinates) |
| `V` | toggle VOICE (SpeechRecognition + speechSynthesis) |
| `C` | clear ink |
| `R` | reset pan to (0, 0) |
| `/` | focus prompt bar |
| `Esc` | leave prompt bar |
| Space + drag / middle-drag | pan while PAN is off |

## Block protocol

```json
{
  "reply": "short conversational response",
  "actions": [
    { "kind": "graph", "title": "PARABOLA", "x": 56, "y": 56, "w": 268,
      "data": { "equation": "x^2 - 4*x + 3" } },
    { "kind": "quiz", "title": "ROOT CHECK", "data": { "question": "…", "answer": "…" } },
    { "kind": "note", "title": "TAKEAWAY", "data": { "text": "…" } }
  ]
}
```

Unknown `kind`s fall through to the `dynamic` renderer, which formats `data.items` / `data.steps`
as a list or pretty-prints the object. Registered renderers live in `ELEMENT_RENDERERS`; add one
and it becomes available to the model immediately.

## Implementation notes

- **Graphs** are compiled with a whitelisted identifier set (`compileExpression`), never raw
  `Function(promptText)`. `^` maps to `Math.pow`, only known math functions/constants resolve, and
  anything else renders `UNPARSED EXPRESSION`. The path breaks at asymptotes so `1/x` and `tan(x)`
  don't get false verticals; units are square; canvases redraw via `ResizeObserver`.
- **Placement** (`pickNonOverlappingPosition`) first-fits inside the visible viewport, then the
  whole world, so blocks never stack on each other or land behind the sidebar.
- **Ink** is a 4000×4000 canvas anchored to world coordinates, drawn under the cards (z-index 5) so
  blocks stay clickable with the pen armed.

## Request flow

```
browser ──POST /api/chat {command, board, viewport}──▶ server.py
                                                       │ injects system prompt + key
                                                       ▼
                                                   Roar AI (OpenAI-compatible)
                                                       │
          hardened {reply, actions} ◀──────────────────┘  strip fence → balance braces
                                                          → trailing-comma repair
                                                          → one strict repair call
                                                          → whitelist/coerce actions
```

What the proxy refuses to pass through: unknown `kind`s are coerced to `note`, non-string titles are
stringified and truncated, unparseable coordinates are dropped and out-of-range ones clamped,
`graph` actions without an equation are discarded, more than 8 actions are dropped, and the reply is
capped at 400 chars. Static serving is whitelisted to front-end extensions, so `.env`, `server.py`
and `.git/` are not reachable over HTTP.

## Known gaps

- The model receives block text plus world coordinates, but **not the ink** — freehand annotations
  are still invisible to it (a downscaled PNG of the pen layer would fix that).
- No persistence (localStorage), no undo, no wheel zoom.
