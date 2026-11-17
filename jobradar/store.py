"""The tracker's memory: data/jobs.json, saved back into the repo after each run.

It remembers every job already seen so nothing is scored or alerted twice.
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_PATH = Path("data/jobs.json")

# Status flow. "new" = strong match you haven't acted on yet.
STATUSES = ["new", "applied", "interviewing", "offer", "rejected", "skip", "closed"]
# When several status labels are on a card, the furthest-along one wins.
LABEL_PRIORITY = ["offer", "interviewing", "applied", "rejected", "skip"]


def load(path: Path = DEFAULT_PATH) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"jobs": {}}


def save(data: dict, path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def find_by_issue(data: dict, number: int) -> dict | None:
    for job in data["jobs"].values():
        if job.get("issue_number") == number:
            return job
    return None
