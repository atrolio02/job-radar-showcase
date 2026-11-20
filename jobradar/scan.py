"""The daily scan. Run with:  python -m jobradar.scan

1. API calls to Ashby, Greenhouse and Lever pull every watched company's jobs.
2. Rules keep only your titles and locations.
3. Claude scores each new match against resume.docx and your preferences.
4. Strong matches get tailored bullets and a call script, a tracker card
   (GitHub Issue) and a Discord alert through its webhook.
5. data/jobs.json and the README dashboard are saved back to the repo.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from . import ai, dashboard, discord, filters, github_api, profile, rules, sources, store, tailor, telemetry

RESUME_DOCX = Path("resume.docx")
RESUMES_DIR = Path("resumes")


def load_config(path: str = "config.yml") -> dict:
    return yaml.safe_load(Path(path).read_text()) or {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def all_companies(cfg: dict, prof: dict) -> dict:
    """Companies from config.yml plus the dream companies added in the quiz."""
    merged = {k: list(v or []) for k, v in (cfg.get("companies") or {}).items()}
    for source, names in (prof.get("extra_companies") or {}).items():
        for n in names:
            if n not in merged.setdefault(source, []):
                merged[source].append(n)
    return merged


def application_questions(job) -> list[dict]:
    """Greenhouse publishes each form's custom questions; the others don't."""
    if job.source != "greenhouse":
        return []
    _, board, job_id = job.key.split(":", 2)
    try:
        return sources.greenhouse_questions(board, job_id)
    except (requests.RequestException, sources.BoardNotFound):
        return []


def company_snapshot(job, data: dict | None, ai_cfg: dict, problems: list) -> dict | None:
    """Employee ratings and CEO approval, reused for snapshot_days per company."""
    if not ai_cfg.get("company_snapshot", True):
        return None
    cache = data.setdefault("companies", {}) if data is not None else {}
    name = job.company.lower()
    cached = cache.get(name)
    if cached:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(cached["fetched_at"])
        if age.days < int(ai_cfg.get("snapshot_days", 30)):
            return cached
    try:
        snap = ai.company_snapshot(job.company, f"hiring for {job.title}; careers page {job.url}",
                                   ai_cfg.get("snapshot_model", "claude-haiku-5-5"),
                                   ai_cfg.get("web_search_tool", "web_search_20250305"))
    except ai.AIError as e:
        problems.append(f"Company snapshot for {job.company} unavailable: {e}")
        return cached
    snap["fetched_at"] = _now()
    cache[name] = snap
    return snap


def same_role(rec: dict) -> str:
    """Company + title, ignoring case and punctuation: one role posted in several cities."""
    return f"{rec.get('company', '').lower()}|{re.sub(r'[^a-z0-9]+', ' ', rec.get('title', '').lower()).strip()}"


def sibling(data: dict, key: str) -> str | None:
    """An already-scored posting of the same role, if there is one."""
    me = same_role(data["jobs"][key])
    for k, rec in data["jobs"].items():
        if k != key and "score" in rec and not rec.get("duplicate_of") and same_role(rec) == me:
            return k
    return None


def load_resume(path: str | Path) -> str:
    """Resume text for Claude: from resume.docx, or a plain-text file."""
    path = Path(path)
    text = tailor.resume_text(path) if path.suffix == ".docx" else path.read_text()
    try:
        return text + profile.extra_experience_text()
    except Exception:  # noqa: BLE001 - no profile (tests, first run): the resume alone
        return text


def default_resume_path() -> str:
    return str(RESUME_DOCX) if RESUME_DOCX.exists() else "resume.md"


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:40] or "job"


def similarity(a: list, b: list) -> float:
    a, b = set(a or []), set(b or [])
    return len(a & b) / len(a | b) if a and b else 0.0


def closest_variant(data: dict, rec: dict) -> tuple[str | None, float]:
    """The library resume made for the most similar job (same role type, most shared tags)."""
    best, best_sim = None, 0.0
    for vid, v in (data.get("resume_variants") or {}).items():
        if v.get("role_family") != rec.get("role_family") or not Path(v["pdf"]).exists():
            continue
        sim = similarity(v.get("tags"), rec.get("tags"))
        if sim > best_sim:
            best, best_sim = vid, sim
    return best, best_sim


def tailored_resume(job, rec: dict, ai_cfg: dict, prof: dict, problems: list,
                    base_docx: Path = RESUME_DOCX, out_root: Path = RESUMES_DIR, data: dict | None = None,
                    asked: dict | None = None, use_ai: bool = True) -> None:
    if not ai_cfg.get("tailored_resume", True) or not base_docx.exists():
        return
    data = data if data is not None else {}
    library = data.setdefault("resume_variants", {})

    # Close enough to one already made? Send that one: no AI cost, nothing new to review.
    vid, sim = closest_variant(data, rec)
    if vid and sim >= float(ai_cfg.get("reuse_resume_similarity", 0.6)):
        v = library[vid]
        v.setdefault("used_by", []).append(rec.get("key", job.key))
        rec["tailored_resume"] = {"pdf": v["pdf"], "docx": v["docx"], "variant": vid, "reused": True,
                                  "similarity": round(sim, 2), "made_for": v.get("made_for", ""),
                                  "changes": v.get("changes", "")}
        return

    if asked is None and not use_ai:
        return  # no rewrites from Claude and no API: the card links your standard resume
    name = _slug((prof.get("contact") or {}).get("full_name") or "Resume").replace("-", "_")
    vid = hashlib.sha1(job.key.encode()).hexdigest()[:6]
    try:
        result = tailor.build(job, base_docx, out_root / vid, f"{name}_Resume",
                              ai_cfg.get("writing_model", "claude-sonnet-5-5"), asked=asked)
    except (ai.AIError, RuntimeError, OSError, subprocess.SubprocessError) as e:
        problems.append(f"Tailored resume for {job.company} not made: {e}")
        return
    library[vid] = {"pdf": result["pdf"], "docx": result["docx"], "role_family": rec.get("role_family", "other"),
                    "tags": rec.get("tags", []), "made_for": f"{job.title} at {job.company}",
                    "made_for_key": job.key, "changes": result.get("changes", ""),
                    "created": _now(), "used_by": [job.key]}
    rec["tailored_resume"] = {**result, "variant": vid, "reused": False}


def make_card(job, rec: dict, resume: str, ai_cfg: dict, prof: dict, problems: list,
              data: dict | None = None, base_docx: Path = RESUME_DOCX, out_root: Path = RESUMES_DIR,
              kit: dict | None = None, use_ai: bool = True) -> None:
    """Strong match: tailored resume, application kit and materials, company snapshot; mark it new.
    `kit` holds work Claude already did through the MCP server; `use_ai` allows API calls for the rest."""
    kit = kit or {}
    if kit.get("materials"):
        rec["materials"] = kit["materials"]
    elif use_ai:
        try:
            with telemetry.stage("materials"):
                rec["materials"] = ai.write_materials(job, resume, ai_cfg.get("writing_model", "claude-sonnet-5-5"),
                                                      prof, application_questions(job))
        except ai.AIError as e:
            problems.append(f"Claude API (writing): {e}")
    if kit.get("research"):
        snap = {**kit["research"], "fetched_at": _now()}
        if data is not None:
            data.setdefault("companies", {})[job.company.lower()] = snap
    elif use_ai:
        with telemetry.stage("company snapshot"):
            snap = company_snapshot(job, data, ai_cfg, problems)
    else:
        snap = ((data or {}).get("companies") or {}).get(job.company.lower())  # cached research, if any
    if snap:
        rec["company_snapshot"] = snap
    with telemetry.stage("tailored resume"):
        tailored_resume(job, rec, ai_cfg, prof, problems, base_docx, out_root, data,
                        asked=kit.get("tailoring"), use_ai=use_ai)
    rec["status"] = "new"
    rec["history"] = [{"status": "new", "at": _now()}]


def open_card(job, rec: dict, data: dict, resume: str, ai_cfg: dict, prof: dict, problems: list,
              base_docx: Path = RESUME_DOCX, out_root: Path = RESUMES_DIR, kit: dict | None = None,
              use_ai: bool = True) -> None:
    """Build the kit, open the tracker card (GitHub Issue) and send the Discord alert."""
    rec.pop("card_waiting", None)
    with telemetry.stage("cards"):
        make_card(job, rec, resume, ai_cfg, prof, problems, data, base_docx, out_root, kit=kit, use_ai=use_ai)
        if github_api.enabled():
            try:
                with telemetry.stage("tracker card"):
                    if rec.get("issue_number"):  # a job you added by hand already has its issue
                        rec["issue_url"] = github_api.turn_into_card(rec["issue_number"], rec, prof)
                    else:
                        rec["issue_number"], rec["issue_url"] = github_api.create_issue(rec, prof)
            except requests.RequestException as e:
                problems.append(f"Couldn't open a tracker card for {rec['title']} at {rec['company']}: {e}")
        with telemetry.stage("discord alert"):
            discord.send(discord.job_alert(rec, rec.get("issue_url")),
                         (rec.get("tailored_resume") or {}).get("pdf"))


def ai_mode(ai_cfg: dict) -> str:
    """mcp: Claude does the last mile through the MCP server (default, no API bill).
    api: the scan calls the Claude API itself (needs ANTHROPIC_API_KEY).
    rules: no AI at all; the rules score is the final score and kits are skipped."""
    mode = str(ai_cfg.get("mode", "mcp")).lower()
    return mode if mode in ("mcp", "api", "rules") else "mcp"


def run(config_path: str = "config.yml", resume_path: str | None = None,
        data_path: Path = store.DEFAULT_PATH, readme_path: Path = Path("README.md"),
        profile_path: Path = profile.DEFAULT_PATH, base_docx: Path = RESUME_DOCX,
        out_root: Path = RESUMES_DIR, mcp_dir: Path | None = None) -> dict:
    from . import lastmile  # imported here: lastmile builds on this module
    mcp_dir = mcp_dir or Path(data_path).parent / "mcp"  # data/mcp next to data/jobs.json
    with telemetry.stage("setup"):
        cfg = load_config(config_path)
        prof = profile.load(profile_path)
        prefs = profile.summary_for_ai(prof)
        resume = load_resume(resume_path or default_resume_path())
        data = store.load(data_path)
    ai_cfg = cfg.get("ai", {})
    alert_min = int(cfg.get("alert_min_score", 7))
    mode = ai_mode(ai_cfg)
    rules_min = int(ai_cfg.get("rules_min_score", 40))
    problems: list[str] = []
    if mode == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
        problems.append("ai.mode is api but ANTHROPIC_API_KEY isn't set, so jobs went to the MCP inbox instead.")
        mode = "mcp"

    # 1-2. Fetch and filter (free)
    with telemetry.stage("fetch"):
        # Only jobs with a wanted title are kept in memory; every job read still counts in the total.
        fetched, fetch_problems = sources.fetch_all(all_companies(cfg, prof),
                                                    keep=lambda j: filters.title_ok(j, cfg, prof))
    problems += fetch_problems
    # Workday lists only a title and a short location: look up the full posting for new jobs with a
    # matching title (it has the real places and remote/hybrid), so the location filter can judge it.
    with telemetry.stage("workday details"):
        for job in fetched:
            if job.source != "workday" or not filters.title_ok(job, cfg, prof):
                continue
            seen = data["jobs"].get(job.key)
            if seen and seen.get("location"):  # looked up on an earlier scan
                job.location, job.workplace = seen["location"], seen.get("workplace", job.workplace)
                job.remote = job.workplace == "remote"
                job.posted = seen.get("posted") or job.posted
            else:
                sources.enrich(job)
    with telemetry.stage("filter"):
        matching = {j.key: j for j in fetched if filters.matches(j, cfg, prof)}
    print(f"Fetched {fetched.total} jobs; {len(matching)} match your titles and locations. AI mode: {mode}.")

    for key, job in matching.items():
        rec = data["jobs"].setdefault(key, {"first_seen": _now()})
        info = job.to_dict()
        info.pop("description")
        rec.update(info)
        rec["last_seen"] = _now()
        if (rec.get("rescore_pending") or rec.get("retailor")) and not rec.get("pending_brief"):
            rec["pending_brief"] = ai.clean_description(job.description, lastmile.KIT_CHARS)

    # Some job sites (Oracle) list only a summary: fetch the full posting for new matches only.
    with telemetry.stage("full descriptions"):
        for key, job in matching.items():
            rec = data["jobs"][key]
            if "score" not in rec and "rules_score" not in rec:
                sources.enrich(job)

    # A focus (profile.yml) sets aside queued jobs outside it; they come back when the focus is removed.
    for rec in data["jobs"].values():
        if rec.get("queued_at") and not filters.focus_ok(rec.get("title"), rec.get("company"), cfg, prof):
            rec.pop("queued_at", None)
            rec.pop("pending_brief", None)

    # 3a. Rules pre-score (free): only promising jobs go on to Claude
    with telemetry.stage("rules"):
        for key, job in matching.items():
            rec = data["jobs"][key]
            if "score" not in rec and "rules_score" not in rec:
                rec["rules_score"], rec["rules_reasons"], rec["rules_tags"] = rules.score(job, resume, prof)
    candidates = [k for k in matching if "score" not in data["jobs"][k] and not data["jobs"][k].get("queued_at")
                  and data["jobs"][k].get("ai_errors", 0) < 3]
    below = [k for k in candidates if data["jobs"][k]["rules_score"] < rules_min]
    candidates = [k for k in candidates if data["jobs"][k]["rules_score"] >= rules_min]
    candidates.sort(key=lambda k: (-data["jobs"][k]["rules_score"], matching[k].posted or ""))
    if below:
        print(f"{len(below)} jobs scored under {rules_min}/100 on the rules and were skipped (no AI cost).")

    scored, new_matches, ai_problem, reused, queued = 0, 0, None, 0, 0
    limit = int(ai_cfg.get("max_jobs_scored_per_run", 25))
    max_cards = int(ai_cfg.get("max_cards_per_run", 5))
    score_fields = ["score", "summary", "strengths", "gaps", "scored_at"]
    use_ai = mode == "api"

    def card(key: str, kit: dict | None = None) -> None:
        nonlocal new_matches
        open_card(matching.get(key) or lastmile.job_from_record(data["jobs"][key]), data["jobs"][key], data,
                  resume, ai_cfg, prof, problems, base_docx, out_root, kit=kit, use_ai=use_ai)
        for k in ("kit_pending", "pending_brief", "questions"):
            data["jobs"][key].pop(k, None)
        new_matches += 1

    # Strong matches that hit an earlier card limit go first, best scores first
    for key in sorted((k for k in matching if data["jobs"][k].get("card_waiting")
                       and not data["jobs"][k].get("status")),
                      key=lambda k: -data["jobs"][k].get("score", 0)):
        if new_matches < max_cards:
            card(key)

    # MCP mode: a kit Claude never sent doesn't block the alert for long
    if mode == "mcp":
        wait = float(ai_cfg.get("kit_wait_hours", 30))
        for key, rec in sorted(data["jobs"].items(), key=lambda kv: -kv[1].get("score", 0)):
            if rec.get("kit_pending") and not rec.get("status") and lastmile._age_hours(rec["kit_pending"]) > wait:
                if new_matches < max_cards:
                    problems.append(f"No kit from Claude after {wait:.0f}h for {rec['title']} at {rec['company']}; "
                                    "opened the card without one.")
                    card(key)

    waiting = 0
    for key in candidates:
        job, rec = matching[key], data["jobs"][key]
        # Same role at the same company posted in several places: score it once
        twin = sibling(data, key)
        if twin:
            rec.update({f: data["jobs"][twin][f] for f in score_fields if f in data["jobs"][twin]})
            rec["duplicate_of"] = twin
            reused += 1
            continue
        if mode == "mcp":
            # 3b. The last mile: queue it for Claude (through the MCP server)
            if any(r.get("queued_at") and same_role(r) == same_role(rec) for r in data["jobs"].values()):
                continue  # its twin is already waiting in the inbox
            rec["queued_at"] = _now()
            rec["pending_brief"] = ai.clean_description(job.description, lastmile.KIT_CHARS)
            with telemetry.stage("application questions"):
                rec["questions"] = application_questions(job)  # free public API; ready if it's a strong match
            queued += 1
            continue
        if scored >= limit:
            waiting += 1
            continue
        try:
            with telemetry.stage("score"):
                if mode == "api":
                    rec.update(ai.score_job(job, resume, ai_cfg.get("scoring_model", "claude-haiku-5-5"), prefs))
                else:
                    rec.update(rules.as_final_score(job, resume, prof))
            rec["scored_at"] = _now()
            scored += 1
        except ai.AIError as e:
            rec["ai_errors"] = rec.get("ai_errors", 0) + 1
            ai_problem = str(e)
            continue

        # 4. Strong match: kit (in api mode), tracker card, Discord alert
        if rec["score"] >= alert_min and not rec.get("status"):
            if new_matches < max_cards:
                card(key)
            else:
                rec["card_waiting"] = True  # daily cap reached: built on the next run
    if waiting:
        print(f"{waiting} jobs will be scored on a later run (limit {limit} per run).")
    if reused:
        print(f"{reused} repeat postings reused an earlier score (no AI cost).")
    if ai_problem:
        problems.append(f"Claude API: {ai_problem}")

    # Nobody has worked the inbox for days? Say so, so the radar doesn't go quiet unnoticed.
    if mode == "mcp":
        oldest = max((lastmile._age_hours(r["queued_at"]) for r in data["jobs"].values() if r.get("queued_at")),
                     default=0)
        stale = float(ai_cfg.get("inbox_stale_hours", 72))
        if oldest > stale:
            problems.append(f"Jobs have waited {oldest / 24:.0f} days for Claude to score them. "
                            "Check that the daily Claude task is running and the Job Radar connector works.")

    # 4b. Scored 6 (card_min_score) but under the alert score: a card with the standard resume
    from . import promote  # imported here: promote builds on this module
    with telemetry.stage("standard cards"):
        promote.auto_cards(data, cfg, prof, problems)

    # 5. Save memory, dashboard, and Claude's briefing and inbox
    with telemetry.stage("save"):
        store.save(data, data_path)
        dashboard.write(data, readme_path, prof)
        dashboard.write_resume_library(data, out_root)
        lastmile.write_context(cfg, prof, resume, base_docx, mcp_dir)
        inbox = lastmile.write_inbox(data, cfg, mcp_dir)

    summary = {"fetched": fetched.total, "matching": len(matching), "scored": scored,
               "new_matches": new_matches, "waiting": waiting + inbox["to_score_waiting"],
               "skipped_by_rules": len(below), "queued_for_claude": len(inbox["to_score"]),
               "kits_for_claude": len(inbox["strong_matches"]), "mode": mode, "problems": problems}
    telemetry.count(**{k: v for k, v in summary.items() if k != "problems"}, reused=reused, newly_queued=queued)
    for p in problems:
        telemetry.warn(p)
    if new_matches or problems:
        with telemetry.stage("discord alert"):
            discord.send(discord.run_summary(new_matches, scored, len(matching), problems, github_api.repo_url()))
    _write_step_summary(summary)
    print(summary)
    return summary


def _write_step_summary(s: dict) -> None:
    """Show a short report on the GitHub Actions run page."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["## Job Radar scan",
             f"- Jobs fetched: **{s['fetched']}**",
             f"- Matching your titles and locations: **{s['matching']}**",
             f"- AI mode: **{s.get('mode', 'api')}**. Skipped by the free rules check: {s.get('skipped_by_rules', 0)}",
             f"- Scored this run: **{s['scored']}** (waiting: {s['waiting']})",
             f"- In Claude's inbox: {s.get('queued_for_claude', 0)} to score, {s.get('kits_for_claude', 0)} kits to write",
             f"- New strong matches: **{s['new_matches']}**"]
    if s["problems"]:
        lines += ["", "### Needs attention"] + [f"- {p}" for p in s["problems"]]
    with open(path, "a") as f:
        f.write("\n".join(lines) + "\n")


def main() -> None:
    if github_api.enabled():
        with telemetry.stage("labels"):
            github_api.ensure_labels()
    run()


if __name__ == "__main__":
    prices = ((load_config().get("health") or {}).get("prices")) if Path("config.yml").exists() else None
    telemetry.main("scan", main, prices=prices)
