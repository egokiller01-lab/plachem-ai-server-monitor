import readline from "node:readline";
import { GatewayClient } from "file:///home/plachem-sever/.npm-global/lib/node_modules/openclaw/dist/plugin-sdk/gateway-runtime.js";
import { loadConfig } from "file:///home/plachem-sever/.npm-global/lib/node_modules/openclaw/dist/plugin-sdk/config-runtime.js";

const allowed = new Set(["bridge.status", "sessions.create", "sessions.resolve", "chat.history", "chat.send", "agent.wait", "chat.abort"]);
const cfg = loadConfig();
const port = cfg.gateway?.port ?? 18789;
const token = typeof cfg.gateway?.auth?.token === "string" ? cfg.gateway.auth.token : process.env.OPENCLAW_GATEWAY_TOKEN;
let connectionId = null;
let sequence = 0;
let readyResolve;
let readyReject;
const ready = new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject; });
const client = new GatewayClient({
  url: `ws://127.0.0.1:${port}`,
  token,
  clientName: "gateway-client",
  clientDisplayName: "PLACHEM War Room persistent adapter",
  mode: "backend",
  role: "operator",
  scopes: ["operator.read", "operator.write"],
  onHelloOk: () => { connectionId = `connection-${++sequence}`; readyResolve(); },
  onConnectError: (error) => readyReject(error),
  onClose: () => { connectionId = null; },
});
client.start();

function testKey(params) {
  return params?.sessionKey ?? params?.key ?? "";
}

await ready;
process.stdout.write(JSON.stringify({ ready: true, connectionId }) + "\n");
const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of lines) {
  let request;
  try {
    request = JSON.parse(line);
    if (!allowed.has(request.method)) throw new Error("method_not_allowed");
    const key = testKey(request.params);
    if (key && !/^agent:[a-z0-9_-]+:war-room-test:[a-z0-9-]+$/i.test(key)) throw new Error("non_disposable_session_rejected");
    if (!connectionId) throw new Error("gateway_connection_unavailable");
    const responseConnectionId = connectionId;
    const result = request.method === "bridge.status"
      ? { connected: true }
      : await client.request(request.method, request.params, { timeoutMs: request.timeoutMs ?? 15000 });
    process.stdout.write(JSON.stringify({ id: request.id, ok: true, result, connectionId: responseConnectionId }) + "\n");
  } catch (error) {
    process.stdout.write(JSON.stringify({ id: request?.id, ok: false, error: String(error?.message ?? error), connectionId }) + "\n");
  }
}
await client.stopAndWait().catch(() => client.stop());
