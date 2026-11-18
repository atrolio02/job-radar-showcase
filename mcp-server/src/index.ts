// Job Radar MCP server: a Cloudflare Worker that speaks the Model Context Protocol
// (JSON-RPC 2.0 over Streamable HTTP) so Claude can use Job Radar as a connector.
//
//   POST https://<worker>/mcp/<MCP_SECRET>       the connector URL you give Claude
//   POST https://<worker>/mcp  + Authorization: Bearer <MCP_SECRET>   for other MCP clients
//
//   GET  https://<worker>/move/<card>/<status>/<signature>   the dashboard's one-tap Move links
//   GET  https://<worker>/promote/<job id>/<signature>        "➕ Move to active" for jobs without a card
//   GET  https://<worker>/batch/<signature>   a page to tick several jobs and move them together (POST saves)
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

// ── Dashboard Move links ──────────────────────────────────────────────────
// Each link is signed with HMAC-SHA256(MCP_SECRET, "move:<card>:<status>"), so it can only do the one
// move it was made for, and the secret itself never appears in the dashboard.
export const MOVE_STATUSES = ["applied", "interviewing", "offer", "rejected", "skip", "active"];

async function sign(secret: string, message: string): Promise<string> {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" },
    false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 32);
}

export const moveSignature = (secret: string, card: number, status: string) => sign(secret, `move:${card}:${status}`);
export const promoteSignature = (secret: string, jobId: string) => sign(secret, `promote:${jobId}`);
export const batchSignature = (secret: string) => sign(secret, "batch");

const page = (title: string, text: string, status = 200) =>
  new Response(
    `<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>${title}</title>` +
      `<body style="font-family:system-ui;max-width:32rem;margin:3rem auto;padding:0 1rem;line-height:1.5">` +
      `<h2>${title}</h2><p>${text}</p></body>`,
    { status, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
                         "X-Robots-Tag": "noindex" } },
  );

const escape = (t: string) => t.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

async function moveLink(url: URL, env: Env): Promise<Response> {
  const m = url.pathname.match(/^\/move\/(\d{1,7})\/([a-z]+)\/([0-9a-f]{32})$/);
  const secret = env.MCP_SECRET || "";
  if (!m || secret.length < MIN_SECRET_LENGTH || !MOVE_STATUSES.includes(m[2])) return notFound();
  const card = Number(m[1]);
  if (!safeEqual(m[3], await moveSignature(secret, card, m[2]))) return notFound();
  const repo = `https://github.com/${env.GITHUB_REPO}`;
  try {
    await new GitHub(env).moveCard(card, m[2]);
  } catch (e) {
    console.error("move failed:", e instanceof Error ? e.message : e);
    return page("Couldn't move it", `GitHub didn't accept the change. Try again in a minute, or use the card: ` +
      `<a href="${repo}/issues/${card}">#${card}</a>.`, 502);
  }
  const where = m[2] === "active" ? "Active" : ["rejected", "skip"].includes(m[2]) ? "Closed out" : "Applied";
  return page(`✅ Moved #${card} to ${escape(m[2])}`,
    `It shows under <b>${where}</b> on the <a href="${repo}">dashboard</a> in about a minute. You can close this tab.`);
}

async function promoteLink(url: URL, env: Env): Promise<Response> {
  const m = url.pathname.match(/^\/promote\/(J-[0-9a-f]{6})\/([0-9a-f]{32})$/);
  const secret = env.MCP_SECRET || "";
  if (!m || secret.length < MIN_SECRET_LENGTH) return notFound();
  if (!safeEqual(m[2], await promoteSignature(secret, m[1]))) return notFound();
  const repo = `https://github.com/${env.GITHUB_REPO}`;
  try {
    await new GitHub(env).startWorkflow("promote.yml", { job: m[1] });
  } catch (e) {
    console.error("promote failed:", e instanceof Error ? e.message : e);
    return page("Couldn't move it", "GitHub didn't accept the request. Try again in a minute.", 502);
  }
  return page(`✅ Moving ${m[1]} to Active`,
    `Job Radar is opening its card with your standard resume. It shows under <b>Active</b> on the ` +
    `<a href="${repo}">dashboard</a> in about a minute. You can close this tab.`);
}

// ── Batch page: tick several jobs, pick where they go ─────────────────────
// Cloudflare's free plan allows 50 GitHub calls per request and each card move takes up to 3,
// so one save moves at most 15 cards (plus any number of jobs without a card, in one call).
export const BATCH_MAX = 15;
const DONE = ["rejected", "skip", "closed"];
const APPLIED = ["applied", "interviewing", "offer"];

async function shortId(key: string): Promise<string> {
  const hash = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(key));
  return "J-" + [...new Uint8Array(hash)].slice(0, 3).map((b) => b.toString(16).padStart(2, "0")).join("");
}

type JobRec = { key?: string; company?: string; title?: string; score?: number; status?: string; issue_number?: number };

async function batchPage(env: Env, sig: string, note = ""): Promise<Response> {
  const data = await new GitHub(env).readJson<{ jobs: Record<string, JobRec> }>("data/jobs.json");
  const jobs = Object.entries(data.jobs).map(([key, j]) => ({ ...j, key }));
  const row = async (j: JobRec & { key: string }) => {
    const id = j.issue_number ? `#${j.issue_number}` : await shortId(j.key);
    return `<label><input type="checkbox" name="pick" value="${id}"><b>${id}</b> · ${j.score ?? "–"} · ` +
      `${escape(j.title || "")} <span>${escape(j.company || "")}${j.status && j.status !== "new" ? " · " + escape(j.status) : ""}</span></label>`;
  };
  const section = async (title: string, list: (JobRec & { key: string })[], hint = "") =>
    `<h3>${title} (${list.length})</h3>${hint}` +
    (list.length ? (await Promise.all(list.map(row))).join("") : "<p><i>Nothing here.</i></p>");
  const byScore = (a: JobRec, b: JobRec) => (b.score ?? 0) - (a.score ?? 0);
  const active = jobs.filter((j) => j.status === "new").sort(byScore);
  const applied = jobs.filter((j) => APPLIED.includes(j.status || "")).sort(byScore);
  const closed = jobs.filter((j) => DONE.includes(j.status || "")).sort(byScore);
  const lower = jobs.filter((j) => !j.status && typeof j.score === "number").sort(byScore).slice(0, 50);
  const btn = (to: string, label: string) => `<button name="to" value="${to}">${label}</button>`;
  const html =
    `<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Move jobs</title>` +
    `<style>body{font-family:system-ui;max-width:42rem;margin:0 auto;padding:1rem 1rem 7rem;line-height:1.4}` +
    `label{display:block;padding:.55rem .3rem;border-bottom:1px solid #ddd}label span{color:#666}` +
    `input{margin-right:.5rem;transform:scale(1.3)}.bar{position:fixed;left:0;right:0;bottom:0;background:#fff;` +
    `border-top:1px solid #ccc;padding:.6rem;display:flex;flex-wrap:wrap;gap:.4rem;justify-content:center}` +
    `button{font-size:1rem;padding:.5rem .7rem;border-radius:.5rem;border:1px solid #888;background:#f5f5f5}` +
    `.note{background:#e8f5e9;padding:.6rem;border-radius:.5rem}</style>` +
    `<h2>☑️ Move several jobs</h2>${note ? `<p class="note">${note}</p>` : ""}` +
    `<p>Tick the jobs, then tap where they go. Up to ${BATCH_MAX} cards per tap.</p>` +
    `<form method="post">` +
    (await section("Active", active)) + (await section("Applied", applied)) + (await section("Closed out", closed)) +
    (await section("Lower-scoring (no card yet)", lower,
      "<p><small>These can only go to Active (they get a card with your standard resume).</small></p>")) +
    `<div class="bar">${btn("applied", "📨 Applied")}${btn("interviewing", "🗣️ Interviewing")}${btn("offer", "🎉 Offer")}` +
    `${btn("rejected", "✖️ Rejected")}${btn("skip", "⏭️ Skip")}${btn("active", "↩️ Active")}</div></form>`;
  return new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex" } });
}

async function batchSave(request: Request, env: Env, sig: string): Promise<Response> {
  const form = await request.formData();
  const to = String(form.get("to") || "");
  const picks = [...new Set(form.getAll("pick").map(String))];
  if (!MOVE_STATUSES.includes(to)) return batchPage(env, sig, "Pick where the jobs should go.");
  if (!picks.length) return batchPage(env, sig, "Tick at least one job first.");
  const gh = new GitHub(env);
  const data = await gh.readJson<{ jobs: Record<string, JobRec> }>("data/jobs.json");
  const cards = new Set(Object.values(data.jobs).map((j) => j.issue_number).filter(Boolean));
  const cardPicks = picks.filter((p) => /^#\d+$/.test(p)).map((p) => Number(p.slice(1))).filter((n) => cards.has(n));
  const promotePicks = picks.filter((p) => /^J-[0-9a-f]{6}$/.test(p));
  const moved: string[] = [], skipped: string[] = [];
  for (const n of cardPicks.slice(0, BATCH_MAX)) {
    try {
      await gh.moveCard(n, to);
      moved.push(`#${n}`);
    } catch {
      skipped.push(`#${n}`);
    }
  }
  skipped.push(...cardPicks.slice(BATCH_MAX).map((n) => `#${n}`));
  if (promotePicks.length && to === "active") {
    await gh.startWorkflow("promote.yml", { job: promotePicks.join(",") });
    moved.push(...promotePicks);
  } else {
    skipped.push(...promotePicks);
  }
  let note = moved.length ? `✅ Moved ${moved.join(", ")} to <b>${escape(to)}</b>. The dashboard updates in about a minute.` : "";
  if (skipped.length) {
    note += ` Not moved: ${skipped.join(", ")}` +
      (promotePicks.length && to !== "active" ? " (jobs without a card can only go to Active)" : "") +
      (cardPicks.length > BATCH_MAX ? ` (${BATCH_MAX} cards per tap; tap again for the rest)` : "") + ".";
  }
  return batchPage(env, sig, note.trim());
}

async function batch(request: Request, url: URL, env: Env): Promise<Response> {
  const m = url.pathname.match(/^\/batch\/([0-9a-f]{32})$/);
  const secret = env.MCP_SECRET || "";
  if (!m || secret.length < MIN_SECRET_LENGTH || !safeEqual(m[1], await batchSignature(secret))) return notFound();
  try {
    return request.method === "POST" ? await batchSave(request, env, m[1]) : await batchPage(env, m[1]);
  } catch (e) {
    console.error("batch failed:", e instanceof Error ? e.message : e);
    return page("Couldn't load your jobs", "GitHub didn't answer. Try again in a minute.", 502);
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/" || url.pathname === "/health") {
      return new Response("ok"); // lets you check the Worker is up without revealing anything
    }
    if (url.pathname.startsWith("/move/") && request.method === "GET") return moveLink(url, env);
    if (url.pathname.startsWith("/promote/") && request.method === "GET") return promoteLink(url, env);
    if (url.pathname.startsWith("/batch/") && (request.method === "GET" || request.method === "POST")) {
      return batch(request, url, env);
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
