import readline from "node:readline";

// This process is deliberately a tiny raw WebSocket RPC client.  The token is
// injected by the service's SecretRef handling and is never read from output
// or logged here.  Do not replace this with an OpenClaw bundled/dist import.
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
  const id = `war-room-${++nextId}`;
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
  if (value.ok === true && value.payload && typeof value.payload === "object") waiter.resolve(value.payload);
  else waiter.reject(new Error(String(value.error?.message || value.error || "openclaw_gateway_rejected")));
});

socket.addEventListener("error", () => {
  for (const waiter of pending.values()) { clearTimeout(waiter.timer); waiter.reject(new Error("openclaw_gateway_transport_failure")); }
  pending.clear();
});

await new Promise((resolve, reject) => {
  socket.addEventListener("open", resolve, { once: true });
  socket.addEventListener("error", () => reject(new Error("openclaw_gateway_transport_failure")), { once: true });
});
const hello = await request("connect", {
  minProtocol: 4,
  maxProtocol: 4,
  client: { id: "gateway-client", version: "plachem-war-room", platform: "linux", mode: "backend" },
  role: "operator",
  scopes: ["operator.read", "operator.write"],
  auth: { token },
});
connectionId = String(hello?.connectionId || "connected");
process.stdout.write(JSON.stringify({ ready: true, connectionId }) + "\n");

const allowed = new Set(["agent", "agent.wait", "chat.history", "chat.abort", "sessions.create", "bridge.status"]);
const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of lines) {
  let input;
  try {
    input = JSON.parse(line);
    if (!allowed.has(input.method)) throw new Error("method_not_allowed");
    const key = input.params?.sessionKey || input.params?.key || "";
    if (key && !/^agent:[a-z0-9_-]+:war-room-test:[a-z0-9-]+$/i.test(key)) throw new Error("non_disposable_session_rejected");
    const result = input.method === "bridge.status" ? { connected: socket.readyState === WebSocket.OPEN } : await request(input.method, input.params || {}, input.timeoutMs || 15000);
    process.stdout.write(JSON.stringify({ id: input.id, ok: true, result, connectionId }) + "\n");
  } catch (error) {
    // Never relay Gateway error text: a provider or auth layer must not be
    // able to echo the SecretRef value into the monitor process or its logs.
    process.stdout.write(JSON.stringify({ id: input?.id, ok: false, error: "openclaw_gateway_rejected", connectionId }) + "\n");
  }
}
socket.close();
