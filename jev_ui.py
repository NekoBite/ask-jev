#!/usr/bin/env python3
"""jev_ui — a small local web UI for ask_jev.py.

Serves ui/index.html on http://127.0.0.1:8765 and exposes the same ask() the CLI uses.
The API key never leaves this machine: the browser talks to this server, the server
talks to TypeSafe.

Usage:
  python3 jev_ui.py                 # start and open the browser
  python3 jev_ui.py --port 9000
  python3 jev_ui.py --no-browser    # just serve (used by the Claude Code preview)

Routes:
  GET  /                 the page
  POST /api/preview      {questions, context, context_file, options, levels, type} -> typed questions, no API call
  POST /api/ask          same body -> answers (also appended to runs/ask_log.jsonl)
  GET  /api/history?n=30 last n entries from runs/ask_log.jsonl, newest first
  GET  /api/contexts     files under decisions/ usable as --context-file
"""
from __future__ import annotations

import argparse
import json
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import ask_jev

HERE = Path(__file__).resolve().parent
PAGE = HERE / "ui" / "index.html"
DECISIONS = HERE / "decisions"


def split_csv(text: str | None) -> list[str] | None:
    items = [t.strip() for t in (text or "").split(",") if t.strip()]
    return items or None


def run_ask(body: dict, dry_run: bool) -> dict:
    questions = [q.strip() for q in body.get("questions", []) if q and q.strip()]
    if not questions:
        raise ask_jev.JevError("Type a question first.")
    context_file = body.get("context_file") or None
    if context_file:
        path = (DECISIONS / Path(context_file).name).resolve()   # only files inside decisions/
        if not path.is_file() or DECISIONS not in path.parents:
            raise ask_jev.JevError(f"Unknown context file: {context_file}")
        context_file = str(path)
    state = ask_jev.build_state(body.get("context") or None, context_file, use_stdin=False)
    return ask_jev.ask(questions, state, body.get("type") or None,
                       split_csv(body.get("options")), split_csv(body.get("levels")), dry_run=dry_run)


def history(n: int) -> list[dict]:
    if not ask_jev.LOG.exists():
        return []
    lines = ask_jev.LOG.read_text().splitlines()
    rows = []
    for line in reversed(lines):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(rows) >= n:
            break
    return rows


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter console: one line per API call only
        if "/api/" in (args[0] if args else ""):
            super().log_message(fmt, *args)

    def send_json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            data = PAGE.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif url.path == "/api/history":
            n = int(parse_qs(url.query).get("n", ["30"])[0])
            self.send_json(history(max(1, min(n, 200))))
        elif url.path == "/api/contexts":
            files = sorted(p.name for p in DECISIONS.glob("*.json")) if DECISIONS.is_dir() else []
            self.send_json(files)
        else:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if self.path not in ("/api/ask", "/api/preview"):
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            body = self.read_json()
            self.send_json(run_ask(body, dry_run=self.path == "/api/preview"))
        except ask_jev.JevError as e:
            self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        except Exception as e:  # noqa: BLE001 - surface anything else to the page rather than a blank 500
            self.send_json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Ask JEV UI at {url}   (Ctrl-C to stop)")
    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
