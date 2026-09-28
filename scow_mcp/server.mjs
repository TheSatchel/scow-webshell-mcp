#!/usr/bin/env node
/**
 * Local MCP bridge for the SCOW WebShell.
 *
 * The MCP process speaks JSON-RPC over stdio.  A small browser-side snippet
 * (bridge-snippet.js) connects to the local WebSocket bridge and reuses the
 * authenticated SCOW page session to carry terminal traffic over WSS.
 */
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";
import process from "node:process";
import { WebSocketServer } from "ws";

const PORT = Number(process.env.SCOW_MCP_BRIDGE_PORT || 43127);
const HOST = "127.0.0.1";
const TARGET = {
  cluster: process.env.SCOW_CLUSTER || "example-cluster",
  loginNode: process.env.SCOW_LOGIN_NODE || "login-node.example.edu",
  path: process.env.SCOW_PATH || "",
  cols: Number(process.env.SCOW_COLS || 120),
  rows: Number(process.env.SCOW_ROWS || 35),
  useRoot: process.env.SCOW_USE_ROOT === "true",
};
const token = crypto.randomBytes(24).toString("hex");
const state = {
  browser: null,
  browserStatus: "waiting for browser bridge",
  lastShellStatus: "unknown",
  chunks: [],
  commandQueue: [],
  activeCommand: null,
  startedAt: new Date().toISOString(),
};

const appDir = path.dirname(new URL(import.meta.url).pathname);
const snippetPath = path.join(appDir, "bridge-snippet.js");
const bootstrapPath = path.join(os.tmpdir(), `scow-mcp-${process.pid}.json`);

function targetQuery() {
  const q = new URLSearchParams({
    cluster: TARGET.cluster,
    loginNode: TARGET.loginNode,
    path: TARGET.path,
    cols: String(TARGET.cols),
    rows: String(TARGET.rows),
    useRoot: String(TARGET.useRoot),
  });
  return q.toString();
}

function browserSnippet() {
  const wsUrl = `ws://${HOST}:${PORT}/?token=${token}`;
  const shellUrl = `wss://scow.example.edu/api/shell?${targetQuery()}`;
  return `(() => {
  const old = window.__SCOW_MCP_BRIDGE__;
  if (old) { try { old.close(); } catch {} }

  const bridge = new WebSocket(${JSON.stringify(wsUrl)});
  const shellUrl = ${JSON.stringify(shellUrl)};
  let shell = null;

  const send = (message) => {
    if (bridge.readyState === WebSocket.OPEN) bridge.send(JSON.stringify(message));
  };

  const status = (value, detail = {}) => send({ type: "status", value, detail });

  bridge.onopen = () => {
    status("bridge-open", { shellUrl });
    shell = new WebSocket(shellUrl);
    window.__SCOW_MCP_SHELL__ = shell;
    shell.onopen = () => {
      status("shell-open");
      shell.send(JSON.stringify({ $case: "resize", resize: { cols: ${TARGET.cols}, rows: ${TARGET.rows} } }));
    };
    shell.onmessage = (event) => send({ type: "shell", payload: event.data });
    shell.onerror = () => status("shell-error");
    shell.onclose = (event) => status("shell-close", { code: event.code, reason: event.reason });
  };

  bridge.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "send" && shell && shell.readyState === WebSocket.OPEN) {
      shell.send(JSON.stringify({ $case: "data", data: { data: message.data } }));
    } else if (message.type === "resize" && shell && shell.readyState === WebSocket.OPEN) {
      shell.send(JSON.stringify({ $case: "resize", resize: message.resize }));
    } else if (message.type === "close") {
      try { shell?.close(); } catch {}
      try { bridge.close(); } catch {}
    }
  };

  bridge.onerror = () => status("bridge-error");
  bridge.onclose = () => { try { shell?.close(); } catch {} };
  window.__SCOW_MCP_BRIDGE__ = bridge;
  console.log("SCOW MCP bridge started; target:", shellUrl);
})();`;
}

function writeBootstrap() {
  fs.writeFileSync(snippetPath, browserSnippet() + "\n", { mode: 0o600 });
  fs.writeFileSync(bootstrapPath, JSON.stringify({
    pid: process.pid,
    host: HOST,
    port: PORT,
    token,
    target: TARGET,
    snippetPath,
    shellUrl: `wss://scow.example.edu/api/shell?${targetQuery()}`,
    startedAt: state.startedAt,
  }, null, 2) + "\n", { mode: 0o600 });
}

function log(...args) {
  process.stderr.write(`[scow-mcp] ${args.join(" ")}\n`);
}

function sendBridge(message) {
  const b = state.browser;
  if (!b || b.readyState !== 1) throw new Error("SCOW browser bridge is not connected. Run the generated bridge snippet in the authenticated SCOW Safari page.");
  b.send(JSON.stringify(message));
}

function decodeShellMessage(raw) {
  try {
    const message = JSON.parse(raw.toString());
    if (message?.$case === "data") return { type: "data", text: message.data?.data || "", raw: message };
    if (message?.$case === "exit") return { type: "exit", text: `\r\n[process exited code=${message.exit?.code ?? "?"}]\r\n`, raw: message };
    return { type: "other", text: "", raw: message };
  } catch {
    return { type: "raw", text: raw.toString(), raw: raw.toString() };
  }
}

function stripAnsi(s) {
  return s
    .replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g, "")
    .replace(/[\u001b\u009b]\[[0-?]*[ -/]*[@-~]/g, "")
    .replace(/\r/g, "");
}

function shellPromptSeen(text) {
  const clean = stripAnsi(text);
  return /(?:^|\n)\[[^\n\]]+@[^\n\]]+ [^\n]*\]\$\s*$/.test(clean);
}

function collectOutput() {
  const out = state.chunks.join("");
  state.chunks.length = 0;
  return out;
}

function waitForCommand(command, timeoutMs) {
  return new Promise((resolve, reject) => {
    const item = { command, resolve, reject, output: "", timer: null };
    item.timer = setTimeout(() => {
      const idx = state.commandQueue.indexOf(item);
      if (idx >= 0) state.commandQueue.splice(idx, 1);
      if (state.activeCommand === item) { state.activeCommand = null; setImmediate(pumpCommands); }
      reject(new Error(`Timed out after ${timeoutMs}ms waiting for the shell prompt. Use scow_read/scow_send to continue an interactive command.`));
    }, timeoutMs);
    state.commandQueue.push(item);
    pumpCommands();
  });
}

function pumpCommands() {
  if (state.activeCommand || state.commandQueue.length === 0) return;
  const item = state.commandQueue.shift();
  state.activeCommand = item;
  try {
    sendBridge({ type: "send", data: `${item.command}\r` });
  } catch (error) {
    clearTimeout(item.timer);
    state.activeCommand = null;
    item.reject(error);
  }
}

function handleShellText(text) {
  state.chunks.push(text);
  if (state.chunks.join("").length > 1000000) state.chunks = [state.chunks.join("").slice(-500000)];
  const item = state.activeCommand;
  if (!item) return;
  item.output += text;
  if (shellPromptSeen(item.output)) {
    clearTimeout(item.timer);
    state.activeCommand = null;
    // Remove command echo and the final prompt only for readability.
    let clean = stripAnsi(item.output);
    clean = clean.replace(/^\s*[^\n]*\r?\n?/, "");
    clean = clean.replace(/\n?\[[^\n\]]+@[^\n\]]+ [^\n]*\]\$\s*$/, "");
    item.resolve(clean.trimEnd());
    setImmediate(pumpCommands);
  }
}

function result(text, isError = false) {
  return {
    content: [{ type: "text", text: String(text) }],
    ...(isError ? { isError: true } : {}),
  };
}

async function callTool(name, args = {}) {
  switch (name) {
    case "scow_status":
      return result(JSON.stringify({
        browserBridge: state.browserStatus,
        shell: state.lastShellStatus,
        target: TARGET,
        bootstrapFile: bootstrapPath,
        snippetFile: snippetPath,
      }, null, 2));
    case "scow_exec": {
      const command = String(args.command || "");
      if (!command) return result("command is required", true);
      const timeoutMs = Math.min(Math.max(Number(args.timeout_ms || 15000), 1000), 120000);
      const output = await waitForCommand(command, timeoutMs);
      return result(output || "(no output)");
    }
    case "scow_send": {
      const data = String(args.data ?? "");
      sendBridge({ type: "send", data });
      return result("sent");
    }
    case "scow_read": {
      const waitMs = Math.min(Math.max(Number(args.wait_ms || 500), 0), 30000);
      if (waitMs) await new Promise((r) => setTimeout(r, waitMs));
      const output = collectOutput();
      return result(output || "(no new output)");
    }
    case "scow_resize": {
      const cols = Math.max(20, Number(args.cols || TARGET.cols));
      const rows = Math.max(5, Number(args.rows || TARGET.rows));
      sendBridge({ type: "resize", resize: { cols, rows } });
      return result(`resized to ${cols}x${rows}`);
    }
    case "scow_disconnect":
      sendBridge({ type: "close" });
      return result("disconnect requested");
    default:
      return result(`unknown tool: ${name}`, true);
  }
}

const tools = [
  {
    name: "scow_status",
    description: "Show whether the authenticated Safari SCOW WebShell bridge is connected.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "scow_exec",
    description: "Run a non-interactive shell command in the authenticated SCOW WebShell and wait for the shell prompt.",
    inputSchema: {
      type: "object",
      properties: {
        command: { type: "string", description: "Shell command to run." },
        timeout_ms: { type: "integer", minimum: 1000, maximum: 120000, default: 15000 },
      },
      required: ["command"],
      additionalProperties: false,
    },
  },
  {
    name: "scow_send",
    description: "Send raw terminal input to SCOW. Use this for interactive programs or control bytes such as Ctrl-C (\\u0003).",
    inputSchema: {
      type: "object",
      properties: { data: { type: "string" } },
      required: ["data"],
      additionalProperties: false,
    },
  },
  {
    name: "scow_read",
    description: "Read terminal output received since the previous read.",
    inputSchema: {
      type: "object",
      properties: { wait_ms: { type: "integer", minimum: 0, maximum: 30000, default: 500 } },
      additionalProperties: false,
    },
  },
  {
    name: "scow_resize",
    description: "Change the remote terminal size.",
    inputSchema: {
      type: "object",
      properties: {
        cols: { type: "integer", minimum: 20 },
        rows: { type: "integer", minimum: 5 },
      },
      required: ["cols", "rows"],
      additionalProperties: false,
    },
  },
  {
    name: "scow_disconnect",
    description: "Close the SCOW browser bridge and WebShell connection.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
];

const wss = new WebSocketServer({ host: HOST, port: PORT });
wss.on("connection", (socket, request) => {
  const url = new URL(request.url || "/", `ws://${HOST}:${PORT}`);
  if (url.searchParams.get("token") !== token) {
    socket.close(1008, "invalid token");
    return;
  }
  if (state.browser && state.browser.readyState === 1) state.browser.close();
  state.browser = socket;
  state.browserStatus = "bridge connected";
  socket.on("message", (raw) => {
    let msg;
    try { msg = JSON.parse(raw.toString()); } catch { return; }
    if (msg.type === "status") {
      state.lastShellStatus = msg.value;
      if (msg.value === "shell-open") log("SCOW WSS shell connected");
    } else if (msg.type === "shell") {
      const parsed = decodeShellMessage(msg.payload);
      if (parsed.type === "data" || parsed.type === "exit") handleShellText(parsed.text);
    }
  });
  socket.on("close", () => {
    if (state.browser === socket) {
      state.browser = null;
      state.browserStatus = "bridge disconnected";
    }
  });
  socket.on("error", () => {});
  log("browser bridge connected");
});
wss.on("listening", () => {
  writeBootstrap();
  log(`bridge listening at ws://${HOST}:${PORT}`);
  log(`run the generated snippet in Safari: ${snippetPath}`);
});
wss.on("error", (error) => {
  log(`bridge error: ${error.message}`);
  process.exitCode = 1;
});

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", async (line) => {
  if (!line.trim()) return;
  let request;
  try { request = JSON.parse(line); } catch { return; }
  const reply = (body) => process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: request.id, ...body }) + "\n");
  try {
    switch (request.method) {
      case "initialize":
        reply({ result: {
          protocolVersion: request.params?.protocolVersion || "2024-11-05",
          capabilities: { tools: {} },
          serverInfo: { name: "scow-webshell", version: "0.1.0" },
        }});
        break;
      case "notifications/initialized":
        break;
      case "ping":
        reply({ result: {} });
        break;
      case "tools/list":
        reply({ result: { tools } });
        break;
      case "tools/call":
        reply({ result: await callTool(request.params?.name, request.params?.arguments || {}) });
        break;
      default:
        if (request.id !== undefined) reply({ error: { code: -32601, message: `Method not found: ${request.method}` } });
    }
  } catch (error) {
    if (request.id !== undefined) reply({ result: result(error?.message || String(error), true) });
  }
});

process.on("SIGINT", () => {
  try { fs.unlinkSync(bootstrapPath); } catch {}
  try { wss.close(); } catch {}
  process.exit(0);
});
