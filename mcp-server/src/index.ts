// Job Radar MCP server: a Cloudflare Worker that speaks the Model Context Protocol
// (JSON-RPC 2.0 over Streamable HTTP) so Claude can use Job Radar as a connector.
//
//   POST https://<worker>/mcp/<MCP_SECRET>       the connector URL you give Claude
//   POST https://<worker>/mcp  + Authorization: Bearer <MCP_SECRET>   for other MCP clients
//
// Anything under /mcp without the secret gets 401 (what MCP clients expect before they send
// credentials); every other path is a plain 404.
// The server is stateless: every request stands alone, and all data lives in the GitHub repo.

import { Env, GitHub, GitHubError } from "./github";
import { TOOLS, ToolError } from "./tools";

const SERVER = { name: "job-radar", title: "Job Radar", version: "1.0.0" };
const PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26"];
const MIN_SECRET_LENGTH = 32;
const MAX_BODY_BYTES = 300_000;

const INSTRUCTIONS =
  "Job Radar is the candidate's personal job tracker. Free code finds and pre-filters jobs; you do the last mile. " +
  "Start with get_briefing and follow its instructions.workflow: score everything in get_jobs_to_score with one " +
  "submit_scores call, wait a few minutes, then write a kit for each job in get_strong_matches with " +
  "submit_application_kit. Never invent experience, numbers, tools or employers that aren't in the resume.";

type Id = string | number | null;
interface RpcRequest {
  jsonrpc: "2.0";
  id?: Id;
  method: string;
  params?: Record<string, unknown>;
}

// Compare secrets in constant time so response timing can't leak the key one character at a time.
export function safeEqual(a: string, b: string): boolean {
  const x = new TextEncoder().encode(a);
  const y = new TextEncoder().encode(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}

function authorized(request: Request, url: URL, env: Env): boolean {
  const secret = env.MCP_SECRET || "";
  if (secret.length < MIN_SECRET_LENGTH) return false; // never run with a weak or missing key
  const fromPath = url.pathname.startsWith("/mcp/") ? decodeURIComponent(url.pathname.slice(5)).trim() : "";
  // Accept "Bearer <key>" (any capitalization), or the bare key, in Authorization or X-API-Key.
  const header = (request.headers.get("Authorization") || request.headers.get("X-API-Key") || "").trim();
  const fromHeader = header.replace(/^bearer\s+/i, "").trim();
  const candidate = fromPath || (url.pathname.replace(/\/+$/, "") === "/mcp" ? fromHeader : "");
  return safeEqual(candidate, secret);
}

const json = (body: unknown, status = 200, extra: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...extra } });

const notFound = () => new Response("Not found", { status: 404 });

const unauthorized = () =>
  json({ jsonrpc: "2.0", id: null, error: { code: -32001, message: "Unauthorized" } }, 401, {
    "WWW-Authenticate": 'Bearer realm="job-radar"',
  });

const rpcError = (id: Id, code: number, message: string) => ({ jsonrpc: "2.0", id, error: { code, message } });

async function handle(msg: RpcRequest, env: Env): Promise<unknown | null> {
  const id = msg.id ?? null;
  const isNotification = msg.id === undefined;
  if (msg.jsonrpc !== "2.0" || typeof msg.method !== "string") {
    return isNotification ? null : rpcError(id, -32600, "Invalid request");
  }
  switch (msg.method) {
    case "initialize": {
      const asked = String(msg.params?.protocolVersion ?? "");
      return {
        jsonrpc: "2.0",
        id,
        result: {
          protocolVersion: PROTOCOL_VERSIONS.includes(asked) ? asked : PROTOCOL_VERSIONS[0],
          capabilities: { tools: { listChanged: false } },
          serverInfo: SERVER,
          instructions: INSTRUCTIONS,
        },
      };
    }
    case "ping":
      return { jsonrpc: "2.0", id, result: {} };
    case "tools/list":
      return {
        jsonrpc: "2.0",
        id,
        result: {
          tools: TOOLS.map(({ name, title, description, inputSchema, annotations }) => ({
            name, title, description, inputSchema, annotations,
          })),
        },
      };
    case "tools/call": {
      const name = String(msg.params?.name ?? "");
      const tool = TOOLS.find((t) => t.name === name);
      if (!tool) return rpcError(id, -32602, `Unknown tool: ${name}`);
      const args = (msg.params?.arguments ?? {}) as Record<string, unknown>;
      if (typeof args !== "object" || args === null || Array.isArray(args)) {
        return rpcError(id, -32602, "Tool arguments must be an object");
      }
      try {
        const result = await tool.run(new GitHub(env), args);
        return {
          jsonrpc: "2.0",
          id,
          result: { content: [{ type: "text", text: JSON.stringify(result, null, 1) }], structuredContent: result },
        };
      } catch (e) {
        // Tool problems go back to Claude as a readable result it can act on, not a protocol error.
        let text = "Something went wrong on Job Radar's side. Try again in a minute.";
        if (e instanceof ToolError) text = e.message;
        else if (e instanceof GitHubError && e.status === 404) text = "Job Radar's data isn't there yet. Has the daily scan run?";
        else if (e instanceof GitHubError && (e.status === 401 || e.status === 403)) text = "Job Radar's GitHub token was refused. It may have expired.";
        else console.error(`tool ${name} failed:`, e instanceof Error ? e.message : e);
        return { jsonrpc: "2.0", id, result: { content: [{ type: "text", text }], isError: true } };
      }
    }
    default:
      if (isNotification) return null; // e.g. notifications/initialized: nothing to answer
      return rpcError(id, -32601, `Method not found: ${msg.method}`);
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/" || url.pathname === "/health") {
      return new Response("ok"); // lets you check the Worker is up without revealing anything
    }
    if (url.pathname !== "/mcp" && !url.pathname.startsWith("/mcp/")) return notFound();
    if (!authorized(request, url, env)) return unauthorized();

    if (request.method !== "POST") {
      // Stateless server: no server-to-client stream to open, no session to delete.
      return new Response("Method not allowed", { status: 405, headers: { Allow: "POST" } });
    }
    const raw = await request.text();
    if (raw.length > MAX_BODY_BYTES) return json(rpcError(null, -32600, "Request too large"), 413);

    let body: unknown;
    try {
      body = JSON.parse(raw);
    } catch {
      return json(rpcError(null, -32700, "Parse error"), 400);
    }
    const messages = (Array.isArray(body) ? body : [body]) as RpcRequest[];
    const replies = (await Promise.all(messages.map((m) => handle(m, env)))).filter((r) => r !== null);
    if (replies.length === 0) return new Response(null, { status: 202 });
    return json(Array.isArray(body) ? replies : replies[0]);
  },
};
