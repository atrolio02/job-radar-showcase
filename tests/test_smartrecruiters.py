"""SmartRecruiters boards and the parallel, memory-light fetch. Nothing real is called."""
from jobradar import sources


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise sources.requests.HTTPError(str(self.status_code))


def posting(i, name, city="Austin", region="TX", country="us", remote=False):
    return {"id": str(7440000 + i), "name": name, "releasedDate": "2026-11-10T15:00:00.000Z",
            "company": {"name": "Globex"},
            "location": {"city": city, "region": region, "country": country, "remote": remote}}


def test_reads_every_page(monkeypatch):
    pages = {0: [posting(i, f"Analyst {i}") for i in range(100)],
             100: [posting(200, "Solutions Engineer", remote=True), posting(201, "Sales Engineer")]}
    monkeypatch.setattr(sources.requests, "get",
                        lambda url, params=None, **k: Resp({"totalFound": 102, "content": pages[params["offset"]]}))
    jobs = sources.fetch_smartrecruiters("Globex")
    assert len(jobs) == 102
    se = jobs[100]
    assert se.key == "smartrecruiters:Globex:7440200" and se.workplace == "remote" and se.remote
    assert se.location == "Remote, Austin, TX, United States"
    assert se.url == "https://jobs.smartrecruiters.com/Globex/7440200" and se.posted.startswith("2026-11-10")


def test_unknown_company_is_not_found(monkeypatch):
    monkeypatch.setattr(sources.requests, "get", lambda *a, **k: Resp({"totalFound": 0, "content": []}))
    jobs, problems = sources.fetch_all({"smartrecruiters": ["NoSuchCo"]})
    assert jobs == [] and "wasn't found" in problems[0]


def test_enrich_adds_description_and_pay(monkeypatch):
    detail = {"jobAd": {"sections": {"jobDescription": {"text": "<p>Run demos.</p>"},
                                     "qualifications": {"text": "<p>Pay: $120,000 - $150,000</p>"}}}}
    monkeypatch.setattr(sources.requests, "get", lambda *a, **k: Resp(detail))
    job = sources.Job("smartrecruiters:Globex:1", "smartrecruiters", "Globex", "SE", "Austin", False, "u", None)
    sources.enrich(job)
    assert "Run demos." in job.description and job.pay_min == 120_000 and job.pay_max == 150_000


def test_keep_drops_jobs_but_counts_them(monkeypatch):
    data = {"jobs": [{"id": i, "title": t, "location": {"name": "Remote"}, "absolute_url": "u"}
                     for i, t in enumerate(["Solutions Engineer", "Accountant", "Nurse"])]}
    monkeypatch.setattr(sources.requests, "get", lambda *a, **k: Resp(data))
    jobs, problems = sources.fetch_all({"greenhouse": ["a", "b"]}, keep=lambda j: "Engineer" in j.title)
    assert jobs.total == 6 and [j.key for j in jobs] == ["greenhouse:a:0", "greenhouse:b:0"] and not problems
