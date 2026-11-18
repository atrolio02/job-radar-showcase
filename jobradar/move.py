"""The dashboard's buttons. Run with:  python -m jobradar.move

Each button on the dashboard opens a short "Move a job" form whose title is already filled in, like
"Move #8 to applied". Submitting it starts this script, which moves the job (status, the card's
labels, open/closed), refreshes the dashboard, posts to Discord and closes the form.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import dashboard, discord, github_api, store, telemetry

TITLE = re.compile(r"move\s+#?(\d+)\s+to\s+(new|active|applied|interviewing|offer|rejected|skip)", re.I)
STATUS_LABELS = set(store.LABEL_PRIORITY)


def parse(title: str) -> tuple[int, str] | None:
    m = TITLE.search(title or "")
    if not m:
        return None
    status = m.group(2).lower()
    return int(m.group(1)), "new" if status == "active" else status


def move(data: dict, number: int, new: str) -> tuple[dict | None, str | None]:
    """Change the saved status. Returns (job, old status), or (None, None) if the card isn't a job."""
    job = store.find_by_issue(data, number)
    if not job:
        return None, None
    old = job.get("status", "new")
    if old != new:
        job["status"] = new
        job.setdefault("history", []).append(
            {"status": new, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return job, old


def _sync_card(number: int, new: str) -> None:
    """Make the card match: one status label, and open unless it's closed out."""
    if not github_api.enabled():
        return
    base = f"{github_api.API}/repos/{os.environ['GITHUB_REPOSITORY']}/issues/{number}"
    h = github_api._headers()
    issue = requests.get(base, headers=h, timeout=30).json()
    for label in {l["name"] for l in issue.get("labels", [])} & STATUS_LABELS - {new}:
        requests.delete(f"{base}/labels/{label}", headers=h, timeout=30)
    if new in STATUS_LABELS:
        requests.post(f"{base}/labels", headers=h, json={"labels": [new]}, timeout=30)
    state = "closed" if new in ("rejected", "skip", "closed") else "open"
    if issue.get("state") != state:
        requests.patch(base, headers=h, json={"state": state}, timeout=30)


def _close_form(number: int, text: str) -> None:
    if not github_api.enabled():
        return
    base = f"{github_api.API}/repos/{os.environ['GITHUB_REPOSITORY']}/issues/{number}"
    h = github_api._headers()
    requests.post(f"{base}/comments", headers=h, json={"body": text}, timeout=30)
    requests.patch(base, headers=h, json={"state": "closed", "state_reason": "completed"}, timeout=30)


def handle(event: dict, data_path: Path = store.DEFAULT_PATH, readme_path: Path = Path("README.md")) -> str | None:
    form = event.get("issue") or {}
    wanted = parse(form.get("title", ""))
    if not wanted:
        _close_form(form.get("number"), "Couldn't read this. The title should look like `Move #8 to applied`.")
        return None
    number, new = wanted
    data = store.load(data_path)
    job, old = move(data, number, new)
    if not job:
        _close_form(form["number"], f"#{number} isn't a Job Radar card, so nothing moved.")
        return None
    store.save(data, data_path)
    dashboard.write(data, readme_path)
    _sync_card(number, new)
    if old != new:
        discord.send(discord.status_update(job, old, new, job.get("issue_url")))
    where = "Active" if new == "new" else "Applied" if new in ("applied", "interviewing", "offer") else "Closed out"
    _close_form(form["number"], f"Moved {job['title']} at {job['company']} (#{number}) to **{new}**. "
                                f"It's under {where} on the dashboard.")
    print(f"#{number}: {old} -> {new}")
    return new


if __name__ == "__main__":
    telemetry.main("move", lambda: handle(json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())))
