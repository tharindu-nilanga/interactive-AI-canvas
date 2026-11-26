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
| drag a card header | move the block |
| drag the bottom-right corner | resize the block (graph canvases re-plot to fit) |
| `Enter` in a quiz input | grade the answer |
| CLEAR ALL BLOCKS (sidebar) | remove every panel; `Ctrl+Z` restores them one at a time |
| `Ctrl`/`Cmd` + `Z` | undo the last removed block (manual or model-driven) |

## Quiz blocks

`quiz` accepts either a free-text answer or `choices`. Free-text is graded **locally** — no model
call: case and spacing are ignored, and numeric answers compare as a set, so `3 and 1` matches
`x = 1 and x = 3`. `choices` renders as buttons. `REVEAL ANSWER` shows the expected answer, and a
wrong attempt reveals **ASK FOR A HINT**, which sends the model the question and what you typed.

## Two-way loop

**What the student does is recorded.** Quiz attempts, reveals, checklist ticks and closed blocks are
queued as semantic events (`quiz_wrong`, `checklist_toggle`, `block_closed`, …) and drained into the
next request. Drags and resizes are not logged — the board snapshot already carries positions. The
proxy turns them into plain sentences above the JSON, because models skip events buried in JSON.

**The model can act on existing blocks**, not just create them:

```json
{ "op": "delete", "id": "el_123" }
{ "op": "update", "id": "el_123", "title": "easier", "data": { "question": "…", "choices": ["…"] } }
{ "op": "move",   "id": "el_123", "x": 600, "y": 420 }
{ "op": "clear" }
{ "op": "spawn",  "kind": "card", "title": "…", "data": {} }   // same as omitting "op"
```

`delete` and `move` also accept a `title` to target a block when the model does not know its id.
Every mutation is logged in the session history (`removed "Cosine"; updated "Pick the pair"`), and
`Ctrl+Z` restores the last removed block — deletes detach the card rather than destroying it.

## Block protocol

```json
{
  "reply": "short conversational response",
  "actions": [
    { "kind": "graph", "title": "PARABOLA", "x": 56, "y": 56, "w": 268,
      "data": { "equation": "x^2 - 4*x + 3" } },
    { "kind": "quiz", "title": "ROOT CHECK", "data": { "question": "…", "answer": "…", "choices": ["…"] } },
    { "kind": "note", "title": "TAKEAWAY", "data": { "text": "…" } },
    { "kind": "steps", "title": "WORKING", "data": { "steps": ["…", "…"] } },
    { "kind": "table", "title": "COMPARE", "data": { "headers": ["a", "b"], "rows": [["1", "2"]] } },
    { "kind": "checklist", "title": "REVISE", "data": { "items": ["…", "…"] } },
    { "kind": "formula", "title": "QUADRATIC", "data": { "expression": "x = (-b ± sqrt(b²-4ac))/2a" } },
    { "kind": "card", "title": "ANYTHING", "data": { "body": "## markdown\n\n- bullets\n- and **bold**" } }
  ]
}
```

Unknown `kind`s fall through to the `dynamic` renderer: it finds the first array in `data`
(`items` / `steps` / anything else), renders `Key: value` bullets as a two-column block, and
pretty-prints objects that have neither. A custom kind that carries `headers` + `rows` is drawn as a
table. Registered renderers live in `ELEMENT_RENDERERS`; add one and the model can use it
immediately.

## Implementation notes

- **Graphs** are compiled with a whitelisted identifier set (`compileExpression`), never raw
  `Function(promptText)`. `^` maps to `Math.pow`, only known math functions/constants resolve, and
  anything else renders `UNPARSED EXPRESSION`. The path breaks at asymptotes so `1/x` and `tan(x)`
  don't get false verticals; units are square; canvases redraw via `ResizeObserver`.
- **Placement** is computed here, not by the model. The model may hint structure — `place`:
  `"right" | "below" | "new-row"`, or `col`: 0-3 — and `layoutBatch()` packs the whole batch into
  equal-width 268px columns with exact 20px gutters, starting at the top-left of the current view,
  sliding any card down until it clears what is already on the board. Any `x`/`y` the model sends
  is ignored; asking a language model for pixel coordinates is what produced scattered cards.
- **The board grows.** It starts at 4000×4000 and `ensureWorldBounds()` extends it in 1000px steps
  whenever blocks come within a screen of the edge, so you never hit a wall mid-session.
- **Ink** is stored as stroke points in world coordinates and replayed onto a viewport-sized canvas,
  so it survives panning, resizing and a growing board without a 4000² bitmap (~64 MB) behind it.
  The ink canvas is `pointer-events: none`: pen input is handled on the stage, which means cards
  stay clickable while the pen is armed.
- **Free-form cards**: `kind: "card"` takes `data.body` and renders a safe markdown subset —
  headings, bold/italic, inline code, fenced blocks, bullets, numbered lists, quotes, pipe tables,
  links. Text is escaped before any markup is applied, so model output can never inject HTML.
  `note` uses the same renderer.

## Request flow

```
browser ──POST /api/chat {command, capabilities, board, viewport}──▶ server.py
                                                       │ builds the system prompt from
                                                       │ capabilities.kinds + KIND_DOCS
                                                       │ injects the key
                                                       ▼
                                                   Roar AI (OpenAI-compatible)
                                                       │
          hardened {reply, actions} ◀──────────────────┘  strip fence → balance braces
                                                          → trailing-comma repair
                                                          → one strict repair call
                                                          → whitelist/coerce actions
```

The client never sends a schema — it sends the list of kinds it can render
(`Object.keys(ELEMENT_RENDERERS)`), and `build_system_prompt()` documents only those. Add a renderer
and the model starts using it; there is no second copy of the spec to drift. `GET /api/prompt`
prints exactly what the model is told.

What the proxy refuses to pass through: unknown `kind`s are coerced to `note`, non-string titles are
stringified and truncated, unparseable coordinates are dropped and out-of-range ones clamped,
`graph` actions without an equation are discarded, more than 8 actions are dropped, and the reply is
capped at 400 chars. Static serving is whitelisted to front-end extensions, so `.env`, `server.py`
and `.git/` are not reachable over HTTP.

## Known gaps

- The model receives block text plus world coordinates, but **not the ink** — freehand annotations
  are still invisible to it (a downscaled PNG of the pen layer would fix that).
- No persistence (localStorage), no undo, no wheel zoom.
