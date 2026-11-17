"""Tests for the error tracker and efficiency log. Nothing real is called."""
import json
from pathlib import Path

import pytest
import requests

from jobradar import github_api, health, scan, sources, telemetry
from test_radar import FakeResp, _fake_claude, _write_cfg, fake_get  # noqa: F401


@pytest.fixture
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for var in ("GITHUB_TOKEN", "DISCORD_WEBHOOK_URL", "GITHUB_STEP_SUMMARY", "RUNNER_TEMP"):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path
    telemetry._remove_hook()
    telemetry._run = None


def runs(tmp):
    return [json.loads(f.read_text()) for f in sorted((tmp / "data/health/runs").glob("*.json"))]


def test_run_is_timed_logged_and_reported(in_tmp):
    def work():
        with telemetry.stage("fetch"):
            telemetry.company("ashby:Northwind", 1.5, 12)
        with telemetry.stage("cards"), telemetry.stage("tailored resume"):
            pass
        telemetry.count(fetched=12, scored=3, new_matches=1, waiting=0)
        telemetry.warn("lever: 'acme' couldn't be reached (ConnectTimeout).")
        return "done"

    assert telemetry.main("scan", work) == "done"
    [r] = runs(in_tmp)
    assert r["outcome"] == "warnings" and r["counts"]["fetched"] == 12
    assert {"fetch", "cards", "cards/tailored resume"} <= set(r["stages"])
    assert r["companies"]["ashby:Northwind"] == {"seconds": 1.5, "jobs": 12, "error": None}
    report = (in_tmp / "data/health/REPORT.md").read_text()
    assert "## At a glance" in report and "| scan | 1 | 0 | 1 |" in report and "ConnectTimeout" in report
    assert requests.sessions.Session.request is telemetry._original_request  # hook removed afterwards


def test_claude_tokens_become_cost(in_tmp, monkeypatch):
    usage = {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 4000,
             "cache_creation_input_tokens": 0, "server_tool_use": {"web_search_requests": 2}}

    def fake_request(self, method, url, *a, **k):
        if "anthropic" in url:
            return FakeResp(200, {"model": "claude-haiku-5-5", "usage": usage})
        return FakeResp(503, {})

    monkeypatch.setattr(telemetry, "_original_request", fake_request)

    def work():
        with telemetry.stage("score"):
            requests.post("https://api.anthropic.com/v1/messages", json={})
        requests.get("https://boards-api.greenhouse.io/v1/boards/x/jobs")

    telemetry.main("scan", work)
    [r] = runs(in_tmp)
    a = r["ai"]["claude-haiku-5-5"]
    expected = (1000 * 0.10 + 200 * 0.50 + 4000 * 0.01) / 1e6 + 2 * telemetry.WEB_SEARCH_PRICE
    assert a["calls"] == 1 and a["cache_read"] == 4000 and a["cost_usd"] == pytest.approx(expected, abs=1e-5)
    assert r["ai_by_stage"]["score"]["calls"] == 1
    assert r["http"]["boards-api.greenhouse.io"]["errors"] == 1
    assert "Claude API" in (in_tmp / "data/health/REPORT.md").read_text()


def test_crash_is_logged_alerted_and_reraised(in_tmp, monkeypatch):
    sent = []
    monkeypatch.setattr(telemetry, "alert", lambda title, body, fp, url=None: sent.append((title, fp)))

    def broken():
        with telemetry.stage("score"):
            raise KeyError("score")

    with pytest.raises(KeyError):
        telemetry.main("scan", broken)
    [r] = runs(in_tmp)
    assert r["outcome"] == "failed" and r["errors"][0]["fatal"] and r["errors"][0]["stage"] == "score"
    assert sent and "KeyError" in sent[0][0] and sent[0][1] == r["errors"][0]["fingerprint"]


def test_logging_problems_never_break_a_run(in_tmp, monkeypatch):
    monkeypatch.setattr(telemetry, "_record_ai", lambda *a: 1 / 0)
    monkeypatch.setattr(telemetry, "_original_request",
                        lambda self, m, u, *a, **k: FakeResp(200, {"model": "x", "usage": {}}))
    assert telemetry.main("scan", lambda: requests.post("https://api.anthropic.com/v1/messages").status_code) == 200


def test_unreadable_board_is_skipped_not_fatal(monkeypatch):
    def fetch_bad(board):
        raise KeyError("jobs")
    monkeypatch.setitem(sources.FETCHERS, "ashby", fetch_bad)
    monkeypatch.setattr(sources.requests, "get", fake_get)
    jobs, problems = sources.fetch_all({"ashby": ["Weird"], "greenhouse": ["acme"]})
    assert jobs and any("couldn't read" in p for p in problems)


def test_full_scan_logs_every_step(in_tmp, monkeypatch):
    monkeypatch.setattr(sources.requests, "get", fake_get)
    _fake_claude(monkeypatch, {"Sales Engineer": 9, "Solutions Architect": 5, "GTM Engineer": 7})
    cfg, resume = _write_cfg(in_tmp)
    telemetry.main("scan", scan.run, str(cfg), str(resume), in_tmp / "jobs.json", in_tmp / "R.md",
                   in_tmp / "p.yml", in_tmp / "none.docx")
    [r] = runs(in_tmp)
    assert {"setup", "fetch", "filter", "score", "cards", "cards/materials", "save"} <= set(r["stages"])
    assert r["counts"]["new_matches"] == 2 and "ashby:missing" in r["companies"]
    assert r["companies"]["ashby:missing"]["error"] == "not found"
    assert any("missing" in w["message"] for w in r["warnings"])


def test_repeating_warning_is_flagged_once_then_weekly():
    def run(n):
        return {"workflow": "scan", "warnings": [{"fingerprint": "abc", "message": "board gone"}], "started": str(n)}
    assert health.repeating_warnings([run(1), run(2)], "scan") == []
    assert health.repeating_warnings([run(1), run(2), run(3)], "scan")[0]["times"] == 3
    assert health.repeating_warnings([run(i) for i in range(4)], "scan") == []
    assert health.repeating_warnings([run(i) for i in range(10)], "scan")[0]["times"] == 10


def test_anomalies_catch_silent_breakage():
    past = [{"workflow": "scan", "outcome": "ok", "seconds": 40, "ai_cost_usd": 0.01,
             "counts": {"fetched": 900}, "companies": {"greenhouse:lumen-data": {"jobs": 30}}} for _ in range(5)]
    run = telemetry.Run("scan")
    run.counts = {"fetched": 0}
    run.companies = {"greenhouse:lumen-data": {"jobs": 0, "error": None}}
    run.stages = {"fetch": {"seconds": 200}}
    run.http = {"api.anthropic.com": {"calls": 4, "errors": 4}}
    notes = " ".join(health.anomalies(run, past))
    assert "no jobs were fetched" in notes and "lumen-data" in notes and "every Claude API call failed" in notes
    assert "usual" in notes


def test_bottleneck_hints():
    scan_run = {"workflow": "scan", "outcome": "ok", "started": "2026-11-17T13:00:00+00:00", "seconds": 300,
                "counts": {"waiting": 12, "scored": 10},
                "stages": {"cards": {"seconds": 200, "count": 1, "max": 200},
                           "cards/tailored resume/libreoffice install": {"seconds": 60, "count": 1, "max": 60}},
                "ai_by_stage": {"score": {"calls": 10, "seconds": 120, "cost_usd": 0.01}},
                "ai": {}, "http": {}, "companies": {"greenhouse:slow": {"seconds": 9, "jobs": 10}}}
    hints = " ".join(health.bottleneck_hints([scan_run]))
    assert "**cards**" in hints and "LibreOffice" in hints and "piling up" in hints
    assert "12.0s per job" in hints and "greenhouse:slow" in hints
    assert "Bottleneck hints" in health.build_report([scan_run])


def test_error_issue_is_reused(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/job-radar")
    posted = []
    monkeypatch.setattr(github_api.requests, "get", lambda *a, **k: FakeResp(200, [
        {"number": 7, "title": "⚠️ scan crashed [abc12345]", "html_url": "u7"}]))
    monkeypatch.setattr(github_api.requests, "post",
                        lambda url, **k: posted.append(url) or FakeResp(201, {"html_url": "new"}))
    assert github_api.report_problem("scan crashed", "body", "abc12345") == "u7"
    assert posted[-1].endswith("/issues/7/comments")
    assert github_api.report_problem("other", "body", "ffff0000") == "new"
    assert posted[-1].endswith("/issues")


def test_failsafe_stays_quiet_when_already_alerted(tmp_path, monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("failsafe", Path(__file__).parent.parent / "jobradar/failsafe.py")
    fs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fs)
    (tmp_path / "jobradar-alerted").write_text("abc")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(fs, "_call", lambda *a, **k: pytest.fail("should not alert twice"))
    fs.main()
    assert "already sent" in capsys.readouterr().out
