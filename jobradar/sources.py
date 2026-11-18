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
from datetime import datetime, timedelta, timezone

import time
from concurrent.futures import ThreadPoolExecutor

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


# ── SmartRecruiters (many large employers) ──
# A board is the company ID from jobs.smartrecruiters.com/<CompanyId>/... (letter case matters).
# The list gives title, place and date; the full description is one more request, made only for jobs
# that pass the filters (see enrich).
SR_PAGE = 100
SR_MAX_PAGES = 100


def fetch_smartrecruiters(board: str) -> list[Job]:
    jobs, offset = [], 0
    for _ in range(SR_MAX_PAGES):
        data = _get_json(f"https://api.smartrecruiters.com/v1/companies/{board}/postings",
                         params={"limit": SR_PAGE, "offset": offset})
        items = data.get("content") or []
        if offset == 0 and not items and not data.get("totalFound"):
            raise BoardNotFound(board)  # unknown companies answer with an empty list, not a 404
        for j in items:
            loc = j.get("location") or {}
            country = (loc.get("country") or "").upper()
            place = ", ".join(p for p in (loc.get("city"), loc.get("region"),
                                          "United States" if country == "US" else country) if p)
            if loc.get("remote"):
                workplace = "remote"
            elif loc.get("hybrid"):
                workplace = "hybrid"
            else:
                workplace = workplace_from(None, place, j.get("name", ""))
            jobs.append(Job(
                key=f"smartrecruiters:{board}:{j['id']}",
                source="smartrecruiters",
                company=board,
                title=(j.get("name") or "").strip(),
                location=("Remote, " + place) if workplace == "remote" else place,
                remote=workplace == "remote",
                url=f"https://jobs.smartrecruiters.com/{board}/{j['id']}",
                posted=_iso(j.get("releasedDate")),
                description="",
                workplace=workplace,
            ))
        offset += len(items)
        if not items or offset >= int(data.get("totalFound") or 0):
            break
    return jobs


def _enrich_smartrecruiters(job: Job) -> None:
    _, board, jid = job.key.split(":", 2)
    d = _get_json(f"https://api.smartrecruiters.com/v1/companies/{board}/postings/{jid}")
    sections = ((d.get("jobAd") or {}).get("sections") or {})
    parts = [_plain((sections.get(k) or {}).get("text")) for k in
             ("companyDescription", "jobDescription", "qualifications", "additionalInformation")]
    full = "\n\n".join(p for p in parts if p)
    if full:
        job.description = full
        lo, hi, text = pay_from_text(full)
        if lo and not job.pay_min:
            _with_pay(job, lo, hi, text)


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


# ── Oracle Recruiting Cloud (Oracle HCM career sites, e.g. a large employer) ──
# A board is written "Company Name=host/SITE", e.g. "Initech=exmp.fa.us2.oraclecloud.com/CX".
# The host and site number come from the site's own job feed address (…oraclecloud.com/hcmRestApi/…siteNumber=CX).
ORACLE_PAGE = 25          # Oracle returns at most 25 jobs per request
ORACLE_MAX_PAGES = 240    # newest 6,000 jobs at most
ORACLE_MAX_AGE_DAYS = 60  # jobs come newest first; stop paging once they're older than this
_ORACLE_WORKPLACE = {"ORA_REMOTE": "remote", "ORA_HYBRID": "hybrid", "ORA_ON_SITE": "onsite"}


def parse_oracle_board(board: str) -> tuple[str, str, str]:
    """'Name=host/SITE' -> (company name, host, site)."""
    name, _, where = board.rpartition("=")
    host, _, site = where.strip().partition("/")
    if not host or not site:
        raise BoardNotFound(board)
    return (name.strip() or host.split(".")[0]), host.strip(), site.strip()


def _oracle_get(host: str, path: str):
    resp = requests.get(f"https://{host}/hcmRestApi/resources/latest/{path}",
                        headers={**HEADERS, "Accept": "application/json"}, timeout=TIMEOUT)
    if resp.status_code == 404:
        raise BoardNotFound(host)
    resp.raise_for_status()
    return resp.json()


def fetch_oracle(board: str) -> list[Job]:
    company, host, site = parse_oracle_board(board)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ORACLE_MAX_AGE_DAYS)).date().isoformat()
    jobs, seen = [], set()
    for page in range(ORACLE_MAX_PAGES):
        data = _oracle_get(host, "recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations"
                                 f"&finder=findReqs;siteNumber={site},limit={ORACLE_PAGE},offset={page * ORACLE_PAGE},"
                                 "sortBy=POSTING_DATES_DESC")
        items = (data.get("items") or [{}])[0]
        reqs = items.get("requisitionList") or []
        too_old = False
        for r in reqs:
            jid = str(r.get("Id") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            posted = (r.get("PostedDate") or "")[:10] or None
            if posted and posted < cutoff:
                too_old = True
                continue
            places = [r.get("PrimaryLocation") or ""] + [x.get("Name") or "" for x in r.get("secondaryLocations") or []]
            location = "; ".join(dict.fromkeys(p for p in places if p))
            workplace = _ORACLE_WORKPLACE.get(r.get("WorkplaceTypeCode") or "") or workplace_from(None, location)
            jobs.append(Job(
                key=f"oracle:{host.split('.')[0]}-{site}:{jid}", source="oracle", company=company,
                title=(r.get("Title") or "").strip(), location=location, remote=workplace == "remote",
                url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}", posted=posted,
                description=_plain(r.get("ShortDescriptionStr")), workplace=workplace))
        total = items.get("TotalJobsCount") or 0
        if too_old or len(reqs) < ORACLE_PAGE or (total and (page + 1) * ORACLE_PAGE >= total):
            break
    return jobs


def enrich(job: Job) -> Job:
    """Fill in the full description for jobs whose list only gives a summary (Oracle). Call it only for
    jobs that passed the filters: it's one extra request per job. Never raises."""
    if job.source not in ("oracle", "workday", "smartrecruiters") or getattr(job, "_enriched", False):
        return job
    try:
        if job.source == "smartrecruiters":
            _enrich_smartrecruiters(job)
            job._enriched = True
            return job
        if job.source == "workday":
            _enrich_workday(job)
            job._enriched = True
            return job
        m = re.match(r"https://([^/]+)/hcmUI/CandidateExperience/[^/]+/sites/([^/]+)/job/", job.url)
        host, site = m.group(1), m.group(2)
        jid = job.key.rsplit(":", 1)[1]
        data = _oracle_get(host, "recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
                                 f'&finder=ById;Id="{jid}",siteNumber={site}')
        d = (data.get("items") or [{}])[0]
        parts = [_plain(d.get(k)) for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr",
                                            "ExternalQualificationsStr")]
        full = "\n\n".join(p for p in parts if p)
        if full:
            job.description = full
    except Exception as e:  # noqa: BLE001 - the summary is still usable
        print(f"[oracle] full description unavailable for {job.key}: {e.__class__.__name__}")
    job._enriched = True
    return job


# ── Workday career sites (many large employers) ──
# A board is written "Company Name=tenant.wdN.myworkdayjobs.com/SiteName", copied from any job link on the
# company's Workday site (https://tenant.wdN.myworkdayjobs.com/SiteName/job/...).
# Big employers list thousands of jobs, so Workday is searched for your role titles instead of read in full.
WORKDAY_SEARCHES = ["solutions engineer", "sales engineer", "solutions consultant", "solutions architect",
                    "presales", "gtm engineer", "revenue operations", "implementation engineer", "forward deployed"]
WORKDAY_PAGE = 20


def parse_workday_board(board: str) -> tuple[str, str, str, str]:
    """'Name=tenant.wdN.myworkdayjobs.com/Site' -> (company, host, tenant, site)."""
    name, _, where = board.rpartition("=")
    host, _, site = where.strip().partition("/")
    site = site.strip("/").split("/")[-1] if site else ""
    if not host.endswith("myworkdayjobs.com") or not site:
        raise BoardNotFound(board)
    tenant = host.split(".")[0]
    return (name.strip() or tenant), host, tenant, site


def _workday_posted(text: str | None) -> str | None:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago' -> ISO date."""
    t = (text or "").lower()
    if "today" in t:
        days = 0
    elif "yesterday" in t:
        days = 1
    else:
        m = re.search(r"(\d+)\+?\s*day", t)
        if not m:
            return None
        days = int(m.group(1)) + (1 if "+" in t else 0)
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


def fetch_workday(board: str) -> list[Job]:
    company, host, tenant, site = parse_workday_board(board)
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    jobs, seen = [], set()
    for term in WORKDAY_SEARCHES:
        resp = requests.post(url, json={"appliedFacets": {}, "limit": WORKDAY_PAGE, "offset": 0, "searchText": term},
                             headers={**HEADERS, "Accept": "application/json"}, timeout=TIMEOUT)
        if resp.status_code in (404, 422):
            raise BoardNotFound(board)
        resp.raise_for_status()
        for j in resp.json().get("jobPostings") or []:
            path = j.get("externalPath") or ""
            if not path or path in seen:
                continue
            seen.add(path)
            location = j.get("locationsText") or ""
            workplace = workplace_from(None, location)
            jid = (j.get("bulletFields") or [path.rsplit("_", 1)[-1]])[0]
            jobs.append(Job(
                key=f"workday:{tenant}:{jid}", source="workday", company=company,
                title=(j.get("title") or "").strip(), location=location, remote=workplace == "remote",
                url=f"https://{host}/{site}{path}", posted=_workday_posted(j.get("postedOn")),
                description="", workplace=workplace))
    return jobs


def _enrich_workday(job: Job) -> None:
    m = re.match(r"https://(([^./]+)\.[^/]+)/([^/]+)(/job/.+)$", job.url)
    host, tenant, site, path = m.group(1), m.group(2), m.group(3), m.group(4)
    resp = requests.get(f"https://{host}/wday/cxs/{tenant}/{site}{path}",
                        headers={**HEADERS, "Accept": "application/json"}, timeout=TIMEOUT)
    resp.raise_for_status()
    info = resp.json().get("jobPostingInfo") or {}
    job.description = _plain(info.get("jobDescription")) or job.description
    places = [info.get("location") or ""] + list(info.get("additionalLocations") or [])
    if any(places):
        job.location = "; ".join(dict.fromkeys(p for p in places if p))
    remote_type = (info.get("remoteType") or "").lower()
    job.workplace = ("remote" if "remote" in remote_type else "hybrid" if "hybrid" in remote_type
                     else workplace_from(None, job.location))
    job.remote = job.workplace == "remote"
    if info.get("startDate"):
        job.posted = str(info["startDate"])[:10]


FETCHERS = {"ashby": fetch_ashby, "greenhouse": fetch_greenhouse, "lever": fetch_lever, "oracle": fetch_oracle,
            "workday": fetch_workday, "smartrecruiters": fetch_smartrecruiters}


class Fetched(list):
    """The jobs fetch_all kept, plus how many it read in total (it may drop jobs it was told to skip)."""
    total = 0


def _fetch_board(source: str, fetch, board: str):
    t, found, err, problem = time.perf_counter(), [], None, None
    try:
        found = fetch(str(board))
    except BoardNotFound:
        err = "not found"
        problem = f"{source}: '{board}' wasn't found. Check the name in config.yml."
    except requests.RequestException as e:
        err = e.__class__.__name__
        problem = f"{source}: '{board}' couldn't be reached ({e.__class__.__name__})."
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        # The board answered with something unexpected: skip it instead of stopping the scan.
        err = e.__class__.__name__
        problem = f"{source}: '{board}' sent data Job Radar couldn't read ({e.__class__.__name__})."
        telemetry.error(f"fetch {source}:{board}", e)
    telemetry.company(f"{source}:{board}", time.perf_counter() - t, len(found), err)
    return found, problem


def fetch_all(companies: dict, keep=None, workers: int = 16) -> tuple[Fetched, list[str]]:
    """Fetch every company in the config, several boards at a time. Returns (jobs, problems).
    keep(job) -> bool, if given, drops jobs as they arrive (so 100,000 full descriptions never sit in
    memory at once); jobs.total still counts every job read."""
    jobs, problems, tasks = Fetched(), [], []
    for source, boards in (companies or {}).items():
        fetch = FETCHERS.get(source)
        if not fetch:
            problems.append(f"Unknown hiring system '{source}' in config.yml")
            continue
        tasks += [(source, fetch, b) for b in boards or []]
    with ThreadPoolExecutor(max(1, workers)) as pool:
        for found, problem in pool.map(lambda t: _fetch_board(*t), tasks):  # map keeps config order
            jobs.total += len(found)
            jobs.extend(j for j in found if keep is None or keep(j))
            if problem:
                problems.append(problem)
    return jobs, problems
