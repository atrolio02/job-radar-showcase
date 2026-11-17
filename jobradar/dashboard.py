"""Rewrite README.md as your dashboard. GitHub shows it on the repo's home page."""
from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ACTIVE = ["new", "applied", "interviewing", "offer"]
DONE = ["rejected", "skip", "closed"]
STATUS_ICON = {"new": "🆕", "applied": "📨", "interviewing": "🗣️", "offer": "🎉",
               "rejected": "✖️", "skip": "⏭️", "closed": "📁"}


def _cell(text: str | None) -> str:
    return (text or "").replace("|", "/").replace("\n", " ").strip()


def _row(job: dict) -> str:
    card = f"[card]({job['issue_url']})" if job.get("issue_url") else ""
    return (f"| **{job.get('score', '–')}** | {_cell(job['company'])} | "
            f"[{_cell(job['title'])}]({job.get('url', '')}) | {_cell(job.get('location'))[:40]} | "
            f"{_cell(job.get('pay_text'))[:24] or '–'} | "
            f"{STATUS_ICON.get(job.get('status'), '')} {job.get('status', '')} | "
            f"{(job.get('posted') or '')[:10]} | {card} |")


def _table(jobs: list[dict]) -> list[str]:
    if not jobs:
        return ["_Nothing here yet._"]
    head = ["| Score | Company | Role | Location | Pay | Status | Posted | Tracker |",
            "|:-:|---|---|---|---|---|---|:-:|"]
    order = {s: i for i, s in enumerate(ACTIVE + DONE)}
    jobs = sorted(jobs, key=lambda j: (order.get(j.get("status"), 99), -j.get("score", 0)))
    return head + [_row(j) for j in jobs]


def _repo_link(path: str) -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    return f"https://github.com/{repo}/{path}" if repo else path


def render(data: dict, now: datetime | None = None, prof: dict | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    jobs = list(data["jobs"].values())
    tracked = [j for j in jobs if j.get("status")]
    counts = Counter(j["status"] for j in tracked)
    scored = [j for j in jobs if "score" in j]
    funnel = " → ".join(f"{STATUS_ICON[s]} {s.title()} **{counts.get(s, 0)}**" for s in ACTIVE)

    lines = [
        "# 📡 Job Radar",
        "",
        f"_Updated {now.strftime('%b %d, %Y %H:%M')} UTC · {len(jobs)} matching jobs seen · "
        f"{len(scored)} scored by AI · {len(tracked)} strong matches_",
        "",
        funnel,
        "",
        *([] if (prof or {}).get("quiz_completed") else [
            f"> 👉 **[Take the job preferences quiz]({_repo_link('issues/new?template=preferences.yml')})** "
            "so Job Radar searches for the work style, places and pay you want.", ""]),
        f"[➕ Add a job you found elsewhere]({_repo_link('issues/new?template=add-job.yml')}) · "
        f"[⚙️ Retake the preferences quiz]({_repo_link('issues/new?template=preferences.yml')})",
        "",
        "## Active",
        *_table([j for j in tracked if j["status"] in ACTIVE]),
        "",
        "## Closed out",
        *_table([j for j in tracked if j["status"] in DONE]),
        "",
        "<details><summary>Lower-scoring jobs (not alerted)</summary>",
        "",
        *_table([j for j in scored if not j.get("status")][:50]),
        "",
        "</details>",
        "",
        "---",
        "How this works: [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) · "
        "Settings: [config.yml](config.yml) · Your resume: [resume.docx](resume.docx) · Tailored resumes: [resumes/](resumes)",
        "",
    ]
    return "\n".join(lines)


def write(data: dict, path: Path = Path("README.md"), prof: dict | None = None) -> None:
    if prof is None:
        from . import profile
        prof = profile.load()
    path.write_text(render(data, prof=prof))


def write_resume_library(data: dict, out_root: Path = Path("resumes")) -> None:
    """resumes/README.md: every tailored resume version, its tags, and the jobs using it."""
    library = data.get("resume_variants") or {}
    if not library:
        return
    jobs = data.get("jobs", {})
    lines = ["# 📄 Resume library", "",
             "Each version was tailored for one job and is reused for similar ones (same role type, "
             "overlapping tags). Cards using a version carry its `resume-` label.", "",
             "| Version | Role | Tags | Made for | Used by | Files |", "|---|---|---|---|:-:|---|"]
    for vid, v in sorted(library.items(), key=lambda kv: kv[1].get("created", ""), reverse=True):
        rel = lambda p: os.path.relpath(p, out_root) if os.path.isabs(p) or p.startswith(str(out_root)) else p
        lines.append(f"| `resume-{vid}` | {v.get('role_family', '')} | {', '.join(v.get('tags', []))} | "
                     f"{_cell(v.get('made_for'))} | {len(set(v.get('used_by', [])))} | "
                     f"[PDF]({rel(v['pdf'])}) · [Word]({rel(v['docx'])}) |")
        users = [jobs[k] for k in dict.fromkeys(v.get("used_by", [])) if k in jobs]
        if len(users) > 1:
            lines.append(f"| | | | ↳ also sent to: {_cell(', '.join(j['company'] for j in users[1:]))} | | |")
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "README.md").write_text("\n".join(lines) + "\n")
