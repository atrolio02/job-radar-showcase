# Set up your own Job Radar

About 30–45 minutes the first time, on a computer. You need a GitHub account, a Discord account, and
(for the AI part) a Claude account. Everything else is free.

Never paste a key or password from these steps into a chat, an issue or a file. They only go into
GitHub's **Secrets** page or Cloudflare.

---

## 1. Make your own private copy (2 minutes)

1. At the top of this repo, click **Use this template → Create a new repository**. If there's no
   template button, click **Fork** instead.
2. Name it `job-radar` and choose **Private**. Your job search, resume and applications will live here,
   so keep it private.
3. In your new repo, open the **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable them**.
4. Click **Reset sample data** → **Run workflow**. This removes the fictional sample jobs, cards and resumes.

## 2. Add your resume (5 minutes)

Replace `resume.docx` with yours (**Add file → Upload files**, same name). Job Radar reads it as follows, so
format it like this or tailoring won't find your bullets:

- Section headings in CAPITALS on their own line: `SUMMARY`, `EXPERIENCE`, `SKILLS`, `PROJECTS`, `EDUCATION`.
- The summary is the paragraph under `SUMMARY`.
- Every bullet starts with a bullet character and a tab (Word's normal bulleted lists written as `•` then Tab).
- One page. Tailored copies are kept to one page too.

Tailored copies only ever reword your bullets. They never add experience, tools or numbers you don't have.

## 3. Choose the companies to watch (5 minutes)

Open `config.yml` and edit the `companies:` list. Job Radar reads five job-board systems:

| If a company's job links look like… | add the name under |
|---|---|
| `jobs.ashbyhq.com/NAME/...` | `ashby:` |
| `boards.greenhouse.io/NAME/...` or `job-boards.greenhouse.io/NAME/...` | `greenhouse:` |
| `jobs.lever.co/NAME/...` | `lever:` |
| `jobs.smartrecruiters.com/NAME/...` | `smartrecruiters:` (letter case matters) |
| `HOST.fa.REGION.oraclecloud.com/hcmUI/CandidateExperience/.../sites/SITE/...` | `oracle:` as `Company=HOST/SITE` |

**Watching many companies:** list candidate names in `tools/board_candidates.txt` and run **Actions → Find
job boards**. It tries each name on every system and logs the ones with open jobs and how many, so you can
add them in bulk. Boards are read in parallel; hundreds of companies take a few minutes.

Companies on other systems (Workday and custom career sites) can't be watched. List them under
`manual_watch:` in `config.yml` and the dashboard shows each one with a LinkedIn job search for your roles
and area; paste any good job into **Add a job** (step 8) to get the full kit.

## 4. Discord alerts (3 minutes)

1. In Discord, make a server for yourself (**+ → Create My Own → For me and my friends**).
2. Hover a channel → **gear icon** → **Integrations → Webhooks → New Webhook → Copy Webhook URL**.
3. In your repo: **Settings → Secrets and variables → Actions → New repository secret**.
   Name `DISCORD_WEBHOOK_URL`, paste the link, **Add secret**.

## 5. Take the preferences quiz (5–10 minutes)

**Issues → New issue → Job preferences quiz → Get started.** Fill it out and submit. It saves your roles,
seniority, locations, pay floor, deal-breakers and application facts to `profile.yml`, then starts a scan.

Tips:
- **Hybrid/on-site cities:** list every nearby city by name; matching is by the words in a job's location.
- **Deal-breakers:** short phrases postings actually use, like `commission only` or `75% travel`.
- **Role ranking:** to rank role types (for example GTM first), edit `role_priority:` in `profile.yml`.
  Retaking the quiz keeps it.

## 6. Pick who does the AI work

Set `ai.mode` in `config.yml`:

| Mode | What it costs | What you set up |
|---|---|---|
| `mcp` (default) | Your Claude subscription, no API bill | The MCP server and a daily Claude task: **[MCP_SERVER.md](MCP_SERVER.md)** (about 20 minutes) |
| `api` | Pay-per-use Claude API (cents per job) | An `ANTHROPIC_API_KEY` secret from console.anthropic.com (set a monthly limit there) |
| `rules` | Free | Nothing. Scores come from keyword rules; cards have no kits or tailored resumes. |

In `mcp` mode, the free code fetches, filters and pre-scores; Claude only does the last mile (scoring the
promising jobs and writing kits) through your own MCP server, which you deploy to your own free Cloudflare
account with your own secret key.

## 7. Turn on the daily scan

Open `.github/workflows/scan.yml` and remove the `# ` in front of `schedule:` and the `cron:` line under it.
The time is in UTC: `17 13 * * 1-5` is weekdays at 13:17 UTC (7:17 a.m. US Central). Commit.

To run it any time: **Actions → Daily job scan → Run workflow**.

## 8. Using it day to day

- **Your dashboard** is the repo's home page (README). It lists strong matches, their status and pay.
- **Each strong match is a card** (an Issue) with the score, tailored resume PDF, application answers,
  a cold-call script, a LinkedIn note and employer research.
- **Sections:** Active (to apply to), Applied (applied, interviewing, offer), Closed out (rejected,
  skipped) and Lower-scoring. Every row has a Job ID (`#12` for cards, `J-1a2b3c` for jobs without one).
- **Cards by score:** 7+ gets a tailored resume and full kit; 6 gets a card with your standard resume
  (`standard_resume_pdf` in `config.yml`); lower scores can be moved to Active with one tap.
- **Move jobs with one tap:** each row's links (📨 Applied, ⏭️ Skip, 🗣️ Interviewing, 🎉 Offer, ✖️ Rejected,
  ↩️ Move to active) move the job. Set `move_links:` in `config.yml` to your MCP server's address to make
  them one-tap; each link is signed for its one move, so the dashboard never shows your key. Without it,
  they open a short prefilled form: press Submit.
- **Quick view:** the **⚡ Quick view** link at the top of the dashboard opens a page served by your MCP
  server where job links open in new tabs (GitHub doesn't allow that in a README), chips filter by
  application site (Greenhouse, Ashby, Workday...) so you can apply to one kind in a row, and checkboxes
  move many jobs at once (up to 15 cards per tap). The dashboard's **Site** column shows the same thing.
- You can also add a label to a card (`applied`, `interviewing`, `offer`, `rejected`, `skip`), comment
  `/applied` and so on, or in `mcp` mode tell Claude "move job 12 to applied."
- **Long application forms:** every card has a **Work history, skills and certifications** section that
  splits the resume it uses into each job's title, dates and description, ready to paste into Workday.
- **Found a job somewhere else?** **Issues → New issue → Add a job**, paste the posting, and it gets scored
  and a kit like any other.
- **Health:** `data/health/REPORT.md` shows failures, the slowest steps, cost and bottlenecks for the last
  30 days. Crashes send a Discord alert and open a `radar-error` issue.

## 9. Keep it safe

- Keep your copy **private**. If you ever make one public, the workflows only react to issues and
  comments from the repo owner, but your data would be visible.
- Set a **$0 budget** for Actions under GitHub **Settings → Billing → Budgets and alerts** so nothing can bill you.
- Rotate keys (Discord webhook, MCP secret, tokens) if one is ever exposed.

## Troubleshooting

| What you see | What to do |
|---|---|
| A warning that a company "wasn't found" | The board name in `config.yml` is wrong or the company moved systems. Open one of its job links and copy the name from it. |
| The quiz isn't in the New issue list | Wait a minute and refresh. If it's still missing, the form file has an error; GitHub hides broken forms. |
| No jobs match | Check the quiz's role types, seniority and cities. Remote jobs must be open US-wide or in your state. |
| Claude says Job Radar's data isn't there | Run the daily scan once; the MCP server reads what the scan saves. |
| Tailored resumes are missing | `resume.docx` needs the format in step 2, and LibreOffice is installed by the workflow on first use. |
