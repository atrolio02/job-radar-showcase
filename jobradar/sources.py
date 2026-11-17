"""Pull job postings from the public APIs that hiring systems offer.

Ashby, Greenhouse and Lever each publish a company's open jobs at a public,
read-only web address meant for careers pages. These are API calls, not
scraping: no login, no browser, nothing against any site's rules.

Every posting is turned into the same simple shape (a "Job") so the rest of
the tool doesn't care which system it came from.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

import time

import requests

from . import telemetry

TIMEOUT = 30
HEADERS = {"User-Agent": "job-radar (personal job tracker)"}


@dataclass
class Job:
    key: str            # unique id, e.g. "ashby:Northwind:979e3b0d-..."
    source: str         # ashby / greenhouse / lever
    company: str
    title: str
    location: str
    remote: bool
    url: str
    posted: str | None  # ISO date, when known
    description: str = field(repr=False, default="")
    workplace: str = "unknown"      # remote / hybrid / onsite / unknown
    pay_min: int | None = None      # yearly USD, when the posting lists it
    pay_max: int | None = None
    pay_text: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class BoardNotFound(Exception):
    """The company name isn't a real job board on that system."""


def _get_json(url: str, params: dict | None = None):
    resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    if resp.status_code == 404:
        raise BoardNotFound(url)
    resp.raise_for_status()
    return resp.json()


def _plain(text: str | None) -> str:
    """Turn HTML (sometimes escaped twice) into readable plain text."""
    if not text:
        return ""
    text = html.unescape(html.unescape(text))
    text = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _iso(value) -> str | None:
    """Accept an ISO string or a millisecond timestamp; return an ISO string."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    return str(value)


PAY_RANGE = re.compile(
    r"\$\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?\s?[kK])\s*(?:usd)?\s*(?:-|–|—|to)\s*"
    r"\$?\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?\s?[kK])", re.I)


def _amount(text: str) -> int:
    text = text.replace(",", "").replace(" ", "")
    return int(float(text[:-1]) * 1000) if text[-1:].lower() == "k" else int(float(text))


def pay_from_text(text: str) -> tuple[int | None, int | None, str]:
    """Find a yearly pay range like "$113,000 - $170,000" or "$120K–$150K" in a posting."""
    for m in PAY_RANGE.finditer(text or ""):
        lo, hi = _amount(m.group(1)), _amount(m.group(2))
        if 30_000 <= lo <= hi <= 1_500_000:
            return lo, hi, m.group(0).strip()
    return None, None, ""


def workplace_from(kind: str | None, *texts: str) -> str:
    kind = (kind or "").lower().replace("-", "").replace(" ", "")
    if kind in ("remote", "hybrid", "onsite"):
        return kind
    joined = " ".join(t or "" for t in texts).lower()
    if "hybrid" in joined:
        return "hybrid"
    if "remote" in joined or "anywhere" in joined:
        return "remote"
    return "unknown"


def _with_pay(job: Job, lo=None, hi=None, text: str = "") -> Job:
    if not lo:
        lo, hi, text = pay_from_text(job.description)
    job.pay_min, job.pay_max, job.pay_text = lo, hi, text
    return job


# ── Ashby ──────────────────────────────────────────────────────────────────
def fetch_ashby(board: str) -> list[Job]:
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}",
                     params={"includeCompensation": "true"})
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        places = [j.get("location") or ""] + [
            s.get("location", "") for s in j.get("secondaryLocations") or []
        ]
        location = " / ".join(p for p in places if p)
        workplace = workplace_from(j.get("workplaceType"), location)
        if workplace == "unknown" and j.get("isRemote"):
            workplace = "remote"
        comp = j.get("compensation") or {}
        salary = next((c for c in comp.get("summaryComponents") or []
                       if (c.get("compensationType") or "").lower() == "salary"
                       and (c.get("currencyCode") or "USD") == "USD"
                       and "year" in (c.get("interval") or "1 YEAR").lower()), None)
        jobs.append(_with_pay(Job(
            key=f"ashby:{board}:{j['id']}",
            source="ashby",
            company=board,
            title=j.get("title", "").strip(),
            location=location,
            remote=workplace == "remote",
            url=j.get("jobUrl") or f"https://jobs.ashbyhq.com/{board}/{j['id']}",
            posted=_iso(j.get("publishedAt")),
            description=j.get("descriptionPlain") or _plain(j.get("descriptionHtml")),
            workplace=workplace,
        ), salary and salary.get("minValue"), salary and salary.get("maxValue"),
           comp.get("compensationTierSummary") or ""))
    return jobs


# ── Greenhouse ─────────────────────────────────────────────────────────────
def fetch_greenhouse(board: str) -> list[Job]:
    data = _get_json(
        f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
        params={"content": "true"},
    )
    jobs = []
    for j in data.get("jobs", []):
        location = (j.get("location") or {}).get("name", "")
        workplace = workplace_from(None, location, j.get("title", ""))
        jobs.append(_with_pay(Job(
            key=f"greenhouse:{board}:{j['id']}",
            source="greenhouse",
            company=board,
            title=j.get("title", "").strip(),
            location=location,
            remote=workplace == "remote",
            url=j.get("absolute_url", ""),
            posted=_iso(j.get("first_published") or j.get("updated_at")),
            description=_plain(j.get("content")),
            workplace=workplace,
        )))
    return jobs


# ── Lever ──────────────────────────────────────────────────────────────────
def fetch_lever(board: str) -> list[Job]:
    data = _get_json(f"https://api.lever.co/v0/postings/{board}", params={"mode": "json"})
    if isinstance(data, dict):  # Lever answers {"ok": false} for unknown companies
        raise BoardNotFound(board)
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        places = cats.get("allLocations") or [cats.get("location") or ""]
        location = " / ".join(p for p in places if p)
        workplace = workplace_from(j.get("workplaceType"), location)
        sal = j.get("salaryRange") or {}
        yearly = sal.get("currency", "USD") == "USD" and "year" in (sal.get("interval") or "per-year-salary")
        parts = [j.get("descriptionPlain") or ""]
        for section in j.get("lists") or []:
            parts.append(section.get("text", ""))
            parts.append(_plain(section.get("content")))
        parts.append(j.get("additionalPlain") or "")
        jobs.append(_with_pay(Job(
            key=f"lever:{board}:{j['id']}",
            source="lever",
            company=board,
            title=(j.get("text") or "").strip(),
            location=location,
            remote=workplace == "remote",
            url=j.get("hostedUrl", ""),
            posted=_iso(j.get("createdAt")),
            description="\n".join(p for p in parts if p).strip(),
            workplace=workplace,
        ), yearly and sal.get("min"), yearly and sal.get("max"),
           f"${sal['min']:,} – ${sal['max']:,}" if yearly and sal.get("min") and sal.get("max") else ""))
    return jobs


# Standard fields every Greenhouse form has; the application kit fills these from your profile.
GH_STANDARD = {"first_name", "last_name", "email", "phone", "resume", "resume_text",
               "cover_letter", "cover_letter_text", "location", "linkedin_profile"}


def greenhouse_questions(board: str, job_id: str) -> list[dict]:
    """The custom questions on a Greenhouse application (public API)."""
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}",
                     params={"questions": "true"})
    out = []
    for q in data.get("questions") or []:
        fields = q.get("fields") or []
        if not fields or all(f.get("name") in GH_STANDARD or f.get("type") == "input_file" for f in fields):
            continue
        options = [v.get("label") for f in fields for v in (f.get("values") or []) if v.get("label")]
        out.append({"question": q.get("label", "").strip(), "required": bool(q.get("required")),
                    "options": options})
    return out


FETCHERS = {"ashby": fetch_ashby, "greenhouse": fetch_greenhouse, "lever": fetch_lever}


def fetch_all(companies: dict) -> tuple[list[Job], list[str]]:
    """Fetch every company in the config. Returns (jobs, problems)."""
    jobs: list[Job] = []
    problems: list[str] = []
    for source, boards in (companies or {}).items():
        fetch = FETCHERS.get(source)
        if not fetch:
            problems.append(f"Unknown hiring system '{source}' in config.yml")
            continue
        for board in boards or []:
            t, found, err = time.perf_counter(), [], None
            try:
                found = fetch(str(board))
                jobs.extend(found)
            except BoardNotFound:
                err = "not found"
                problems.append(f"{source}: '{board}' wasn't found. Check the name in config.yml.")
            except requests.RequestException as e:
                err = e.__class__.__name__
                problems.append(f"{source}: '{board}' couldn't be reached ({e.__class__.__name__}).")
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                # The board answered with something unexpected: skip it instead of stopping the scan.
                err = e.__class__.__name__
                problems.append(f"{source}: '{board}' sent data Job Radar couldn't read ({e.__class__.__name__}).")
                telemetry.error(f"fetch {source}:{board}", e)
            telemetry.company(f"{source}:{board}", time.perf_counter() - t, len(found), err)
    return jobs, problems
