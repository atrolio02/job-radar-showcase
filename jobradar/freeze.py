"""Keep the resume you applied with. Once a job is marked applied, its card and resume are frozen:
the exact Word and PDF files it used are copied to resumes/applied/<card number>/ and the card keeps
pointing at them, even when resume.docx changes later. That way you can see which resume version
led to interviews and which didn't.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

APPLIED_STATUSES = {"applied", "interviewing", "offer", "rejected"}
ROOT = Path("resumes/applied")


def was_applied(rec: dict) -> bool:
    return rec.get("status") in APPLIED_STATUSES or any(h.get("status") == "applied" for h in rec.get("history", []))


def freeze(rec: dict, base_docx: Path = Path("resume.docx"), standard_pdf: Path | None = None,
           root: Path = ROOT) -> bool:
    """Copy the files this card uses into its own folder and point the card at them. Returns True if it
    froze now (False if it was already frozen or has no card)."""
    if rec.get("resume_frozen") or not rec.get("issue_number"):
        return False
    from .github_api import _standard_pdf
    folder = root / str(rec["issue_number"])
    folder.mkdir(parents=True, exist_ok=True)
    tr = rec.get("tailored_resume") or {}
    if tr.get("docx") and Path(tr["docx"]).exists():
        for kind in ("docx", "pdf"):
            src = Path(tr[kind])
            if src.exists():
                dst = folder / src.name
                shutil.copy(src, dst)
                tr[kind] = str(dst)
    else:
        pdf = Path(standard_pdf or _standard_pdf())
        if pdf.exists():
            shutil.copy(pdf, folder / pdf.name)
            rec["standard_pdf"] = str(folder / pdf.name)
        if base_docx.exists():
            shutil.copy(base_docx, folder / base_docx.name)
            rec["standard_docx"] = str(folder / base_docx.name)
    rec["resume_frozen"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return True


def refresh(rec: dict) -> None:
    """Point the GitHub card's links at the frozen copies (best effort)."""
    from . import github_api, profile, telemetry
    if not github_api.enabled():
        return
    try:
        github_api.refresh_card(rec["issue_number"], rec, profile.load())
    except Exception as e:  # noqa: BLE001 - the status change matters more than the card text
        telemetry.warn(f"card #{rec['issue_number']} not refreshed after freezing ({e.__class__.__name__})")
