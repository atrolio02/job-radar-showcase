// The tools Claude can call. Each one reads or writes Job Radar's files in the repo.
// Python (GitHub Actions) does the authoritative safety checks when it applies a
// submission; these checks just stop obvious mistakes early with a clear message.

import { GitHub } from "./github";

export class ToolError extends Error {}

type Json = Record<string, unknown>;

interface Inbox {
  updated: string;
  to_score: Json[];
  to_score_waiting: number;
  strong_matches: Json[];
  strong_matches_waiting: number;
}

interface Tool {
  name: string;
  title: string;
  description: string;
  inputSchema: Json;
  annotations?: Json;
  run: (gh: GitHub, args: Json) => Promise<unknown>;
}

const STATUSES = ["applied", "interviewing", "offer", "rejected", "skip", "active"];
const MAX_SUBMISSION_BYTES = 200_000;

const str = (max: number) => ({ type: "string", maxLength: max });
const strList = (items: number, max: number) => ({ type: "array", maxItems: items, items: str(max) });

async function inbox(gh: GitHub): Promise<Inbox> {
  return gh.readJson<Inbox>("data/mcp/inbox.json");
}

async function jobs(gh: GitHub): Promise<Record<string, Json>> {
  return (await gh.readJson<{ jobs: Record<string, Json> }>("data/jobs.json")).jobs;
}

function stamp(): string {
  const rand = crypto.getRandomValues(new Uint8Array(4));
  const hex = Array.from(rand, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${new Date().toISOString().replace(/[:.]/g, "-")}-${hex}`;
}

async function submit(gh: GitHub, kind: string, payload: Json, message: string): Promise<string> {
  const body = JSON.stringify({ kind, submitted_at: new Date().toISOString(), ...payload }, null, 1);
  if (new TextEncoder().encode(body).length > MAX_SUBMISSION_BYTES) {
    throw new ToolError("That submission is too large. Send fewer jobs at a time.");
  }
  const name = `${stamp()}-${kind}.json`;
  await gh.createFile(`data/mcp/incoming/${name}`, body, message);
  return name;
}

function needString(args: Json, key: string): string {
  const v = args[key];
  if (typeof v !== "string" || !v.trim()) throw new ToolError(`'${key}' is required.`);
  return v;
}

export const TOOLS: Tool[] = [
  {
    name: "get_briefing",
    title: "Get briefing",
    description:
      "Start here. Returns the candidate's resume, preferences and application facts, the exact instructions " +
      "for scoring, kits, resume tailoring and research, and how much work is waiting.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true },
    run: async (gh) => {
      const [context, box] = await Promise.all([gh.readJson<Json>("data/mcp/context.json"), inbox(gh)]);
      return {
        ...context,
        waiting: {
          inbox_updated: box.updated,
          jobs_to_score: box.to_score.length + box.to_score_waiting,
          strong_matches_needing_kits: box.strong_matches.length + box.strong_matches_waiting,
        },
      };
    },
  },
  {
    name: "get_jobs_to_score",
    title: "Get jobs to score",
    description:
      "Jobs that passed Job Radar's free rules check and need a fit score. Score every one with the scoring " +
      "instructions from get_briefing, then send them all in one submit_scores call.",
    inputSchema: {
      type: "object",
      properties: { limit: { type: "integer", minimum: 1, maximum: 60, description: "Most jobs to return (default 40)." } },
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true },
    run: async (gh, args) => {
      const box = await inbox(gh);
      const limit = typeof args.limit === "number" ? args.limit : 40;
      return { jobs: box.to_score.slice(0, limit), more_waiting: Math.max(0, box.to_score.length - limit) + box.to_score_waiting };
    },
  },
  {
    name: "submit_scores",
    title: "Submit scores",
    description:
      "Save fit scores for jobs from get_jobs_to_score. The reply's write_kits_now lists your strong matches " +
      "(at or above alert_min_score) with the full posting and application questions, so you can write their " +
      "kits right away with submit_application_kit.",
    inputSchema: {
      type: "object",
      required: ["scores"],
      additionalProperties: false,
      properties: {
        scores: {
          type: "array",
          minItems: 1,
          maxItems: 60,
          items: {
            type: "object",
            required: ["key", "score", "summary"],
            properties: {
              key: { ...str(300), description: "The job's key, exactly as given." },
              score: { type: "integer", minimum: 1, maximum: 10 },
              summary: { ...str(400), description: "One sentence on the fit." },
              strengths: strList(3, 200),
              gaps: strList(3, 200),
              role_family: { ...str(40), description: "One of role_families from get_briefing." },
              tags: { ...strList(8, 40), description: "3-8 tags from the tags list in get_briefing." },
            },
          },
        },
      },
    },
    annotations: { readOnlyHint: false, destructiveHint: false },
    run: async (gh, args) => {
      const scores = args.scores;
      if (!Array.isArray(scores) || scores.length === 0) throw new ToolError("'scores' must be a non-empty list.");
      const waiting = new Set((await inbox(gh)).to_score.map((j) => String(j.key)));
      const unknown: string[] = [];
      const items = scores.filter((s): s is Json => {
        const ok = typeof s === "object" && s !== null && waiting.has(String((s as Json).key));
        if (!ok) unknown.push(String((s as Json)?.key));
        return ok;
      });
      for (const s of items) {
        const n = Number(s.score);
        if (!Number.isInteger(n) || n < 1 || n > 10) throw new ToolError(`Score for ${s.key} must be a whole number 1-10.`);
      }
      if (items.length === 0) throw new ToolError("None of those keys are waiting to be scored. Call get_jobs_to_score again.");
      const file = await submit(gh, "scores", { items }, `Claude scored ${items.length} jobs`);
      const [context, all, box] = await Promise.all([gh.readJson<Json>("data/mcp/context.json"), jobs(gh), inbox(gh)]);
      const min = Number(context.alert_min_score ?? 7);
      const briefs = new Map(box.to_score.map((j) => [String(j.key), j]));
      const writeKitsNow = items
        .filter((s) => Number(s.score) >= min || all[String(s.key)]?.force_kit)
        .map((s) => {
          const rec = all[String(s.key)] ?? {};
          const brief = briefs.get(String(s.key)) ?? {};
          return {
            key: s.key, company: rec.company, title: rec.title, score: s.score, url: rec.url, pay: rec.pay_text ?? "",
            posting: rec.pending_brief ?? brief.posting ?? "", application_questions: rec.questions ?? [],
            research_needed: brief.research_needed ?? true, tailoring_needed: true,
          };
        });
      return {
        saved: items.length, skipped_unknown_keys: unknown, file, write_kits_now: writeKitsNow,
        next: writeKitsNow.length
          ? "Write a kit for each job in write_kits_now and send each with submit_application_kit."
          : "No strong matches in this batch. Check get_strong_matches for older ones.",
      };
    },
  },
  {
    name: "get_strong_matches",
    title: "Get strong matches",
    description:
      "Strong matches that need an application kit, with the full posting and any application questions. " +
      "research_needed and tailoring_needed say which optional parts to include.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true },
    run: async (gh) => {
      const box = await inbox(gh);
      return { jobs: box.strong_matches, more_waiting: box.strong_matches_waiting, inbox_updated: box.updated };
    },
  },
  {
    name: "submit_application_kit",
    title: "Submit application kit",
    description:
      "Save one strong match's kit: materials (always), research (when research_needed), and tailoring (when " +
      "tailoring_needed). Job Radar safety-checks the resume rewrites, builds the PDF, opens the tracker card " +
      "and sends the Discord alert.",
    inputSchema: {
      type: "object",
      required: ["key", "materials"],
      additionalProperties: false,
      properties: {
        key: str(300),
        materials: {
          type: "object",
          required: ["bullets", "why_company", "cold_call", "linkedin_note"],
          properties: {
            bullets: strList(3, 400),
            why_company: str(1500),
            answers: {
              type: "array",
              maxItems: 30,
              items: { type: "object", required: ["question", "answer"], properties: { question: str(500), answer: str(2000) } },
            },
            cold_call: str(1200),
            linkedin_note: str(300),
          },
        },
        research: {
          type: "object",
          properties: {
            company_name: str(200), rating: str(200), ceo: str(200), recommend: str(200),
            pros: strList(3, 300), cons: strList(3, 300), as_of: str(60),
            discussions: {
              type: "array",
              maxItems: 5,
              items: { type: "object", properties: { where: str(100), summary: str(400), url: str(500) } },
            },
            red_flags: strList(4, 300),
            sources: strList(6, 500),
          },
        },
        tailoring: {
          type: "object",
          properties: {
            summary: str(1200),
            bullets: {
              type: "array",
              maxItems: 6,
              items: { type: "object", required: ["n", "text"], properties: { n: { type: "integer", minimum: 0 }, text: str(500) } },
            },
            changes: str(400),
            missing: strList(6, 200),
          },
        },
      },
    },
    annotations: { readOnlyHint: false, destructiveHint: false },
    run: async (gh, args) => {
      const key = needString(args, "key");
      const [box, all] = await Promise.all([inbox(gh), jobs(gh)]);
      // Waiting for a kit, or just scored in this session (Job Radar checks the score when it applies the kit).
      const match = box.strong_matches.find((j) => j.key === key) ??
        (all[key] && all[key].pending_brief && !all[key].status ? all[key] : undefined);
      if (!match) throw new ToolError(`${key} isn't waiting for a kit. Call get_strong_matches again.`);
      if (typeof args.materials !== "object" || args.materials === null) throw new ToolError("'materials' is required.");
      const payload: Json = { key, materials: args.materials };
      if (args.research && typeof args.research === "object") payload.research = args.research;
      if (args.tailoring && typeof args.tailoring === "object") payload.tailoring = args.tailoring;
      const file = await submit(gh, "kit", payload, `Claude wrote the kit for ${match.title} at ${match.company}`);
      return { saved: key, file, next: "The tracker card and Discord alert follow in about 2-5 minutes." };
    },
  },
  {
    name: "list_tracked_jobs",
    title: "List tracked jobs",
    description: "Jobs with a tracker card, newest first, with status and links. Optionally filter by status.",
    inputSchema: {
      type: "object",
      properties: { status: { type: "string", enum: ["new", ...STATUSES, "closed"] }, limit: { type: "integer", minimum: 1, maximum: 100 } },
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true },
    run: async (gh, args) => {
      const data = await gh.readJson<{ jobs: Record<string, Json> }>("data/jobs.json");
      const jobs = Object.values(data.jobs)
        .filter((j) => j.status && (!args.status || j.status === args.status))
        .sort((a, b) => String(b.first_seen ?? "").localeCompare(String(a.first_seen ?? "")))
        .slice(0, typeof args.limit === "number" ? args.limit : 50)
        .map((j) => ({
          key: j.key, company: j.company, title: j.title, score: j.score, status: j.status,
          issue_number: j.issue_number, card: j.issue_url, posting: j.url,
        }));
      return { jobs };
    },
  },
  {
    name: "update_job_status",
    title: "Update job status",
    description:
      "Move a tracker card (its job ID is the card number) to applied, interviewing, offer, rejected, skip, or " +
      "back to active. Job Radar updates the dashboard and posts to Discord.",
    inputSchema: {
      type: "object",
      required: ["issue_number", "status"],
      additionalProperties: false,
      properties: { issue_number: { type: "integer", minimum: 1 }, status: { type: "string", enum: STATUSES } },
    },
    annotations: { readOnlyHint: false, destructiveHint: false },
    run: async (gh, args) => {
      const status = needString(args, "status");
      if (!STATUSES.includes(status)) throw new ToolError(`status must be one of ${STATUSES.join(", ")}.`);
      const n = Number(args.issue_number);
      const data = await gh.readJson<{ jobs: Record<string, Json> }>("data/jobs.json");
      const job = Object.values(data.jobs).find((j) => j.issue_number === n);
      if (!job) throw new ToolError(`#${n} isn't a Job Radar tracker card.`);
      await gh.moveCard(n, status);
      return { updated: `#${n} ${job.title} at ${job.company}`, status };
    },
  },
  {
    name: "get_health_report",
    title: "Get health report",
    description: "Job Radar's health report for the last 30 days: failures, slowest steps, cost, errors and bottleneck hints.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true },
    run: async (gh) => ({ report: await gh.readText("data/health/REPORT.md") }),
  },
  {
    name: "run_scan",
    title: "Run a scan now",
    description: "Start Job Radar's job scan now instead of waiting for the morning run. New jobs reach the inbox in about 5 minutes.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true },
    run: async (gh) => {
      await gh.startWorkflow("scan.yml");
      return { started: true, next: "Call get_briefing in about 5 minutes to see what's waiting." };
    },
  },
];
