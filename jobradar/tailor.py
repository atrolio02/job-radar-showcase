"""Tailored resume for each strong match.

Claude rewords your summary and the bullets most relevant to the posting.
Safety checks reject any rewrite that adds a number not already on your
resume. The changes are written into a copy of resume.docx (so your
formatting stays exactly the same), turned into a PDF, and kept to one page.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from docx import Document

from . import ai, telemetry

BULLET = "•\t"
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


# ── Reading the resume ─────────────────────────────────────────────────────
def _section_paragraphs(doc):
    """Yield (section heading, paragraph) for every paragraph."""
    section = ""
    for p in doc.paragraphs:
        text = p.text.strip()
        if text and text.isupper() and len(text) < 40 and not text.startswith(BULLET.strip()):
            section = text
        yield section, p


def resume_text(docx_path: Path) -> str:
    return "\n".join(p.text for p in Document(str(docx_path)).paragraphs if p.text.strip())


def editable_parts(docx_path: Path) -> tuple[str, list[str]]:
    """The summary paragraph and every experience/venture bullet, in order."""
    summary, bullets = "", []
    for section, p in _section_paragraphs(Document(str(docx_path))):
        text = p.text.strip()
        if section == "SUMMARY" and text and text != "SUMMARY":
            summary = text
        elif p.text.startswith(BULLET):
            bullets.append(p.text[len(BULLET):].strip())
    return summary, bullets


# ── Safety checks ──────────────────────────────────────────────────────────
def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in NUMBER.findall(text)}


def safe_rewrite(original: str, new: str, full_resume: str, max_growth: float = 1.12) -> bool:
    """Accept a rewrite only if it adds no new numbers and doesn't grow much."""
    if not new or len(new) > len(original) * max_growth + 8:
        return False
    allowed = _numbers(full_resume)
    return _numbers(new) <= allowed


def check_changes(changes: dict, summary: str, bullets: list[str], full_resume: str) -> dict:
    ok = {"summary": None, "bullets": {}, "rejected": 0}
    if changes.get("summary"):
        if safe_rewrite(summary, changes["summary"], full_resume):
            ok["summary"] = changes["summary"]
        else:
            ok["rejected"] += 1
    for n, text in changes.get("bullets", {}).items():
        if 0 <= n < len(bullets) and safe_rewrite(bullets[n], text, full_resume):
            ok["bullets"][n] = text
        else:
            ok["rejected"] += 1
    return ok


# ── Writing the copy ───────────────────────────────────────────────────────
def _set_text(p, new: str, prefix: str = "") -> None:
    """Replace a paragraph's words but keep its formatting (the longest run's style)."""
    runs = [r for r in p.runs]
    main = max(runs, key=lambda r: len(r.text))
    if prefix and runs[0] is not main and runs[0].text == prefix:
        main.text = new
    else:
        main.text = prefix + new
    for r in runs:
        if r is not main and not (prefix and r is runs[0] and r.text == prefix):
            r.text = ""


def apply(docx_in: Path, docx_out: Path, summary: str | None, bullets: dict[int, str]) -> None:
    doc = Document(str(docx_in))
    i = 0
    for section, p in _section_paragraphs(doc):
        text = p.text.strip()
        if section == "SUMMARY" and text and text != "SUMMARY" and summary:
            _set_text(p, summary)
        elif p.text.startswith(BULLET):
            if i in bullets:
                _set_text(p, bullets[i], BULLET)
            i += 1
    doc.save(str(docx_out))


def _install_libreoffice() -> None:
    """On GitHub's servers, install LibreOffice the first time a PDF is needed in a run
    (most runs have no strong match, so this skips about a minute of setup on those days)."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    print("Installing LibreOffice to make the resume PDF...")
    subprocess.run("sudo apt-get update -qq && sudo apt-get install -y -qq --no-install-recommends "
                   "libreoffice-writer-nogui fonts-crosextra-carlito > /dev/null",
                   shell=True, check=False, timeout=600)


def to_pdf(docx_path: Path, out_dir: Path) -> Path:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        with telemetry.stage("libreoffice install"):
            _install_libreoffice()
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError("LibreOffice isn't installed, so no PDF could be made")
    with telemetry.stage("pdf"), tempfile.TemporaryDirectory() as profile_dir:
        subprocess.run([soffice, f"-env:UserInstallation=file://{profile_dir}", "--headless",
                        "--convert-to", "pdf", "--outdir", str(out_dir), str(docx_path)],
                       check=True, capture_output=True, timeout=180)
    return out_dir / (docx_path.stem + ".pdf")


def page_count(pdf: Path) -> int:
    return len(re.findall(rb"/Type\s*/Page(?!s)", pdf.read_bytes()))


# ── The whole step ─────────────────────────────────────────────────────────
TAILOR_SYSTEM = (
    "You tailor a resume to a job posting. Reword only; never add experience, employers, tools, "
    "certifications or numbers that aren't already in the resume, and never change a number. "
    "Never mention the hiring company's name: the same copy may be reused for similar jobs. "
    "Keep each rewrite about the same length as the original. Reply with JSON only."
)

TAILOR_PROMPT = """Tailor this resume (the full text is above) to the job by rewording the summary and the
bullets that matter most for this posting, so its language and keywords show up. Leave the rest alone.

<summary>
{summary}
</summary>

<bullets>
{bullets}
</bullets>

<job>
{job}
</job>

Rewrite the summary and at most 6 bullets. Then list the skills, tools or qualifications the posting asks
for that the resume doesn't show anywhere; don't add them to the resume.

Reply with exactly this JSON:
{{"summary": "<rewritten summary, same length or shorter>",
  "bullets": [{{"n": <bullet number>, "text": "<rewritten bullet>"}}],
  "changes": "<one sentence on what you emphasized>",
  "missing": ["<up to 6 short items the posting wants that the resume doesn't show>"]}}"""


def ask_claude(job, resume: str, summary: str, bullets: list[str], model: str) -> dict:
    numbered = "\n".join(f"{i}. {b}" for i, b in enumerate(bullets))
    prompt = TAILOR_PROMPT.format(summary=summary, bullets=numbered, job=ai._job_block(job))
    return clean_tailoring(ai._parse_json(ai._call_claude(model, TAILOR_SYSTEM, prompt, 2500,
                                                          cached=ai.resume_block(resume))))


def clean_tailoring(r: dict) -> dict:
    """Tidy rewrite instructions from the API or from Claude through the MCP server.
    The safety checks in check_changes still run on every rewrite afterwards."""
    missing = r.get("missing") if isinstance(r.get("missing"), list) else []
    out = {"summary": str(r.get("summary", "")), "bullets": {}, "changes": str(r.get("changes", "")),
           "missing": [str(m) for m in missing][:6]}
    for b in r.get("bullets", []) if isinstance(r.get("bullets"), list) else []:
        try:
            out["bullets"][int(b["n"])] = str(b["text"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
    return out


def build(job, base_docx: Path, out_dir: Path, file_stem: str, model: str, asked: dict | None = None) -> dict:
    """Make the tailored Word file and one-page PDF. Returns paths and notes.
    `asked` is rewrites Claude already wrote (through the MCP server); otherwise the API is asked."""
    full = resume_text(base_docx)
    try:  # facts from the profile's extra_experience may be used in rewrites
        from . import profile
        full += profile.extra_experience_text()
    except Exception:  # noqa: BLE001
        pass
    summary, bullets = editable_parts(base_docx)
    if asked is None:
        asked = ask_claude(job, full, summary, bullets, model)
    ok = check_changes(asked, summary, bullets, full)

    out_dir.mkdir(parents=True, exist_ok=True)
    docx_out = out_dir / f"{file_stem}.docx"
    keep = dict(ok["bullets"])
    # Most-grown rewrites go first if the page overflows
    by_growth = sorted(keep, key=lambda n: len(keep[n]) - len(bullets[n]), reverse=True)
    summary_new = ok["summary"]
    dropped = 0
    while True:
        apply(base_docx, docx_out, summary_new, keep)
        pdf = to_pdf(docx_out, out_dir)
        if page_count(pdf) <= 1:
            break
        if by_growth:
            keep.pop(by_growth.pop(0))
        elif summary_new:
            summary_new = None
        else:
            break  # your own resume is already over a page
        dropped += 1
    return {"docx": str(docx_out), "pdf": str(pdf), "changed_bullets": len(keep),
            "summary_changed": bool(summary_new), "rejected_for_safety": ok["rejected"],
            "dropped_for_length": dropped, "changes": asked.get("changes", ""),
            "missing": asked.get("missing", [])}
