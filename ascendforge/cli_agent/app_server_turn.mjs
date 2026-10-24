// Minimal Codex app-server websocket turn.
// Emits JSONL records on stdout so the Python host can persist/stream them.
// This is a transport bridge, not an optimization policy.
const args = Object.fromEntries(process.argv.slice(2).map((item) => {
  const i = item.indexOf("=");
  return i < 0 ? [item, "1"] : [item.slice(0, i), item.slice(i + 1)];
}));
const port = args.port;
const cwd = args.cwd;
const prompt = args.prompt || "";
const threadId = args.thread || "";
const model = args.model || "";
const timeoutMs = Number(args.timeout_ms || 1800000);

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}
function fail(message) {
  emit({ type: "bridge.error", message: String(message || "unknown") });
  process.exit(2);
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
}

ws.onopen = () => {
  send(1, "initialize", { clientInfo: { name: "AscendForge", version: "0.1" } });
};

ws.onmessage = (event) => {
  let obj;
  try { obj = JSON.parse(String(event.data)); } catch (_) { return; }
  if (obj.id === 1 && obj.result) {
    if (activeThread) {
      send(2, "thread/resume", { threadId: activeThread, cwd });
    } else {
      send(2, "thread/start", { cwd });
    }
    return;
  }
  if (obj.id === 2 && obj.result && obj.result.thread) {
    activeThread = obj.result.thread.id;
    emit({ type: "thread.started", thread_id: activeThread, source: obj.result.thread.source || null });
    startTurn();
    return;
  }
  if (obj.id === 3 && obj.result && obj.result.turn) {
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
    emit({ type: "bridge.error", error: obj.error || obj.params || obj });
  }
};

ws.onerror = () => fail("websocket connection failed");
ws.onclose = () => {
  if (!completed) fail("websocket closed before turn completed");
};
