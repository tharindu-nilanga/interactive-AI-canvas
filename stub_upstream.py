#!/usr/bin/env python3
"""Fake OpenAI-compatible upstream for testing server.py's JSON hardening.

Serves a *deliberately messy* reply: prose preamble, ```json fence, trailing
comma, wrong types, an unknown kind, an out-of-range coordinate and one action
that must be dropped (graph with no equation).

    python3 stub_upstream.py 8100
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


MESSY = {
    "reply": "Here are two blocks for you.",
    "actions": [
        {"kind": "graph", "title": "PARABOLA", "x": 56, "y": 56,
         "data": {"equation": "x^2 - 4*x + 3"}},
        {"kind": "quiz", "title": "ROOT CHECK",
         "data": {"question": "Roots of x^2 - 4x + 3?", "answer": "1 and 3"}},
        {"kind": "Graph", "title": 12345, "x": "9999", "y": -50,
         "data": {"equation": "sin(x)"}},
        {"kind": "steps", "title": "WORKING",
         "data": {"steps": ["a", "b", "c"]}},
        {"kind": "graph", "title": "BROKEN", "data": {}},
        {"type": "add_note", "data": {"text": "flattened type field"}},
    ],
}


class Stub(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        sys.stderr.write("[stub] %s\n" % (format % args))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")

        # If this looks like the repair call, answer with clean JSON.
        last = body.get("messages", [{}])[-1].get("content", "")
        if "not valid JSON" in last:
            payload = json.dumps(MESSY, indent=None)
        else:
            # messy: prose + fence + trailing comma + smart quotes
            payload = (
                "Sure! Here is the JSON you asked for:\n\n```json\n"
                + json.dumps(MESSY, indent=2).replace("}\n  ]", "},\n  ]")
                + "\n```\nHope that helps."
            )

        out = {
            "id": "chatcmpl-stub",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": payload}}],
        }
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8100
    print(f"[stub] messy upstream on http://127.0.0.1:{port}/v1/chat/completions")
    ThreadingHTTPServer(("127.0.0.1", port), Stub).serve_forever()
