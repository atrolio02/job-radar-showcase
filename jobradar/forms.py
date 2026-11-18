"""Read the answers out of a submitted GitHub issue form.

GitHub turns each form field into a "### Label" heading followed by the
answer, so the labels in .github/ISSUE_TEMPLATE/*.yml are the keys.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

TEMPLATES = Path(".github/ISSUE_TEMPLATE")
EMPTY = {"_No response_", "None", ""}


def field_ids(template: str, base: Path = TEMPLATES) -> dict[str, tuple[str, str]]:
    """label -> (field id, field type) for one form template."""
    spec = yaml.safe_load((base / template).read_text())
    return {f["attributes"]["label"]: (f["id"], f["type"])
            for f in spec["body"] if f.get("id")}


def parse(body: str, template: str, base: Path = TEMPLATES) -> dict:
    fields = field_ids(template, base)
    answers: dict = {}
    for chunk in re.split(r"^### ", body or "", flags=re.M)[1:]:
        label, _, value = chunk.partition("\n")
        label, value = label.strip(), value.strip()
        if label not in fields:
            continue
        fid, ftype = fields[label]
        if ftype == "checkboxes":
            answers[fid] = re.findall(r"^- \[[xX]\] (.+)$", value, flags=re.M)
        else:
            answers[fid] = "" if value in EMPTY else value
    return answers


# Links to a company's job board -> (system, board name)
BOARD_LINKS = [
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([^/?#\s]+)", re.I)),
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_(?:board|app)\?for=)?([^/?#&=\s]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([^/?#\s]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([^/?#\s]+)", re.I)),
    # Workday career sites: tenant.wdN.myworkdayjobs.com/[en-US/]SiteName
    ("workday", re.compile(r"([a-z0-9-]+\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?(?!wday/)([A-Za-z0-9_-]+)", re.I)),
    # Oracle HCM career sites: the job feed or candidate site address gives the host and site number
    ("oracle", re.compile(r"([a-z0-9-]+\.fa\.[a-z0-9-]+\.oraclecloud\.com)/hcm(?:UI|RestApi)\S*?(?:/sites/|siteNumber=)([A-Za-z0-9_]+)", re.I)),
]


def boards_in(text: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for source, pattern in BOARD_LINKS:
        for name in pattern.findall(text or ""):
            name = "/".join(name) if isinstance(name, tuple) else name  # Oracle: host/SITE
            if name not in found.setdefault(source, []):
                found[source].append(name)
    return {k: v for k, v in found.items() if v}
