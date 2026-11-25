# CANVAS — spatial AI study workspace

Whiteboard-style study board where the student pans, sketches, and drags blocks, while an AI
co-pilot reads board state and answers with structured JSON actions that spawn interactive blocks.

Single file, no build step: `index.html`.

## Run

```bash
python3 -m http.server 8000
# open http://localhost:8000
```

Serve over `http://localhost` rather than opening the file directly — a `file://` page sends
`Origin: null`, which most model APIs reject via CORS, and it keeps mic permissions predictable.

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

## Known gaps

- No backend yet: `ROAR_CONFIG` is empty and the mock returns a fixed parabola + quiz. A proxy that
  injects the API key server-side is required before going live.
- The model receives `id/kind/title/x/y` only — no block text and no ink image, so it cannot reason
  about what is actually on the board.
- No persistence (localStorage), no undo, no wheel zoom, no JSON repair for malformed model output.
