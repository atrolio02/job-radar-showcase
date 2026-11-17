"""Score a job you added by hand (from LinkedIn, Indeed, a referral...).

Runs when you submit the "Add a job" form (a GitHub webhook event). Claude
scores it against your resume and preferences, writes the application kit,
and the same issue becomes its tracker card.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import ai, dashboard, discord, forms, github_api, profile, rules, scan, sources, store, telemetry

WORKPLACE = {"Remote": "remote", "Hybrid": "hybrid", "On-site": "onsite"}


def job_from_form(answers: dict, number: int) -> sources.Job:
    lo, hi, text = sources.pay_from_text(answers.get("pay", ""))
    job = sources.Job(
        key=f"manual:{number}", source="manual", company=answers.get("company", "").strip(),
        title=answers.get("title", "").strip(), location=answers.get("location", ""),
        remote=answers.get("workplace") == "Remote", url=answers.get("link", "").strip(), posted=None,
        description=answers.get("description", ""),
        workplace=WORKPLACE.get(answers.get("workplace", ""), "unknown"),
    )
    job.pay_min, job.pay_max = lo, hi
    job.pay_text = text or answers.get("pay", "")
    return job


def handle(event: dict, config_path: str = "config.yml", resume_path: str | None = None,
           data_path: Path = store.DEFAULT_PATH, readme_path: Path = Path("README.md"),
           profile_path: Path = profile.DEFAULT_PATH, base_docx: Path = scan.RESUME_DOCX,
           out_root: Path = scan.RESUMES_DIR) -> dict:
    issue = event["issue"]
    answers = forms.parse(issue.get("body", ""), "add-job.yml")
    job = job_from_form(answers, issue["number"])
    cfg, prof = scan.load_config(config_path), profile.load(profile_path)
    ai_cfg, resume = cfg.get("ai", {}), scan.load_resume(resume_path or scan.default_resume_path())

    data = store.load(data_path)
    rec = data["jobs"].setdefault(job.key, {"first_seen": scan._now()})
    info = job.to_dict()
    info.pop("description")
    rec.update(info)

    problems: list[str] = []
    mode = scan.ai_mode(ai_cfg)
    if mode == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
        mode = "mcp"
    rec["issue_number"] = issue["number"]
    if mode == "mcp":
        # Claude scores it and writes the kit on its next pass (you picked it, so it always gets a kit).
        from . import lastmile
        rec.update(queued_at=scan._now(), pending_brief=ai.clean_description(job.description, lastmile.KIT_CHARS),
                   force_kit=True, issue_url=issue.get("html_url"))
        store.save(data, data_path)
        lastmile.write_inbox(data, cfg, Path(data_path).parent / "mcp")
        print(f"Queued {job.title} at {job.company} for Claude.")
        return rec
    if mode == "api":
        rec.update(ai.score_job(job, resume, ai_cfg.get("scoring_model", "claude-haiku-5-5"),
                                profile.summary_for_ai(prof)))
    else:
        rec.update(rules.as_final_score(job, resume, prof))
    rec["scored_at"] = scan._now()
    scan.make_card(job, rec, resume, ai_cfg, prof, problems, data, base_docx, out_root,
                   use_ai=mode == "api")  # you picked it, so always build the kit
    if github_api.enabled():
        rec["issue_url"] = github_api.turn_into_card(issue["number"], rec, prof)
    else:
        rec["issue_url"] = issue.get("html_url")

    store.save(data, data_path)
    dashboard.write(data, readme_path, prof)
    dashboard.write_resume_library(data, out_root)
    discord.send(discord.job_alert(rec, rec.get("issue_url")), (rec.get("tailored_resume") or {}).get("pdf"))
    for p in problems:
        print(p)
    return rec


if __name__ == "__main__":
    telemetry.main("add-job", lambda: handle(json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())))
