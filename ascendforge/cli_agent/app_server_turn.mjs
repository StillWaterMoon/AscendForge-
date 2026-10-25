// Minimal Codex app-server websocket turn.
// Emits JSONL records on stdout so the Python host can persist/stream them.
// This is a transport bridge, not an optimization policy.
const args = Object.fromEntries(process.argv.slice(2).map((item) => {
  const i = item.indexOf("=");
  return i < 0 ? [item, "1"] : [item.slice(0, i), item.slice(i + 1)];
}));
const port = args.port;
const cwd = args.cwd;
const promptFile = args.prompt_file || "";
const fs = await import("node:fs");
const prompt = promptFile
  ? fs.readFileSync(promptFile, "utf8")
  : (args.prompt || "");
const threadId = args.thread || "";
const threadName = args.thread_name || "";
const model = args.model || "";
const toolsFile = args.tools_file || "";
const readline = await import("node:readline");
const timeoutMs = Number(args.timeout_ms || 1800000);
const dynamicTools = toolsFile && fs.existsSync(toolsFile)
  ? JSON.parse(fs.readFileSync(toolsFile, "utf8"))
  : [];
const pendingTools = new Map();
let threadStartTimer = null;
let turnStartTimer = null;
let fatalError = null;

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}
function fail(message) {
  if (fatalError) {
    emit({ type: "bridge.transport_error", message: String(message || "unknown"), after: fatalError.code });
    return;
  }
  emit({ type: "bridge.error", fatal: true, message: String(message || "unknown") });
  process.exit(2);
}

function clearStartTimers() {
  if (threadStartTimer) clearTimeout(threadStartTimer);
  if (turnStartTimer) clearTimeout(turnStartTimer);
  threadStartTimer = null;
  turnStartTimer = null;
}

function failRpc(obj, stage) {
  const rawError = obj.error;
  const code = rawError && typeof rawError === "object" ? rawError.code : null;
  const message = rawError && typeof rawError === "object"
    ? String(rawError.message || JSON.stringify(rawError))
    : String(rawError || obj.message || "request failed");
  fatalError = { code, message, stage, request_id: obj.id, thread_id: activeThread };
  clearStartTimers();
  // Emit the first real failure and stop this request immediately. Do not wait
  // for thread_start_timeout or let a later websocket close overwrite it.
  emit({ type: "bridge.rpc_error", fatal: true, ...fatalError });
  try { ws.close(); } catch (_) {}
  setTimeout(() => process.exit(2), 50);
}

const ws = new WebSocket(`ws://127.0.0.1:${port}`);
let activeThread = threadId || null;
let finalText = "";
let usage = {};
let completed = false;
const timer = setTimeout(() => {
  emit({ type: "bridge.timeout", thread_id: activeThread });
  try { ws.close(); } catch (_) {}
  process.exit(3);
}, timeoutMs);

function send(id, method, params) {
  ws.send(JSON.stringify({ id, method, params }));
}

function startTurn() {
  const params = {
    threadId: activeThread,
    cwd,
    input: [{ type: "text", text: prompt }],
    approvalPolicy: "never",
    sandboxPolicy: { type: "workspaceWrite", networkAccess: false },
  };
  if (model) params.model = model;
  send(3, "turn/start", params);
  // 恢复旧 thread 时 app-server 可能不返回 turn；30 秒内没有
  // turn.started/响应就交给 Python 做“新 thread”回退。
  turnStartTimer = setTimeout(() => {
    emit({ type: "bridge.turn_start_timeout", thread_id: activeThread });
    try { ws.close(); } catch (_) {}
    setTimeout(() => process.exit(4), 50);
  }, 30000);
}

ws.onopen = () => {
  send(1, "initialize", {
    clientInfo: { name: "AscendForge", version: "0.1" },
    capabilities: { experimentalApi: true },
  });
};

ws.onmessage = (event) => {
  let obj;
  try { obj = JSON.parse(String(event.data)); } catch (_) { return; }
  if (obj.id === 1 && obj.result) {
    if (activeThread) {
      send(2, "thread/resume", { threadId: activeThread, cwd, dynamicTools });
    } else {
      send(2, "thread/start", { cwd, dynamicTools });
    }
    threadStartTimer = setTimeout(() => {
      emit({ type: "bridge.thread_start_timeout", thread_id: activeThread });
      try { ws.close(); } catch (_) {}
      setTimeout(() => process.exit(4), 50);
    }, 30000);
    return;
  }
  if (obj.error && obj.id != null) {
    const stage = obj.id === 2
      ? (threadId ? "thread_resume" : "thread_start")
      : obj.id === 3 ? "turn_start" : "request";
    if (obj.id === 4) {
      // 会话命名失败不是致命错误；继续执行 turn。
      emit({ type: "bridge.notice", stage: "thread_name_set", error: obj.error });
      return;
    }
    failRpc(obj, stage);
    return;
  }
  if (obj.id === 2 && obj.result && obj.result.thread) {
    if (threadStartTimer) clearTimeout(threadStartTimer);
    threadStartTimer = null;
    activeThread = obj.result.thread.id;
    emit({ type: "thread.started", thread_id: activeThread, source: obj.result.thread.source || null });
    // 会话名称：题目名 + 算子优化，例如 AddN算子优化。
    if (threadName) {
      send(4, "thread/name/set", { threadId: activeThread, name: threadName });
    }
    startTurn();
    return;
  }
  if (obj.id === 3 && obj.result && obj.result.turn) {
    if (turnStartTimer) clearTimeout(turnStartTimer);
    turnStartTimer = null;
    emit({ type: "turn.started", turn_id: obj.result.turn.id });
    return;
  }
  if (obj.method === "item/agentMessage/delta") {
    finalText += String((obj.params || {}).delta || "");
    return;
  }
  if (obj.method === "item/completed") {
    const item = (obj.params || {}).item || {};
    if (item.type === "agentMessage" && item.text) finalText = String(item.text);
    return;
  }
  if (obj.method === "item/tool/call") {
    const params = obj.params || {};
    pendingTools.set(String(obj.id), obj.id);
    emit({
      type: "tool_call",
      request_id: obj.id,
      call_id: params.callId,
      thread_id: params.threadId,
      tool: params.tool,
      arguments: params.arguments,
    });
    return;
  }
  if (obj.method === "thread/tokenUsage/updated") {
    usage = ((obj.params || {}).tokenUsage || {}).total || usage;
    return;
  }
  if (obj.method === "turn/completed") {
    completed = true;
    clearTimeout(timer);
    emit({ type: "turn.completed", thread_id: activeThread, text: finalText, usage });
    try { ws.close(); } catch (_) {}
    setTimeout(() => process.exit(0), 50);
    return;
  }
  if (obj.method === "error" || obj.error) {
    // 无 id 的通知只记录诊断，不能当成整个 turn 的致命错误。
    emit({ type: "bridge.error", fatal: false, error: obj.error || obj.params || obj });
  }
};

readline.createInterface({ input: process.stdin }).on("line", (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch (_) { return; }
  if (msg.type !== "tool_response" || !pendingTools.has(String(msg.request_id))) return;
  const requestId = pendingTools.get(String(msg.request_id));
  pendingTools.delete(String(msg.request_id));
  const success = msg.success !== false;
  const text = typeof msg.result === "string" ? msg.result : JSON.stringify(msg.result ?? null);
  ws.send(JSON.stringify({
    id: requestId,
    result: {
      success,
      contentItems: [{ type: "inputText", text }],
    },
  }));
});

ws.onerror = () => fail("websocket connection failed");
ws.onclose = () => {
  if (fatalError || completed) return;
  fail("websocket closed before turn completed");
};
