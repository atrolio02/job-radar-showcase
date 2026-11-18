"""The MCP path: free code does the work, Claude only does the last mile. Nothing real is called."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from jobradar import ai, discord, lastmile, preferences, rules, scan, sources, store
from test_radar import CFG, fake_get

RESUME = ("Jordan Rivera, Solutions Engineer. Ran discovery and demos, built REST API and webhook "
          "integrations, wrote SQL. Proof of concept work with SMB customers.")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(sources.requests, "get", fake_get)
    for var in ("GITHUB_TOKEN", "DISCORD_WEBHOOK_URL", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    def no_ai(*a, **k):
        raise AssertionError("the Claude API must not be called in this mode")
    monkeypatch.setattr(ai, "_call_claude", no_ai)
    sent = []
    monkeypatch.setattr(discord, "send", lambda payload, attachment=None: sent.append(payload) or True)

    def setup(mode="mcp", **ai_extra):
        cfg = dict(CFG, companies={"ashby": ["Northwind"], "greenhouse": ["acme"], "lever": ["acme"]})
        cfg["ai"] = {**CFG["ai"], "mode": mode, "rules_min_score": 40, **ai_extra}
        (tmp_path / "config.yml").write_text(yaml.safe_dump(cfg))
        (tmp_path / "resume.md").write_text(RESUME)
        return dict(config_path=str(tmp_path / "config.yml"), resume_path=str(tmp_path / "resume.md"),
                    data_path=tmp_path / "data/jobs.json", readme_path=tmp_path / "README.md",
                    profile_path=tmp_path / "profile.yml", base_docx=tmp_path / "none.docx",
                    out_root=tmp_path / "resumes")

    return tmp_path, setup, sent


def inbox(tmp):
    return json.loads((tmp / "data/mcp/inbox.json").read_text())


def submit(tmp, name, payload):
    d = tmp / "data/mcp/incoming"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(payload))


def test_scan_queues_jobs_for_claude_without_any_ai(env, monkeypatch):
    tmp, setup, sent = env
    monkeypatch.chdir(tmp)
    args = setup()
    s = scan.run(**args)
    assert s["mode"] == "mcp" and s["scored"] == 0 and s["queued_for_claude"] == 3
    box = inbox(tmp)
    first = box["to_score"][0]
    assert first["rules_score"] >= 40 and first["posting"] and first["key"]
    assert [j["rules_score"] for j in box["to_score"]] == sorted((j["rules_score"] for j in box["to_score"]), reverse=True)
    ctx = json.loads((tmp / "data/mcp/context.json").read_text())
    assert "webhook" in ctx["resume"] and ctx["alert_min_score"] == 7 and "workflow" in ctx["instructions"]
    assert scan.run(**args)["queued_for_claude"] == 3  # a second scan doesn't queue them twice


def test_rules_skip_weak_jobs():
    job = sources.Job("k", "ashby", "Acme", "Sales Engineer", "Remote", True, "u", None,
                      description="Payroll HCM expertise required. Kubernetes, Snowflake, Jamf.", workplace="remote")
    weak, _, _ = rules.score(job, RESUME, {"role_types": ["Sales / Solutions Engineer"]})
    job.description = "Discovery, demos, REST APIs, webhooks, SQL and proof of concept work."
    strong, reasons, tags = rules.score(job, RESUME, {"role_types": ["Sales / Solutions Engineer"]})
    assert strong > weak and strong >= 70 and "webhooks" in tags and reasons
    job.description += " Requires 50% travel."
    broke, reasons, _ = rules.score(job, RESUME, {"role_types": ["Sales / Solutions Engineer"],
                                                  "deal_breakers": "50% travel, cold calling"})
    assert broke <= strong - 40 and any("deal-breaker" in r for r in reasons)


def test_claude_scores_then_kit_build_the_card(env, monkeypatch):
    tmp, setup, sent = env
    monkeypatch.chdir(tmp)
    args = setup()
    scan.run(**args)
    keys = [j["key"] for j in inbox(tmp)["to_score"]]
    submit(tmp, "1-scores.json", {"kind": "scores", "items": [
        {"key": keys[0], "score": 9, "summary": "Strong fit", "strengths": ["APIs"], "gaps": [],
         "role_family": "sales engineer", "tags": ["api-integrations", "webhooks", "not-a-tag"]},
        {"key": keys[1], "score": 4, "summary": "Weak", "strengths": [], "gaps": ["onsite"]},
        {"key": "nope:1", "score": 8}]})
    apply_args = {k: v for k, v in args.items()}
    s = lastmile.apply_incoming(**apply_args)
    assert s["scored"] == 2 and not list((tmp / "data/mcp/incoming").glob("*.json"))
    data = store.load(args["data_path"])
    strong, weak = data["jobs"][keys[0]], data["jobs"][keys[1]]
    assert strong["tags"] == ["api-integrations", "webhooks"] and strong["kit_pending"]
    assert "pending_brief" not in weak and "kit_pending" not in weak
    box = inbox(tmp)
    assert [m["key"] for m in box["strong_matches"]] == [keys[0]]
    assert box["strong_matches"][0]["research_needed"] is True and box["strong_matches"][0]["posting"]
    assert keys[0] not in [j["key"] for j in box["to_score"]]

    submit(tmp, "2-kit.json", {"kind": "kit", "key": keys[0],
                               "materials": {"bullets": ["b1", "b2", "b3", "b4"], "why_company": "Because.",
                                             "cold_call": "Hi", "linkedin_note": "x" * 400, "answers": []},
                               "research": {"company_name": "Northwind", "rating": "4.4/5", "pros": ["Team"],
                                            "sources": ["https://example.com", "javascript:alert(1)"]}})
    s = lastmile.apply_incoming(**apply_args)
    data = store.load(args["data_path"])
    card = data["jobs"][keys[0]]
    assert s["kits"] == 1 and card["status"] == "new" and len(card["materials"]["bullets"]) == 3
    assert len(card["materials"]["linkedin_note"]) == 300
    assert card["company_snapshot"]["sources"] == ["https://example.com"]
    assert data["companies"]["northwind"]["rating"] == "4.4/5" and "kit_pending" not in card
    assert any("Northwind" in json.dumps(p) for p in sent)  # Discord alert went out
    assert inbox(tmp)["strong_matches"] == []


def test_bad_submissions_are_rejected_not_fatal(env, monkeypatch):
    tmp, setup, sent = env
    monkeypatch.chdir(tmp)
    args = setup()
    scan.run(**args)
    key = inbox(tmp)["to_score"][0]["key"]
    submit(tmp, "a.json", {"kind": "scores", "items": [{"key": key, "score": "very good"}]})
    submit(tmp, "b.json", "not even an object")
    submit(tmp, "c.json", {"kind": "delete_everything"})
    s = lastmile.apply_incoming(**args)
    assert s["submissions"] == 3 and s["scored"] == 0 and len(s["problems"]) == 3
    assert "score" not in store.load(args["data_path"])["jobs"][key]


def test_card_opens_without_kit_if_claude_never_sends_one(env, monkeypatch):
    tmp, setup, sent = env
    monkeypatch.chdir(tmp)
    args = setup(kit_wait_hours=30)
    scan.run(**args)
    key = inbox(tmp)["to_score"][0]["key"]
    submit(tmp, "s.json", {"kind": "scores", "items": [{"key": key, "score": 8, "summary": "Good"}]})
    lastmile.apply_incoming(**args)
    data = store.load(args["data_path"])
    data["jobs"][key]["kit_pending"] = (datetime.now(timezone.utc) - timedelta(hours=31)).isoformat()
    store.save(data, args["data_path"])
    s = scan.run(**args)
    rec = store.load(args["data_path"])["jobs"][key]
    assert s["new_matches"] == 1 and rec["status"] == "new" and "materials" not in rec
    assert any("No kit from Claude" in p for p in s["problems"])


def test_stale_inbox_warns(env, monkeypatch):
    tmp, setup, sent = env
    monkeypatch.chdir(tmp)
    args = setup()
    scan.run(**args)
    data = store.load(args["data_path"])
    for rec in data["jobs"].values():
        if rec.get("queued_at"):
            rec["queued_at"] = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    store.save(data, args["data_path"])
    assert any("waited 4 days" in p for p in scan.run(**args)["problems"])


def test_rules_mode_alerts_with_no_ai(env, monkeypatch):
    tmp, setup, sent = env
    monkeypatch.chdir(tmp)
    s = scan.run(**setup("rules"))
    assert s["mode"] == "rules" and s["scored"] >= 1 and s["new_matches"] >= 1


def test_quiz_resets_queue(tmp_path):
    data = {"jobs": {"a": {"queued_at": "x", "rules_score": 50, "pending_brief": "p"},
                     "b": {"score": 8, "kit_pending": "x", "status": None},
                     "c": {"score": 9, "status": "applied"}}}
    preferences.rescore_unalerted(data)
    assert data["jobs"]["a"] == {} and data["jobs"]["b"] == {"status": None}
    assert data["jobs"]["c"]["score"] == 9


def test_added_job_is_queued_with_a_guaranteed_kit(env, monkeypatch):
    from jobradar import addjob
    from test_radar import form_body
    tmp, setup, sent = env
    body = form_body("add-job.yml", {"link": "https://x.com/1", "company": "Acme", "title": "Solutions Engineer",
                                     "location": "Remote", "workplace": "Remote", "pay": "",
                                     "description": "Demo our API."})
    import shutil
    shutil.copytree(Path(__file__).parents[1] / ".github/ISSUE_TEMPLATE", tmp / ".github/ISSUE_TEMPLATE")
    monkeypatch.chdir(tmp)
    args = setup()
    rec = addjob.handle({"issue": {"number": 42, "body": body, "html_url": "https://github.com/x/y/issues/42"}},
                        args["config_path"], args["resume_path"], args["data_path"], args["readme_path"],
                        args["profile_path"], args["base_docx"], args["out_root"])
    assert rec["force_kit"] and rec["queued_at"] and "score" not in rec
    submit(tmp, "s.json", {"kind": "scores", "items": [{"key": "manual:42", "score": 5, "summary": "Meh"}]})
    lastmile.apply_incoming(**args)
    assert inbox(tmp)["strong_matches"][0]["key"] == "manual:42"  # you picked it, so it still gets a kit


def test_refresh_rebuilds_briefing_and_inbox(env, monkeypatch):
    tmp, setup, sent = env
    monkeypatch.chdir(tmp)
    args = setup()
    scan.run(**args)
    (tmp / "data/mcp/context.json").unlink()  # lost in a merge
    (tmp / "data/mcp/inbox.json").unlink()
    monkeypatch.setattr(scan, "default_resume_path", lambda: str(tmp / "resume.md"))
    lastmile.refresh(args["config_path"])
    assert (tmp / "data/mcp/context.json").exists() and inbox(tmp)["to_score"]


def test_role_priority_ranks_gtm_first():
    from jobradar import profile
    prof = {"role_types": ["Sales / Solutions Engineer", "Solutions Architect / Consultant", "GTM / Growth Engineer"],
            "role_priority": ["GTM / Growth Engineer", "Solutions Architect / Consultant", "Sales / Solutions Engineer"]}
    assert profile.role_rank(prof, "GTM Engineer") == 0
    assert profile.role_rank(prof, "Solutions Consultant, Commercial") == 1
    assert profile.role_rank(prof, "Sales Engineer") == 2
    assert "GTM / Growth Engineer > Solutions Architect" in profile.summary_for_ai(prof)
    job = lambda t: sources.Job("k", "ashby", "Acme", t, "Remote", True, "u", None,
                                description="Discovery, demos, REST APIs, webhooks, SQL.", workplace="remote")
    gtm, _, _ = rules.score(job("GTM Engineer"), RESUME, prof)
    se, _, _ = rules.score(job("Sales Engineer"), RESUME, prof)
    assert gtm > se


def test_rules_handle_date_only_posted():
    job = sources.Job("k", "manual", "Acme", "Sales Engineer", "Remote", True, "u", "2026-11-15",
                      description="Demos and REST APIs.", workplace="remote")
    assert rules.score(job, RESUME, {"role_types": ["Sales / Solutions Engineer"]})[0] > 0


def test_focus_keeps_only_gtm_and_ai_solutions_roles():
    from jobradar import filters
    cfg = {"ai_companies": ["openai", "Northwind"]}
    prof = {"focus": ["GTM / Growth Engineer", "AI Solutions Engineer"]}
    ok = lambda t, c="acme": filters.focus_ok(t, c, cfg, prof)
    assert ok("GTM Engineer") and ok("RevOps Engineer") and ok("Senior GTM Systems Engineer")
    assert ok("AI Solutions Engineer") and ok("Solutions Engineer, Agents") and ok("Forward Deployed Engineer")
    assert ok("Solutions Engineer", "openai") and ok("Sales Engineer", "Northwind")
    assert not ok("Solutions Engineer") and not ok("Solutions Consultant, Commercial", "wingtip")
    assert not ok("Account Executive, AI", "openai") and not ok("Email Marketing Manager")
    assert filters.focus_ok("Sales Engineer", "acme", cfg, {})  # no focus: everything passes


def test_focus_sets_aside_queued_jobs(env, monkeypatch):
    tmp, setup, sent = env
    monkeypatch.chdir(tmp)
    args = setup()
    scan.run(**args)
    assert inbox(tmp)["to_score"]
    import yaml
    (tmp / "profile.yml").write_text(yaml.safe_dump({"focus": ["GTM / Growth Engineer"]}))
    scan.run(**args)
    assert [j["title"] for j in inbox(tmp)["to_score"]] == ["GTM Engineer"]


def test_focus_includes_sales_ops_and_supply_chain():
    from jobradar import filters
    prof = {"focus": ["Sales Operations", "BizOps / Supply Chain Systems"]}
    assert filters.focus_ok("Sales Operations Analyst", "x", {}, prof)
    assert filters.focus_ok("Supply Chain Analyst", "x", {}, prof)
    assert not filters.focus_ok("Deal Desk Specialist", "x", {}, prof)
    assert not filters.focus_ok("Deal Operations Analyst", "x", {}, prof)
    assert not filters.focus_ok("Software Engineer", "x", {}, prof)
    assert filters.focus_ok("Strategy & Operations Associate", "x", {}, prof)
    assert not filters.focus_ok("Logistics Coordinator", "x", {}, prof)
    assert not filters.focus_ok("Sourcing Specialist", "x", {}, prof)
    assert not filters.focus_ok("People Operations Specialist", "x", {}, prof)
    assert not filters.focus_ok("Recruiting and Sourcing Analyst", "x", {}, prof)
