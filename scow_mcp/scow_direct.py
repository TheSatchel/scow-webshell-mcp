#!/usr/bin/env python3
"""Direct SCOW HTTP-login + WebSocket WebShell client.

No browser is needed. The login flow uses requests.Session, and the terminal
uses the same authenticated session's cookies when opening SCOW's WSS shell.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import queue
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

import requests
import websocket

try:
    import cairosvg
except Exception:
    cairosvg = None

BASE_URL = os.environ.get("SCOW_BASE_URL", "https://scow.example.edu").rstrip("/")
CLUSTER = os.environ.get("SCOW_CLUSTER", "example-cluster")
LOGIN_NODE = os.environ.get("SCOW_LOGIN_NODE", "login-node.example.edu")
SESSION_DIR = Path(os.environ.get("SCOW_SESSION_DIR", Path.home() / ".scow-mcp"))
COOKIE_FILE = SESSION_DIR / "cookies.json"


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.chmod(temp, mode)
    temp.replace(path)


def _cookie_header(cookies: requests.cookies.RequestsCookieJar) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.get_dict().items())


def _strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value).strip()


class ScowAuth:
    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "scow-direct-client/0.1"})
        self.auth_url = ""
        self.token = ""
        self.callback_url = ""
        self.captcha_svg = ""

    def begin_login(self) -> Path:
        response = self.session.get(f"{BASE_URL}/api/auth", allow_redirects=True, timeout=20)
        response.raise_for_status()
        self.auth_url = response.url
        token_match = re.search(r'name="token"\s+value="([^"]+)"', response.text)
        callback_match = re.search(r'name="callbackUrl"\s+value="([^"]+)"', response.text)
        captcha_match = re.search(r'<div id="captcha"[^>]*>(.*?)</div>', response.text, re.S)
        if not token_match or not callback_match or not captcha_match:
            raise RuntimeError("SCOW login form or captcha was not found")
        self.token = token_match.group(1)
        self.callback_url = callback_match.group(1)
        self.captcha_svg = captcha_match.group(1).strip()
        captcha_path = SESSION_DIR / "captcha.svg"
        _atomic_write(captcha_path, self.captcha_svg)
        if cairosvg is not None:
            png_path = SESSION_DIR / "captcha.png"
            cairosvg.svg2png(bytestring=self.captcha_svg.encode(), write_to=str(png_path))
            return png_path
        return captcha_path

    def login(self, username: str, password: str, code: str) -> dict[str, Any]:
        if not self.auth_url or not self.token:
            self.begin_login()
        payload = {
            "username": username,
            "password": password,
            "code": code,
            "token": self.token,
            "callbackUrl": self.callback_url,
        }
        response = self.session.post(self.auth_url, data=payload, allow_redirects=False, timeout=20)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location", "")
            if location:
                callback_response = self.session.get(urljoin(response.url, location), allow_redirects=True, timeout=20)
            else:
                callback_response = response
        else:
            callback_response = response
        if callback_response.status_code >= 400:
            body = callback_response.text
            messages = []
            for pattern in [r'用户名[^<]{0,80}', r'密码[^<]{0,80}', r'验证码[^<]{0,80}', r'登录失败[^<]{0,80}', r'错误[^<]{0,80}']:
                messages.extend(re.findall(pattern, body))
            message = " | ".join(dict.fromkeys(m.strip() for m in messages if m.strip()))
            if not message:
                message = _strip_html(body)[:500]
            raise RuntimeError(f"SCOW login failed ({callback_response.status_code}): {message}")
        # A successful callback normally leaves an authenticated cookie.
        if not self.session.cookies:
            raise RuntimeError("SCOW login returned no session cookie; the captcha or credentials may be invalid")
        data = {
            "base_url": BASE_URL,
            "cluster": CLUSTER,
            "login_node": LOGIN_NODE,
            "cookies": self.session.cookies.get_dict(),
            "saved_at": time.time(),
        }
        _atomic_write(COOKIE_FILE, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        return {"status": "ok", "cookie_file": str(COOKIE_FILE), "redirect": callback_response.url}

    @classmethod
    def load_saved(cls) -> "ScowAuth":
        if not COOKIE_FILE.exists():
            raise FileNotFoundError(f"No saved SCOW session. Run: python {Path(__file__).name} login")
        data = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        session = requests.Session()
        session.headers.update({"User-Agent": "scow-direct-client/0.1"})
        for key, value in data.get("cookies", {}).items():
            session.cookies.set(key, value, domain=BASE_URL.split("//", 1)[-1].split("/", 1)[0], path="/")
        obj = cls(session)
        obj.saved = data
        return obj

    def validate(self) -> bool:
        try:
            response = self.session.get(f"{BASE_URL}/api/getUserInfo", timeout=15)
            return response.status_code < 400 and "登录" not in response.text[:200]
        except requests.RequestException:
            return False


@dataclass
class ShellMessage:
    kind: str
    text: str = ""
    raw: Optional[dict[str, Any]] = None


class ScowShell:
    def __init__(self, auth: ScowAuth, cols: int = 120, rows: int = 35):
        self.auth = auth
        self.cols = cols
        self.rows = rows
        self.ws: Optional[websocket.WebSocket] = None
        self.reader: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.messages: queue.Queue[ShellMessage] = queue.Queue()
        self.connected = False
        self.last_error = ""

    @property
    def url(self) -> str:
        from urllib.parse import urlencode
        query = urlencode({
            "cluster": CLUSTER,
            "loginNode": LOGIN_NODE,
            "path": "",
            "cols": str(self.cols),
            "rows": str(self.rows),
            "useRoot": "false",
        })
        return f"{BASE_URL.replace('https://', 'wss://').replace('http://', 'ws://')}/api/shell?{query}"

    def connect(self) -> None:
        if self.connected and self.ws:
            return
        cookie = _cookie_header(self.auth.session.cookies)
        if not cookie:
            raise RuntimeError("No SCOW session cookie. Run the login helper first.")
        self.stop_event.clear()
        try:
            self.ws = websocket.create_connection(
                self.url,
                cookie=cookie,
                origin=BASE_URL,
                timeout=10,
                enable_multithread=True,
            )
            self.ws.send(json.dumps({"$case": "resize", "resize": {"cols": self.cols, "rows": self.rows}}))
            self.connected = True
            self.reader = threading.Thread(target=self._read_loop, name="scow-wss-reader", daemon=True)
            self.reader.start()
        except Exception as exc:
            self.last_error = str(exc)
            self.connected = False
            raise RuntimeError(f"SCOW WSS connection failed: {exc}") from exc

    def _read_loop(self) -> None:
        assert self.ws is not None
        while not self.stop_event.is_set():
            try:
                raw = self.ws.recv()
                if raw is None:
                    break
                message = json.loads(raw)
                case = message.get("$case")
                if case == "data":
                    self.messages.put(ShellMessage("data", message.get("data", {}).get("data", ""), message))
                elif case == "exit":
                    self.messages.put(ShellMessage("exit", json.dumps(message, ensure_ascii=False), message))
                else:
                    self.messages.put(ShellMessage("other", "", message))
            except Exception as exc:
                if not self.stop_event.is_set():
                    self.last_error = str(exc)
                    self.messages.put(ShellMessage("error", str(exc)))
                break
        self.connected = False

    def send(self, data: str) -> None:
        self.connect()
        assert self.ws is not None
        self.ws.send(json.dumps({"$case": "data", "data": {"data": data}}))

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        self.connect()
        assert self.ws is not None
        self.ws.send(json.dumps({"$case": "resize", "resize": {"cols": cols, "rows": rows}}))

    def read(self, wait: float = 0.5) -> str:
        parts: list[str] = []
        deadline = time.monotonic() + max(wait, 0)
        while True:
            remaining = deadline - time.monotonic()
            try:
                message = self.messages.get(timeout=max(0, remaining))
            except queue.Empty:
                break
            if message.kind == "data":
                parts.append(message.text)
            elif message.kind == "exit":
                parts.append("\r\n[process exited]\r\n")
            elif message.kind == "error":
                parts.append(f"\r\n[WSS error: {message.text}]\r\n")
            if remaining <= 0:
                break
        return "".join(parts)

    def disconnect(self) -> None:
        self.stop_event.set()
        self.connected = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self.ws = None


def interactive_login() -> int:
    auth = ScowAuth()
    captcha = auth.begin_login()
    print(f"验证码已保存到: {captcha}")
    if sys.platform == "darwin":
        try:
            import subprocess
            subprocess.run(["open", str(captcha)], check=False)
        except Exception:
            pass
    print("请查看验证码图片；如果图片没有自动打开，请手动打开上面的路径。")
    username = input("SCOW 用户名: ").strip()
    password = getpass.getpass("SCOW 密码: ")
    code = input("验证码: ").strip()
    result = auth.login(username, password, code)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def interactive_exec(command: str) -> int:
    auth = ScowAuth.load_saved()
    shell = ScowShell(auth)
    shell.connect()
    shell.read(0.3)  # drain the banner
    shell.send(command + "\r")
    end = time.monotonic() + 20
    output = ""
    while time.monotonic() < end:
        output += shell.read(0.5)
        if re.search(r"\n\[[^\n\]]+@[^\n\]]+ [^\n]*\]\$\s*$", re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output).replace("\r", "")):
            break
    print(output, end="")
    shell.disconnect()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Direct SCOW HTTP/WSS client (no browser)")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("login", help="interactive login; asks for password and captcha")
    ex = sub.add_parser("exec", help="run a simple shell command with saved session")
    ex.add_argument("command")
    args = parser.parse_args()
    if args.action == "login":
        return interactive_login()
    if args.action == "exec":
        return interactive_exec(args.command)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
