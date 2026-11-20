"""Keep every resume and card in step with the base resume. Run with:  python -m jobradar.rebase

Starts automatically whenever resume.docx changes (the "Resume updated" workflow). It:
  1. rebuilds every tailored resume from the new base, keeping each one's job-specific rewrites
     (a rewrite is any summary or bullet that differs from every earlier version of the base);
     rewrites that no longer pass the safety checks fall back to the new base text;
  2. rebuilds the standard resume PDF that 6-scored cards link to;
  3. rewrites every tracker card, so its resume links and its work history (the job descriptions
     in the application section) come from the current resume.
Jobs you've applied to are frozen (see freeze.py) and are left exactly as they were sent.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import dashboard, refit, github_api, profile, store, tailor, telemetry

BASE = Path("resume.docx")


def earlier_bases(limit: int = 30) -> list[Path]:
    """Every committed version of resume.docx (newest first), as temp files."""
    try:
        revs = subprocess.run(["git", "log", f"-{limit}", "--format=%H", "--", str(BASE)],
                              capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return []
    out, tmp = [], Path(tempfile.mkdtemp())
    for i, rev in enumerate(revs):
        blob = subprocess.run(["git", "show", f"{rev}:{BASE}"], capture_output=True)
        if blob.returncode == 0 and blob.stdout:
            p = tmp / f"base_{i}.docx"
            p.write_bytes(blob.stdout)
            out.append(p)
    return out


def rewrites(variant_docx: Path, bases: list[Path]) -> dict:
    """The parts of a tailored resume that were written for its job (not copied from any base)."""
    v_sum, v_bullets = tailor.editable_parts(variant_docx)
    known_sum, known = set(), {}
    for b in bases:
        s, bl = tailor.editable_parts(b)
        known_sum.add(s)
        for n, text in enumerate(bl):
            known.setdefault(n, set()).add(text)
    asked = {"bullets": [{"n": n, "text": t} for n, t in enumerate(v_bullets) if t not in known.get(n, set())]}
    if v_sum and v_sum not in known_sum:
        asked["summary"] = v_sum
    return tailor.clean_tailoring(asked)


def rebuild_variants(data: dict, bases: list[Path]) -> list[str]:
    users: dict[str, list[dict]] = {}
    for rec in data["jobs"].values():
        tr = rec.get("tailored_resume") or {}
        if rec.get("resume_frozen"):
            continue  # applied: keeps the exact resume it was sent with
        if tr.get("variant") and tr.get("docx"):
            users.setdefault(tr["variant"], []).append(rec)
    notes = []
    for variant, recs in sorted(users.items()):
        docx = Path(recs[0]["tailored_resume"]["docx"])
        if not docx.exists():
            continue
        out_dir = docx.parent
        snapshot = Path(tempfile.mkdtemp()) / docx.name
        shutil.copy(docx, snapshot)  # build() overwrites the file it reads from
        out = tailor.build(None, BASE, out_dir, docx.stem, "", asked=rewrites(snapshot, bases))
        for rec in recs:
            rec["tailored_resume"].update({k: out[k] for k in ("changed_bullets", "summary_changed",
                                                              "rejected_for_safety", "dropped_for_length",
                                                              "docx", "pdf") if k in out})
        notes.append(f"{variant}: {out.get('changed_bullets', 0)} rewrites kept, "
                     f"{out.get('rejected_for_safety', 0) + out.get('dropped_for_length', 0)} back to base text")
    return notes


def rebuild_standard() -> Path:
    target = Path(github_api._standard_pdf())
    target.parent.mkdir(parents=True, exist_ok=True)
    pdf = tailor.to_pdf(BASE, Path(tempfile.mkdtemp()))
    shutil.copy(pdf, target)
    return target


def refresh_cards(data: dict, frozen: bool = False) -> int:
    """Rewrite the cards: the live ones by default, or (frozen=True) only the applied ones, from
    their frozen files."""
    if not github_api.enabled():
        return 0
    prof, n = profile.load(), 0
    for rec in data["jobs"].values():
        if rec.get("issue_number") and bool(rec.get("resume_frozen")) == frozen:
            try:
                github_api.refresh_card(rec["issue_number"], rec, prof)
                n += 1
            except Exception as e:  # noqa: BLE001 - one card failing shouldn't stop the rest
                telemetry.warn(f"card #{rec['issue_number']} not refreshed ({e.__class__.__name__})")
    return n


def run(data_path: Path = store.DEFAULT_PATH) -> None:
    data = store.load(data_path)
    with telemetry.stage("tailored resumes"):
        for line in rebuild_variants(data, earlier_bases()):
            print(line)
    with telemetry.stage("standard resume"):
        print("standard resume:", rebuild_standard())
    print("active cards queued for a fresh score:", refit.queue(data, "resume changed"))
    store.save(data, data_path)
    with telemetry.stage("cards"):
        print("cards refreshed:", refresh_cards(data))


def run_frozen(data_path: Path = store.DEFAULT_PATH) -> None:
    with telemetry.stage("frozen cards"):
        print("applied cards restored:", refresh_cards(store.load(data_path), frozen=True))


def run_cards(numbers: list[int], data_path: Path = store.DEFAULT_PATH) -> None:
    """Rewrite just these cards, title included (after a re-score), and the dashboard."""
    data, prof = store.load(data_path), profile.load()
    with telemetry.stage("cards"):
        for rec in data["jobs"].values():
            if rec.get("issue_number") in numbers:
                github_api.refresh_card(rec["issue_number"], rec, prof, retitle=True)
                print("refreshed card", rec["issue_number"])
    dashboard.write(data, prof=prof)


if __name__ == "__main__":
    if "--cards" in sys.argv:
        nums = Path("data/refresh_cards.txt").read_text().split()
        telemetry.main("rebase", lambda: run_cards([int(n) for n in nums if n.isdigit()]))
    elif "--frozen" in sys.argv:
        telemetry.main("rebase", run_frozen)
    else:
        telemetry.main("rebase", run)
    sys.exit(0)
