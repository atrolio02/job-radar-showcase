"""Rebuilding tailored resumes when the base resume changes."""
import shutil
from pathlib import Path

from docx import Document

from jobradar import rebase, tailor


def _edit(src: Path, dst: Path, old: str, new: str) -> Path:
    shutil.copy(src, dst)
    d = Document(str(dst))
    for p in d.paragraphs:
        for r in p.runs:
            if old in r.text:
                r.text = r.text.replace(old, new)
    d.save(str(dst))
    return dst


def test_keeps_job_rewrites_and_ignores_old_base_text(tmp_path):
    base = Path("resume.docx")
    summary, bullets = tailor.editable_parts(base)
    old_base = _edit(base, tmp_path / "old.docx", bullets[1][:20], "OLD " + bullets[1][:16])
    # A tailored copy built from the old base, with one real rewrite of bullet 0
    variant = _edit(old_base, tmp_path / "v.docx", bullets[0][:12], "Owned " + bullets[0][:6])
    asked = rebase.rewrites(variant, [base, old_base])
    assert list(asked["bullets"]) == [0]          # bullet 1 was old base text, not a rewrite
    assert not asked.get("summary")


def test_freeze_copies_the_files_and_rebase_leaves_them(tmp_path, monkeypatch):
    from jobradar import freeze
    src = tmp_path / "v"; src.mkdir()
    (src / "R.docx").write_text("d"); (src / "R.pdf").write_text("p")
    rec = {"issue_number": 9, "status": "applied",
           "tailored_resume": {"variant": "v", "docx": str(src / "R.docx"), "pdf": str(src / "R.pdf")}}
    assert freeze.freeze(rec, root=tmp_path / "applied")
    assert rec["tailored_resume"]["pdf"].endswith("applied/9/R.pdf") and rec["resume_frozen"]
    assert not freeze.freeze(rec, root=tmp_path / "applied")       # only once
    data = {"jobs": {"a": rec}}
    assert rebase.rebuild_variants(data, []) == []                  # frozen jobs aren't rebuilt


def test_merge_keeps_the_frozen_resume():
    from jobradar.merge import merge
    frozen = {"status": "applied", "history": [{}], "resume_frozen": "x",
              "tailored_resume": {"pdf": "resumes/applied/9/R.pdf"}}
    stale = {"status": "applied", "history": [{}], "tailored_resume": {"pdf": "resumes/v/R.pdf"}}
    out = merge({"jobs": {"k": stale}}, {"jobs": {"k": frozen}})["jobs"]["k"]
    assert out["tailored_resume"]["pdf"] == "resumes/applied/9/R.pdf" and out["resume_frozen"]
