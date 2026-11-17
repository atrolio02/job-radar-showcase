"""Claude API calls: score each job against the resume, then write tailored
material for the strong matches.

Uses the Claude Messages API directly over HTTPS (POST /v1/messages).
"""
from __future__ import annotations

import json
import os
import re
import time

import requests

API_URL = "https://api.anthropic.com/v1/messages"
MAX_DESCRIPTION_CHARS = 12000   # writing: the full posting
SCORING_DESCRIPTION_CHARS = 6000  # scoring: the useful part is near the top

# Paragraphs that never help judge fit (legal and benefits boilerplate) are dropped to save tokens.
BOILERPLATE = re.compile(
    r"equal opportunity|equal employment|without regard to|reasonable accommodation|e-verify|"
    r"pay transparency|privacy (notice|policy)|applicant privacy|background check|drug[- ]free|"
    r"affirmative action|veteran status|gender identity|by applying,? you|recruiting agencies|"
    r"unsolicited resumes|cookie", re.I)


class AIError(Exception):
    pass


def _call_claude(model: str, system: str, prompt: str, max_tokens: int, tools: list | None = None,
                 cached: str | None = None) -> str:
    """One Claude API call. `cached` is text that repeats across calls (your resume and preferences):
    it's marked for prompt caching, so repeat calls within a few minutes read it at a fraction of the cost."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise AIError("ANTHROPIC_API_KEY is not set")
    system_blocks: str | list = system
    if cached:
        system_blocks = [{"type": "text", "text": system},
                         {"type": "text", "text": cached, "cache_control": {"type": "ephemeral"}}]
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": [{"role": "user", "content": prompt}],
    }
    if tools:
        body["tools"] = tools
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    text_parts: list[str] = []
    for attempt in range(6):
        resp = requests.post(API_URL, json=body, headers=headers, timeout=180)
        # 429 = too many requests, 529 = overloaded, 5xx = temporary server trouble
        if resp.status_code in (429, 529) or resp.status_code >= 500:
            time.sleep(float(resp.headers.get("retry-after", 2 ** (attempt + 2))))
            continue
        if resp.status_code != 200:
            raise AIError(f"Claude API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        content = data.get("content", [])
        text_parts += [b.get("text", "") for b in content if b.get("type") == "text"]
        if data.get("stop_reason") == "pause_turn":
            # A long web search paused mid-answer: send it back so Claude can finish.
            body["messages"] = body["messages"][:1] + [{"role": "assistant", "content": content}]
            continue
        return "".join(text_parts)
    raise AIError("Claude API kept returning busy errors; will retry next run")


def _parse_json(text: str) -> dict:
    """Pull the JSON object out of Claude's reply, even if it adds a code fence."""
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        raise AIError(f"Expected JSON from Claude, got: {text[:200]}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise AIError(f"Couldn't read Claude's JSON: {e}") from e


def clean_description(text: str, limit: int) -> str:
    kept = [p for p in re.split(r"\n\s*\n|\n", text or "") if p.strip() and not BOILERPLATE.search(p)]
    return "\n".join(kept)[:limit]


def resume_block(resume: str, preferences: str | None = None) -> str:
    out = f"<resume>\n{resume}\n</resume>"
    if preferences is not None:
        out += f"\n\n<preferences>\n{preferences or 'None given'}\n</preferences>"
    return out


def _job_block(job, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    pay = f"\nPosted pay: {job.pay_text}" if getattr(job, "pay_text", "") else ""
    return (
        f"Company: {job.company}\nTitle: {job.title}\nLocation: {job.location}\n"
        f"Work arrangement: {getattr(job, 'workplace', 'unknown')}{pay}\n"
        f"Posting:\n{clean_description(job.description, limit)}"
    )


SCORE_SYSTEM = (
    "You are an experienced tech recruiter screening jobs for one candidate. "
    "Judge fit honestly using only what the resume shows, and weigh the candidate's stated "
    "preferences. Reply with JSON only."
)

SCORE_PROMPT = """Rate how well this job fits the candidate whose resume and preferences are above: both
whether his experience matches what they need, and whether the job matches what he wants.

<job>
{job}
</job>

Reply with exactly this JSON:
{{"score": <integer 1-10, where 7+ means worth applying; at most 4 if it clearly hits a deal-breaker
            or pays well below his minimum>,
  "summary": "<one sentence on the fit>",
  "strengths": ["<up to 3 short reasons he fits>"],
  "gaps": ["<up to 3 short gaps or risks, or empty>"],
  "role_family": "<one of: {families}>",
  "tags": ["<3 to 8 tags from this list that describe what the job needs: {tags}>"]}}"""

# Fixed lists so similar jobs get the same tags (used to reuse tailored resumes)
ROLE_FAMILIES = ["sales engineer", "solutions architect", "gtm engineer", "implementation", "account executive",
                 "other"]
TAGS = ["api-integrations", "webhooks", "crm", "salesforce", "security", "endpoint-management", "networking",
        "cloud", "aws", "data-warehouse", "sql", "analytics", "ai-llm", "automation", "devtools", "observability",
        "payments-fintech", "hr-payroll", "martech", "communications-sms", "home-services", "healthcare",
        "smb", "mid-market", "enterprise", "poc-demos", "technical-discovery", "onboarding", "python",
        "prospecting", "quota-carrying", "travel"]


def score_job(job, resume: str, model: str, preferences: str = "") -> dict:
    prompt = SCORE_PROMPT.format(job=_job_block(job, SCORING_DESCRIPTION_CHARS),
                                 families=", ".join(ROLE_FAMILIES), tags=", ".join(TAGS))
    text = _call_claude(model, SCORE_SYSTEM, prompt, 500, cached=resume_block(resume, preferences))
    return clean_score(_parse_json(text))


def clean_score(result: dict) -> dict:
    """Check and tidy a score, whether it came from the API or from Claude through the MCP server."""
    try:
        score = int(float(result.get("score", 0)))
    except (TypeError, ValueError):
        raise AIError(f"Score isn't a number: {result.get('score')!r}")
    as_list = lambda v: v if isinstance(v, list) else []
    return {
        "score": max(1, min(10, score)),
        "summary": str(result.get("summary", "")),
        "strengths": [str(s) for s in as_list(result.get("strengths"))][:3],
        "gaps": [str(g) for g in as_list(result.get("gaps"))][:3],
        "role_family": result.get("role_family") if result.get("role_family") in ROLE_FAMILIES else "other",
        "tags": sorted({str(t).lower() for t in as_list(result.get("tags")) if str(t).lower() in TAGS}),
    }


WRITE_SYSTEM = (
    "You help a sales engineer apply for jobs. Never invent experience, numbers, "
    "tools or employers: only reword what is in the resume to match the job's language. "
    "Reply with JSON only."
)

WRITE_PROMPT = """Write application material for this job, using the resume above.

<candidate_facts>
{facts}
</candidate_facts>

<job>
{job}
</job>

<application_questions>
{questions}
</application_questions>

Answer every application question from the resume and candidate facts. For a question with options,
answer with one of the options exactly. If the facts don't cover it, answer "[Fill in yourself]".
Skip demographic or voluntary self-identification questions.

Reply with exactly this JSON:
{{"bullets": ["<3 resume bullets reworded from the resume to mirror this posting's keywords>"],
  "why_company": "<3-4 sentences for 'Why do you want to work here?', specific to this company and role>",
  "answers": [{{"question": "<question text>", "answer": "<answer>"}}],
  "cold_call": "<a 30-second cold call to the hiring leader, about 80 words, asking for 15 minutes>",
  "linkedin_note": "<a LinkedIn connection note under 300 characters>"}}"""


def _facts(prof: dict) -> str:
    c = prof.get("contact") or {}
    rows = [("Lives in", c.get("city")), ("Work authorization", prof.get("work_authorization")),
            ("Needs visa sponsorship", prof.get("needs_sponsorship")), ("Can start", prof.get("start_date")),
            ("Open to relocation", "Yes" if prof.get("open_to_relocation") else "No"),
            ("Salary expectations", prof.get("salary_answer")), ("Travel OK", prof.get("travel")),
            ("LinkedIn", c.get("linkedin")), ("GitHub/portfolio", c.get("github"))]
    return "\n".join(f"{k}: {v}" for k, v in rows if v) or "None given"


def write_materials(job, resume: str, model: str, prof: dict | None = None,
                    questions: list[dict] | None = None) -> dict:
    qtext = "\n".join(
        f"- {q['question']}" + (f" (options: {' | '.join(q['options'])})" if q.get("options") else "")
        for q in questions or []) or "None listed. Only answer 'Why do you want to work here?'."
    prompt = WRITE_PROMPT.format(job=_job_block(job), facts=_facts(prof or {}), questions=qtext)
    return clean_materials(_parse_json(_call_claude(model, WRITE_SYSTEM, prompt, 3000, cached=resume_block(resume))))


def clean_materials(result: dict) -> dict:
    as_list = lambda v: v if isinstance(v, list) else []
    answers = [{"question": str(a.get("question", "")), "answer": str(a.get("answer", ""))}
               for a in as_list(result.get("answers")) if isinstance(a, dict)]
    return {
        "bullets": [str(b) for b in as_list(result.get("bullets"))][:3],
        "why_company": str(result.get("why_company", "")),
        "answers": answers[:30],
        "cold_call": str(result.get("cold_call", "")),
        "linkedin_note": str(result.get("linkedin_note", ""))[:300],
    }


RESEARCH_SYSTEM = (
    "You research employers for a job seeker. Use web search. Report only figures you actually find in "
    "search results, with the source and date; never estimate or fill gaps. Reply with JSON only."
)

RESEARCH_PROMPT = """Find what current and former employees say about working at {company} ({hint}).
Look for its overall employee rating, CEO approval and "recommend to a friend" figures on Glassdoor,
Indeed, Comparably or similar review sites, and common themes in reviews. Also look for recent discussion
threads about working there or interviewing there on Reddit and X (Twitter), plus news of layoffs,
lawsuits or leadership changes in the last year.

Reply with exactly this JSON (use "" or [] for anything you didn't find):
{{"company_name": "<full company name you researched>",
  "rating": "<overall rating, e.g. 4.1/5 (Glassdoor)>",
  "ceo": "<CEO name and approval, e.g. Jane Doe, 89% approve (Glassdoor)>",
  "recommend": "<e.g. 82% would recommend to a friend (Glassdoor)>",
  "pros": ["<up to 3 common positives from reviews>"],
  "cons": ["<up to 3 common complaints from reviews>"],
  "as_of": "<date or month of the figures, if shown>",
  "discussions": [{{"where": "<Reddit or X, with subreddit or account>", "summary": "<one sentence on what was said>",
                    "url": "<thread URL>"}}],
  "red_flags": ["<recent layoffs, lawsuits, exec departures or repeated complaints, each with its date; [] if none>"],
  "sources": ["<URLs you used>"]}}"""


def company_snapshot(company: str, hint: str, model: str, tool_type: str = "web_search_20250305") -> dict:
    tools = [{"type": tool_type, "name": "web_search", "max_uses": 4}]
    text = _call_claude(model, RESEARCH_SYSTEM, RESEARCH_PROMPT.format(company=company, hint=hint), 2500, tools)
    return clean_snapshot(_parse_json(text), company)


def clean_snapshot(r: dict, company: str) -> dict:
    as_list = lambda v, n: [str(x) for x in (v if isinstance(v, list) else [])][:n]
    return {
        "company_name": str(r.get("company_name", "")) or company,
        "rating": str(r.get("rating", "")), "ceo": str(r.get("ceo", "")),
        "recommend": str(r.get("recommend", "")),
        "pros": as_list(r.get("pros"), 3), "cons": as_list(r.get("cons"), 3),
        "as_of": str(r.get("as_of", "")),
        "discussions": [
            {"where": str(d.get("where", "")), "summary": str(d.get("summary", "")), "url": str(d.get("url", ""))}
            for d in (r.get("discussions") if isinstance(r.get("discussions"), list) else [])
            if isinstance(d, dict) and str(d.get("url", "")).startswith("http")][:5],
        "red_flags": as_list(r.get("red_flags"), 4),
        "sources": [u for u in as_list(r.get("sources"), 6) if u.startswith("http")],
    }
