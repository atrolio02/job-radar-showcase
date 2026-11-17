"""Incoming webhook handler. Run with:  python -m jobradar.status

When you label, comment on, close or reopen a tracker card, GitHub fires a
webhook event that starts this workflow. It reads the event, updates the
job's status, refreshes the dashboard and posts the change to Discord.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import dashboard, discord, github_api, store, telemetry

COMMAND = re.compile(r"^\s*/(applied|interviewing|offer|rejected|skip)\b", re.I)


def new_status_from_event(event_name: str, event: dict, current: str) -> str | None:
    issue = event.get("issue") or {}
    if event_name == "issue_comment":
        m = COMMAND.match((event.get("comment") or {}).get("body", ""))
        return m.group(1).lower() if m else None

    labels = {l["name"].lower() for l in issue.get("labels", [])}
    for status in store.LABEL_PRIORITY:
        if status in labels:
            return status
    action = event.get("action")
    if action == "closed":
        return "closed"
    if action in ("reopened", "unlabeled") or current == "closed" and issue.get("state") == "open":
        return "new"
    return None


def _add_label(number: int, label: str) -> None:
    """Keep the card's labels in step with a /command comment."""
    if not github_api.enabled():
        return
    requests.post(f"{github_api.API}/repos/{os.environ['GITHUB_REPOSITORY']}/issues/{number}/labels",
                  headers=github_api._headers(), json={"labels": [label]}, timeout=30)


def handle(event_name: str, event: dict, data_path: Path = store.DEFAULT_PATH,
           readme_path: Path = Path("README.md")) -> str | None:
    number = (event.get("issue") or {}).get("number")
    data = store.load(data_path)
    job = store.find_by_issue(data, number) if number else None
    if not job:
        print(f"Issue #{number} isn't a Job Radar card; nothing to do.")
        return None

    old = job.get("status", "new")
    new = new_status_from_event(event_name, event, old)
    if not new or new == old:
        print(f"No status change for #{number} ({old}).")
        return None

    job["status"] = new
    job.setdefault("history", []).append(
        {"status": new, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    store.save(data, data_path)
    dashboard.write(data, readme_path)
    if event_name == "issue_comment":
        _add_label(number, new)
    discord.send(discord.status_update(job, old, new, job.get("issue_url")))
    print(f"#{number}: {old} -> {new}")
    return new


if __name__ == "__main__":
    telemetry.main("status", lambda: handle(os.environ["GITHUB_EVENT_NAME"],
                                            json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())))
