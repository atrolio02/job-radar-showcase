"""Health report: reads the run logs in data/health/runs/ and writes data/health/REPORT.md.

Run it any time:  python -m jobradar.health          (prints the report)
                  python -m jobradar.health --write  (also saves REPORT.md)

The report answers: Is anything broken? Where does the time go? What does
each run and each strong match cost? Which companies or calls are slow?
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HEALTH_DIR = Path("data/health")


def load_runs(health_dir: Path = HEALTH_DIR, days: int = 30) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    runs = []
    for f in sorted((health_dir / "runs").glob("*.json")):
        try:
            r = json.loads(f.read_text())
            if datetime.fromisoformat(r["started"]) >= cutoff:
                runs.append(r)
        except (ValueError, KeyError, OSError):
            continue
    return sorted(runs, key=lambda r: r["started"])


def _avg(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def _secs(s: float) -> str:
    return f"{s:.1f}s" if s < 90 else f"{s / 60:.1f} min"


def _usd(x: float) -> str:
    return f"${x:.4f}" if x < 0.1 else f"${x:.2f}"


# ── Checks used while a run finishes ────────────────────────────────────────
def anomalies(run, history: list[dict]) -> list[str]:
    """Things that didn't raise an error but look wrong compared with recent runs."""
    notes = []
    past = [r for r in history if r["workflow"] == run.workflow and r["outcome"] != "failed"][-10:]
    seconds = sum(s["seconds"] for p, s in run.stages.items() if "/" not in p)
    cost = sum(m["cost_usd"] for m in run.ai.values())
    if run.workflow == "scan" and "fetched" in run.counts:
        before = [r["counts"].get("fetched", 0) for r in past if r["counts"].get("fetched")]
        if run.counts["fetched"] == 0 and before:
            notes.append("Unusual: no jobs were fetched at all (usually "
                         f"{int(statistics.median(before))}). The job boards or the network may be down.")
        for name, c in run.companies.items():
            had = [r["companies"].get(name, {}).get("jobs", 0) for r in past[-3:]]
            if len(had) == 3 and min(had) >= 5 and c["jobs"] == 0 and not c.get("error"):
                notes.append(f"Unusual: {name} returned 0 jobs after {min(had)}+ on each of the last 3 runs. "
                             "Its job board may have moved.")
    if len(past) >= 3:
        usual = statistics.median(r["seconds"] for r in past)
        if seconds > 60 and seconds > 2.5 * usual:
            notes.append(f"Unusual: this run's steps took {_secs(seconds)}, about {seconds / usual:.1f}x the usual {_secs(usual)}.")
        usual_cost = statistics.median(r.get("ai_cost_usd", 0) for r in past)
        if cost > 0.05 and cost > 3 * max(usual_cost, 0.01):
            notes.append(f"Unusual: Claude API cost {_usd(cost)} this run, about {cost / max(usual_cost, 0.01):.0f}x the usual.")
    ai_http = run.http.get("api.anthropic.com", {})
    if ai_http.get("calls", 0) >= 3 and ai_http.get("errors", 0) == ai_http["calls"]:
        notes.append("Unusual: every Claude API call failed. Check the API key and your credit balance.")
    return notes


def repeating_warnings(runs: list[dict], workflow: str, streak: int = 3) -> list[dict]:
    """Warnings seen in the last `streak` runs of a workflow in a row. Reported once when the
    streak first reaches 3, then once a week while it continues."""
    mine = [r for r in runs if r["workflow"] == workflow]
    if len(mine) < streak:
        return []
    latest = {w["fingerprint"]: w for w in mine[-1].get("warnings", [])}
    out = []
    for fp, w in latest.items():
        n = 0
        for r in reversed(mine):
            if fp not in {x["fingerprint"] for x in r.get("warnings", [])}:
                break
            n += 1
        if n == streak or (n > streak and (n - streak) % 7 == 0):
            out.append({**w, "times": n})
    return out


# ── The report ──────────────────────────────────────────────────────────────
def build_report(runs: list[dict], days: int = 30) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = ["# Job Radar health", "",
           f"Updated {now}. Covers the last {days} days: **{len(runs)} run{'s' if len(runs) != 1 else ''}**. "
           "Built from the run logs in `data/health/runs/`.", ""]
    if not runs:
        return "\n".join(out + ["No runs logged yet."]) + "\n"

    # At a glance
    out += ["## At a glance", "", "| Workflow | Runs | Failed | With warnings | Avg time | Longest | Last run |",
            "|---|---|---|---|---|---|---|"]
    by_wf = defaultdict(list)
    for r in runs:
        by_wf[r["workflow"]].append(r)
    for wf, rs in sorted(by_wf.items()):
        o = Counter(r["outcome"] for r in rs)
        last = rs[-1]
        out.append(f"| {wf} | {len(rs)} | {o['failed']} | {o['warnings']} | {_secs(_avg(r['seconds'] for r in rs))} | "
                   f"{_secs(max(r['seconds'] for r in rs))} | {last['started'][:16].replace('T', ' ')} ({last['outcome']}) |")

    scans = by_wf.get("scan", [])
    out += _time_section(scans or runs)
    out += _cost_section(runs, scans)
    out += _company_section(scans)
    out += _http_section(runs)
    out += _problem_section(runs)
    hints = bottleneck_hints(runs)
    out += ["", "## Bottleneck hints", ""] + ([f"- {h}" for h in hints] or ["- Nothing stands out yet."])
    return "\n".join(out) + "\n"


def _time_section(runs: list[dict]) -> list[str]:
    totals, maxes, counts = defaultdict(float), defaultdict(float), defaultdict(int)
    for r in runs:
        for name, s in r["stages"].items():
            totals[name] += s["seconds"]
            maxes[name] = max(maxes[name], s["max"])
            counts[name] += s["count"]
    run_total = sum(r["seconds"] for r in runs) or 1
    wf = runs[0]["workflow"] if len({r["workflow"] for r in runs}) == 1 else "all workflows"
    out = ["", f"## Where the time goes ({wf})", "",
           "Indented steps are part of the step above them.", "",
           "| Step | Avg per run | Share of run time | Times done | Slowest single time |", "|---|---|---|---|---|"]
    tops = sorted((n for n in totals if "/" not in n), key=lambda n: -totals[n])
    for top in tops:
        for name in [top] + sorted((n for n in totals if n.startswith(top + "/")), key=lambda n: -totals[n]):
            label = name if name == top else "&nbsp;&nbsp;↳ " + name.split("/", 1)[1]
            out.append(f"| {label} | {_secs(totals[name] / len(runs))} | {100 * totals[name] / run_total:.0f}% | "
                       f"{counts[name]} | {_secs(maxes[name])} |")
    return out


def _cost_section(runs: list[dict], scans: list[dict]) -> list[str]:
    models = defaultdict(lambda: defaultdict(float))
    for r in runs:
        for m, a in r.get("ai", {}).items():
            for k, v in a.items():
                models[m][k] += v
    if not models:
        return ["", "## Claude API", "", "No Claude API calls logged yet."]
    out = ["", "## Claude API", "",
           "| Model | Calls | Avg per call | Tokens in / out | Cache reads | Web searches | Cost |",
           "|---|---|---|---|---|---|---|"]
    for m, a in sorted(models.items()):
        fresh = a["input"] + a["cache_write"]
        hit = 100 * a["cache_read"] / (a["cache_read"] + fresh) if a["cache_read"] + fresh else 0
        out.append(f"| {m} | {int(a['calls'])} | {_secs(a['seconds'] / max(a['calls'], 1))} | "
                   f"{int(a['input'] + a['cache_read'] + a['cache_write']):,} / {int(a['output']):,} | "
                   f"{hit:.0f}% of input | {int(a['web_searches'])} | {_usd(a['cost_usd'])} |")
    total = sum(r.get("ai_cost_usd", 0) for r in runs)
    days = max(1, (datetime.fromisoformat(runs[-1]["started"]) - datetime.fromisoformat(runs[0]["started"])).days + 1)
    out += ["", f"- Total: **{_usd(total)}** over {days} day(s), about **{_usd(total / days * 30)} a month** at this pace."]
    if scans:
        out.append(f"- Average daily scan: {_usd(_avg(r.get('ai_cost_usd', 0) for r in scans))}.")
    matches = sum(r["counts"].get("new_matches", 0) for r in runs)
    card_cost = sum(s["cost_usd"] for r in runs for n, s in r.get("ai_by_stage", {}).items() if n.startswith("cards"))
    if matches:
        out.append(f"- Each strong match's workup (resume, kit, research) costs about {_usd(card_cost / matches)}.")
    scored = sum(r["counts"].get("scored", 0) for r in runs)
    score_cost = sum(s["cost_usd"] for r in runs for n, s in r.get("ai_by_stage", {}).items() if n == "score")
    if scored:
        out.append(f"- Scoring one job costs about {_usd(score_cost / scored)}.")
    return out


def _company_section(scans: list[dict]) -> list[str]:
    if not scans:
        return []
    secs, jobs, errs = defaultdict(list), defaultdict(list), Counter()
    for r in scans:
        for name, c in r.get("companies", {}).items():
            secs[name].append(c["seconds"])
            jobs[name].append(c["jobs"])
            if c.get("error"):
                errs[name] += 1
    if not secs:
        return []
    out = ["", "## Job boards", "", "Slowest to fetch:", "", "| Board | Avg fetch | Avg jobs listed | Failed fetches |",
           "|---|---|---|---|"]
    for name in sorted(secs, key=lambda n: -_avg(secs[n]))[:8]:
        out.append(f"| {name} | {_secs(_avg(secs[name]))} | {_avg(jobs[name]):.0f} | {errs[name]} |")
    broken = [n for n in errs if errs[n] == len(secs[n])]
    if broken:
        out += ["", "Failed on every scan (check the name in config.yml): " + ", ".join(sorted(broken))]
    return out


def _http_section(runs: list[dict]) -> list[str]:
    hosts = defaultdict(lambda: {"calls": 0, "seconds": 0.0, "errors": 0, "statuses": Counter()})
    for r in runs:
        for host, h in r.get("http", {}).items():
            agg = hosts[host]
            agg["calls"] += h["calls"]
            agg["seconds"] += h["seconds"]
            agg["errors"] += h["errors"]
            agg["statuses"].update(h["statuses"])
    if not hosts:
        return []
    out = ["", "## Web requests", "", "| Service | Calls | Total time | Avg per call | Errors | Answers |",
           "|---|---|---|---|---|---|"]
    for host, h in sorted(hosts.items(), key=lambda kv: -kv[1]["seconds"]):
        codes = ", ".join(f"{k}×{v}" for k, v in h["statuses"].most_common(4))
        out.append(f"| {host} | {h['calls']} | {_secs(h['seconds'])} | {_secs(h['seconds'] / max(h['calls'], 1))} | "
                   f"{h['errors']} | {codes} |")
    slow = sorted((c for r in runs for c in r.get("slowest_calls", [])), key=lambda c: -c["seconds"])[:5]
    if slow:
        out += ["", "Slowest single requests:", ""] + [
            f"- {_secs(c['seconds'])}: {c['method']} {c['host']} during *{c['stage']}* (answer: {c['status']})" for c in slow]
    return out


def _problem_section(runs: list[dict]) -> list[str]:
    errors, warns = {}, {}
    for r in runs:
        for e in r.get("errors", []):
            g = errors.setdefault(e["fingerprint"], {**e, "times": 0, "last": r["started"], "workflow": r["workflow"]})
            g["times"] += 1
            g["last"] = r["started"]
        for w in r.get("warnings", []):
            g = warns.setdefault(w["fingerprint"], {**w, "times": 0, "last": r["started"], "workflow": r["workflow"]})
            g["times"] += 1
            g["last"] = r["started"]
    out = ["", "## Errors", ""]
    if errors:
        out += ["| Id | Error | Workflow | Times | Last seen |", "|---|---|---|---|---|"]
        for e in sorted(errors.values(), key=lambda e: e["last"], reverse=True):
            msg = f"{e['type']}: {e['message'][:120]}".replace("|", "/").replace("\n", " ")
            out.append(f"| `{e['fingerprint']}` | {'**crash** ' if e['fatal'] else ''}{msg} | {e['workflow']} | "
                       f"{e['times']} | {e['last'][:10]} |")
    else:
        out.append("None. 🎉")
    out += ["", "## Warnings", ""]
    if warns:
        out += ["| Warning | Workflow | Times | Last seen |", "|---|---|---|---|"]
        for w in sorted(warns.values(), key=lambda w: (-w["times"], w["last"]))[:15]:
            out.append(f"| {w['message'][:160].replace('|', '/')} | {w['workflow']} | {w['times']} | {w['last'][:10]} |")
    else:
        out.append("None.")
    return out


def bottleneck_hints(runs: list[dict]) -> list[str]:
    """Plain-language pointers at the biggest, most fixable costs."""
    hints = []
    scans = [r for r in runs if r["workflow"] == "scan"]
    if not scans:
        return hints
    stage_avg = defaultdict(float)
    for r in scans:
        for n, s in r["stages"].items():
            stage_avg[n] += s["seconds"] / len(scans)
    run_avg = _avg(r["seconds"] for r in scans) or 1
    tops = {n: v for n, v in stage_avg.items() if "/" not in n}
    if tops and run_avg >= 20:
        worst = max(tops, key=tops.get)
        hints.append(f"The biggest step is **{worst}**: {_secs(tops[worst])} of an average {_secs(run_avg)} scan "
                     f"({100 * tops[worst] / run_avg:.0f}%).")
    lo = [s for r in scans for n, s in r["stages"].items() if n.endswith("libreoffice install")]
    if lo and _avg(s["seconds"] for s in lo) > 20:
        hints.append(f"Installing LibreOffice for resume PDFs takes about {_secs(_avg(s['seconds'] for s in lo))} each "
                     "time a scan has a strong match. Caching it in the workflow would save most of that.")
    waiting = [r["counts"].get("waiting", 0) for r in scans[-7:]]
    if waiting and min(waiting) > 0:
        hints.append(f"Jobs are piling up for scoring (about {int(_avg(waiting))} waiting after each recent scan). "
                     "Raise ai.max_jobs_scored_per_run in config.yml, or narrow the companies list.")
    score = [s for r in scans for n, s in r.get("ai_by_stage", {}).items() if n == "score"]
    calls = sum(s["calls"] for s in score)
    if calls >= 5:
        per = sum(s["seconds"] for s in score) / calls
        if per > 8:
            hints.append(f"Scoring takes {per:.1f}s per job. Scoring several jobs at once (in parallel) would cut that step a lot.")
    retries = sum(int(v) for r in scans for v in [r.get("http", {}).get("api.anthropic.com", {}).get("statuses", {}).get(c, 0)
                                                 for c in ("429", "529")])
    if retries:
        hints.append(f"Claude answered 'busy' {retries} times; each one costs a wait of several seconds.")
    models = defaultdict(lambda: [0, 0])
    for r in scans:
        for m, a in r.get("ai", {}).items():
            models[m][0] += a["cache_read"]
            models[m][1] += a["input"] + a["cache_write"]
    for m, (read, fresh) in models.items():
        if fresh > 50_000 and read < fresh * 0.3:
            hints.append(f"{m} is reading little from the prompt cache ({100 * read / (read + fresh):.0f}%). "
                         "The resume is probably being re-sent at full price; worth a look.")
    slow_boards = [(n, _avg(c["seconds"] for r in scans if (c := r.get("companies", {}).get(n))))
                   for n in {n for r in scans for n in r.get("companies", {})}]
    slow_boards = [b for b in slow_boards if b[1] > 5]
    if slow_boards:
        n, s = max(slow_boards, key=lambda b: b[1])
        hints.append(f"{n} is the slowest job board ({_secs(s)} per fetch). Fetching boards in parallel would hide this.")
    return hints


def write_report(health_dir: Path = HEALTH_DIR, days: int = 30) -> Path:
    path = health_dir / "REPORT.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_report(load_runs(health_dir, days), days))
    return path


def write_step_summary(record: dict) -> None:
    """Show a short health note on the GitHub Actions run page."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    tops = sorted(((n, s["seconds"]) for n, s in record["stages"].items() if "/" not in n), key=lambda x: -x[1])[:3]
    lines = ["", "### Run health", f"- Outcome: **{record['outcome']}** in {_secs(record['seconds'])}",
             f"- Claude API cost: {_usd(record.get('ai_cost_usd', 0))}"]
    if tops:
        lines.append("- Slowest steps: " + ", ".join(f"{n} ({_secs(s)})" for n, s in tops))
    lines += [f"- ⚠️ {w['message']}" for w in record["warnings"][:10]]
    lines += [f"- ❌ {e['type']}: {e['message'][:200]}" for e in record["errors"][:5]]
    with open(path, "a") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    report = build_report(load_runs())
    print(report)
    if "--write" in sys.argv:
        print(f"Saved {write_report()}")
