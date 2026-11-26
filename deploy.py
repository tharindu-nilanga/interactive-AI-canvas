#!/usr/bin/env python3
"""
Deploy CANVAS to Roar AI app hosting.

    export ROAR_DEPLOY_KEY=roar_live_...      # a key with apps:read + apps:write
    python3 deploy.py                          # deploy
    python3 deploy.py --dry-run                # show what would be sent
    python3 deploy.py --name canvas-demo --model deepseek-v4.1-flash

What it does, in the order the API requires it:
  1. POST /client/apps/uploads                      open an upload
  2. POST /client/apps/uploads/{id}/inline          send the files as text
  3. POST /client/apps/uploads/{id}/deploy          deploy it
  4. GET  /client/apps/{id}/progress                poll until live, print the URL

Only index.html and server.py are sent. `.env` is never uploaded — the hosted app
gets its own ROAR_API_KEY injected by the platform, so no secret leaves this machine.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("ROAR_API", "https://api.roar-ai.com")
ROOT = os.path.dirname(os.path.abspath(__file__))
FILES = ["index.html", "server.py"]
START_COMMAND = "python3 server.py"


def key():
    value = os.environ.get("ROAR_DEPLOY_KEY") or os.environ.get("ROAR_API_KEY") or ""
    value = value.strip()
    if not value:
        sys.exit(
            "No deploy key. Create one in the Roar console (API keys -> New key) with\n"
            "the apps scopes, then: export ROAR_DEPLOY_KEY=roar_live_..."
        )
    return value


def call(method, path, body=None, token=None, timeout=60):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            message = json.loads(detail)["error"]["message"]
        except Exception:  # noqa: BLE001 - fall back to the raw body
            message = detail[:400]
        raise SystemExit(f"{method} {path} -> HTTP {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"{method} {path} -> {exc.reason}") from exc


def gather():
    files = []
    for name in FILES:
        path = os.path.join(ROOT, name)
        if not os.path.isfile(path):
            sys.exit(f"missing {name}")
        with open(path, encoding="utf-8") as fh:
            files.append({"path": name, "content": fh.read()})
    return files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="canvas")
    parser.add_argument("--model", default=os.environ.get("ROAR_MODEL", "deepseek-v4.1-flash"))
    parser.add_argument("--app-id", default="", help="deploy a new version of an existing app")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = key()
    files = gather()
    total = sum(len(f["content"]) for f in files)

    print(f"[deploy] {len(files)} file(s), {total/1024:.0f} KB: {', '.join(FILES)}")
    print(f"[deploy] start command: {START_COMMAND}")
    print(f"[deploy] env: ROAR_MODEL={args.model}")
    if args.dry_run:
        for f in files:
            print(f"  {f['path']:16} {len(f['content'])/1024:6.1f} KB")
        print("[deploy] dry run — nothing sent")
        return

    upload = call("POST", "/client/apps/uploads", {"appId": args.app_id} if args.app_id else {}, token)
    upload_id = upload.get("id")
    if not upload_id:
        sys.exit(f"upload not opened: {upload}")
    print(f"[deploy] upload {upload_id} opened")

    call("POST", f"/client/apps/uploads/{upload_id}/inline", {"files": files}, token)
    print("[deploy] files sent")

    body = {
        "name": args.name,
        "startCommand": START_COMMAND,
        "databaseEnabled": False,
        "env": {"ROAR_MODEL": args.model},
    }
    if args.app_id:
        body["appId"] = args.app_id

    deployed = call("POST", f"/client/apps/uploads/{upload_id}/deploy", body, token)
    app_id = deployed.get("id") or deployed.get("appId") or args.app_id
    if not app_id:
        apps = call("GET", "/client/apps", None, token).get("apps") or []
        match = [a for a in apps if a.get("name") == args.name]
        app_id = match[-1].get("id") if match else None
    if not app_id:
        sys.exit(f"deploy started but no app id came back: {deployed}")
    print(f"[deploy] app {app_id} building…")

    url = ""
    seen = set()
    for _ in range(120):  # up to ~6 minutes
        time.sleep(3)
        progress = call("GET", f"/client/apps/{app_id}/progress", None, token)
        phase = progress.get("phase") or progress.get("runState") or "?"
        deployment = (progress.get("deployment") or {}).get("status", "")
        note = f"[deploy] {phase} {deployment}".strip()
        if note not in seen:
            print(note)
            seen.add(note)
        url = progress.get("url") or (progress.get("domain") or {}).get("domain") or url
        if url and str(deployment).lower() in {"live", "ready", "success", "running", "deployed"}:
            break
        if progress.get("error"):
            sys.exit(f"deploy failed: {progress['error']}")

    if not url:
        print(f"[deploy] still building — check https://roar-ai.com/console (app {app_id})")
        return
    if not url.startswith("http"):
        url = f"https://{url}"
    print(f"\n[deploy] live at {url}")


if __name__ == "__main__":
    main()
