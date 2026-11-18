"""GitHub REST API calls: one tracker card (a GitHub Issue) per strong match.

Inside GitHub Actions, GITHUB_TOKEN and GITHUB_REPOSITORY are provided
automatically, so no extra setup is needed for this part.
"""
from __future__ import annotations

import os
from urllib.parse import quote, quote_plus

import requests

API = "https://api.github.com"

STATUS_LABELS = {
    # label name: (color, description)
    "applied": ("1d76db", "You applied"),
    "interviewing": ("5319e7", "In the interview process"),
    "offer": ("0e8a16", "Offer received"),
    "rejected": ("b60205", "Turned down"),
    "skip": ("cccccc", "Not interested"),
    "job": ("fbca04", "Job Radar card"),
    "preferences": ("c5def5", "Job preferences quiz"),
    "move": ("ededed", "Dashboard button form (closes itself)"),
    "add-job": ("bfd4f2", "Job added by hand"),
    "radar-error": ("d93f0b", "Job Radar hit a problem (opened automatically)"),
}


def _repo() -> str | None:
    return os.environ.get("GITHUB_REPOSITORY")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def enabled() -> bool:
    return bool(os.environ.get("GITHUB_TOKEN") and _repo())


def ensure_labels() -> None:
    for name, (color, desc) in STATUS_LABELS.items():
        r = requests.post(f"{API}/repos/{_repo()}/labels", headers=_headers(), timeout=30,
                          json={"name": name, "color": color, "description": desc})
        if r.status_code not in (201, 422):  # 422 = label already exists
            r.raise_for_status()


def file_link(path: str) -> str:
    """Link to a file saved in this repo (GitHub shows PDFs in the browser)."""
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    return f"{repo_url()}/blob/{branch}/{quote(path)}" if repo_url() else path


STANDARD_RESUME_PDF = "resumes/standard/Jordan_Rivera_Resume.pdf"  # config.yml standard_resume_pdf overrides


def _standard_pdf() -> str:
    try:
        import yaml
        return (yaml.safe_load(open("config.yml")) or {}).get("standard_resume_pdf") or STANDARD_RESUME_PDF
    except (OSError, ValueError):
        return STANDARD_RESUME_PDF


def _tailored(job: dict) -> list[str]:
    tr = job.get("tailored_resume")
    if not tr and job.get("standard_resume"):
        return ["", "## 📄 Resume", "",
                f"**[Download PDF]({file_link(_standard_pdf())})** · [Word version]({file_link('resume.docx')})", "",
                "_This job scored under 7, so it uses your standard resume (no tailoring)._"]
    if not tr:
        return []
    if tr.get("reused"):
        return ["", "## 📄 Tailored resume", "",
                f"**[Download PDF]({file_link(tr['pdf'])})** · [Word version]({file_link(tr['docx'])}) · "
                f"version `resume-{tr.get('variant')}`", "",
                f"♻️ Reusing the resume made for **{tr.get('made_for', 'a similar job')}**: this job's needs overlap "
                f"{int(tr.get('similarity', 0) * 100)}%, so there's nothing new to review."]
    what = []
    if tr.get("summary_changed"):
        what.append("summary")
    if tr.get("changed_bullets"):
        what.append(f"{tr['changed_bullets']} bullets")
    out = ["", "## 📄 Tailored resume", "",
           f"**[Download PDF]({file_link(tr['pdf'])})** · [Word version]({file_link(tr['docx'])})", "",
           f"Reworded: {', '.join(what) or 'nothing (your resume already fit)'}. {tr.get('changes', '')}".strip()]
    if tr.get("rejected_for_safety"):
        out.append(f"_{tr['rejected_for_safety']} suggested edits were thrown out for adding numbers "
                   "or running long._")
    out.append("_Every fact and number is from your real resume. Skim it before sending._")
    if tr.get("missing"):
        out += ["", "**The posting asks for these, and your resume doesn't show them.** If any are true, "
                "add them to `resume.docx` once and every future copy will include them:"]
        out += [f"- {m}" for m in tr["missing"]]
    return out


def review_links(company: str) -> str:
    q = quote_plus(company)
    talk = quote_plus(f'"{company}" (working OR interview OR culture)')
    return (f"[Glassdoor](https://www.glassdoor.com/Search/results.htm?keyword={q}) · "
            f"[Indeed reviews](https://www.indeed.com/companies/search?q={q}) · "
            f"[LinkedIn](https://www.linkedin.com/search/results/companies/?keywords={q}) · "
            f"[Reddit](https://www.reddit.com/search/?q={talk}) · "
            f"[X](https://x.com/search?q={talk}&f=live)")


def _snapshot(job: dict) -> list[str]:
    snap = job.get("company_snapshot") or {}
    name = snap.get("company_name") or job["company"]
    out = ["", "## 🏢 What employees say", ""]
    facts = [("Overall rating", snap.get("rating")), ("CEO approval", snap.get("ceo")),
             ("Would recommend", snap.get("recommend"))]
    found = [(k, v) for k, v in facts if v]
    if found:
        out += [f"- **{k}:** {v}" for k, v in found]
        if snap.get("as_of"):
            out.append(f"- _Figures as of {snap['as_of']}_")
    elif snap:
        out.append("_No ratings found in search. Check the links below._")
    if snap.get("pros"):
        out += ["", "**Employees like:** " + "; ".join(snap["pros"])]
    if snap.get("cons"):
        out += ["", "**Common complaints:** " + "; ".join(snap["cons"])]
    if snap.get("red_flags"):
        out += ["", "**⚠️ Worth asking about:**"] + [f"- {f}" for f in snap["red_flags"]]
    if snap.get("discussions"):
        out += ["", "**On Reddit and X** _(individual opinions, not facts)_"]
        out += [f"- {d['where']}: {d['summary']} ([thread]({d['url']}))" for d in snap["discussions"]]
    if snap.get("sources"):
        out += ["", "Sources: " + " · ".join(f"[{i + 1}]({u})" for i, u in enumerate(snap["sources"]))]
    out += ["", f"Read the reviews yourself: {review_links(name)}",
            "_Ratings come from web search and can be out of date; check them before relying on them._"]
    return out


def _kit(job: dict, prof: dict) -> list[str]:
    """Copy-paste answers for the application form."""
    c = (prof or {}).get("contact") or {}
    m = job.get("materials") or {}
    first, _, last = (c.get("full_name") or "").partition(" ")
    rows = [("First name", first), ("Last name", last), ("Email", c.get("email")), ("Phone", c.get("phone")),
            ("Location", c.get("city")), ("LinkedIn", c.get("linkedin")), ("GitHub / portfolio", c.get("github")),
            ("Authorized to work in the US", (prof or {}).get("work_authorization")),
            ("Needs sponsorship", (prof or {}).get("needs_sponsorship")),
            ("Start date", (prof or {}).get("start_date")),
            ("Salary expectations", (prof or {}).get("salary_answer"))]
    out = ["", "## 📝 Application kit", "",
           "_Tap the copy button on each box and paste it into the form. Read every answer before you submit._"]
    for k, v in rows:
        out += ["", f"**{k}**"] + (["```", v, "```"] if v else ["_[fill in yourself]_"])
    tr = job.get("tailored_resume") or {}
    attach = (f"Attach your tailored resume ([PDF]({file_link(tr['pdf'])})). " if tr.get("pdf")
              else f"Attach your standard resume ([PDF]({file_link(_standard_pdf())})). " if job.get("standard_resume")
              else "Attach your resume PDF. ")
    out += ["", attach + "Answer any demographic questions yourself."]
    if m.get("why_company"):
        out += ["", "**Why do you want to work here?**", "```", m["why_company"], "```"]
    for a in m.get("answers") or []:
        out += ["", f"**{a['question']}**", "```", a["answer"], "```"]
    return out


def issue_body(job: dict, prof: dict | None = None) -> str:
    def bullets(items):
        return "\n".join(f"- {x}" for x in items) or "- None noted"

    m = job.get("materials") or {}
    parts = [
        f"**{job.get('score', '–')}/10** · {job.get('summary', '')}",
        "",
        f"**Company:** {job['company']}  ",
        f"**Location:** {job.get('location') or 'Not listed'} ({job.get('workplace', 'unknown')})  ",
        f"**Pay:** {job.get('pay_text') or 'Not posted'}  ",
        f"**Posted:** {(job.get('posted') or 'Unknown')[:10]}  ",
        f"**Posting:** {job.get('url')}",
        "",
        "### Why you fit", bullets(job.get("strengths", [])),
        "", "### Watch out for", bullets(job.get("gaps", [])),
    ]
    parts += _tailored(job)
    parts += _snapshot(job)
    if m:
        parts += [
            "", "### Resume bullets for this job", bullets(m.get("bullets", [])),
            "", "### 30-second cold call", "> " + m.get("cold_call", "").replace("\n", "\n> "),
            "", "### LinkedIn note", "> " + m.get("linkedin_note", ""),
        ]
    parts += _kit(job, prof or {})
    from . import workhistory  # imported here: it reads resume files only when a card is written
    parts += workhistory.card_section(job)
    parts += [
        "", "---",
        "**Update the status** by adding a label (`applied`, `interviewing`, `offer`, `rejected`, `skip`) "
        "or by commenting `/applied`, `/interviewing`, `/offer`, `/rejected` or `/skip`. "
        "The dashboard and Discord update automatically.",
        f"<!-- job-key: {job['key']} -->",
    ]
    return "\n".join(parts)


def card_labels(job: dict) -> list[str]:
    labels = ["job"]
    vid = (job.get("tailored_resume") or {}).get("variant")
    if vid:
        label = f"resume-{vid}"
        r = requests.post(f"{API}/repos/{_repo()}/labels", headers=_headers(), timeout=30,
                          json={"name": label, "color": "ededed", "description": "Tailored resume version used"})
        if r.status_code in (201, 422):
            labels.append(label)
    return labels


def card_title(job: dict) -> str:
    if job.get("score") is None:  # added by hand without a score (e.g. already applied)
        return f"{job['title']} at {job['company']}"
    return f"{job['score']}/10 · {job['title']} at {job['company']}"


def create_issue(job: dict, prof: dict | None = None) -> tuple[int, str]:
    r = requests.post(f"{API}/repos/{_repo()}/issues", headers=_headers(), timeout=30, json={
        "title": card_title(job), "body": issue_body(job, prof), "labels": card_labels(job),
    })
    r.raise_for_status()
    data = r.json()
    return data["number"], data["html_url"]


def refresh_card(number: int, job: dict, prof: dict | None = None) -> None:
    """Rewrite an existing card's text (keeps its title, labels and status)."""
    r = requests.patch(f"{API}/repos/{_repo()}/issues/{number}", headers=_headers(), timeout=30,
                       json={"body": issue_body(job, prof)})
    r.raise_for_status()


def turn_into_card(number: int, job: dict, prof: dict | None = None) -> str:
    """Rewrite a hand-added issue as a tracker card."""
    r = requests.patch(f"{API}/repos/{_repo()}/issues/{number}", headers=_headers(), timeout=30, json={
        "title": card_title(job), "body": issue_body(job, prof), "labels": card_labels(job),
    })
    r.raise_for_status()
    return r.json()["html_url"]


def start_workflow(file_name: str) -> None:
    """Start another workflow now (GitHub allows this one kind of self-trigger)."""
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    requests.post(f"{API}/repos/{_repo()}/actions/workflows/{file_name}/dispatches",
                  headers=_headers(), json={"ref": branch}, timeout=30)


def repo_url() -> str | None:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    return f"{server}/{_repo()}" if _repo() else None


def report_problem(title: str, body: str, fingerprint: str) -> str:
    """One open issue per problem: a repeat adds a comment instead of a new issue."""
    r = requests.get(f"{API}/repos/{_repo()}/issues", headers=_headers(), timeout=30,
                     params={"labels": "radar-error", "state": "open", "per_page": 100})
    r.raise_for_status()
    for issue in r.json():
        if f"[{fingerprint}]" in issue.get("title", ""):
            c = requests.post(f"{API}/repos/{_repo()}/issues/{issue['number']}/comments", headers=_headers(),
                              timeout=30, json={"body": "Happened again.\n\n" + body})
            c.raise_for_status()
            return issue["html_url"]
    r = requests.post(f"{API}/repos/{_repo()}/issues", headers=_headers(), timeout=30, json={
        "title": f"⚠️ {title}"[:250], "labels": ["radar-error"],
        "body": body + "\n\n---\nOpened automatically by Job Radar's error tracker. Close it once it's fixed; "
                       "if it happens again, a new one opens."})
    r.raise_for_status()
    return r.json()["html_url"]
