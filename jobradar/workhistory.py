"""Work history, skills and certifications from your resume, ready to paste into application forms
(Workday and similar sites ask for each job's title, dates and description, plus skills and certifications).

Read from the resume each card uses: the tailored copy when the job has one, otherwise resume.docx.
Nothing is written by AI here; it's your resume's own text, split into the form's fields.
"""
from __future__ import annotations

import re
from pathlib import Path

# Who issues each certification (forms often ask). Unknown ones are left blank to fill in.
ISSUERS = {"snowpro": "Snowflake", "comptia": "CompTIA", "general assembly": "General Assembly",
           "aws": "Amazon Web Services", "salesforce": "Salesforce"}
SECTION = re.compile(r"^(SUMMARY|EXPERIENCE|SKILLS|PROJECTS.*|EDUCATION|CERTIFICATIONS)$")


def _split_list(text: str) -> list[str]:
    """Split on commas that aren't inside parentheses: 'AWS (Lambda, SES), SQL' -> ['AWS (Lambda, SES)', 'SQL']."""
    out, depth, cur = [], 0, ""
    for ch in text:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return [x for x in out if x]


def _dates(text: str) -> tuple[str, str]:
    start, _, end = text.partition("–")
    if not end:
        start, _, end = text.partition("-")
    return start.strip(), end.strip()


def parse(text: str) -> dict:
    sections: dict[str, list[str]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if SECTION.match(line.strip()):
            current = line.strip().split()[0]
            sections[current] = []
        elif current and line.strip():
            sections[current].append(line)

    jobs, company, blurb_next = [], None, False
    for line in sections.get("EXPERIENCE", []):
        if line.lstrip().startswith("•"):
            if jobs:
                jobs[-1]["bullets"].append(line.lstrip("•\t ").strip())
        elif "\t" in line:
            title, _, when = line.partition("\t")
            start, end = _dates(when)
            jobs.append({"company": (company or {}).get("name", ""), "location": (company or {}).get("location", ""),
                         "title": title.strip(), "start": start, "end": end, "bullets": []})
            blurb_next = False
        elif blurb_next:
            company["about"] = line.strip()
            blurb_next = False
        else:
            name, _, where = line.partition(", ")
            if "(" in where:  # "Sacramento, CA (formerly known as ...)"
                where, _, note = where.partition(" (")
                name = f"{name} ({note.rstrip(')')})" if "formerly" not in note else name
            company = {"name": name.strip(), "location": where.strip()}
            blurb_next = True

    skills, certs = [], []
    for line in sections.get("SKILLS", []):
        label, _, items = line.partition(":")
        if label.strip().lower().startswith("certification"):
            certs += _split_list(items)
        else:
            skills += _split_list(items)
    return {"jobs": jobs, "skills": skills,
            "certifications": [{"name": c, "issuer": next((v for k, v in ISSUERS.items() if k in c.lower()), "")}
                               for c in certs]}


def for_job(job: dict, base_docx: Path = Path("resume.docx")) -> dict | None:
    from . import tailor
    path = Path((job.get("tailored_resume") or {}).get("docx") or base_docx)
    if not path.exists():
        path = base_docx
    try:
        return parse(tailor.resume_text(path))
    except Exception:  # noqa: BLE001 - the card still works without this section
        return None


def card_section(job: dict) -> list[str]:
    info = for_job(job)
    if not info or not info["jobs"]:
        return []
    out = ["", "## 🧾 Work history, skills and certifications", "",
           "_For forms that ask for each job separately (Workday and similar). Taken from the resume this card "
           "uses, so it matches what you attach._"]
    for j in info["jobs"]:
        out += ["", f"### {j['title']} · {j['company']}",
                f"**Job title** `{j['title']}` · **Company** `{j['company']}` · **Location** `{j['location']}`  ",
                f"**From** `{j['start']}` · **To** `{j['end']}`" + (" (check “I currently work here”)" if
                                                                     j['end'].lower() == "present" else "")]
        desc = "\n".join(f"• {b}" for b in j["bullets"])
        out += ["", "**Role description**"] + (["```", desc, "```"] if desc else ["_[fill in yourself]_"])
    out += ["", "### Skills", "_Type these one at a time where the form uses skill tags._", "```",
            ", ".join(info["skills"]), "```", "", "### Certifications"]
    for c in info["certifications"]:
        out.append(f"- **{c['name']}**" + (f" · issued by {c['issuer']}" if c["issuer"] else ""))
    return out
