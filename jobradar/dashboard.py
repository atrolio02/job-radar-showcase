"""Rewrite README.md as your dashboard. GitHub shows it on the repo's home page."""
from __future__ import annotations

import hashlib
import hmac
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ACTIVE = ["new", "applied", "interviewing", "offer"]
APPLIED = ["applied", "interviewing", "offer"]
DONE = ["rejected", "skip", "closed"]
STATUS_ICON = {"new": "🆕", "applied": "📨", "interviewing": "🗣️", "offer": "🎉",
               "rejected": "✖️", "skip": "⏭️", "closed": "📁"}


def _cell(text: str | None) -> str:
    return (text or "").replace("|", "/").replace("\n", " ").strip()


# Buttons: each opens the "Move a job" form with its title filled in; press Submit and it's done.
BUTTONS = {"applied": "📨 Applied", "skip": "⏭️ Skip", "interviewing": "🗣️ Interviewing", "offer": "🎉 Offer",
           "rejected": "✖️ Rejected", "active": "↩️ Move to active"}
NEXT = {"new": ["applied", "skip"], "applied": ["interviewing", "rejected"],
        "interviewing": ["offer", "rejected"], "offer": ["rejected"],
        "rejected": ["active"], "skip": ["active"], "closed": ["active"]}


def _move_base() -> str | None:
    """The MCP server's address (config.yml `move_links`), if one-tap links are set up."""
    try:
        import yaml
        return ((yaml.safe_load(Path("config.yml").read_text()) or {}).get("move_links") or "").rstrip("/") or None
    except (OSError, ValueError):
        return None


def move_link(n: int, status: str) -> str:
    """One tap moves the card: a link to the MCP server signed for just this card and status.
    Without the server's secret (MOVE_LINK_SECRET), falls back to the prefilled "Move a job" form."""
    secret, base = os.environ.get("MOVE_LINK_SECRET", "").strip(), _move_base()
    if secret and base:
        sig = hmac.new(secret.encode(), f"move:{n}:{status}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"{base}/move/{n}/{status}/{sig}"
    return _repo_link(f"issues/new?template=move.yml&title=Move+%23{n}+to+{status}")


def short_id(key: str) -> str:
    """ID for a job without a card yet, like J-1a2b3c (cards use their number, like #8)."""
    return "J-" + hashlib.sha1(key.encode()).hexdigest()[:6]


def promote_link(key: str) -> str:
    """One tap gives a lower-scoring job a card (standard resume) and moves it to Active."""
    jid = short_id(key)
    secret, base = os.environ.get("MOVE_LINK_SECRET", "").strip(), _move_base()
    if secret and base:
        sig = hmac.new(secret.encode(), f"promote:{jid}".encode(), hashlib.sha256).hexdigest()[:32]
        return f"{base}/promote/{jid}/{sig}"
    return ""


def batch_link() -> str:
    """The page for moving several jobs at once (signed like the other links)."""
    secret, base = os.environ.get("MOVE_LINK_SECRET", "").strip(), _move_base()
    if secret and base:
        return f"{base}/batch/{hmac.new(secret.encode(), b'batch', hashlib.sha256).hexdigest()[:32]}"
    return ""


def _buttons(job: dict) -> str:
    n = job.get("issue_number")
    if not n:
        link = promote_link(job["key"]) if job.get("key") else ""
        return f"[➕ Move to active]({link})" if link else ""
    return " · ".join(f"[{BUTTONS[s]}]({move_link(n, s)})" for s in NEXT.get(job.get("status") or "new", []))


def _row(job: dict, buttons: bool = False) -> str:
    card = f"[card]({job['issue_url']})" if job.get("issue_url") else ""
    first = ""
    if buttons:
        jid = f"#{job['issue_number']}" if job.get("issue_number") else short_id(job.get("key", ""))
        first = f"**{jid}** | {_buttons(job)} | "
    return (f"| {first}**{job.get('score', '–')}** | {_cell(job['company'])} | "
            f"[{_cell(job['title'])}]({job.get('url', '')}) | {_cell(job.get('location'))[:40]} | "
            f"{_cell(job.get('pay_text'))[:24] or '–'} | "
            f"{STATUS_ICON.get(job.get('status'), '')} {job.get('status', '')} | "
            f"{(job.get('posted') or '')[:10]} | {card} |")


def _table(jobs: list[dict], buttons: bool = False, empty: str = "_Nothing here yet._",
           always_head: bool = False) -> list[str]:
    if not jobs and not always_head:
        return [empty]
    head = ["| " + ("Job ID | Move to (one tap) | " if buttons else "") + "Score | Company | Role | Location | Pay | Status | Posted | Tracker |",
            "|" + (":-:|---|" if buttons else "") + ":-:|---|---|---|---|---|---|:-:|"]
    order = {s: i for i, s in enumerate(ACTIVE + DONE)}
    jobs = sorted(jobs, key=lambda j: (order.get(j.get("status"), 99), -j.get("score", 0)))
    if not jobs:  # keep the columns visible so every section looks the same
        cols = head[0].count("|") - 1
        return head + ["| " + " | ".join(["–"] * cols) + " |", "", empty]
    return head + [_row(j, buttons) for j in jobs]


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
        *([f"**[☑️ Move several jobs at once]({batch_link()})**", ""] if batch_link() else []),
        f"[➕ Add a job you found elsewhere]({_repo_link('issues/new?template=add-job.yml')}) · "
        f"[⚙️ Retake the preferences quiz]({_repo_link('issues/new?template=preferences.yml')})",
        "",
        "## Active",
        "_Jobs to apply to. Tap **📨 Applied** once you've sent it; the job moves to Applied in about a minute._",
        "",
        *_table([j for j in tracked if j["status"] == "new"], buttons=True),
        "",
        "## Applied",
        *_table([j for j in tracked if j["status"] in APPLIED], buttons=True, always_head=True,
                empty="_Nothing yet. Jobs move here when you tap 📨 Applied._"),
        "",
        "## Closed out",
        *_table([j for j in tracked if j["status"] in DONE], buttons=True, always_head=True,
                empty="_Nothing yet. Skipped and rejected jobs land here, each with a ↩️ Move to active link._"),
        "",
        "<details><summary>Lower-scoring jobs (not alerted)</summary>",
        "",
        "_Scored under 6, so no card yet. Tap **➕ Move to active** to make one (standard resume)._",
        "",
        *_table([j for j in scored if not j.get("status")][:50], buttons=True),
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
