"""Your preferences (from the quiz) and how they turn into search rules.

profile.yml is written by the Preferences quiz. Until you take it, the
starting answers below are used.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

DEFAULT_PATH = Path("profile.yml")

# Quiz choice -> phrases a job title must contain
ROLE_TITLES = {
    "Sales / Solutions Engineer": ["sales engineer", "solutions engineer", "solution engineer",
                                   "presales", "pre-sales", "presales engineer",
                                   "field applications engineer", "technical sales"],
    "Solutions Architect / Consultant": ["solutions architect", "solution architect",
                                         "solutions consultant", "solution consultant"],
    "GTM / Growth Engineer": ["gtm engineer", "gtm engineering", "go-to-market engineer", "go to market engineer",
                              "growth engineer", "revops engineer", "revenue operations engineer",
                              "gtm systems", "go-to-market systems", "gtm automation", "growth automation",
                              "gtm operations", "revenue operations", "revops", "gtm strategy",
                              "ai solutions specialist", "gtm architect", "growth systems"],
    "Forward Deployed / Implementation Engineer": ["forward deployed engineer", "implementation engineer",
                                                   "deployment engineer"],
    "BizOps / Supply Chain Systems": ["business operations", "bizops", "biz ops", "strategy and operations",
                                      "strategy & operations", "operations analyst", "s&op",
                                      "sales and operations planning", "supply chain systems",
                                      "supply chain analytics", "supply chain analyst", "supply chain data",
                                      "supply chain technology", "supply chain solutions"],
    "Sales Operations": ["sales operations", "sales ops", "salesops", "sales strategy", "sales analyst",
                         "sales systems", "commercial operations", "gtm operations",
                         "revenue operations", "revops", "crm analyst", "salesforce administrator",
                         "salesforce admin", "business systems analyst"],
    "Account Executive": ["account executive"],
}

# Operations titles cover many teams; these aren't the business/supply-chain/sales kind.
_OPS_NOISE = ["legal", "people", "hr", "human resources", "recruit", "recruiting", "talent", "security",
              "it operations", "clinical", "nurse", "warehouse associate", "driver", "payroll", "facilities", "deal"]
ROLE_EXCLUDES = {"BizOps / Supply Chain Systems": _OPS_NOISE, "Sales Operations": _OPS_NOISE}


def role_matches(role: str, title: str) -> bool:
    """Whether a lower-case title fits a role type: one of its phrases, none of its exclusions."""
    t = f" {re.sub(r'[^a-z0-9&+]+', ' ', title.lower())} "
    if not any(p in title.lower() for p in ROLE_TITLES.get(role, [role.lower()])):
        return False
    return not any(f" {w} " in t for w in ROLE_EXCLUDES.get(role, []))


# Seniority choice -> title words that get excluded when it ISN'T picked
SENIORITY_WORDS = {
    "Entry / Associate": ["associate", "junior", "jr"],
    "Mid-level": [],
    "Senior": ["senior", "sr"],
    "Lead / Staff / Principal": ["lead", "staff", "principal"],
}

DEFAULTS = {
    "quiz_completed": False,
    "contact": {"full_name": "Jordan Rivera", "email": "", "phone": "", "linkedin": "",
                "github": "", "city": "Austin, TX"},
    "work_arrangements": ["remote", "hybrid", "onsite"],
    "remote_scope": "us",
    "areas": ["austin", "san marcos", "pflugerville", "round rock", "irving", "frisco", "richardson", "arlington"],
    "open_to_relocation": False,
    "min_base_salary": None,
    "target_total_comp": None,
    "salary_answer": "",
    "role_types": ["Sales / Solutions Engineer", "Solutions Architect / Consultant", "GTM / Growth Engineer"],
    "role_priority": [],   # role types, most wanted first (falls back to role_types order)
    "seniority": ["Mid-level", "Senior"],
    "company_sizes": [],
    "industries_want": "",
    "industries_avoid": "",
    "travel": "",
    "work_authorization": "",
    "needs_sponsorship": "",
    "start_date": "",
    "deal_breakers": "",
    "extra_companies": {},
}


def load(path: Path = DEFAULT_PATH) -> dict:
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    prof = {**DEFAULTS, **(data or {})}
    prof["contact"] = {**DEFAULTS["contact"], **(prof.get("contact") or {})}
    return prof


def save(prof: dict, path: Path = DEFAULT_PATH) -> None:
    header = ("# Written by the Job preferences quiz. Retake the quiz any time from\n"
              "# Issues → New issue → \"Job preferences quiz\" instead of editing by hand.\n")
    path.write_text(header + yaml.safe_dump(prof, sort_keys=False, allow_unicode=True))


def title_phrases(prof: dict) -> list[str]:
    phrases = []
    for role in prof.get("role_types") or []:
        phrases += ROLE_TITLES.get(role, [role.lower()])
    return phrases


def role_priority(prof: dict) -> list[str]:
    """Role types, most wanted first. Types you picked but didn't rank go after the ranked ones."""
    picked = list(prof.get("role_types") or [])
    ranked = [r for r in (prof.get("role_priority") or []) if r in picked]
    return ranked + [r for r in picked if r not in ranked]


def role_rank(prof: dict, title: str) -> int | None:
    """0 for the most wanted role type a title matches, 1 for the next, ... None if it matches none."""
    t = (title or "").lower()
    for i, role in enumerate(role_priority(prof)):
        if role_matches(role, t):
            return i
    return None


def title_excludes(prof: dict) -> list[str]:
    picked = set(prof.get("seniority") or SENIORITY_WORDS)
    words = []
    for level, ws in SENIORITY_WORDS.items():
        if level not in picked:
            words += ws
    return words


def summary_for_ai(prof: dict) -> str:
    """Plain-text preferences for Claude to weigh when scoring."""
    lines = [
        f"Role types, most wanted first: {' > '.join(role_priority(prof)) or 'any'} "
        "(when two jobs fit equally well, score the more wanted role type higher)",
        f"Work arrangements wanted: {', '.join(prof.get('work_arrangements') or []) or 'any'}",
        f"Preferred areas for hybrid/on-site: {', '.join(prof.get('areas') or []) or 'any'}",
        f"Open to relocation: {'yes' if prof.get('open_to_relocation') else 'no'}",
    ]
    if prof.get("min_base_salary"):
        lines.append(f"Minimum base salary: ${int(prof['min_base_salary']):,}")
    if prof.get("target_total_comp"):
        lines.append(f"Target total compensation (OTE): ${int(prof['target_total_comp']):,}")
    for key, label in [("seniority", "Seniority"), ("company_sizes", "Company sizes")]:
        if prof.get(key):
            lines.append(f"{label}: {', '.join(prof[key])}")
    for key, label in [("industries_want", "Industries wanted"), ("industries_avoid", "Industries to avoid"),
                       ("travel", "Travel tolerance"), ("deal_breakers", "Deal-breakers")]:
        if prof.get(key):
            lines.append(f"{label}: {prof[key]}")
    return "\n".join(lines)
