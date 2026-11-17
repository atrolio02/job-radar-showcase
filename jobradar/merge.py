"""Combine two versions of data/jobs.json when two updates happen at once.

Example: you mark a card "applied" while the daily scan is running. Both want
to save. The second to finish merges instead of overwriting, so nothing is
lost and nothing is re-alerted.

Usage:  python -m jobradar.merge <path to our version>
        (the copy currently in data/jobs.json is the newer saved one)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import dashboard, store


def merge(ours: dict, theirs: dict) -> dict:
    merged = {"jobs": {}}
    for key in set(ours["jobs"]) | set(theirs["jobs"]):
        a, b = ours["jobs"].get(key), theirs["jobs"].get(key)
        if a is None or b is None:
            merged["jobs"][key] = a or b
            continue
        rec = {**b, **a}  # every field from both; ours wins on overlap...
        if len(b.get("history", [])) > len(a.get("history", [])):
            # ...except status, where the side with more recorded changes is newer.
            rec["status"], rec["history"] = b.get("status"), b["history"]
        merged["jobs"][key] = rec
    # Company snapshots: keep both sides, newest wins.
    companies = {**theirs.get("companies", {})}
    for name, snap in ours.get("companies", {}).items():
        if snap.get("fetched_at", "") >= companies.get(name, {}).get("fetched_at", ""):
            companies[name] = snap
    if companies:
        merged["companies"] = companies
    # Resume library: keep every version; combine the lists of jobs using each one.
    variants = {k: dict(v) for k, v in theirs.get("resume_variants", {}).items()}
    for vid, v in ours.get("resume_variants", {}).items():
        both = list(dict.fromkeys(variants.get(vid, {}).get("used_by", []) + v.get("used_by", [])))
        variants[vid] = {**variants.get(vid, {}), **v, "used_by": both}
    if variants:
        merged["resume_variants"] = variants
    return merged


if __name__ == "__main__":
    ours = json.loads(Path(sys.argv[1]).read_text())
    result = merge(ours, store.load())
    store.save(result)
    dashboard.write(result)
    dashboard.write_resume_library(result)
    print(f"Merged {len(result['jobs'])} jobs.")
