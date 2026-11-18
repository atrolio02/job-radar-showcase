"""Oracle HCM career sites (e.g. a large employer). Nothing real is called."""
from datetime import datetime, timedelta, timezone

import pytest

from jobradar import sources

BOARD = "Initech=exmp.fa.us2.oraclecloud.com/CX"


def day(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).date().isoformat()


def req(i, title, posted, code="ORA_ON_SITE", where="Austin, TX, United States", more=()):
    return {"Id": str(25000000 + i), "Title": title, "PostedDate": posted, "PrimaryLocation": where,
            "WorkplaceTypeCode": code, "ShortDescriptionStr": "<p>Short summary.</p>",
            "secondaryLocations": [{"Name": m} for m in more]}


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise sources.requests.HTTPError(str(self.status_code))


def test_reads_pages_until_jobs_get_old(monkeypatch):
    page1 = [req(i, f"Engineer {i}", day(i)) for i in range(25)]
    page2 = [req(100, "Field Applications Engineer", day(3), "ORA_HYBRID", more=["Round Rock, TX, United States"]),
             req(101, "Sales Representative", day(5), "ORA_REMOTE", "United States"),
             req(102, "Too old", day(90))]
    calls = []

    def fake_get(url, **k):
        calls.append(url)
        assert "siteNumber=CX" in url and url.startswith("https://exmp.fa.us2.oraclecloud.com/hcmRestApi/")
        return Resp({"items": [{"TotalJobsCount": 900, "requisitionList": page1 if "offset=0," in url else page2}]})

    monkeypatch.setattr(sources.requests, "get", fake_get)
    jobs = sources.fetch_oracle(BOARD)
    assert len(calls) == 2  # stopped at the old job instead of paging through all 900
    by_title = {j.title: j for j in jobs}
    assert "Too old" not in by_title and len(jobs) == 27
    fae = by_title["Field Applications Engineer"]
    assert fae.company == "Initech" and fae.workplace == "hybrid"
    assert fae.location == "Austin, TX, United States; Round Rock, TX, United States"
    assert fae.key == "oracle:exmp-CX:25000100"
    assert fae.url == "https://exmp.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/25000100"
    assert by_title["Sales Representative"].remote and fae.description == "Short summary."


def test_enrich_fetches_the_full_posting_once(monkeypatch):
    job = sources.Job("oracle:exmp-CX:25000100", "oracle", "Initech", "FAE", "Austin, TX", False,
                      "https://exmp.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/25000100",
                      day(1), description="Short summary.")
    calls = []

    def fake_get(url, **k):
        calls.append(url)
        assert 'ById;Id="25000100",siteNumber=CX' in url
        return Resp({"items": [{"ExternalDescriptionStr": "<p>Full role.</p>",
                                "ExternalQualificationsStr": "<ul><li>BSEE</li></ul>"}]})

    monkeypatch.setattr(sources.requests, "get", fake_get)
    sources.enrich(job)
    sources.enrich(job)
    assert len(calls) == 1 and "Full role." in job.description and "BSEE" in job.description


def test_enrich_keeps_summary_on_errors(monkeypatch):
    job = sources.Job("oracle:exmp-CX:1", "oracle", "TI", "FAE", "Austin", False,
                      "https://exmp.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/1", None,
                      description="Short summary.")
    monkeypatch.setattr(sources.requests, "get", lambda *a, **k: Resp({}, 500))
    assert sources.enrich(job).description == "Short summary."


def test_bad_oracle_boards_are_reported(monkeypatch):
    monkeypatch.setattr(sources.requests, "get", lambda *a, **k: Resp({}, 404))
    jobs, problems = sources.fetch_all({"oracle": ["Nope=bad.fa.us2.oraclecloud.com/XX", "no-slash"]})
    assert jobs == [] and len(problems) == 2 and all("wasn't found" in p for p in problems)


def test_other_sources_are_never_enriched(monkeypatch):
    monkeypatch.setattr(sources.requests, "get", lambda *a, **k: pytest.fail("should not fetch"))
    job = sources.Job("ashby:x:1", "ashby", "X", "SE", "Remote", True, "u", None, description="d")
    assert sources.enrich(job).description == "d"


WD = "Salesforce=initrode.wd12.myworkdayjobs.com/External_Career_Site"


def test_workday_searches_your_roles_and_dedupes(monkeypatch):
    posts = []

    def fake_post(url, json=None, **k):
        posts.append(json["searchText"])
        assert url == "https://initrode.wd12.myworkdayjobs.com/wday/cxs/initrode/External_Career_Site/jobs"
        hits = [{"title": "Solutions Engineer", "externalPath": "/job/Texas---Austin/Solutions-Engineer_JR1",
                 "locationsText": "2 Locations", "postedOn": "Posted 3 Days Ago", "bulletFields": ["JR1"]},
                {"title": "Account Executive", "externalPath": "/job/Remote/AE_JR2", "locationsText": "Remote - USA",
                 "postedOn": "Posted Today", "bulletFields": ["JR2"]}]
        return Resp({"total": 2, "jobPostings": hits})

    monkeypatch.setattr(sources.requests, "post", fake_post)
    jobs = sources.fetch_workday(WD)
    assert posts == sources.WORKDAY_SEARCHES and len(jobs) == 2  # same job from every search counted once
    se = jobs[0]
    assert se.key == "workday:initrode:JR1" and se.company == "Salesforce" and se.posted == day(3)
    assert se.url == "https://initrode.wd12.myworkdayjobs.com/External_Career_Site/job/Texas---Austin/Solutions-Engineer_JR1"
    assert jobs[1].remote


def test_workday_detail_fills_location_and_remote(monkeypatch):
    job = sources.Job("workday:initrode:JR1", "workday", "Salesforce", "Solutions Engineer", "2 Locations", False,
                      "https://initrode.wd12.myworkdayjobs.com/External_Career_Site/job/Texas---Austin/SE_JR1",
                      None, description="")

    def fake_get(url, **k):
        assert url == "https://initrode.wd12.myworkdayjobs.com/wday/cxs/initrode/External_Career_Site/job/Texas---Austin/SE_JR1"
        return Resp({"jobPostingInfo": {"jobDescription": "<p>Demo our platform.</p>", "location": "Texas - Austin",
                                        "additionalLocations": ["Remote - Texas"], "remoteType": "Hybrid",
                                        "startDate": "2026-11-12"}})

    monkeypatch.setattr(sources.requests, "get", fake_get)
    sources.enrich(job)
    assert job.description == "Demo our platform." and job.location == "Texas - Austin; Remote - Texas"
    assert job.workplace == "hybrid" and job.posted == "2026-11-12"


def test_bad_workday_boards_are_reported(monkeypatch):
    monkeypatch.setattr(sources.requests, "post", lambda *a, **k: Resp({}, 422))
    jobs, problems = sources.fetch_all({"workday": [WD, "Nope=example.com/x"]})
    assert jobs == [] and len(problems) == 2 and all("wasn't found" in p for p in problems)


def test_posted_text():
    assert sources._workday_posted("Posted Today") == day(0)
    assert sources._workday_posted("Posted Yesterday") == day(1)
    assert sources._workday_posted("Posted 30+ Days Ago") == day(31)
    assert sources._workday_posted("") is None
