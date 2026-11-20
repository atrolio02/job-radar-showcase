"""Save your quiz answers. Run by the "Save preferences" workflow when you submit
the Job preferences quiz (a GitHub webhook event).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import requests

from . import refit, discord, forms, github_api, profile, store, telemetry

ARRANGEMENTS = {"Remote": "remote", "Hybrid": "hybrid", "On-site": "onsite"}


def _money(text: str) -> int | None:
    """'120000', '$120,000' or '120k' -> 120000."""
    m = re.search(r"(\d[\d,.]*)\s*([kK])?", text or "")
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    return int(value * 1000 if m.group(2) or value < 1000 else value)


def _split(text: str) -> list[str]:
    return [p.strip().lower() for p in re.split(r"[,\n;]", text or "") if p.strip()]


def build_profile(answers: dict, current: dict) -> dict:
    a = answers
    prof = dict(current)
    prof["quiz_completed"] = True
    prof["contact"] = {k: a.get(k, "") for k in ["full_name", "email", "phone", "linkedin", "github", "city"]}
    prof["work_arrangements"] = [ARRANGEMENTS[x] for x in a.get("work_arrangements", []) if x in ARRANGEMENTS] \
        or profile.DEFAULTS["work_arrangements"]
    prof["remote_scope"] = "anywhere" if a.get("remote_scope", "").startswith("Anywhere") else "us"
    prof["areas"] = _split(a.get("areas", "")) or current.get("areas", [])
    prof["open_to_relocation"] = a.get("relocation", "No").startswith("Yes")
    prof["min_base_salary"] = _money(a.get("min_base", ""))
    prof["target_total_comp"] = _money(a.get("target_ote", ""))
    prof["salary_answer"] = a.get("salary_answer", "")
    prof["role_types"] = a.get("role_types") or profile.DEFAULTS["role_types"]
    prof["seniority"] = a.get("seniority") or profile.DEFAULTS["seniority"]
    prof["company_sizes"] = a.get("company_sizes", [])
    for key in ["industries_want", "industries_avoid", "travel", "work_authorization",
                "needs_sponsorship", "start_date", "deal_breakers"]:
        prof[key] = a.get(key, "")
    extra = {k: list(v) for k, v in (current.get("extra_companies") or {}).items()}
    for source, names in forms.boards_in(a.get("companies", "")).items():
        for n in names:
            if n not in extra.setdefault(source, []):
                extra[source].append(n)
    prof["extra_companies"] = extra
    return prof


def describe(prof: dict) -> str:
    pay = f"${prof['min_base_salary']:,}+ base" if prof.get("min_base_salary") else "no pay floor"
    added = sum(len(v) for v in prof.get("extra_companies", {}).values())
    return (f"Saved. Searching for **{', '.join(prof['role_types'])}** roles · "
            f"**{', '.join(prof['work_arrangements'])}** · areas: {', '.join(prof['areas']) or 'any'} · "
            f"{pay} · {added} extra companies watched. Jobs that weren't alerted yet will be "
            f"re-scored with your new preferences, and a fresh scan is starting now.")


def rescore_unalerted(data: dict) -> int:
    count = 0
    for job in data["jobs"].values():
        if job.get("status"):
            continue
        if "score" in job:
            count += 1
        # Forget scores, the free rules check and any place in Claude's inbox: all redone with the new answers.
        for k in ["score", "summary", "strengths", "gaps", "scored_at", "ai_errors", "duplicate_of", "rules_score",
                  "rules_reasons", "rules_tags", "queued_at", "pending_brief", "kit_pending", "questions",
                  "card_waiting"]:
            job.pop(k, None)
    return count


def handle(event: dict, profile_path: Path = profile.DEFAULT_PATH,
           data_path: Path = store.DEFAULT_PATH) -> dict:
    issue = event["issue"]
    answers = forms.parse(issue.get("body", ""), "preferences.yml")
    prof = build_profile(answers, profile.load(profile_path))
    profile.save(prof, profile_path)
    data = store.load(data_path)
    rescore_unalerted(data)
    refit.queue(data, "preferences changed")
    store.save(data, data_path)

    message = describe(prof)
    if github_api.enabled():
        repo, h = os.environ["GITHUB_REPOSITORY"], github_api._headers()
        requests.post(f"{github_api.API}/repos/{repo}/issues/{issue['number']}/comments",
                      headers=h, json={"body": message}, timeout=30)
        requests.patch(f"{github_api.API}/repos/{repo}/issues/{issue['number']}",
                       headers=h, json={"state": "closed", "state_reason": "completed"}, timeout=30)
        # The fresh scan is started by the workflow's last step, after the new answers are saved.
    discord.send({"username": "Job Radar", "embeds": [{"title": "Preferences updated", "description": message,
                                                         "color": discord.BLUE}]})
    return prof


if __name__ == "__main__":
    telemetry.main("preferences", lambda: handle(json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())))
