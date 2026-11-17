"""Health tracking: an error tracker and an efficiency log for every run.

Every workflow run (daily scan, status change, added job, quiz) is wrapped in
`telemetry.main(...)`. While it runs, this module:

- times each step ("fetch", "score", "cards/tailor", ...) with `stage(...)`
- times every web request (job boards, Claude API, GitHub, Discord) by hooking
  into the `requests` library once, so no other code has to remember to log
- reads Claude's token counts from each reply and turns them into a dollar cost
- records warnings (things that went wrong but didn't stop the run) and errors

When the run ends it saves one JSON file in data/health/runs/ and rebuilds
data/health/REPORT.md (see health.py). If the run crashed, it sends a Discord
alert and opens (or comments on) a GitHub Issue labeled "radar-error".

Fail-safe rule: nothing in here is allowed to break a run. Every bookkeeping
step is wrapped so a logging problem is printed and ignored.
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

HEALTH_DIR = Path("data/health")
KEEP_DAYS = 90  # run files older than this are deleted

# USD per million tokens (Claude API price list, Nov 2026). Override in config.yml under health: prices:
PRICES = {
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.10, "cache_write": 2.50},
}
WEB_SEARCH_PRICE = 0.01  # USD per web search Claude runs

_run: "Run | None" = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe(fn):
    """Bookkeeping must never break a run: print the problem and carry on."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001
            print(f"[health] {fn.__name__} skipped: {e.__class__.__name__}: {e}")
            return None
    return wrapper


def fingerprint(*parts: str) -> str:
    """Same kind of problem -> same short id, so repeats are grouped. Numbers are ignored."""
    text = "|".join(re.sub(r"\d+", "#", str(p)) for p in parts)
    return hashlib.sha1(text.encode()).hexdigest()[:8]


class Run:
    def __init__(self, workflow: str, prices: dict | None = None):
        self.workflow = workflow
        self.started_at = _now()
        self.t0 = time.perf_counter()
        self.stack: list[str] = []
        self.stages: dict[str, dict] = {}
        self.http: dict[str, dict] = {}
        self.ai: dict[str, dict] = {}
        self.ai_by_stage: dict[str, dict] = {}
        self.companies: dict[str, dict] = {}
        self.slowest: list[dict] = []
        self.warnings: list[dict] = []
        self.errors: list[dict] = []
        self.counts: dict = {}
        self.prices = {**PRICES, **(prices or {})}
        self.failed_in: dict[int, str] = {}  # error id -> innermost step it passed through

    @property
    def current(self) -> str:
        return "/".join(self.stack) or "other"

    def cost(self, model: str, u: dict) -> float:
        p = self.prices.get(model) or next((v for k, v in self.prices.items() if model.startswith(k)), None)
        if not p:
            return 0.0
        dollars = (u.get("input_tokens", 0) * p["input"] + u.get("output_tokens", 0) * p["output"]
                   + u.get("cache_read_input_tokens", 0) * p["cache_read"]
                   + u.get("cache_creation_input_tokens", 0) * p["cache_write"]) / 1_000_000
        searches = (u.get("server_tool_use") or {}).get("web_search_requests", 0)
        return dollars + searches * WEB_SEARCH_PRICE


# ── Recording ───────────────────────────────────────────────────────────────
@contextlib.contextmanager
def stage(name: str):
    """Time a step. Steps nest: stage('cards') then stage('tailor') logs 'cards/tailor'."""
    run = _run
    if run is None:
        yield
        return
    run.stack.append(name)
    path, t = run.current, time.perf_counter()
    try:
        yield
    except BaseException as e:
        run.failed_in.setdefault(id(e), path)
        raise
    finally:
        _add_stage(run, path, time.perf_counter() - t)
        if run.stack and run.stack[-1] == name:
            run.stack.pop()


@_safe
def _add_stage(run: Run, path: str, seconds: float) -> None:
    s = run.stages.setdefault(path, {"seconds": 0.0, "count": 0, "max": 0.0})
    s["seconds"] += seconds
    s["count"] += 1
    s["max"] = max(s["max"], seconds)


@_safe
def record_http(method: str, url: str, seconds: float, status, response=None) -> None:
    run = _run
    if run is None:
        return
    host = urlparse(url).netloc or "unknown"
    h = run.http.setdefault(host, {"calls": 0, "seconds": 0.0, "errors": 0, "statuses": {}})
    h["calls"] += 1
    h["seconds"] += seconds
    h["statuses"][str(status)] = h["statuses"].get(str(status), 0) + 1
    if not isinstance(status, int) or status >= 400:
        h["errors"] += 1
    call = {"stage": run.current, "host": host, "method": method, "status": status, "seconds": round(seconds, 2)}
    run.slowest = sorted(run.slowest + [call], key=lambda c: -c["seconds"])[:10]
    if host == "api.anthropic.com" and status == 200 and response is not None:
        data = response.json()
        _record_ai(run, data.get("model", "unknown"), data.get("usage") or {}, seconds)


def _record_ai(run: Run, model: str, usage: dict, seconds: float) -> None:
    cost = run.cost(model, usage)
    m = run.ai.setdefault(model, {"calls": 0, "seconds": 0.0, "input": 0, "output": 0,
                                  "cache_read": 0, "cache_write": 0, "web_searches": 0, "cost_usd": 0.0})
    m["calls"] += 1
    m["seconds"] += seconds
    m["input"] += usage.get("input_tokens", 0)
    m["output"] += usage.get("output_tokens", 0)
    m["cache_read"] += usage.get("cache_read_input_tokens", 0)
    m["cache_write"] += usage.get("cache_creation_input_tokens", 0)
    m["web_searches"] += (usage.get("server_tool_use") or {}).get("web_search_requests", 0)
    m["cost_usd"] += cost
    s = run.ai_by_stage.setdefault(run.current, {"calls": 0, "seconds": 0.0, "cost_usd": 0.0})
    s["calls"] += 1
    s["seconds"] += seconds
    s["cost_usd"] += cost


@_safe
def company(name: str, seconds: float, jobs: int, error: str | None = None) -> None:
    if _run is not None:
        _run.companies[name] = {"seconds": round(seconds, 2), "jobs": jobs, "error": error}


@_safe
def warn(message: str) -> None:
    if _run is not None:
        _run.warnings.append({"message": str(message), "stage": _run.current,
                              "fingerprint": fingerprint(_run.workflow, message)})


@_safe
def error(where: str, exc: BaseException, fatal: bool = False) -> dict | None:
    if _run is None:
        return None
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]
    rec = {"where": where, "stage": _run.failed_in.get(id(exc), _run.current), "type": exc.__class__.__name__, "message": str(exc)[:500],
           "fatal": fatal, "fingerprint": fingerprint(_run.workflow, where, exc.__class__.__name__, str(exc)[:120]),
           "traceback": tb, "at": _now()}
    _run.errors.append(rec)
    return rec


@_safe
def count(**values) -> None:
    if _run is not None:
        _run.counts.update(values)


# ── Hooking every web request ───────────────────────────────────────────────
_original_request = requests.sessions.Session.request


def _timed_request(self, method, url, *args, **kwargs):
    t = time.perf_counter()
    try:
        resp = _original_request(self, method, url, *args, **kwargs)
    except Exception as e:
        record_http(method, url, time.perf_counter() - t, e.__class__.__name__)
        raise
    record_http(method, url, time.perf_counter() - t, resp.status_code, resp)
    return resp


def _install_hook() -> None:
    requests.sessions.Session.request = _timed_request


def _remove_hook() -> None:
    requests.sessions.Session.request = _original_request


# ── Start, finish, alerts ───────────────────────────────────────────────────
def start(workflow: str, prices: dict | None = None) -> Run:
    global _run
    _run = Run(workflow, prices)
    _install_hook()
    return _run


def _record(run: Run, outcome: str) -> dict:
    ai_total = sum(m["cost_usd"] for m in run.ai.values())
    return {
        "workflow": run.workflow, "outcome": outcome, "started": run.started_at, "finished": _now(),
        "seconds": round(time.perf_counter() - run.t0, 2),
        "github_run": os.environ.get("GITHUB_RUN_ID"),
        "run_url": _run_url(),
        "counts": run.counts,
        "stages": {k: {"seconds": round(v["seconds"], 2), "count": v["count"], "max": round(v["max"], 2)}
                   for k, v in run.stages.items()},
        "http": {k: {**v, "seconds": round(v["seconds"], 2)} for k, v in run.http.items()},
        "ai": {k: {**v, "seconds": round(v["seconds"], 2), "cost_usd": round(v["cost_usd"], 5)}
               for k, v in run.ai.items()},
        "ai_by_stage": {k: {**v, "seconds": round(v["seconds"], 2), "cost_usd": round(v["cost_usd"], 5)}
                        for k, v in run.ai_by_stage.items()},
        "ai_cost_usd": round(ai_total, 5),
        "companies": run.companies,
        "slowest_calls": run.slowest,
        "warnings": run.warnings,
        "errors": run.errors,
    }


def _run_url() -> str | None:
    repo, rid = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    return f"{server}/{repo}/actions/runs/{rid}" if repo and rid else None


@_safe
def save(record: dict, health_dir: Path = HEALTH_DIR) -> Path:
    runs = health_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = record["started"].replace(":", "-").replace("+00-00", "Z")
    path = runs / f"{stamp}_{record['workflow']}_{os.getpid()}.json"
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n")
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    for old in runs.glob("*.json"):
        try:
            if datetime.fromisoformat(json.loads(old.read_text())["started"]) < cutoff:
                old.unlink()
        except (ValueError, KeyError, OSError):
            continue
    return path


def finish(outcome: str | None = None, health_dir: Path = HEALTH_DIR) -> dict | None:
    """Save the run's record, rebuild the report, and add anomaly warnings. Returns the record."""
    global _run
    run = _run
    if run is None:
        return None
    _remove_hook()
    _run = None
    try:
        from . import health
        for note in health.anomalies(run, health.load_runs(health_dir, days=30)) or []:
            run.warnings.append({"message": note, "stage": "health check",
                                 "fingerprint": fingerprint(run.workflow, "anomaly", note[:40])})
    except Exception as e:  # noqa: BLE001
        print(f"[health] anomaly check skipped: {e}")
    if outcome is None:
        outcome = "failed" if any(e["fatal"] for e in run.errors) else ("warnings" if run.warnings or run.errors else "ok")
    record = _record(run, outcome)
    save(record, health_dir)
    _after_save(record, health_dir)
    return record


@_safe
def _after_save(record: dict, health_dir: Path) -> None:
    from . import health
    health.write_report(health_dir)
    health.write_step_summary(record)
    repeating = health.repeating_warnings(health.load_runs(health_dir, days=30), record["workflow"])
    for w in repeating:
        alert(f"Keeps happening: {w['message'][:120]}",
              f"This warning showed up in the last {w['times']} `{record['workflow']}` runs in a row:\n\n"
              f"> {w['message']}\n\nIt doesn't stop the run, but something needs fixing.",
              w["fingerprint"], record.get("run_url"))


def alert(title: str, body: str, fp: str, run_url: str | None = None) -> None:
    """Discord message + one GitHub Issue per problem (repeats add a comment instead)."""
    from . import discord, github_api
    link = f"\n\n[Open the run]({run_url})" if run_url else ""
    try:
        discord.send({"embeds": [{"title": f"🚨 Job Radar: {title}"[:250],
                                  "description": discord._clip(body + link, 3500), "color": 0xE74C3C}]})
    except Exception as e:  # noqa: BLE001
        print(f"[health] Discord alert failed: {e}")
    try:
        if github_api.enabled():
            github_api.report_problem(f"{title} [{fp}]", body + link, fp)
    except Exception as e:  # noqa: BLE001
        print(f"[health] GitHub error issue failed: {e}")
    marker = os.environ.get("RUNNER_TEMP")
    if marker:  # tells the workflow's last-resort step that an alert already went out
        with contextlib.suppress(OSError):
            Path(marker, "jobradar-alerted").write_text(fp)


def main(workflow: str, fn, *args, prices: dict | None = None, **kwargs):
    """Run a workflow with health tracking. A crash is logged and alerted, then re-raised
    so GitHub still marks the run as failed."""
    start(workflow, prices)
    try:
        result = fn(*args, **kwargs)
    except BaseException as e:
        if isinstance(e, SystemExit) and not e.code:
            finish()
            raise
        rec = error(workflow, e, fatal=True) or {"fingerprint": "unknown", "traceback": repr(e)}
        record = finish("failed") or {}
        alert(f"{workflow} crashed: {e.__class__.__name__}",
              f"**{e.__class__.__name__}: {str(e)[:300]}**\n\n```\n{rec['traceback'][-1500:]}\n```",
              rec["fingerprint"], record.get("run_url"))
        raise
    finish()
    return result
