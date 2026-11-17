"""Tests with made-up API responses, so nothing real is called."""
import json
import json as json_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jobradar import addjob, ai, dashboard, discord, filters, forms, github_api, preferences, profile, scan, sources, status, store

NOW = datetime.now(timezone.utc)
RECENT = (NOW - timedelta(days=3)).isoformat()
OLD = (NOW - timedelta(days=200)).isoformat()

ASHBY = {"apiVersion": "1", "jobs": [
    {"id": "a1", "title": "Sales Engineer", "location": "United States", "isRemote": True,
     "workplaceType": "Remote", "isListed": True, "jobUrl": "https://jobs.ashbyhq.com/Northwind/a1",
     "publishedAt": RECENT, "descriptionPlain": "Own discovery, demos, APIs, webhooks, SQL.",
     "compensation": {"compensationTierSummary": "$113K – $170K • Equity", "summaryComponents": [
         {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD", "minValue": 113000, "maxValue": 170000}]}},
    {"id": "a2", "title": "Sales Engineering Manager", "location": "Remote", "isRemote": True,
     "isListed": True, "jobUrl": "x", "publishedAt": RECENT, "descriptionPlain": "Manage SEs."},
    {"id": "a3", "title": "Solutions Engineer", "location": "London", "isRemote": False,
     "isListed": True, "jobUrl": "x", "publishedAt": RECENT, "descriptionPlain": "UK role."},
]}
GREENHOUSE = {"jobs": [
    {"id": 11, "title": "Solutions Architect", "location": {"name": "Austin, TX"},
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/11", "first_published": RECENT,
     "content": "&lt;p&gt;Design &amp;amp; demo integrations&lt;/p&gt;"},
    {"id": 12, "title": "Solutions Engineer", "location": {"name": "Remote - Canada"},
     "absolute_url": "x", "first_published": RECENT, "content": "Canada"},
    {"id": 13, "title": "Sales Engineer", "location": {"name": "Remote, US"},
     "absolute_url": "x", "first_published": OLD, "content": "Old posting"},
]}
LEVER = [
    {"id": "l1", "text": "GTM Engineer", "categories": {"location": "Remote", "allLocations": ["Remote"]},
     "workplaceType": "remote", "hostedUrl": "https://jobs.lever.co/acme/l1",
     "createdAt": int((NOW - timedelta(days=1)).timestamp() * 1000), "descriptionPlain": "Build enrichment flows.",
     "lists": [{"text": "Requirements", "content": "<li>Python</li>"}],
     "salaryRange": {"currency": "USD", "interval": "per-year-salary", "min": 140000, "max": 180000}},
    {"id": "l2", "text": "Account Executive", "categories": {"location": "Remote"},
     "hostedUrl": "x", "createdAt": 0, "descriptionPlain": "Sell."},
]

CFG = scan.load_config(str(Path(__file__).parents[1] / "config.yml"))
# These tests cover the direct-API path; test_lastmile.py covers the MCP path.
CFG["ai"] = {**CFG["ai"], "mode": "api", "rules_min_score": 0}


class FakeResp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))


GH_QUESTIONS = {"questions": [
    {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text"}]},
    {"label": "Resume/CV", "required": True, "fields": [{"name": "resume", "type": "input_file"}]},
    {"label": "Why Acme?", "required": True, "fields": [{"name": "question_1", "type": "textarea"}]},
    {"label": "Will you require sponsorship?", "required": True, "fields": [
        {"name": "question_2", "type": "multi_value_single_select", "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
]}


def fake_get(url, params=None, headers=None, timeout=None):
    if "greenhouse.io" in url and url.endswith("/jobs/11"):
        assert params == {"questions": "true"}
        return FakeResp(200, GH_QUESTIONS)
    if "ashbyhq.com" in url:
        return FakeResp(200, ASHBY) if url.endswith("/Northwind") else FakeResp(404, {})
    if "greenhouse.io" in url:
        return FakeResp(200, GREENHOUSE) if "/acme/" in url else FakeResp(404, {})
    if "lever.co" in url:
        return FakeResp(200, LEVER) if url.endswith("/acme") else FakeResp(200, {"ok": False})
    raise AssertionError(url)


@pytest.fixture
def no_network(monkeypatch):
    monkeypatch.setattr(sources.requests, "get", fake_get)


def test_fetchers_normalize(no_network):
    a = sources.fetch_ashby("Northwind")
    g = sources.fetch_greenhouse("acme")
    l = sources.fetch_lever("acme")
    assert a[0].key == "ashby:Northwind:a1" and a[0].remote and a[0].workplace == "remote"
    assert (a[0].pay_min, a[0].pay_max) == (113000, 170000) and "Equity" in a[0].pay_text
    assert (l[0].pay_min, l[0].pay_max) == (140000, 180000) and l[0].workplace == "remote"
    assert g[0].workplace == "unknown" and g[0].pay_max is None
    qs = sources.greenhouse_questions("acme", "11")
    assert [q["question"] for q in qs] == ["Why Acme?", "Will you require sponsorship?"]
    assert qs[1]["options"] == ["Yes", "No"]
    assert g[0].description == "Design & demo integrations"
    assert l[0].posted.startswith(str((NOW - timedelta(days=1)).date()))
    assert "Python" in l[0].description


def test_bad_board_names_are_reported(no_network):
    jobs, problems = sources.fetch_all({"ashby": ["nope"], "lever": ["nope"], "workday": ["x"]})
    assert jobs == [] and len(problems) == 3


PROF = profile.load(Path("/nonexistent"))  # starting answers: remote US + DFW hybrid/on-site


def mk(title, loc, workplace="unknown", posted=RECENT, pay_max=None):
    job = sources.Job("k", "x", "c", title, loc, workplace == "remote", "u", posted, workplace=workplace)
    job.pay_max = pay_max
    return job


def test_filters_default_profile():
    assert filters.matches(mk("Sales Engineer", "United States", "remote"), CFG, PROF)
    assert filters.matches(mk("Solutions Architect", "Austin, TX"), CFG, PROF)
    assert not filters.matches(mk("Sales Engineering Manager", "Remote", "remote"), CFG, PROF)
    assert not filters.matches(mk("Solutions Engineer", "Remote - Canada", "remote"), CFG, PROF)
    assert not filters.matches(mk("Solutions Engineer", "New York, NY"), CFG, PROF)
    assert not filters.matches(mk("Sales Engineer", "Remote", "remote", OLD), CFG, PROF)
    assert not filters.matches(mk("Account Executive", "Remote", "remote"), CFG, PROF)
    assert not filters.matches(mk("Principal Solutions Engineer", "Remote", "remote"), CFG, PROF)
    # whole words only: "Duke" isn't "UK", "MVP" isn't "VP"
    assert filters.matches(mk("Solutions Engineer", "Remote (Duke Street team)", "remote"), CFG, PROF)
    assert filters.matches(mk("MVP Solutions Engineer", "Remote", "remote"), CFG, PROF)


def test_filters_follow_quiz_answers():
    prof = dict(PROF, work_arrangements=["remote"], min_base_salary=130000,
                role_types=["GTM / Growth Engineer"], seniority=["Mid-level", "Senior", "Lead / Staff / Principal"])
    assert filters.matches(mk("GTM Engineer", "Remote US", "remote"), CFG, prof)
    assert filters.matches(mk("Staff GTM Engineer", "Remote US", "remote"), CFG, prof)
    assert not filters.matches(mk("GTM Engineer", "Austin, TX", "hybrid"), CFG, prof)
    assert not filters.matches(mk("Sales Engineer", "Remote US", "remote"), CFG, prof)
    assert not filters.matches(mk("GTM Engineer", "Remote", "remote", pay_max=110000), CFG, prof)
    assert filters.matches(mk("GTM Engineer", "Remote", "remote", pay_max=None), CFG, prof)
    reloc = dict(PROF, open_to_relocation=True)
    assert filters.matches(mk("Sales Engineer", "Denver, CO", "onsite"), CFG, reloc)
    assert not filters.matches(mk("Sales Engineer", "London", "onsite"), CFG, reloc)
    world = dict(PROF, remote_scope="anywhere")
    assert filters.matches(mk("Sales Engineer", "Remote - Canada", "remote"), CFG, world)


def test_pay_and_workplace_detection():
    assert sources.pay_from_text("Base pay: $113,000 - $170,000 USD")[:2] == (113000, 170000)
    assert sources.pay_from_text("$120K–$150K plus equity")[:2] == (120000, 150000)
    assert sources.pay_from_text("raised $20M - $30M Series B")[:2] == (None, None)
    assert sources.workplace_from("OnSite") == "onsite"
    assert sources.workplace_from(None, "Hybrid - Austin") == "hybrid"
    assert sources.workplace_from("unspecified", "Austin, TX") == "unknown"


def test_parse_json_with_code_fence():
    assert ai._parse_json('```json\n{"score": 8}\n```') == {"score": 8}
    with pytest.raises(ai.AIError):
        ai._parse_json("no json here")


TAILOR_REPLY = {
    "summary": "Sales engineer who pairs technical depth in APIs and integrations with full-cycle sales experience.",
    "bullets": [
        {"n": 0, "text": "Run the full sales cycle from prospecting through technical demos and negotiation for mid-market accounts"},
        {"n": 1, "text": "Built $9.9M in pipeline"},                       # number not on the resume: rejected
        {"n": 4, "text": "x" * 400},                                       # far too long: rejected
        {"n": 99, "text": "No such bullet"},                               # rejected
    ],
    "changes": "Emphasized integrations and demos.",
    "missing": ["Kubernetes", "Ruby on Rails"]}


SYSTEMS: list = []
JOB_TAGS: dict = {}


def _fake_claude(monkeypatch, scores):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        assert headers["x-api-key"] == "test-key" and headers["anthropic-version"]
        calls.append(json["model"] + ("+search" if json.get("tools") else ""))
        SYSTEMS.append(json["system"])
        prompt = json["messages"][0]["content"]
        if json.get("tools"):
            assert json["tools"][0]["name"] == "web_search"
            snap = {"company_name": "Northwind Inc.", "rating": "4.4/5 (Glassdoor)", "ceo": "J. Doe, 92% approve",
                    "recommend": "", "pros": ["Smart team"], "cons": ["Fast pace"], "as_of": "Oct 2026",
                    "sources": ["https://example.com/reviews", "not-a-url"],
                    "discussions": [{"where": "Reddit r/sales", "summary": "Good SE comp", "url": "https://reddit.com/r/sales/1"},
                                    {"where": "X", "summary": "no link", "url": ""}],
                    "red_flags": ["Layoffs of 10% in Aug 2026"]}
            # Search replies arrive as several text blocks around tool results
            raw = json_mod.dumps(snap)
            return FakeResp(200, {"stop_reason": "end_turn", "content": [
                {"type": "server_tool_use", "name": "web_search", "input": {"query": "x"}},
                {"type": "web_search_tool_result", "content": []},
                {"type": "text", "text": raw[:20]}, {"type": "text", "text": raw[20:]}]})
        if "Tailor this resume" in prompt:
            text = json_mod.dumps(TAILOR_REPLY)
            return FakeResp(200, {"content": [{"type": "text", "text": text}]})
        if "Rate how well" in prompt:
            title = next(t for t in scores if f"Title: {t}\n" in prompt)
            text = json_mod.dumps({"score": scores[title], "summary": "fit", "strengths": ["demos"], "gaps": [],
                                   "role_family": "sales engineer",
                                   "tags": JOB_TAGS.get(title, ["api-integrations", "poc-demos", "mid-market"])})
        else:
            qs = [q for q in ["Why Acme?", "Will you require sponsorship?"] if f"- {q}" in prompt]
            text = json_mod.dumps({"bullets": ["b1", "b2", "b3"], "cold_call": "Hi", "linkedin_note": "Hello",
                                   "why_company": "Great fit.", "answers": [{"question": q, "answer": "ok"} for q in qs]})
        return FakeResp(200, {"content": [{"type": "text", "text": text}]})

    monkeypatch.setattr(ai.requests, "post", fake_post)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return calls


def _write_cfg(tmp_path):
    cfg = dict(CFG, companies={"ashby": ["Northwind", "missing"], "greenhouse": ["acme"], "lever": ["acme"]})
    import yaml
    p = tmp_path / "config.yml"
    p.write_text(yaml.safe_dump(cfg))
    r = tmp_path / "resume.md"
    r.write_text("Jordan Rivera, Solutions Engineer")
    return p, r


def test_full_scan_and_no_duplicates(tmp_path, monkeypatch, no_network):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    calls = _fake_claude(monkeypatch, {"Sales Engineer": 9, "Solutions Architect": 5, "GTM Engineer": 7})
    cfg, resume = _write_cfg(tmp_path)
    data_path, readme = tmp_path / "data/jobs.json", tmp_path / "README.md"

    prof_path = tmp_path / "profile.yml"
    s = scan.run(str(cfg), str(resume), data_path, readme, prof_path, tmp_path / "no-resume.docx")
    assert s["matching"] == 3 and s["scored"] == 3 and s["new_matches"] == 2
    assert any("missing" in p for p in s["problems"])
    data = store.load(data_path)
    se = data["jobs"]["ashby:Northwind:a1"]
    assert se["status"] == "new" and se["materials"]["bullets"] == ["b1", "b2", "b3"]
    assert "status" not in data["jobs"]["greenhouse:acme:11"]  # 5/10: scored, not alerted
    assert "description" not in se
    assert "Sales Engineer" in readme.read_text()
    assert calls.count("claude-sonnet-5-5") == 2 and calls.count("claude-haiku-5-5+search") == 2
    assert se["company_snapshot"]["rating"] == "4.4/5 (Glassdoor)"
    assert se["company_snapshot"]["sources"] == ["https://example.com/reviews"]
    assert [d["where"] for d in se["company_snapshot"]["discussions"]] == ["Reddit r/sales"]  # no-link thread dropped
    assert se["company_snapshot"]["red_flags"] == ["Layoffs of 10% in Aug 2026"]
    assert set(data["companies"]) == {"northwind", "acme"}
    gtm = data["jobs"]["lever:acme:l1"]
    assert gtm["materials"]["answers"] == []  # Lever doesn't publish its questions
    assert gtm["pay_text"] and gtm["workplace"] == "remote"

    s2 = scan.run(str(cfg), str(resume), data_path, readme, prof_path, tmp_path / "no-resume.docx")  # second run: nothing new
    assert s2["scored"] == 0 and s2["new_matches"] == 0


def test_scan_without_api_key_still_tracks(tmp_path, monkeypatch, no_network):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    cfg, resume = _write_cfg(tmp_path)
    s = scan.run(str(cfg), str(resume), tmp_path / "j.json", tmp_path / "R.md", tmp_path / "p.yml",
                 tmp_path / "no-resume.docx")
    assert s["scored"] == 0 and any("ANTHROPIC_API_KEY" in p for p in s["problems"])


def test_discord_payload_limits():
    job = {"score": 9, "title": "T" * 300, "company": "C", "summary": "s" * 5000,
           "strengths": ["a"], "gaps": [], "location": "Remote", "posted": RECENT, "url": "u"}
    emb = discord.job_alert(job, "https://github.com/x/y/issues/1")["embeds"][0]
    assert len(emb["title"]) <= 256 and len(emb["description"]) <= 4096


def _seed(tmp_path):
    data = {"jobs": {"k": {"key": "k", "title": "Sales Engineer", "company": "Northwind", "score": 9,
                           "status": "new", "issue_number": 5, "issue_url": "u", "url": "u"}}}
    p = tmp_path / "jobs.json"
    store.save(data, p)
    return p


@pytest.mark.parametrize("event_name,event,expected", [
    ("issues", {"action": "labeled", "issue": {"number": 5, "labels": [{"name": "job"}, {"name": "applied"}]}}, "applied"),
    ("issues", {"action": "labeled", "issue": {"number": 5, "labels": [{"name": "applied"}, {"name": "interviewing"}]}}, "interviewing"),
    ("issue_comment", {"action": "created", "issue": {"number": 5, "labels": []}, "comment": {"body": "/offer 🎉"}}, "offer"),
    ("issues", {"action": "closed", "issue": {"number": 5, "labels": [{"name": "job"}]}}, "closed"),
    ("issue_comment", {"action": "created", "issue": {"number": 5, "labels": []}, "comment": {"body": "nice"}}, None),
    ("issues", {"action": "labeled", "issue": {"number": 99, "labels": [{"name": "applied"}]}}, None),
])
def test_status_webhook(tmp_path, monkeypatch, event_name, event, expected):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    p = _seed(tmp_path)
    assert status.handle(event_name, event, p, tmp_path / "README.md") == expected
    if expected:
        job = store.load(p)["jobs"]["k"]
        assert job["status"] == expected and job["history"][-1]["status"] == expected


def test_discord_failure_does_not_crash(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(discord.requests, "post", lambda *a, **k: FakeResp(404, {"message": "Unknown Webhook"}))
    assert discord.send({"embeds": [{"title": "x"}]}) is False


def test_dashboard_sections():
    data = {"jobs": {
        "a": {"key": "a", "title": "SE", "company": "A", "score": 9, "status": "applied", "url": "u"},
        "b": {"key": "b", "title": "SA", "company": "B", "score": 4, "url": "u"},
        "c": {"key": "c", "title": "GTM | Eng", "company": "C", "score": 8, "status": "rejected", "url": "u"}}}
    md = dashboard.render(data)
    active, rest = md.split("## Closed out")
    assert "| A |" in active and "| C |" in rest and "GTM / Eng" in rest
    assert "Applied **1**" in md


def test_merge_keeps_both_sides():
    from jobradar import merge
    # Scan (ours) found a new job and re-scored k; meanwhile you marked k applied (theirs).
    theirs = {"jobs": {"k": {"key": "k", "status": "applied", "score": 9,
                             "history": [{"status": "new"}, {"status": "applied"}]}}}
    ours = {"jobs": {"k": {"key": "k", "status": "new", "score": 9, "last_seen": "today",
                           "history": [{"status": "new"}]},
                     "n": {"key": "n", "score": 8, "status": "new", "history": [{"status": "new"}]}}}
    m = merge.merge(ours, theirs)["jobs"]
    assert m["k"]["status"] == "applied" and m["k"]["last_seen"] == "today"
    assert "n" in m
    # Status run (ours) vs a scan that scored a new job (theirs): both kept.
    m2 = merge.merge(theirs, ours)["jobs"]
    assert m2["k"]["status"] == "applied" and "n" in m2


def test_discord_drops_empty_links(monkeypatch):
    sent = {}
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(discord.requests, "post", lambda url, json=None, timeout=None: sent.update(json) or FakeResp(204, None))
    assert discord.send(discord.status_update({"title": "SE", "company": "A"}, "new", "applied", None))
    assert "url" not in sent["embeds"][0]


ROOT = Path(__file__).parents[1]
TEMPLATES = ROOT / ".github/ISSUE_TEMPLATE"


def form_body(template: str, answers: dict) -> str:
    """Build an issue body the way GitHub does when a form is submitted."""
    import yaml
    spec = yaml.safe_load((TEMPLATES / template).read_text())
    out = []
    for f in spec["body"]:
        if not f.get("id"):
            continue
        label, value = f["attributes"]["label"], answers.get(f["id"])
        if f["type"] == "checkboxes":
            opts = [o["label"] for o in f["attributes"]["options"]]
            value = "\n".join(f"- [{'X' if o in (value or []) else ' '}] {o}" for o in opts)
        out.append(f"### {label}\n\n{value or '_No response_'}\n")
    return "\n".join(out)


QUIZ = {"full_name": "Jordan Rivera", "email": "a@example.com", "phone": "555-0100",
        "linkedin": "https://linkedin.com/in/x", "city": "Austin, TX",
        "work_arrangements": ["Remote", "Hybrid"], "remote_scope": "US only",
        "areas": "Austin, San Marcos\nRound Rock", "relocation": "No", "min_base": "$125k",
        "target_ote": "175,000", "salary_answer": "$130K–$150K base",
        "role_types": ["Sales / Solutions Engineer", "GTM / Growth Engineer"], "seniority": ["Mid-level", "Senior"],
        "company_sizes": ["Growth (50–500)"], "travel": "Up to 25%",
        "work_authorization": "Authorized to work in the US", "needs_sponsorship": "No",
        "start_date": "Two weeks after an offer", "deal_breakers": "No full-time travel",
        "companies": "https://jobs.ashbyhq.com/Northwind/979e\nhttps://job-boards.greenhouse.io/copperline/jobs/1\nhttps://jobs.lever.co/acme/x"}


def test_quiz_form_parses_and_builds_profile(monkeypatch):
    monkeypatch.chdir(ROOT)
    answers = forms.parse(form_body("preferences.yml", QUIZ), "preferences.yml")
    assert answers["work_arrangements"] == ["Remote", "Hybrid"] and answers["github"] == ""
    prof = preferences.build_profile(answers, profile.load(Path("/nonexistent")))
    assert prof["quiz_completed"] and prof["work_arrangements"] == ["remote", "hybrid"]
    assert prof["areas"] == ["austin", "san marcos", "round rock"]
    assert prof["min_base_salary"] == 125000 and prof["target_total_comp"] == 175000
    assert prof["extra_companies"] == {"ashby": ["Northwind"], "greenhouse": ["copperline"], "lever": ["acme"]}
    assert "remote" in profile.summary_for_ai(prof) and "$125,000" in profile.summary_for_ai(prof)


def test_preferences_webhook_saves_and_rescores(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    data = tmp_path / "jobs.json"
    store.save({"jobs": {"a": {"key": "a", "score": 6}, "b": {"key": "b", "score": 9, "status": "applied"}}}, data)
    event = {"issue": {"number": 3, "body": form_body("preferences.yml", QUIZ), "labels": [{"name": "preferences"}]}}
    preferences.handle(event, tmp_path / "profile.yml", data)
    saved = profile.load(tmp_path / "profile.yml")
    assert saved["contact"]["email"] == "a@example.com" and saved["quiz_completed"]
    jobs = store.load(data)["jobs"]
    assert "score" not in jobs["a"] and jobs["b"]["score"] == 9  # only un-alerted jobs re-scored


def test_scan_watches_dream_companies():
    prof = dict(PROF, extra_companies={"ashby": ["Northwind", "copperline"], "workable": ["x"]})
    merged = scan.all_companies({"companies": {"ashby": ["Northwind"], "lever": ["example-co"]}}, prof)
    assert merged["ashby"] == ["Northwind", "copperline"] and merged["lever"] == ["example-co"] and merged["workable"] == ["x"]


def test_add_job_webhook_scores_and_builds_kit(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    _fake_claude(monkeypatch, {"Solutions Engineer": 6})
    body = form_body("add-job.yml", {"link": "https://www.linkedin.com/jobs/view/1", "company": "Acme",
                                     "title": "Solutions Engineer", "location": "Remote", "workplace": "Remote",
                                     "pay": "$120,000 - $150,000", "description": "Demo our API."})
    cfg, resume = _write_cfg(tmp_path)
    rec = addjob.handle({"issue": {"number": 42, "body": body, "html_url": "https://github.com/x/y/issues/42"}},
                        str(cfg), str(resume), tmp_path / "jobs.json", tmp_path / "R.md",
                        tmp_path / "p.yml", tmp_path / "no-resume.docx")
    assert rec["key"] == "manual:42" and rec["score"] == 6 and rec["status"] == "new"
    assert (rec["pay_min"], rec["pay_max"]) == (120000, 150000) and rec["workplace"] == "remote"
    assert rec["materials"]["why_company"] and rec["issue_number"] == 42


def test_application_kit_in_card():
    prof = dict(PROF, contact={"full_name": "Jordan Rivera", "email": "a@example.com", "phone": "",
                               "linkedin": "", "github": "", "city": "Austin, TX"},
                needs_sponsorship="No", salary_answer="$130K+ base")
    job = {"key": "k", "score": 8, "title": "SE", "company": "Acme", "url": "u", "pay_text": "$113K – $170K",
           "workplace": "remote", "materials": {"bullets": ["b"], "cold_call": "c", "linkedin_note": "n",
                                                "why_company": "Because APIs.",
                                                "answers": [{"question": "Will you require sponsorship?", "answer": "No"}]}}
    body = github_api.issue_body(job, prof)
    assert "**First name**\n```\nJordan\n```" in body and "**Last name**\n```\nRivera\n```" in body
    assert "**Phone**\n_[fill in yourself]_" in body and "```\n$130K+ base\n```" in body
    assert "```\nBecause APIs.\n```" in body and "**Will you require sponsorship?**\n```\nNo\n```" in body
    assert "$113K – $170K" in body


def test_dashboard_quiz_banner():
    assert "Take the job preferences quiz" in dashboard.render({"jobs": {}}, prof=PROF)
    assert "Take the job preferences quiz" not in dashboard.render({"jobs": {}}, prof=dict(PROF, quiz_completed=True))


def test_greenhouse_card_answers_custom_questions(tmp_path, monkeypatch, no_network):
    _fake_claude(monkeypatch, {})
    job = sources.fetch_greenhouse("acme")[0]
    rec, problems = {}, []
    scan.make_card(job, rec, "resume", CFG["ai"], PROF, problems, None, tmp_path / "none.docx")
    assert [a["question"] for a in rec["materials"]["answers"]] == ["Why Acme?", "Will you require sponsorship?"]
    assert rec["status"] == "new" and not problems


def test_snapshot_cached_and_failure_is_soft(monkeypatch, no_network):
    calls = _fake_claude(monkeypatch, {})
    job = sources.fetch_ashby("Northwind")[0]
    data, problems = {"jobs": {}}, []
    first = scan.company_snapshot(job, data, CFG["ai"], problems)
    again = scan.company_snapshot(job, data, CFG["ai"], problems)
    assert first is again and calls.count("claude-haiku-5-5+search") == 1  # reused, not re-searched

    def broken(*a, **k):
        return FakeResp(400, {"error": {"message": "unknown tool type"}})
    monkeypatch.setattr(ai.requests, "post", broken)
    job.company = "Other"
    assert scan.company_snapshot(job, data, CFG["ai"], problems) is None
    assert any("snapshot" in p for p in problems)
    assert scan.company_snapshot(job, data, dict(CFG["ai"], company_snapshot=False), []) is None


def test_pause_turn_is_continued(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    replies = iter([
        FakeResp(200, {"stop_reason": "pause_turn", "content": [{"type": "text", "text": '{"a": '}]}),
        FakeResp(200, {"stop_reason": "end_turn", "content": [{"type": "text", "text": '1}'}]}),
    ])
    sent = []
    monkeypatch.setattr(ai.requests, "post", lambda url, json=None, headers=None, timeout=None: sent.append(
        [m["role"] for m in json["messages"]]) or next(replies))
    assert ai._call_claude("m", "s", "p", 10, [{"type": "t", "name": "web_search"}]) == '{"a": 1}'
    assert sent[1] == ["user", "assistant"]


def test_card_shows_employee_snapshot_and_links():
    job = {"key": "k", "score": 8, "title": "SE", "company": "Northwind", "url": "u",
           "company_snapshot": {"company_name": "Northwind Inc.", "rating": "4.4/5", "ceo": "J. Doe, 92% approve",
                                "recommend": "", "pros": ["Smart team"], "cons": [], "as_of": "Oct 2026",
                                "sources": ["https://example.com/r"],
                                "discussions": [{"where": "Reddit r/salesengineers", "summary": "Strong SE team",
                                                 "url": "https://reddit.com/x"}],
                                "red_flags": ["CEO left in Sep 2026"]}}
    body = github_api.issue_body(job, PROF)
    assert "Reddit r/salesengineers: Strong SE team ([thread](https://reddit.com/x))" in body
    assert "CEO left in Sep 2026" in body and "reddit.com/search" in body and "x.com/search" in body
    assert "**Overall rating:** 4.4/5" in body and "**CEO approval:** J. Doe, 92% approve" in body
    assert "Would recommend" not in body and "glassdoor.com/Search/results.htm?keyword=Northwind+Inc." in body
    emb = discord.job_alert(dict(job, summary="", strengths=[], gaps=[]), None)["embeds"][0]
    assert any(f["name"] == "Employees say" and "92%" in f["value"] for f in emb["fields"])
    assert any("Worth asking" in f["name"] for f in emb["fields"])


def test_merge_keeps_company_snapshots():
    from jobradar import merge
    ours = {"jobs": {}, "companies": {"a": {"fetched_at": "2026-11-02"}, "b": {"fetched_at": "2026-11-01"}}}
    theirs = {"jobs": {}, "companies": {"b": {"fetched_at": "2026-11-05"}, "c": {"fetched_at": "2026-11-03"}}}
    m = merge.merge(ours, theirs)["companies"]
    assert set(m) == {"a", "b", "c"} and m["b"]["fetched_at"] == "2026-11-05"


RESUME_DOCX = ROOT / "resume.docx"


def test_rewrite_safety_checks():
    from jobradar import tailor
    full = "Built $5.2M in pipeline across 10 opportunities, 118% of quota"
    assert tailor.safe_rewrite("Built $5.2M in pipeline", "Generated $5.2M of pipeline", full)
    assert not tailor.safe_rewrite("Built $5.2M in pipeline", "Built $7.5M in pipeline", full)
    assert not tailor.safe_rewrite("Short bullet", "A much, much longer bullet that keeps going and going", full)


@pytest.mark.skipif(not __import__("shutil").which("soffice"), reason="needs LibreOffice")
def test_tailored_resume_pdf(tmp_path, monkeypatch):
    from jobradar import tailor
    _fake_claude(monkeypatch, {})
    job = mk("Sales Engineer", "Remote", "remote")
    job.company, job.key = "Northwind", "ashby:Northwind:a1"
    rec, problems = {}, []
    scan.tailored_resume(job, rec, CFG["ai"], PROF, problems, RESUME_DOCX, tmp_path)
    tr = rec["tailored_resume"]
    assert not problems and tr["pdf"].endswith("/Jordan_Rivera_Resume.pdf")  # no company name in the file
    assert tailor.page_count(Path(tr["pdf"])) == 1
    assert tr["summary_changed"] and tr["changed_bullets"] == 1 and tr["rejected_for_safety"] == 3
    assert tr["missing"] == ["Kubernetes", "Ruby on Rails"]
    assert "Kubernetes" not in tailor.resume_text(Path(tr["docx"]))  # reported, never added
    text = tailor.resume_text(Path(tr["docx"]))
    assert "technical demos and negotiation" in text and "$9.9M" not in text
    assert "$5.2M" in text  # untouched bullets kept
    # The original resume is never changed
    assert "technical demos and negotiation" not in tailor.resume_text(RESUME_DOCX)


@pytest.mark.skipif(not __import__("shutil").which("soffice"), reason="needs LibreOffice")
def test_tailored_resume_drops_edits_to_stay_one_page(tmp_path, monkeypatch):
    from jobradar import tailor
    _fake_claude(monkeypatch, {})
    pages = iter([2, 2, 1])
    monkeypatch.setattr(tailor, "page_count", lambda pdf: next(pages))
    job = mk("Sales Engineer", "Remote", "remote")
    result = tailor.build(job, RESUME_DOCX, tmp_path, "r", "m")
    assert result["dropped_for_length"] == 2 and result["changed_bullets"] == 0 and not result["summary_changed"]


def test_card_links_tailored_resume():
    job = {"key": "k", "score": 8, "title": "SE", "company": "Northwind", "url": "u",
           "tailored_resume": {"pdf": "resumes/Northwind-SE-ab12/Jordan_Rivera_Resume_Northwind.pdf",
                               "docx": "resumes/Northwind-SE-ab12/Jordan_Rivera_Resume_Northwind.docx",
                               "summary_changed": True, "changed_bullets": 4, "rejected_for_safety": 1,
                               "changes": "Emphasized APIs.", "missing": ["Kubernetes"]},
           "materials": {"bullets": [], "cold_call": "", "linkedin_note": ""}}
    body = github_api.issue_body(job, PROF)
    assert "## 📄 Tailored resume" in body and "Reworded: summary, 4 bullets." in body
    assert "Jordan_Rivera_Resume_Northwind.pdf" in body and "1 suggested edits were thrown out" in body
    assert "Attach your tailored resume" in body
    assert "your resume doesn't show them" in body and "- Kubernetes" in body


def test_discord_attaches_pdf(tmp_path, monkeypatch):
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    seen = {}

    def fake_post(url, data=None, files=None, json=None, timeout=None):
        seen.update(data=data, files=files)
        return FakeResp(200, {})
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(discord.requests, "post", fake_post)
    assert discord.send({"embeds": [{"title": "x"}]}, str(pdf))
    assert "payload_json" in seen["data"] and seen["files"]["files[0]"][0] == "r.pdf"


def test_resume_is_cached_and_boilerplate_trimmed(monkeypatch):
    SYSTEMS.clear()
    _fake_claude(monkeypatch, {"Sales Engineer": 8})
    job = mk("Sales Engineer", "Remote", "remote")
    job.description = ("Own technical discovery and demos.\n\nWe are an equal opportunity employer and do not "
                       "discriminate.\nReasonable accommodation is available.\n" + "Build integrations. " * 800)
    ai.score_job(job, "MY RESUME", "claude-haiku-5-5", "Remote only")
    system = SYSTEMS[-1]
    assert isinstance(system, list) and system[1]["cache_control"] == {"type": "ephemeral"}
    assert "MY RESUME" in system[1]["text"] and "Remote only" in system[1]["text"]
    block = ai._job_block(job, ai.SCORING_DESCRIPTION_CHARS)
    assert "equal opportunity" not in block and "accommodation" not in block
    assert "technical discovery" in block and len(block) < ai.SCORING_DESCRIPTION_CHARS + 300


def test_repeat_postings_scored_once_and_daily_card_cap(tmp_path, monkeypatch, no_network):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    global ASHBY
    many = [dict(ASHBY["jobs"][0], id=f"c{i}", location=f"Remote US {i}") for i in range(3)]   # same role 3x
    roles = [dict(ASHBY["jobs"][0], id=f"r{i}", title=f"Solutions Engineer {name}")
             for i, name in enumerate(["Payments", "Data", "Security"])]
    monkeypatch.setitem(ASHBY, "jobs", many + roles)
    titles = {"Sales Engineer": 9, "Solutions Engineer Payments": 8, "Solutions Engineer Data": 8,
              "Solutions Engineer Security": 7}
    calls = _fake_claude(monkeypatch, titles)
    import yaml
    cfg = dict(CFG, companies={"ashby": ["Northwind"]}, ai=dict(CFG["ai"], max_cards_per_run=2,
                                                            company_snapshot=False, tailored_resume=False))
    cp = tmp_path / "config.yml"
    cp.write_text(yaml.safe_dump(cfg))
    resume = tmp_path / "resume.md"
    resume.write_text("resume")
    args = (str(cp), str(resume), tmp_path / "jobs.json", tmp_path / "R.md", tmp_path / "p.yml", tmp_path / "x.docx")

    s1 = scan.run(*args)
    jobs = store.load(tmp_path / "jobs.json")["jobs"]
    assert calls.count("claude-haiku-5-5") == 4          # 6 postings, 4 distinct roles
    assert sum(1 for j in jobs.values() if j.get("duplicate_of")) == 2
    assert s1["new_matches"] == 2                         # capped at 2 cards today
    assert sum(1 for j in jobs.values() if j.get("card_waiting")) == 2

    s2 = scan.run(*args)                                  # next morning: the waiting two get built
    jobs = store.load(tmp_path / "jobs.json")["jobs"]
    assert s2["new_matches"] == 2 and s2["scored"] == 0
    assert sum(1 for j in jobs.values() if j.get("status") == "new") == 4
    assert not any(j.get("card_waiting") for j in jobs.values())


def test_score_keeps_only_known_tags(monkeypatch):
    JOB_TAGS["Sales Engineer"] = ["webhooks", "made-up-tag", "SECURITY"]
    try:
        _fake_claude(monkeypatch, {"Sales Engineer": 8})
        r = ai.score_job(mk("Sales Engineer", "Remote", "remote"), "resume", "claude-haiku-5-5")
        assert r["tags"] == ["security", "webhooks"] and r["role_family"] == "sales engineer"
    finally:
        JOB_TAGS.clear()


@pytest.mark.skipif(not __import__("shutil").which("soffice"), reason="needs LibreOffice")
def test_similar_jobs_reuse_a_library_resume(tmp_path, monkeypatch):
    calls = _fake_claude(monkeypatch, {})
    data, problems = {"jobs": {}}, []
    first = mk("Sales Engineer", "Remote", "remote")
    first.company, first.key = "Northwind", "ashby:Northwind:a1"
    rec1 = {"key": first.key, "role_family": "sales engineer", "tags": ["api-integrations", "poc-demos", "smb"]}
    scan.tailored_resume(first, rec1, CFG["ai"], PROF, problems, RESUME_DOCX, tmp_path, data)
    tailor_calls = len(calls)

    # Close match (3 of 4 tags shared): same file, no AI call
    second = mk("Sales Engineer", "Remote", "remote")
    second.company, second.key = "Acme", "greenhouse:acme:1"
    rec2 = {"key": second.key, "role_family": "sales engineer",
            "tags": ["api-integrations", "poc-demos", "smb", "webhooks"]}
    scan.tailored_resume(second, rec2, CFG["ai"], PROF, problems, RESUME_DOCX, tmp_path, data)
    assert len(calls) == tailor_calls
    assert rec2["tailored_resume"]["reused"] and rec2["tailored_resume"]["pdf"] == rec1["tailored_resume"]["pdf"]
    assert rec2["tailored_resume"]["made_for"] == "Sales Engineer at Northwind"

    # Different kind of job: a new version is made
    third = mk("Sales Engineer", "Remote", "remote")
    third.company, third.key = "Bank", "lever:bank:1"
    rec3 = {"key": third.key, "role_family": "sales engineer", "tags": ["security", "enterprise", "networking"]}
    scan.tailored_resume(third, rec3, CFG["ai"], PROF, problems, RESUME_DOCX, tmp_path, data)
    assert not rec3["tailored_resume"]["reused"] and len(data["resume_variants"]) == 2
    vid = rec1["tailored_resume"]["variant"]
    assert data["resume_variants"][vid]["used_by"] == ["ashby:Northwind:a1", "greenhouse:acme:1"]

    data["jobs"] = {"ashby:Northwind:a1": {"company": "Northwind"}, "greenhouse:acme:1": {"company": "Acme"}}
    dashboard.write_resume_library(data, tmp_path)
    page = (tmp_path / "README.md").read_text()
    assert f"`resume-{vid}`" in page and "also sent to: Acme" in page and "security" in page


def test_reused_resume_card_text():
    job = {"key": "k", "score": 8, "title": "SE", "company": "Acme", "url": "u",
           "tailored_resume": {"pdf": "resumes/ab12cd/Jordan_Rivera_Resume.pdf",
                               "docx": "resumes/ab12cd/Jordan_Rivera_Resume.docx", "variant": "ab12cd",
                               "reused": True, "similarity": 0.75, "made_for": "Sales Engineer at Northwind"}}
    body = github_api.issue_body(job, PROF)
    assert "Reusing the resume made for **Sales Engineer at Northwind**" in body and "75%" in body
    assert "`resume-ab12cd`" in body


def test_merge_keeps_resume_library():
    from jobradar import merge
    ours = {"jobs": {}, "resume_variants": {"a": {"pdf": "p", "used_by": ["j1", "j3"]}}}
    theirs = {"jobs": {}, "resume_variants": {"a": {"pdf": "p", "used_by": ["j1", "j2"]}, "b": {"used_by": ["j4"]}}}
    m = merge.merge(ours, theirs)["resume_variants"]
    assert set(m) == {"a", "b"} and m["a"]["used_by"] == ["j1", "j2", "j3"]


@pytest.mark.parametrize("where, ok", [
    ("Remote, United States", True), ("United States (Remote)", True), ("Remote U.S.", True), ("Remote", True),
    ("Remote, AMER", True), ("US-SF, US-NYC, US-Remote", True), ("Illinois, USA, Remote; Texas, USA, Remote", True),
    ("Atlanta, Georgia; Remote - Colorado; Remote - Texas", True), ("Remote (United States | Canada)", True),
    ("Chicago, Illinois; Remote - California; Remote - New York; United States", True),
    ("US-DE-Remote / US-VA-Remote / US-MD-Remote", False), ("Remote - FL; Remote - GA; Remote - NC", False),
    ("BE-Belgium-Remote", False), ("Switzerland (Remote)", False), ("Remote, UAE", False), ("Remote - Canada", False),
    ("Arkansas; Remote - Illinois; Remote - Ohio", False), ("Remote, Colorado, United States, AMER", False),
])
def test_remote_jobs_must_be_open_to_your_state(where, ok):
    prof = {"contact": {"city": "Austin Texas"}}
    assert filters.remote_us_ok(where, prof, {"non_us_markers": ["canada", "emea"]}) is ok


def test_remote_state_lists_allowed_when_home_state_unknown():
    assert filters.remote_us_ok("Remote - FL; Remote - GA", {}, {})
