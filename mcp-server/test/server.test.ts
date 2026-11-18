// Tests with a fake GitHub API, so nothing real is called.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import worker, { batchSignature, moveSignature, promoteSignature, safeEqual, siteOf } from "../src/index";

const SECRET = "s".repeat(40);
const env = { MCP_SECRET: SECRET, GITHUB_TOKEN: "ghp_test", GITHUB_REPO: "me/job-radar" };

const INBOX = {
  updated: "2026-11-17T13:20:00+00:00",
  to_score: [{ key: "ashby:Northwind:a1", company: "Northwind", title: "Sales Engineer", rules_score: 88, posting: "APIs" }],
  to_score_waiting: 0,
  strong_matches: [{ key: "greenhouse:acme:11", company: "Acme", title: "Solutions Architect", research_needed: true }],
  strong_matches_waiting: 2,
};
const JOBS = {
  jobs: {
    a: { key: "a", company: "Acme", title: "SA", status: "new", issue_number: 7, first_seen: "2026-11-10" },
    b: { key: "b", company: "Beta", title: "SE", status: "applied", issue_number: 9, first_seen: "2026-11-12" },
    c: { key: "c", company: "Gamma", title: "SE" },
    "ashby:Northwind:a1": { key: "ashby:Northwind:a1", company: "Northwind", title: "Sales Engineer", pending_brief: "Full posting text",
      questions: [{ question: "Why Northwind?" }], url: "https://jobs.ashbyhq.com/Northwind/a1", queued_at: "x" },
  },
};
const FILES: Record<string, string> = {
  "data/mcp/inbox.json": JSON.stringify(INBOX),
  "data/mcp/context.json": JSON.stringify({ resume: "Jordan Rivera", alert_min_score: 7, instructions: { workflow: "score, then kits" } }),
  "data/jobs.json": JSON.stringify(JOBS),
  "data/health/REPORT.md": "# Job Radar health",
};

let calls: { method: string; url: string; body?: any; auth?: string }[];
let failWith: number | null;

beforeEach(() => {
  calls = [];
  failWith = null;
  vi.stubGlobal("fetch", async (url: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    const body = init.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, url, body, auth: (init.headers as Record<string, string>)?.Authorization });
    if (failWith) return new Response("{}", { status: failWith });
    const path = decodeURIComponent(new URL(url).pathname.replace("/repos/me/job-radar/contents/", ""));
    if (method === "GET" && FILES[path] !== undefined) return new Response(FILES[path]);
    if (method === "GET" && /\/issues\/\d+$/.test(path)) {
      return new Response(JSON.stringify({ state: "closed", labels: [{ name: "job" }, { name: "skip" }, { name: "resume-x" }] }));
    }
    if (method === "GET") return new Response("{}", { status: 404 });
    return new Response("{}", { status: 201 });
  });
});
afterEach(() => vi.unstubAllGlobals());

async function rpc(body: unknown, path = `/mcp/${SECRET}`, headers: Record<string, string> = {}, e: any = env) {
  const req = new Request(`https://radar.example.workers.dev${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
  return worker.fetch(req, e);
}

async function call(name: string, args: unknown = {}) {
  const resp = await rpc({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } });
  const out = (await resp.json()) as any;
  return out.result as { content: { text: string }[]; structuredContent?: any; isError?: boolean };
}

describe("security", () => {
  it("turns away anyone without the secret", async () => {
    const msg = { jsonrpc: "2.0", id: 1, method: "tools/list" };
    const noKey = await rpc(msg, "/mcp");
    expect(noKey.status).toBe(401); // what MCP clients expect before they send credentials
    expect(noKey.headers.get("WWW-Authenticate")).toMatch(/^Bearer/);
    expect((await rpc(msg, "/mcp/wrong-secret")).status).toBe(401);
    expect((await rpc(msg, "/mcp", { Authorization: "Bearer nope" })).status).toBe(401);
    expect((await rpc(msg, "/other")).status).toBe(404);
    expect((await rpc(msg, "/.well-known/oauth-protected-resource")).status).toBe(404);
    expect(calls).toHaveLength(0); // GitHub never touched
  });

  it("accepts small differences in how the key is sent", async () => {
    const msg = { jsonrpc: "2.0", id: 1, method: "ping" };
    expect((await rpc(msg, "/mcp", { Authorization: `bearer  ${SECRET} ` })).status).toBe(200);
    expect((await rpc(msg, "/mcp", { Authorization: SECRET })).status).toBe(200);
    expect((await rpc(msg, "/mcp/", { Authorization: `Bearer ${SECRET}` })).status).toBe(200);
    expect((await rpc(msg, "/mcp", { "X-API-Key": SECRET })).status).toBe(200);
  });

  it("accepts the secret in the URL or as a bearer token", async () => {
    const msg = { jsonrpc: "2.0", id: 1, method: "ping" };
    expect((await rpc(msg)).status).toBe(200);
    expect((await rpc(msg, "/mcp", { Authorization: `Bearer ${SECRET}` })).status).toBe(200);
  });

  it("refuses to run with a short or missing secret", async () => {
    const msg = { jsonrpc: "2.0", id: 1, method: "ping" };
    expect((await rpc(msg, "/mcp/short", {}, { ...env, MCP_SECRET: "short" })).status).toBe(401);
    expect((await rpc(msg, "/mcp/", {}, { ...env, MCP_SECRET: "" })).status).toBe(401);
  });

  it("compares in constant time and handles different lengths", () => {
    expect(safeEqual("abc", "abc")).toBe(true);
    expect(safeEqual("abc", "abd")).toBe(false);
    expect(safeEqual("abc", "abcd")).toBe(false);
  });

  it("never echoes GitHub's raw errors", async () => {
    failWith = 401;
    const r = await call("get_briefing");
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toMatch(/token was refused/);
    expect(r.content[0].text).not.toMatch(/ghp_|Bearer/);
  });

  it("rejects oversized requests and bad JSON", async () => {
    const big = await worker.fetch(new Request(`https://x.dev/mcp/${SECRET}`, { method: "POST", body: "x".repeat(400_000) }), env);
    expect(big.status).toBe(413);
    const bad = await worker.fetch(new Request(`https://x.dev/mcp/${SECRET}`, { method: "POST", body: "{nope" }), env);
    expect(bad.status).toBe(400);
  });
});

describe("protocol", () => {
  it("initializes with tools and instructions", async () => {
    const r = (await (await rpc({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18" } })).json()) as any;
    expect(r.result.protocolVersion).toBe("2025-06-18");
    expect(r.result.capabilities.tools).toBeDefined();
    expect(r.result.instructions).toMatch(/get_briefing/);
  });

  it("answers notifications with 202 and no body", async () => {
    const r = await rpc({ jsonrpc: "2.0", method: "notifications/initialized" });
    expect(r.status).toBe(202);
  });

  it("lists every tool with a schema", async () => {
    const r = (await (await rpc({ jsonrpc: "2.0", id: 2, method: "tools/list" })).json()) as any;
    const names = r.result.tools.map((t: any) => t.name);
    expect(names).toEqual(["get_briefing", "get_jobs_to_score", "submit_scores", "get_strong_matches",
      "submit_application_kit", "list_tracked_jobs", "update_job_status", "get_health_report", "run_scan"]);
    for (const t of r.result.tools) expect(t.inputSchema.type).toBe("object");
  });

  it("rejects GET (no stream) and unknown methods", async () => {
    const get = await worker.fetch(new Request(`https://x.dev/mcp/${SECRET}`), env);
    expect(get.status).toBe(405);
    const r = (await (await rpc({ jsonrpc: "2.0", id: 3, method: "resources/list" })).json()) as any;
    expect(r.error.code).toBe(-32601);
  });
});

describe("tools", () => {
  it("briefing combines the context and what's waiting", async () => {
    const r = await call("get_briefing");
    expect(r.structuredContent.resume).toBe("Jordan Rivera");
    expect(r.structuredContent.waiting).toEqual({
      inbox_updated: INBOX.updated, jobs_to_score: 1, strong_matches_needing_kits: 3,
    });
    expect(calls.every((c) => c.auth === "Bearer ghp_test")).toBe(true);
  });

  it("submits scores as a new file, keeping only jobs in the inbox", async () => {
    const r = await call("submit_scores", {
      scores: [
        { key: "ashby:Northwind:a1", score: 9, summary: "Strong" },
        { key: "made:up", score: 10, summary: "?" },
      ],
    });
    expect(r.isError).toBeUndefined();
    expect(r.structuredContent.saved).toBe(1);
    expect(r.structuredContent.skipped_unknown_keys).toEqual(["made:up"]);
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.url).toMatch(/contents\/data\/mcp\/incoming\/.+-scores\.json$/);
    const saved = JSON.parse(atob(put.body.content));
    expect(saved.kind).toBe("scores");
    expect(saved.items).toEqual([{ key: "ashby:Northwind:a1", score: 9, summary: "Strong" }]);
    // Strong matches come back with everything needed to write the kit in the same session
    expect(r.structuredContent.write_kits_now).toEqual([expect.objectContaining({
      key: "ashby:Northwind:a1", posting: "Full posting text", application_questions: [{ question: "Why Northwind?" }],
    })]);
  });

  it("returns no kits to write for weak scores", async () => {
    const r = await call("submit_scores", { scores: [{ key: "ashby:Northwind:a1", score: 5, summary: "Meh" }] });
    expect(r.structuredContent.write_kits_now).toEqual([]);
  });

  it("accepts a kit for a job just scored in this session", async () => {
    const r = await call("submit_application_kit", {
      key: "ashby:Northwind:a1", materials: { bullets: ["a"], why_company: "b", cold_call: "c", linkedin_note: "d" },
    });
    expect(r.isError).toBeUndefined();
    const done = await call("submit_application_kit", { key: "a", materials: { bullets: [] } });
    expect(done.isError).toBe(true); // already has a card
  });

  it("rejects bad scores", async () => {
    const r = await call("submit_scores", { scores: [{ key: "ashby:Northwind:a1", score: 11, summary: "x" }] });
    expect(r.isError).toBe(true);
    expect(calls.some((c) => c.method === "PUT")).toBe(false);
    const none = await call("submit_scores", { scores: [{ key: "nope", score: 5, summary: "x" }] });
    expect(none.isError).toBe(true);
  });

  it("submits a kit only for a job that's waiting for one", async () => {
    const ok = await call("submit_application_kit", {
      key: "greenhouse:acme:11",
      materials: { bullets: ["a"], why_company: "b", cold_call: "c", linkedin_note: "d" },
      research: { rating: "4.2/5" },
    });
    expect(ok.structuredContent.saved).toBe("greenhouse:acme:11");
    const put = calls.find((c) => c.method === "PUT")!;
    const saved = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(put.body.content), (ch) => ch.charCodeAt(0))));
    expect(saved).toMatchObject({ kind: "kit", key: "greenhouse:acme:11", research: { rating: "4.2/5" } });
    expect(saved.tailoring).toBeUndefined();

    const wrong = await call("submit_application_kit", { key: "other", materials: {} });
    expect(wrong.isError).toBe(true);
  });

  it("keeps non-English text intact", async () => {
    await call("submit_application_kit", {
      key: "greenhouse:acme:11",
      materials: { bullets: ["Señor café — ✓"], why_company: "b", cold_call: "c", linkedin_note: "d" },
    });
    const put = calls.find((c) => c.method === "PUT")!;
    const text = new TextDecoder().decode(Uint8Array.from(atob(put.body.content), (ch) => ch.charCodeAt(0)));
    expect(text).toContain("Señor café — ✓");
  });

  it("lists tracked jobs and filters by status", async () => {
    const all = await call("list_tracked_jobs");
    expect(all.structuredContent.jobs.map((j: any) => j.key)).toEqual(["b", "a"]); // only jobs with cards
    const applied = await call("list_tracked_jobs", { status: "applied" });
    expect(applied.structuredContent.jobs).toHaveLength(1);
  });

  it("updates status by relabeling a real tracker card only", async () => {
    const r = await call("update_job_status", { issue_number: 7, status: "applied" });
    expect(r.structuredContent.status).toBe("applied");
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.url).toMatch(/issues\/7\/labels$/);
    expect(put.body).toEqual({ labels: ["job", "resume-x", "applied"] });
    expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({ state: "open" });
    expect((await call("update_job_status", { issue_number: 99, status: "applied" })).isError).toBe(true);
    expect((await call("update_job_status", { issue_number: 7, status: "hired!" })).isError).toBe(true);
  });

  it("reads the health report and starts a scan", async () => {
    expect((await call("get_health_report")).structuredContent.report).toMatch(/health/);
    await call("run_scan");
    expect(calls.at(-1)!.url).toMatch(/actions\/workflows\/scan\.yml\/dispatches$/);
  });

  it("explains a missing inbox clearly", async () => {
    delete FILES["data/mcp/inbox.json"];
    const r = await call("get_jobs_to_score");
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toMatch(/daily scan/);
    FILES["data/mcp/inbox.json"] = JSON.stringify(INBOX);
  });
});

describe("dashboard Move links", () => {
  const link = async (path: string) => worker.fetch(new Request(`https://x.dev${path}`), env);

  it("moves a closed card back to active with a signed link", async () => {
    const sig = await moveSignature(SECRET, 4, "active");
    const resp = await link(`/move/4/active/${sig}`);
    expect(resp.status).toBe(200);
    expect(await resp.text()).toMatch(/Moved #4 to active/);
    expect(calls.find((c) => c.method === "PUT")!.body).toEqual({ labels: ["job", "resume-x"] });
    expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({ state: "open" });
  });

  it("refuses links that are unsigned, tampered or for another card", async () => {
    const sig = await moveSignature(SECRET, 4, "active");
    for (const path of [`/move/4/active/${"0".repeat(32)}`, `/move/5/active/${sig}`, `/move/4/applied/${sig}`,
                        `/move/4/hired/${sig}`, "/move/4/active"]) {
      expect((await link(path)).status).toBe(404);
    }
    expect(calls).toHaveLength(0);
  });

  it("promotes a job without a card by starting the promote workflow", async () => {
    const sig = await promoteSignature(SECRET, "J-1a2b3c");
    const resp = await link(`/promote/J-1a2b3c/${sig}`);
    expect(resp.status).toBe(200);
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.url).toMatch(/actions\/workflows\/promote\.yml\/dispatches$/);
    expect(post.body).toEqual({ ref: "main", inputs: { job: "J-1a2b3c" } });
    expect((await link(`/promote/J-ffffff/${sig}`)).status).toBe(404);
  });

  it("shows the batch page and moves several jobs in one tap", async () => {
    const sig = await batchSignature(SECRET);
    const pageResp = await link(`/batch/${sig}`);
    const html = await pageResp.text();
    expect(html).toMatch(/quick view/);
    expect(html).toMatch(/value="#7"/);   // Acme, active
    expect(html).toMatch(/value="#9"/);   // Beta, applied
    const body = new URLSearchParams([["pick", "#7"], ["pick", "#9"], ["pick", "#99"], ["to", "skip"]]);
    const resp = await worker.fetch(new Request(`https://x.dev/batch/${sig}`, { method: "POST", body,
      headers: { "Content-Type": "application/x-www-form-urlencoded" } }), env);
    expect(await resp.text()).toMatch(/Moved #7, #9 to <b>skip<\/b>/);
    const puts = calls.filter((c) => c.method === "PUT");
    expect(puts.map((c) => c.url.match(/issues\/(\d+)/)![1])).toEqual(["7", "9"]);
    expect(puts[0].body).toEqual({ labels: ["job", "resume-x", "skip"] });
  });

  it("sends jobs without a card to the promote workflow in one call", async () => {
    const sig = await batchSignature(SECRET);
    const body = new URLSearchParams([["pick", "J-1a2b3c"], ["pick", "J-4d5e6f"], ["to", "active"]]);
    await worker.fetch(new Request(`https://x.dev/batch/${sig}`, { method: "POST", body }), env);
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.body).toEqual({ ref: "main", inputs: { job: "J-1a2b3c,J-4d5e6f" } });
    expect((await link(`/batch/${"0".repeat(32)}`)).status).toBe(404);
  });

  it("names each job's application site", () => {
    expect(siteOf({ url: "https://job-boards.greenhouse.io/x/jobs/1" })).toBe("Greenhouse");
    expect(siteOf({ url: "https://www.example.com/careers/job?gh_jid=5" })).toBe("Greenhouse");
    expect(siteOf({ url: "https://acme.wd1.myworkdayjobs.com/en-US/External/job/1" })).toBe("Workday");
    expect(siteOf({ url: "https://careers.example.com/1", source: "ashby" })).toBe("Ashby");
    expect(siteOf({ url: "https://www.example.org/jobs/2" })).toBe("example.org");
  });
});
