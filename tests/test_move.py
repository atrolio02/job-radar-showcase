"""The dashboard's Move buttons."""
from jobradar import dashboard, move, store


def data():
    return {"jobs": {
        "a": {"company": "Northwind", "title": "SE", "status": "new", "issue_number": 5, "score": 8},
        "b": {"company": "Contoso", "title": "SE", "status": "skip", "issue_number": 4, "score": 8},
        "c": {"company": "Fabrikam", "title": "SE", "status": "applied", "issue_number": 6, "score": 7}}}


def test_parse_titles():
    assert move.parse("Move #8 to applied") == (8, "applied")
    assert move.parse("move 12 to Active") == (12, "new")
    assert move.parse("New job") is None


def test_handle_moves_and_records_history(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    store.save(data(), path)
    monkeypatch.setattr(dashboard, "write", lambda *a, **k: None)
    monkeypatch.setattr(move.discord, "send", lambda *a, **k: None)
    assert move.handle({"issue": {"number": 50, "title": "Move #4 to active"}}, path) == "new"
    job = store.load(path)["jobs"]["b"]
    assert job["status"] == "new" and job["history"][-1]["status"] == "new"
    assert move.handle({"issue": {"number": 51, "title": "Move #99 to applied"}}, path) is None


def test_dashboard_has_applied_section_and_buttons(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/job-radar")
    md = dashboard.render(data(), prof={"quiz_completed": True})
    active, applied, closed = md.split("## Applied")[0], md.split("## Applied")[1].split("## Closed out")[0], \
        md.split("## Closed out")[1].split("<details>")[0]
    assert "Northwind" in active and "Move+%2305+to" not in active and "Move+%235+to+applied" in active
    assert "Fabrikam" in applied and "Move+%236+to+interviewing" in applied
    assert "Contoso" in closed and "Move+%234+to+active" in closed and "↩️ Move to active" in closed


def test_signed_one_tap_links_match_the_server(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yml").write_text("move_links: https://radar.example.workers.dev/\n")
    monkeypatch.setenv("MOVE_LINK_SECRET", "s" * 40 + "\n")
    # Same signature the Worker computes (mcp-server/test/server.test.ts uses this secret too)
    assert dashboard.move_link(4, "active") == "https://radar.example.workers.dev/move/4/active/da42228748f8da57cca84f3ee6edb56a"
    md = dashboard.render(data(), prof={"quiz_completed": True})
    assert "issues/new?template=move.yml" not in md and "| **#5** |" in md


def test_score_6_gets_a_standard_card_and_lower_gets_a_promote_link(monkeypatch, tmp_path):
    from jobradar import promote, scan
    opened = []
    monkeypatch.setattr(scan, "open_card", lambda job, rec, *a, **k: (opened.append(rec["key"]),
                                                                       rec.update(status="new", issue_number=40)))
    d = {"jobs": {"six": {"key": "six", "company": "A", "title": "SE", "score": 6, "kit_pending": "x"},
                  "five": {"key": "five", "company": "B", "title": "SE", "score": 5},
                  "seven": {"key": "seven", "company": "C", "title": "SE", "score": 7}}}
    cfg = {"card_min_score": 6, "alert_min_score": 7}
    assert promote.auto_cards(d, cfg, {}, []) == 1 and opened == ["six"]
    assert d["jobs"]["six"]["standard_resume"] and "kit_pending" not in d["jobs"]["six"]

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yml").write_text("move_links: https://radar.example.workers.dev\n")
    monkeypatch.setenv("MOVE_LINK_SECRET", "s" * 40)
    md = dashboard.render(d, prof={"quiz_completed": True})
    jid = dashboard.short_id("five")
    assert f"/promote/{jid}/" in md and "➕ Move to active" in md
    assert promote.find(d, jid.lower())["key"] == "five"
    closed = md.split("## Closed out")[1].split("<details>")[0]
    assert "| Job ID | Move to (one tap) |" in closed  # same columns as Active even when empty


def test_promote_signature_matches_the_server():
    import hashlib, hmac
    assert hmac.new(b"s" * 40, b"promote:J-1a2b3c", hashlib.sha256).hexdigest()[:32] == "3992e97ea92956b603dd30e16085346b"


def test_site_column_names_the_application_site():
    assert dashboard.site({"url": "https://job-boards.greenhouse.io/x/jobs/1"}) == "Greenhouse"
    assert dashboard.site({"url": "https://www.example.com/careers?gh_jid=5"}) == "Greenhouse"
    assert dashboard.site({"url": "https://acme.wd1.myworkdayjobs.com/job/1"}) == "Workday"
    assert dashboard.site({"url": "https://careers.example.com/1", "source": "ashby"}) == "Ashby"
    assert dashboard.site({"url": "https://www.example.org/jobs/2"}) == "example.org"


def test_manual_watch_links_each_company_to_a_linkedin_search(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yml").write_text(
        "manual_watch:\n  keywords: operations\n  location: Austin, Texas\n  groups:\n    Energy: [Acme Oil, Globex]\n")
    md = dashboard.render(data(), prof={"quiz_completed": True})
    assert "Companies to check by hand" in md and "**Energy:**" in md
    assert "[Acme Oil](https://www.linkedin.com/jobs/search/?keywords=Acme%20Oil%20operations&location=Austin%2C%20Texas)" in md


def test_unscored_job_gets_a_discord_alert():
    from jobradar import discord
    msg = discord.job_alert({"title": "FDE", "company": "Glean", "url": "https://x"}, None)
    assert "FDE at Glean" in str(msg)
