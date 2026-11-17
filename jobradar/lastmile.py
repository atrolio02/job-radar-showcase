"""The last mile: the part Claude does through the MCP server.

The free code (GitHub Actions) does everything it can: fetch, filter, pre-score
with rules, remember, dedupe, build PDFs, open tracker cards, send alerts.
What's left needs judgment and writing. That is the "last mile":

  1. score the jobs that passed the rules        -> Claude, via `submit_scores`
  2. write the kit for each strong match         -> Claude, via `submit_application_kit`
     (reworded bullets, call script, answers, tailored resume rewrites,
      and company research when the cached research is stale)

Files Claude reads (written here, after every scan and every apply):
  data/mcp/context.json   resume, preferences, facts, and the exact rules for each task
  data/mcp/inbox.json     jobs waiting to be scored, and strong matches waiting for a kit

Files Claude writes (through the MCP server, one new file per submission):
  data/mcp/incoming/*.json

A push of a file into data/mcp/incoming/ starts the "Apply Claude's work" workflow,
which runs:  python -m jobradar.lastmile
It checks every submission with the same safety rules as the API path, then
builds cards, PDFs and alerts. No AI is called here.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import ai, dashboard, profile, scan, sources, store, tailor, telemetry

MCP_DIR = Path("data/mcp")
BRIEF_CHARS = 2500   # what Claude reads to score a job
KIT_CHARS = 6000     # what Claude reads to write a kit


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_hours(ts: str | None) -> float:
    if not ts:
        return 0.0
    return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 3600


def job_from_record(rec: dict) -> sources.Job:
    fields = {k: rec.get(k) for k in ("key", "source", "company", "title", "location", "remote", "url", "posted")}
    job = sources.Job(**fields, description=rec.get("pending_brief", ""))
    job.workplace = rec.get("workplace", "unknown")
    job.pay_min, job.pay_max, job.pay_text = rec.get("pay_min"), rec.get("pay_max"), rec.get("pay_text", "")
    return job


# ── What Claude reads ───────────────────────────────────────────────────────
INSTRUCTIONS = {
    "workflow": (
        "1) Call get_jobs_to_score and score every job in it, then call submit_scores once with all of them. "
        "Its reply lists write_kits_now: your strong matches, with the full posting and application questions. "
        "2) For each of those, write the application kit and call submit_application_kit once per job. "
        "3) Call get_strong_matches and do the same for any job still listed there (older matches, jobs added "
        "by hand). Include research only when research_needed is true, and tailoring unless tailoring_needed "
        "is false. 4) Reply with a short summary: how many jobs you scored and which ones got kits."),
    "scoring": ai.SCORE_SYSTEM + " Score each job on how well it fits the candidate (resume and preferences "
               "in this briefing): both whether his experience matches what they need, and whether the job "
               "matches what he wants. 7+ means worth applying; at most 4 if it clearly hits a deal-breaker "
               "or pays well below his minimum. The rules_score is a free keyword pre-score; use your own judgment.",
    "kit": ai.WRITE_SYSTEM + " Answer every application question from the resume and candidate facts. For a "
           "question with options, answer with one of the options exactly. If the facts don't cover it, answer "
           "\"[Fill in yourself]\". Skip demographic or voluntary self-identification questions. Bullets: 3, "
           "reworded from the resume to mirror the posting. why_company: 3-4 sentences. cold_call: about 80 words, "
           "asking the hiring leader for 15 minutes. linkedin_note: under 300 characters.",
    "tailoring": tailor.TAILOR_SYSTEM.replace(" Reply with JSON only.", "") + " Use the numbered bullets in "
                 "resume_parts. Rewrite the summary and at most 6 bullets, each about the same length or shorter, "
                 "then list up to 6 things the posting wants that the resume doesn't show (don't add them).",
    "research": ai.RESEARCH_SYSTEM.replace(" Reply with JSON only.", "") + " Find the company's overall employee "
                "rating, CEO approval and recommend-to-a-friend figures (Glassdoor, Indeed, Comparably), common "
                "review themes, recent Reddit/X threads about working or interviewing there, and layoffs, lawsuits "
                "or leadership changes in the last year. Use \"\" or [] for anything you didn't find.",
}


def write_context(cfg: dict, prof: dict, resume: str, base_docx: Path = scan.RESUME_DOCX,
                  mcp_dir: Path = MCP_DIR) -> Path:
    summary, bullets = tailor.editable_parts(base_docx) if base_docx.exists() else ("", [])
    ctx = {
        "updated": _now(),
        "resume": resume,
        "resume_parts": {"summary": summary, "bullets": [{"n": i, "text": b} for i, b in enumerate(bullets)]},
        "preferences": profile.summary_for_ai(prof),
        "candidate_facts": ai._facts(prof),
        "alert_min_score": int(cfg.get("alert_min_score", 7)),
        "role_families": ai.ROLE_FAMILIES,
        "tags": ai.TAGS,
        "instructions": INSTRUCTIONS,
    }
    mcp_dir.mkdir(parents=True, exist_ok=True)
    path = mcp_dir / "context.json"
    path.write_text(json.dumps(ctx, indent=1, ensure_ascii=False) + "\n")
    return path


def _research_needed(data: dict, rec: dict, ai_cfg: dict) -> bool:
    cached = (data.get("companies") or {}).get((rec.get("company") or "").lower())
    fresh = cached and _age_hours(cached.get("fetched_at")) < 24 * int(ai_cfg.get("snapshot_days", 30))
    return bool(ai_cfg.get("company_snapshot", True)) and not fresh


def _brief(rec: dict, chars: int) -> dict:
    return {"key": rec["key"], "company": rec.get("company"), "title": rec.get("title"),
            "location": rec.get("location"), "workplace": rec.get("workplace"), "pay": rec.get("pay_text") or "",
            "posted": (rec.get("posted") or "")[:10], "url": rec.get("url"),
            "rules_score": rec.get("rules_score"), "rules_notes": rec.get("rules_reasons", []),
            "posting": (rec.get("pending_brief") or "")[:chars]}


def to_score(data: dict) -> list[dict]:
    return sorted((r for r in data["jobs"].values() if r.get("queued_at") and "score" not in r
                   and not r.get("status")), key=lambda r: -(r.get("rules_score") or 0))


def to_build(data: dict) -> list[dict]:
    return sorted((r for r in data["jobs"].values() if r.get("kit_pending") and not r.get("status")),
                  key=lambda r: -r.get("score", 0))


def write_inbox(data: dict, cfg: dict, mcp_dir: Path = MCP_DIR) -> dict:
    ai_cfg = cfg.get("ai", {})
    limit = int(ai_cfg.get("max_jobs_scored_per_run", 25))
    cards = int(ai_cfg.get("max_cards_per_run", 5))
    scoring = [{**_brief(r, BRIEF_CHARS), "research_needed": _research_needed(data, r, ai_cfg)}
               for r in to_score(data)]
    building = []
    for r in to_build(data)[:cards]:
        item = _brief(r, KIT_CHARS)
        item.update(score=r.get("score"), summary=r.get("summary"), strengths=r.get("strengths", []),
                    gaps=r.get("gaps", []), application_questions=r.get("questions", []))
        item["research_needed"] = _research_needed(data, r, ai_cfg)
        vid, sim = scan.closest_variant(data, r)
        reuse = vid and sim >= float(ai_cfg.get("reuse_resume_similarity", 0.6))
        item["tailoring_needed"] = bool(ai_cfg.get("tailored_resume", True)) and not reuse
        building.append(item)
    inbox = {"updated": _now(), "to_score": scoring[:limit], "to_score_waiting": max(0, len(scoring) - limit),
             "strong_matches": building, "strong_matches_waiting": max(0, len(to_build(data)) - len(building))}
    mcp_dir.mkdir(parents=True, exist_ok=True)
    (mcp_dir / "inbox.json").write_text(json.dumps(inbox, indent=1, ensure_ascii=False) + "\n")
    return inbox


# ── What Claude sends back ──────────────────────────────────────────────────
def _apply_scores(items: list, data: dict, alert_min: int, problems: list) -> int:
    done = 0
    for item in items if isinstance(items, list) else []:
        rec = data["jobs"].get(str((item or {}).get("key", "")))
        if rec is None:
            problems.append(f"Claude scored a job Job Radar doesn't know: {item.get('key')!r}")
            continue
        if "score" in rec:
            continue  # already scored (a repeat submission)
        try:
            rec.update(ai.clean_score(item))
        except ai.AIError as e:
            problems.append(f"Score for {rec.get('title')} at {rec.get('company')} rejected: {e}")
            continue
        rec.update(scored_at=_now(), scored_by="claude-mcp")
        rec.pop("queued_at", None)
        done += 1
        # Same role posted in several places: one score covers them all.
        for other_key, other in data["jobs"].items():
            if other is not rec and "score" not in other and scan.same_role(other) == scan.same_role(rec):
                other.update({f: rec[f] for f in ("score", "summary", "strengths", "gaps", "scored_at")})
                other["duplicate_of"] = rec["key"]
                other.pop("queued_at", None)
                other.pop("pending_brief", None)
        if (rec["score"] >= alert_min or rec.get("force_kit")) and not rec.get("status"):
            rec["kit_pending"] = _now()
            if "questions" not in rec:
                with telemetry.stage("application questions"):
                    rec["questions"] = scan.application_questions(job_from_record(rec))
        else:
            rec.pop("pending_brief", None)
    return done


def _apply_kit(item: dict, data: dict, cfg: dict, prof: dict, resume: str, problems: list,
               base_docx: Path, out_root: Path) -> bool:
    rec = data["jobs"].get(str(item.get("key", "")))
    if rec is None:
        problems.append(f"Claude sent a kit for a job Job Radar doesn't know: {item.get('key')!r}")
        return False
    if rec.get("status"):
        return False  # card already made
    if "score" not in rec:
        problems.append(f"Kit for {rec.get('title')} at {rec.get('company')} arrived before its score; ignored.")
        return False
    if rec["score"] < int(cfg.get("alert_min_score", 7)) and not rec.get("force_kit"):
        problems.append(f"Kit for {rec.get('title')} at {rec.get('company')} ignored: it scored "
                        f"{rec['score']}, under the alert score.")
        return False
    kit = {}
    if isinstance(item.get("materials"), dict):
        kit["materials"] = ai.clean_materials(item["materials"])
    if isinstance(item.get("research"), dict):
        kit["research"] = ai.clean_snapshot(item["research"], rec.get("company", ""))
    if isinstance(item.get("tailoring"), dict):
        kit["tailoring"] = tailor.clean_tailoring(item["tailoring"])
    scan.open_card(job_from_record(rec), rec, data, resume, cfg.get("ai", {}), prof, problems,
                   base_docx, out_root, kit=kit, use_ai=False)
    for k in ("kit_pending", "pending_brief", "questions", "force_kit"):
        rec.pop(k, None)
    return True


def apply_incoming(config_path: str = "config.yml", data_path: Path = store.DEFAULT_PATH,
                   readme_path: Path = Path("README.md"), profile_path: Path = profile.DEFAULT_PATH,
                   base_docx: Path = scan.RESUME_DOCX, out_root: Path = scan.RESUMES_DIR,
                   mcp_dir: Path | None = None, resume_path: str | None = None) -> dict:
    mcp_dir = mcp_dir or Path(data_path).parent / "mcp"
    cfg, prof = scan.load_config(config_path), profile.load(profile_path)
    resume = scan.load_resume(resume_path or scan.default_resume_path())
    data = store.load(data_path)
    alert_min = int(cfg.get("alert_min_score", 7))
    problems: list[str] = []
    scored = kits = 0
    processed = []
    for path in sorted((mcp_dir / "incoming").glob("*.json")):
        try:
            sub = json.loads(path.read_text())
            kind = sub.get("kind")
            if kind == "scores":
                with telemetry.stage("apply scores"):
                    scored += _apply_scores(sub.get("items"), data, alert_min, problems)
            elif kind == "kit":
                with telemetry.stage("apply kit"):
                    kits += _apply_kit(sub, data, cfg, prof, resume, problems, base_docx, out_root)
            else:
                problems.append(f"{path.name}: unknown submission kind {kind!r}")
        except (ValueError, TypeError, AttributeError, KeyError, ai.AIError) as e:
            problems.append(f"{path.name} couldn't be used ({e.__class__.__name__}: {e})")
            telemetry.error(f"apply {path.name}", e)
        processed.append(path)
        data.setdefault("mcp", {})["last_submission"] = _now()

    with telemetry.stage("save"):
        store.save(data, data_path)
        dashboard.write(data, readme_path, prof)
        dashboard.write_resume_library(data, out_root)
        write_context(cfg, prof, resume, base_docx, mcp_dir)
        write_inbox(data, cfg, mcp_dir)
    for p in processed:
        p.unlink()
    runner_temp = __import__("os").environ.get("RUNNER_TEMP")
    if runner_temp:  # the save step deletes these again if it has to merge with a newer version
        Path(runner_temp, "processed.txt").write_text("\n".join(str(p) for p in processed) + "\n")
    for p in problems:
        telemetry.warn(p)
    summary = {"submissions": len(processed), "scored": scored, "kits": kits, "problems": problems}
    telemetry.count(**{k: v for k, v in summary.items() if k != "problems"}, new_matches=kits)
    print(summary)
    return summary


def refresh(config_path: str = "config.yml") -> None:
    """Rebuild Claude's briefing and inbox from the saved files (used after two updates are merged)."""
    cfg = scan.load_config(config_path)
    write_context(cfg, profile.load(), scan.load_resume(scan.default_resume_path()))
    write_inbox(store.load(), cfg)


if __name__ == "__main__":
    if "--refresh" in sys.argv:
        refresh()
    else:
        telemetry.main("apply", apply_incoming)
