# SCOW WebShell MCP

A browserless local MCP bridge for SCOW WebShell. It logs in through SCOW's HTTP form, preserves the authenticated session locally, and connects to the SCOW WSS shell endpoint with `websocket-client`.

## Current status

- Pure Python MCP server implemented.
- HTTP login flow implemented, including captcha retrieval.
- Authenticated WSS shell connection implemented.
- MCP tools: login start, login, status, exec, send, read, resize, disconnect.
- Tested against a private SCOW deployment with a short-lived GPU probe and CoreX toolchain probe. Detailed environment-specific results are kept in a separate private repository.

## Safety

- Passwords are accepted in memory and are not written to the repository.
- Session cookies are stored only in the user's local session directory (`~/.scow-mcp/cookies.json`).
- Do not commit generated captcha images, browser bridge snippets, cookies, or site-specific credentials.

## Run

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r scow_mcp/requirements.txt
PYTHONPATH=scow_mcp python scow_mcp/server.py
```

The deployment-specific values can be set with `SCOW_BASE_URL`, `SCOW_CLUSTER`, and `SCOW_LOGIN_NODE`.

Configure `SCOW_BASE_URL`, `SCOW_CLUSTER`, and `SCOW_LOGIN_NODE` before connecting; defaults are placeholders. The tested deployment details are intentionally excluded.
