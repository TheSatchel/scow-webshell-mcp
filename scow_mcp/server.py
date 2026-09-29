#!/usr/bin/env python3
"""Direct, browserless SCOW MCP server.

It logs in through SCOW's HTTP form (with a captcha step) and then connects
straight to the authenticated WSS WebShell endpoint.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import sys
import threading
from pathlib import Path

from scow_direct import ScowAuth, ScowShell, BASE_URL, CLUSTER, LOGIN_NODE, COOKIE_FILE

AUTH = ScowAuth()
SHELL: ScowShell | None = None
LOCK = threading.RLock()


def reply(req_id, payload):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, **payload}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def text_result(text: str, error: bool = False):
    result = {"content": [{"type": "text", "text": str(text)}]}
    if error:
        result["isError"] = True
    return result


def captcha_result(captcha_path: Path):
    """Return the captcha as MCP image content for direct client inspection."""
    content = []
    if captcha_path.exists():
        mime_type = mimetypes.guess_type(captcha_path.name)[0] or "image/png"
        content.append({
            "type": "image",
            "data": base64.b64encode(captcha_path.read_bytes()).decode("ascii"),
            "mimeType": mime_type,
        })
    content.append({
        "type": "text",
        "text": json.dumps({
            "captcha_image": str(captcha_path),
            "captcha_svg": str(captcha_path.with_suffix(".svg")),
            "message": "The captcha image is attached above. Inspect it directly, then call scow_login with username, password, and the captcha text. Do not use a terminal login helper or a default SVG viewer.",
        }, ensure_ascii=False, indent=2),
    })
    return {"content": content}



def upload_file(local_path: str, remote_dir: str, cluster: str = CLUSTER) -> dict:
    """Upload a local file using SCOW's multipart file-manager HTTP API."""
    source = Path(local_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"local file not found: {source}")
    remote_dir = remote_dir or "."
    name = source.name
    session = AUTH
    if not session.validate():
        session = ScowAuth.load_saved()
    base = BASE_URL
    init = session.session.post(
        f"{base}/api/file/initMultipartUpload",
        json={"cluster": cluster, "path": remote_dir, "name": name},
        timeout=30,
    )
    init.raise_for_status()
    data = init.json()
    temp_dir = data["tempFileDir"]
    chunk_size = int(data["chunkSizeByte"])
    size = source.stat().st_size
    uploaded = {int(item["name"].rsplit("_", 1)[-1].split(".", 1)[0])
                for item in data.get("filesInfo", [])
                if "_" in item.get("name", "") and item.get("name", "").endswith(".scowuploadtemp")}
    count = (size + chunk_size - 1) // chunk_size
    with source.open("rb") as handle:
        for index in range(1, count + 1):
            chunk = handle.read(chunk_size)
            if index in uploaded:
                continue
            digest = hashlib.sha256(chunk).hexdigest()
            temp_name = f"{digest}_{index}.scowuploadtemp"
            response = session.session.post(
                f"{base}/api/file/upload",
                params={"cluster": cluster, "path": f"{temp_dir.rstrip('/')}/{temp_name}"},
                files={"file": (temp_name, chunk)},
                timeout=300,
            )
            response.raise_for_status()
    merged = session.session.post(
        f"{base}/api/file/mergeFileChunks",
        json={"cluster": cluster, "path": remote_dir, "name": name, "sizeByte": size},
        timeout=300,
    )
    merged.raise_for_status()
    return {"status": "uploaded", "name": name, "remote_path": f"{remote_dir.rstrip('/')}/{name}", "size": size, "chunks": count, "chunk_size": chunk_size}

def get_shell() -> ScowShell:
    global SHELL
    with LOCK:
        if SHELL is None:
            auth = ScowAuth.load_saved()
            SHELL = ScowShell(auth)
        SHELL.connect()
        return SHELL


def call_tool(name, args):
    global AUTH, SHELL
    if name == "scow_login_start":
        with LOCK:
            AUTH = ScowAuth()
            captcha = AUTH.begin_login()
        return captcha_result(Path(captcha))

    if name == "scow_login":
        username = str(args.get("username", "")).strip()
        password = str(args.get("password", ""))
        code = str(args.get("captcha", "")).strip()
        if not username or not password or not code:
            return text_result("username, password, and captcha are required", True)
        with LOCK:
            result = AUTH.login(username, password, code)
            SHELL = None
        # Do not echo the password or cookies.
        return text_result(json.dumps({"status": "logged_in", "cookie_file": result["cookie_file"]}, ensure_ascii=False, indent=2))

    if name == "scow_upload":
        local_path = str(args.get("local_path", "")).strip()
        remote_dir = str(args.get("remote_dir", "~/models")).strip()
        cluster = str(args.get("cluster", CLUSTER)).strip() or CLUSTER
        if not local_path:
            return text_result("local_path is required", True)
        with LOCK:
            result = upload_file(local_path, remote_dir, cluster)
        return text_result(json.dumps(result, ensure_ascii=False, indent=2))

    if name == "scow_status":
        with LOCK:
            saved = COOKIE_FILE.exists()
            shell_status = "disconnected" if SHELL is None else ("connected" if SHELL.connected else "not connected")
        return text_result(json.dumps({
            "base_url": BASE_URL,
            "cluster": CLUSTER,
            "login_node": LOGIN_NODE,
            "saved_session": saved,
            "shell": shell_status,
            "mode": "direct Python HTTP + WSS; no browser",
        }, ensure_ascii=False, indent=2))

    if name == "scow_exec":
        command = str(args.get("command", ""))
        if not command:
            return text_result("command is required", True)
        sh = get_shell()
        sh.read(0.2)
        sh.send(command + "\r")
        output = ""
        for _ in range(80):
            output += sh.read(0.25)
            clean = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", output)
            clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", clean).replace("\r", "")
            if re.search(r"\n\[[^\n\]]+@[^\n\]]+ [^\n]*\]\$\s*$", clean):
                break
        return text_result(output or "(no output)")

    if name == "scow_send":
        get_shell().send(str(args.get("data", "")))
        return text_result("sent")

    if name == "scow_read":
        wait_ms = max(0, min(int(args.get("wait_ms", 500)), 30000))
        return text_result(get_shell().read(wait_ms / 1000) or "(no new output)")

    if name == "scow_resize":
        cols = max(20, int(args.get("cols", 120)))
        rows = max(5, int(args.get("rows", 35)))
        get_shell().resize(cols, rows)
        return text_result(f"resized to {cols}x{rows}")

    if name == "scow_disconnect":
        with LOCK:
            if SHELL is not None:
                SHELL.disconnect()
        return text_result("disconnected")

    return text_result(f"unknown tool: {name}", True)


TOOLS = [
    {"name": "scow_login_start", "description": "Start SCOW login and return the generated captcha as inline MCP image content. Inspect the attached image directly, then call scow_login; do not use a terminal login helper or default SVG viewer.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "scow_login", "description": "Complete browserless SCOW login. The password is used only in memory and is never echoed.", "inputSchema": {"type": "object", "properties": {"username": {"type": "string"}, "password": {"type": "string"}, "captcha": {"type": "string"}}, "required": ["username", "password", "captcha"], "additionalProperties": False}},
    {"name": "scow_upload", "description": "Upload a local file to SCOW using the authenticated multipart file-manager API. Resumes completed chunks and merges them when done.", "inputSchema": {"type": "object", "properties": {"local_path": {"type": "string"}, "remote_dir": {"type": "string", "default": "~/models"}, "cluster": {"type": "string", "default": CLUSTER}}, "required": ["local_path"], "additionalProperties": False}},
    {"name": "scow_status", "description": "Show direct Python SCOW HTTP/WSS client status.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "scow_exec", "description": "Run a non-interactive shell command in the SCOW WebShell.", "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"], "additionalProperties": False}},
    {"name": "scow_send", "description": "Send raw terminal input to SCOW, useful for interactive programs and control bytes.", "inputSchema": {"type": "object", "properties": {"data": {"type": "string"}}, "required": ["data"], "additionalProperties": False}},
    {"name": "scow_read", "description": "Read output from the SCOW WebShell.", "inputSchema": {"type": "object", "properties": {"wait_ms": {"type": "integer", "minimum": 0, "maximum": 30000}}, "additionalProperties": False}},
    {"name": "scow_resize", "description": "Resize the SCOW terminal.", "inputSchema": {"type": "object", "properties": {"cols": {"type": "integer"}, "rows": {"type": "integer"}}, "required": ["cols", "rows"], "additionalProperties": False}},
    {"name": "scow_disconnect", "description": "Close the direct SCOW WSS connection.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
]


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            method = req.get("method")
            req_id = req.get("id")
            if method == "initialize":
                reply(req_id, {"result": {"protocolVersion": req.get("params", {}).get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}}, "serverInfo": {"name": "scow-python-direct", "version": "0.1.0"}}})
            elif method in ("notifications/initialized", "notifications/cancelled"):
                continue
            elif method == "ping":
                reply(req_id, {"result": {}})
            elif method == "tools/list":
                reply(req_id, {"result": {"tools": TOOLS}})
            elif method == "tools/call":
                try:
                    result = call_tool(req.get("params", {}).get("name"), req.get("params", {}).get("arguments", {}))
                    reply(req_id, {"result": result})
                except Exception as exc:
                    reply(req_id, {"result": text_result(str(exc), True)})
            elif req_id is not None:
                reply(req_id, {"error": {"code": -32601, "message": f"Method not found: {method}"}})
        except Exception as exc:
            if "req_id" in locals() and req_id is not None:
                reply(req_id, {"error": {"code": -32700, "message": str(exc)}})


if __name__ == "__main__":
    main()
