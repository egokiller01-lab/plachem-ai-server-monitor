import readline from "node:readline";

const port = process.env.OPENCLAW_GATEWAY_PORT || "18789";
const token = process.env.OPENCLAW_GATEWAY_TOKEN;
if (!token) throw new Error("openclaw_gateway_token_missing");

const socket = new WebSocket(`ws://127.0.0.1:${port}`);
let nextId = 0;
let connectionId = null;
const pending = new Map();

function frame(id, method, params) {
  return JSON.stringify({ type: "req", id, method, params });
}

function request(method, params, timeoutMs = 15000) {
  const id = `jev-recovery-abort-${++nextId}`;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error("openclaw_gateway_timeout"));
    }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    socket.send(frame(id, method, params));
  });
}

socket.addEventListener("message", (event) => {
  let value;
  try { value = JSON.parse(String(event.data)); } catch { return; }
  if (value?.type !== "res") return;
  const waiter = pending.get(value.id);
  if (!waiter) return;
  pending.delete(value.id);
  clearTimeout(waiter.timer);
  if (value.ok === true && value.payload && typeof value.payload === "object") {
    waiter.resolve(value.payload);
  } else {
    const rawCode = String(value?.error?.code || "REJECTED");
    const safeCode = rawCode.replace(/[^a-zA-Z0-9_]/g, "_").slice(0, 64) || "REJECTED";
    const rawMessage = String(value?.error?.message || "");
    const safeMessage = rawMessage.replace(/[^a-zA-Z0-9 _:\-.]/g, "_").slice(0, 120);
    waiter.reject(new Error(`gateway_${safeCode}${safeMessage ? "__" + safeMessage : ""}`));
  }
});

socket.addEventListener("error", () => {
  for (const waiter of pending.values()) {
    clearTimeout(waiter.timer);
    waiter.reject(new Error("openclaw_gateway_transport_failure"));
  }
  pending.clear();
});

await new Promise((resolve, reject) => {
  socket.addEventListener("open", resolve, { once: true });
  socket.addEventListener("error", () => reject(new Error("openclaw_gateway_transport_failure")), { once: true });
});

const hello = await request("connect", {
  minProtocol: 4,
  maxProtocol: 4,
  client: { id: "gateway-client", version: "plachem-jev-recovery-abort", platform: "linux", mode: "backend" },
  role: "operator",
  scopes: ["operator.read", "operator.write", "operator.admin"],
  auth: { token },
});
connectionId = String(hello?.connectionId || "connected");
process.stdout.write(JSON.stringify({ ready: true, connectionId }) + "\n");

const protectedKey = /:(telegram|cron|fast-gateway|whatsapp|slack|email|discord|signal|imessage|feishu|matrix|line|zalo|sms):/i;
const sessionPattern = /^agent:([a-z0-9_-]+):(.+)$/i;
const runPattern = /^[a-z0-9][a-z0-9-]{7,127}$/i;

const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of lines) {
  let input;
  try {
    input = JSON.parse(line);
    if (input.method !== "sessions.abort") throw new Error("method_not_allowed");
    const params = input.params || {};
    const key = String(params.key || "");
    const agentId = String(params.agentId || "").toLowerCase();
    const runId = String(params.runId || "");
    const match = key.match(sessionPattern);
    if (!match) throw new Error("invalid_session_key");
    if (!agentId || match[1].toLowerCase() !== agentId) throw new Error("agent_mismatch");
    if (agentId === "main") throw new Error("main_agent_protected");
    if (protectedKey.test(key)) throw new Error("protected_session_key");
    if (!runPattern.test(runId)) throw new Error("run_id_required");
    const result = await request("sessions.abort", { key, agentId, runId }, input.timeoutMs || 15000);
    process.stdout.write(JSON.stringify({ id: input.id, ok: true, result, connectionId }) + "\n");
  } catch (error) {
    const message = String(error?.message || "jev_recovery_abort_rejected");
    const safe = /^gateway_[a-zA-Z0-9_]+(?:__[a-zA-Z0-9 _:\-.]{1,120})?$/.test(message) ? message : "jev_recovery_abort_rejected";
    process.stdout.write(JSON.stringify({ id: input?.id, ok: false, error: safe, connectionId }) + "\n");
  }
}
socket.close();
