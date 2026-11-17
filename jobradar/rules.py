"""Free pre-scoring: rules instead of AI.

Every matching job gets a 0-100 "rules score" from plain code:

    skills overlap  0-45  skills the posting asks for that your resume also shows
    title fit       0-20  how closely the title matches your roles, more for the role types you rank higher
    pay             0-15  posted pay vs. your minimum (unknown pay gets partial credit)
    arrangement     0-10  remote / hybrid / on-site vs. what you want
    freshness       0-10  newer postings first
    deal-breakers   -40   a deal-breaker from the quiz appears in the posting

Only jobs at or above `rules_min_score` (config.yml) go to Claude, so Claude's
time is spent on the jobs most likely to be worth it. In `ai.mode: rules`, this
score (divided by 10) is the final score and nothing uses AI at all.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from . import ai, profile as profile_mod

# Skill phrases -> the tag they count toward. One tag is counted once.
SKILLS: dict[str, list[str]] = {
    "api-integrations": ["api", "apis", "rest", "integration", "integrations", "json", "postman", "sdk"],
    "webhooks": ["webhook", "webhooks", "event-driven"],
    "crm": ["crm", "hubspot", "pipeline"],
    "salesforce": ["salesforce", "sfdc"],
    "security": ["security", "soc 2", "sso", "saml", "iam", "zero trust", "compliance"],
    "endpoint-management": ["endpoint", "mdm", "device management", "intune", "jamf"],
    "networking": ["networking", "dns", "vpn", "firewall", "tcp/ip"],
    "cloud": ["cloud", "saas", "gcp", "azure", "kubernetes", "docker"],
    "aws": ["aws", "amazon web services", "lambda", "s3"],
    "data-warehouse": ["snowflake", "bigquery", "redshift", "data warehouse", "databricks", "etl"],
    "sql": ["sql", "postgres", "mysql"],
    "analytics": ["analytics", "dashboards", "reporting", "bi", "looker", "tableau"],
    "ai-llm": ["ai", "llm", "machine learning", "genai", "generative ai", "claude", "openai", "agents"],
    "automation": ["automation", "workflow", "workflows", "zapier", "clay", "n8n", "make.com", "scripting"],
    "devtools": ["developer", "developers", "devtools", "git", "github", "ci/cd"],
    "observability": ["observability", "monitoring", "logging", "apm", "datadog"],
    "payments-fintech": ["payments", "fintech", "billing", "invoicing"],
    "hr-payroll": ["payroll", "hcm", "hris", "human capital"],
    "martech": ["marketing automation", "martech", "email marketing"],
    "communications-sms": ["sms", "voice", "messaging", "twilio", "telephony"],
    "poc-demos": ["demo", "demos", "proof of concept", "poc", "pocs", "pilot", "pilots"],
    "technical-discovery": ["discovery", "requirements", "solution design", "technical validation"],
    "onboarding": ["onboarding", "implementation", "implementations", "deployment", "go-live"],
    "python": ["python", "javascript", "typescript", "node"],
    "prospecting": ["prospecting", "outbound", "pipeline generation"],
    "smb": ["smb", "small business"],
    "mid-market": ["mid-market", "midmarket", "commercial"],
    "enterprise": ["enterprise", "strategic accounts"],
}

ROLE_FAMILY_WORDS = [
    ("solutions architect", ["solutions architect", "solution architect", "solutions consultant"]),
    ("gtm engineer", ["gtm engineer", "growth engineer", "go-to-market engineer", "revops engineer"]),
    ("sales engineer", ["sales engineer", "solutions engineer", "pre-sales", "presales", "solution engineer"]),
    ("implementation", ["implementation", "onboarding", "deployment", "integration engineer"]),
    ("account executive", ["account executive"]),
]


def _has(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def tags_in(text: str) -> set[str]:
    text = (text or "").lower()
    return {tag for tag, phrases in SKILLS.items() if any(_has(text, p) for p in phrases)}


def role_family(title: str) -> str:
    t = (title or "").lower()
    return next((fam for fam, words in ROLE_FAMILY_WORDS if any(w in t for w in words)), "other")


def score(job, resume: str, prof: dict) -> tuple[int, list[str], list[str]]:
    """(0-100 score, short reasons, tags the job needs)."""
    text = f"{job.title}\n{ai.clean_description(job.description, 20000)}".lower()
    reasons: list[str] = []

    wanted, have = tags_in(text), tags_in(resume)
    overlap = wanted & have
    skills = round(45 * len(overlap) / len(wanted)) if wanted else 20
    if overlap:
        reasons.append(f"skills match {len(overlap)}/{len(wanted)}: {', '.join(sorted(overlap)[:5])}")

    title = (job.title or "").lower()
    phrases = profile_mod.title_phrases(prof)
    rank = profile_mod.role_rank(prof, title)
    # Most wanted role type gets the most points: 20, 17, 14, 11...
    best = 20 - 3 * min(rank, 3) if rank is not None else 12
    if any(title.startswith(p) or title == p for p in phrases):
        title_pts = best
    elif any(_has(title, p) for p in phrases):
        title_pts = best - 5
    else:
        title_pts = 6
    if rank is not None:
        reasons.append(f"role type #{rank + 1} on your list")
    if re.search(r"\b(senior|sr\.?|staff|principal|lead)\b", title) and "Senior" not in (prof.get("seniority") or []):
        title_pts -= 6

    floor = prof.get("min_base_salary")
    if job.pay_max and floor:
        pay = 15 if job.pay_max >= floor else 0
        reasons.append("pay meets your minimum" if pay else "pay below your minimum")
    elif job.pay_max:
        pay = 12
    else:
        pay = 7

    arrangements = set(prof.get("work_arrangements") or ["remote", "hybrid", "onsite"])
    arrangement = 10 if job.workplace in arrangements else (5 if job.workplace == "unknown" else 0)

    fresh = 5
    if job.posted:
        try:
            posted = datetime.fromisoformat(job.posted.replace("Z", "+00:00"))
            if posted.tzinfo is None:  # date-only or no zone: treat as UTC
                posted = posted.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - posted).days
            fresh = 10 if age <= 3 else 8 if age <= 7 else 5 if age <= 21 else 2
        except ValueError:
            pass

    total = skills + title_pts + pay + arrangement + fresh
    breakers = [b.strip().lower() for b in re.split(r"[,;\n]", prof.get("deal_breakers") or "") if len(b.strip()) > 2]
    hit = [b for b in breakers if _has(text, b)]
    if hit:
        total -= 40
        reasons.append(f"deal-breaker: {', '.join(hit[:2])}")
    avoid = [b.strip().lower() for b in re.split(r"[,;\n]", prof.get("industries_avoid") or "") if len(b.strip()) > 2]
    if any(_has(text, a) for a in avoid):
        total -= 15
        reasons.append("industry you want to avoid")
    return max(0, min(100, total)), reasons, sorted(wanted)


def as_final_score(job, resume: str, prof: dict) -> dict:
    """`ai.mode: rules`: the rules score stands in for Claude's score."""
    pts, reasons, tags = score(job, resume, prof)
    return {"score": max(1, min(10, round(pts / 10))), "summary": f"Rules-based match ({pts}/100).",
            "strengths": reasons[:3], "gaps": [], "role_family": role_family(job.title),
            "tags": [t for t in tags if t in ai.TAGS]}
