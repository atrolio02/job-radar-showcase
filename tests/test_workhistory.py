"""Work history, skills and certifications for application forms."""
from jobradar import workhistory

RESUME = """JORDAN RIVERA
EXPERIENCE
Acme, Austin, TX
Makes rockets
Account Executive\tJuly 2026 – Present
•\tBuilt $5.2M in pipeline
Beta Co, Sacramento, CA (formerly known as Old Beta)
Engineer Apprentice\tOctober 2023 – May 2024
SKILLS
Technical: Python, AWS (Lambda, SES), SQL
Certifications: SnowPro Core (COF-C02), MEDDPICC
EDUCATION
TCU
"""


def test_parse_splits_resume_into_form_fields():
    info = workhistory.parse(RESUME)
    a, b = info["jobs"]
    assert (a["company"], a["location"], a["title"], a["start"], a["end"]) == \
        ("Acme", "Austin, TX", "Account Executive", "July 2026", "Present")
    assert a["bullets"] == ["Built $5.2M in pipeline"]
    assert (b["company"], b["location"], b["bullets"]) == ("Beta Co", "Sacramento, CA", [])
    assert info["skills"] == ["Python", "AWS (Lambda, SES)", "SQL"]
    assert info["certifications"] == [{"name": "SnowPro Core (COF-C02)", "issuer": "Snowflake"},
                                      {"name": "MEDDPICC", "issuer": ""}]


def test_card_section_uses_the_real_resume():
    from jobradar import tailor
    from pathlib import Path
    first = workhistory.parse(tailor.resume_text(Path("resume.docx")))["jobs"][0]
    text = "\n".join(workhistory.card_section({"key": "x"}))
    assert "Work history, skills and certifications" in text and "Role description" in text
    assert first["title"] in text and "### Certifications" in text
