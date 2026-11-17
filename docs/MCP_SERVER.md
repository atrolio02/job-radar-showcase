# Job Radar MCP server

Job Radar's own connector for Claude. It's a remote **MCP (Model Context Protocol) server**: a small
Cloudflare Worker, written in TypeScript, that exposes Job Radar's inbox and tracker to Claude as tools.

```
GitHub Actions (free)                 MCP server (Cloudflare, free)          Claude (your subscription)
─────────────────────                 ─────────────────────────────          ──────────────────────────
fetch → filter → rules pre-score      get_briefing                           daily task: score jobs,
→ data/mcp/inbox.json  ─────────────► get_jobs_to_score  ◄─────────────────  write kits, research
                                      submit_scores      ─────────────────►
lastmile.py: safety checks, PDF,  ◄── submit_application_kit
tracker card, Discord alert           (new file in data/mcp/incoming/)
```

## What Claude can do with it

| Tool | What it does |
|---|---|
| `get_briefing` | Resume, preferences, application facts, the exact rules for each task, and what's waiting |
| `get_jobs_to_score` | Jobs that passed the free rules check, each with a short posting |
| `submit_scores` | Saves scores; replies with the full posting and questions for each strong match |
| `get_strong_matches` | Older strong matches still waiting for a kit (and jobs you added by hand) |
| `submit_application_kit` | Saves one kit: materials, research if needed, resume rewrites |
| `list_tracked_jobs` | Your tracker cards and their status |
| `update_job_status` | Labels a card applied, interviewing, offer, rejected or skip |
| `get_health_report` | The last 30 days of run health, cost and bottlenecks |
| `run_scan` | Starts a scan now |

You can also use it in any Claude chat: "What did Job Radar find this week?", "Mark the Northwind one applied."

## Security

- **Secret key.** Every request must carry `Authorization: Bearer <MCP_SECRET>` (or the key in the URL path,
  as a fallback for clients that can't send headers). The key must be at least 32 characters, and it's checked
  in constant time. Without it, the server answers 401 Unauthorized and never touches GitHub; every path
  other than `/mcp` is a plain 404.
- **Least privilege.** The server's GitHub token is fine-grained: it can reach only this repository, and only
  its contents, issues and workflow runs. It can't see any of your other repos.
- **Write-only inbox.** Claude can't edit `data/jobs.json` directly. Each submission becomes a new file, and
  `lastmile.py` checks it with the same safety rules as the API path before anything changes. For example,
  resume rewrites can't add numbers that aren't on your resume, and links must be `https://`.
- **No leaks.** GitHub's error messages are never passed back, and tokens never appear in responses or logs.
- **Secrets stay in GitHub and Cloudflare.** They're never in the code, never in chat.

If the key ever leaks, make a new one (step 3), update the `MCP_SECRET` secret, re-run **Deploy MCP server**,
and update the header in Claude.

## Setup (about 20 minutes, on a computer)

Never paste any of these codes into a Claude chat. They go only where each step says.

### 1. A GitHub token just for the server
1. GitHub → your profile picture → **Settings** → **Developer settings** → **Personal access tokens** →
   **Fine-grained tokens** → **Generate new token**.
2. Name: `job-radar-mcp`. Expiration: **1 year**, and put a reminder in your calendar.
3. **Repository access:** Only select repositories → `job-radar`.
4. **Permissions:** Contents: **Read and write**. Issues: **Read and write**. Actions: **Read and write**.
5. **Generate token** and copy it into a temporary note.

### 2. Cloudflare (free)
1. Sign up at dash.cloudflare.com.
2. Open **Workers & Pages** once. If it asks you to pick a `workers.dev` subdomain, pick one.
   Copy your **Account ID** from that page into the note.
3. Profile icon → **My Profile** → **API Tokens** → **Create Token** → template **Edit Cloudflare Workers** →
   **Use template** → under Account Resources pick your account → **Continue to summary** → **Create Token**.
   Copy it into the note.

### 3. The secret key
In Terminal (Mac) run `openssl rand -hex 32` and copy the 64 characters into the note.
(GitHub Desktop → Repository → Open in Terminal works too.)

### 4. Save the four secrets in GitHub
This repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**, four times:

| Name | Value |
|---|---|
| `MCP_GITHUB_TOKEN` | the GitHub token from step 1 |
| `CLOUDFLARE_ACCOUNT_ID` | your Cloudflare Account ID |
| `CLOUDFLARE_API_TOKEN` | the Cloudflare token from step 2 |
| `MCP_SECRET` | the 64-character key from step 3 |

### 5. Deploy
**Actions** tab → **Deploy MCP server** → **Run workflow**. Wait for the green check.
Then in Cloudflare → **Workers & Pages** → `job-radar-mcp`, copy its address, like
`https://job-radar-mcp.YOUR-SUBDOMAIN.workers.dev`. Opening `/health` on it should show `ok`.

### 6. Connect it to Claude
1. In Claude: **Customize** → **Connectors** → **+ Add** → **Add custom connector**.
2. Name: `Job Radar`. URL: `https://job-radar-mcp.YOUR-SUBDOMAIN.workers.dev/mcp/YOUR-KEY`
   (your 64-character key goes at the end). Click **Continue**.
3. Authentication: **No sign in**. Leave **Request headers** empty. Click **Add**.
4. Treat that URL like a password. Then delete the note with your codes.
5. Set the Job Radar tools to **Always allow** (Customize → Connectors → Job Radar), or the daily task
   will stop and wait for approval.

Claude checks a connector before it sends any request headers, so putting the key in a header
(`Authorization: Bearer …`) didn't connect; the key in the URL does. Other MCP clients can use the header.

Test it in a new chat: turn on Job Radar under **+** → **Connectors**, then ask
"Use Job Radar's get_briefing and tell me what's waiting."

### 7. The daily Claude task
Ask Claude to set up a scheduled task (weekdays, about an hour after the 7:17 a.m. scan) with this prompt:

> Use the Job Radar connector. Call get_briefing and follow instructions.workflow exactly: score every job
> from get_jobs_to_score in one submit_scores call, write and submit a kit for each job in its write_kits_now,
> then do the same for anything in get_strong_matches. Never invent experience, numbers, tools or employers.
> Finish with a short summary of how many jobs you scored and which ones got kits.

Make sure the task can use the Job Radar connector without asking each time, or it will stop and wait.

## If something goes wrong

- **Claude says the token was refused:** the GitHub token from step 1 expired or lost a permission. Make a new
  one, update `MCP_GITHUB_TOKEN`, and re-run **Deploy MCP server**.
- **Claude says the data isn't there yet:** the daily scan hasn't run since setup. Run it from the Actions tab.
- **Jobs pile up in the inbox:** the health log warns after 3 days, and strong matches still get their card
  after 30 hours. Check the daily task ran and the connector is on.
- **Deploy MCP server says "Not deployed yet":** one of the four secrets is missing or misnamed.

## For developers

```
cd mcp-server
npm ci
npm test            # 22 tests against a fake GitHub API
npm run typecheck
```

`src/index.ts` handles the protocol (JSON-RPC 2.0 over Streamable HTTP, stateless) and the key check,
`src/tools.ts` defines the tools, and `src/github.ts` is the GitHub REST client.
