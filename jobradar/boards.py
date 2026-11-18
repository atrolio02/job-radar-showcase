"""Find job boards to watch.

Checks a list of company names on every hiring system Job Radar can read and reports how many open
jobs each one has, so new companies can be added to config.yml in bulk:

    python -m jobradar.boards tools/board_candidates.txt --out board-check.json

Each line of the list is a company's board name (the part after jobs.ashbyhq.com/,
boards.greenhouse.io/, jobs.lever.co/ or jobs.smartrecruiters.com/). Lines starting with # are notes.
A name found on more than one system is reported on the one with the most jobs.
Board names already in config.yml are skipped.
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml

from .sources import HEADERS

TIMEOUT = 20


def _count(system: str, name: str) -> int:
    """Open jobs on one board, or 0 if the board doesn't exist there."""
    try:
        if system == "greenhouse":
            r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{name}/jobs", headers=HEADERS,
                             timeout=TIMEOUT)
            return len(r.json().get("jobs", [])) if r.ok else 0
        if system == "ashby":
            r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{name}", headers=HEADERS,
                             timeout=TIMEOUT)
            return sum(1 for j in r.json().get("jobs", []) if j.get("isListed") is not False) if r.ok else 0
        if system == "lever":
            r = requests.get(f"https://api.lever.co/v0/postings/{name}", params={"mode": "json"},
                             headers=HEADERS, timeout=TIMEOUT)
            data = r.json() if r.ok else {}
            return len(data) if isinstance(data, list) else 0
        if system == "smartrecruiters":
            r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{name}/postings",
                             params={"limit": 1}, headers=HEADERS, timeout=TIMEOUT)
            return int(r.json().get("totalFound", 0)) if r.ok else 0
    except (requests.RequestException, ValueError, AttributeError):
        return 0
    return 0


SYSTEMS = ("greenhouse", "ashby", "lever", "smartrecruiters")


def probe(name: str) -> dict:
    if "/" in name:  # Oracle HCM site: "Company=host/SITE"
        from .sources import fetch_oracle
        try:
            n = len(fetch_oracle(name))
        except Exception:  # noqa: BLE001 - a wrong site just isn't found
            n = 0
        return {"name": name, "system": "oracle" if n else None, "jobs": n, "all": {"oracle": n}}
    counts = {s: _count(s, name) for s in SYSTEMS}
    best = max(counts, key=counts.get)
    return {"name": name, "system": best if counts[best] else None, "jobs": counts[best], "all": counts}


def watched(config_path: str = "config.yml") -> set[str]:
    try:
        cfg = yaml.safe_load(Path(config_path).read_text()) or {}
    except FileNotFoundError:
        return set()
    return {str(b).lower() for boards in (cfg.get("companies") or {}).values() for b in boards or []}


def read_names(path: str) -> list[str]:
    names, seen = [], set()
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        for name in ([line] if "/" in line else line.split()):
            if name and name.lower() not in seen:
                seen.add(name.lower())
                names.append(name)
    return names


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("candidates")
    ap.add_argument("--out", default="board-check.json")
    ap.add_argument("--config", default="config.yml")
    ap.add_argument("--workers", type=int, default=24)
    args = ap.parse_args(argv)
    skip = watched(args.config)
    names = [n for n in read_names(args.candidates) if n.lower() not in skip]
    print(f"Checking {len(names)} names on {len(SYSTEMS)} hiring systems...")
    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(probe, names))
    found = sorted((r for r in results if r["system"]), key=lambda r: -r["jobs"])
    Path(args.out).write_text(json.dumps({"found": found, "missing": [r["name"] for r in results
                                                                       if not r["system"]]}, indent=1))
    total = sum(r["jobs"] for r in found)
    print(f"Found {len(found)} of {len(names)} boards with {total:,} open jobs.")
    for r in found:
        print(f"FOUND|{r['system']}|{r['name']}|{r['jobs']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
