"""Move a lower-scoring job (no tracker card yet) to Active. Run with:  python -m jobradar.promote <job id>

Cards by score (config.yml): alert_min_score (7) and up get a card with a tailored resume and Claude's full
kit. card_min_score (6) up to that get a card automatically too, but with your standard resume (no
tailoring, no waiting on Claude). Anything lower can be moved to Active by hand with the dashboard's
"➕ Move to active" link, and gets the standard resume the same way.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .dashboard import short_id
from . import dashboard, lastmile, profile, scan, store, telemetry


def find(data: dict, jid: str) -> dict | None:
    jid = jid.strip().upper().replace("J-", "")
    for key, rec in data["jobs"].items():
        if short_id(key)[2:].upper() == jid:
            return rec
    return None


def standard_card(rec: dict, data: dict, cfg: dict, prof: dict, problems: list) -> None:
    """Open a tracker card with the standard resume and the basic application kit (no Claude needed)."""
    ai_cfg = {**cfg.get("ai", {}), "tailored_resume": False}
    scan.open_card(lastmile.job_from_record(rec), rec, data, "", ai_cfg, prof, problems, use_ai=False)
    rec["standard_resume"] = True
    rec["promoted"] = scan._now()
    for k in ("kit_pending", "pending_brief", "force_kit", "queued_at", "questions"):
        rec.pop(k, None)


def auto_cards(data: dict, cfg: dict, prof: dict, problems: list, limit: int | None = None) -> int:
    """Every scored job from card_min_score up to (not including) alert_min_score gets a standard card."""
    low, high = int(cfg.get("card_min_score", 6)), int(cfg.get("alert_min_score", 7))
    limit = limit if limit is not None else int(cfg.get("ai", {}).get("max_cards_per_run", 15))
    picked = sorted((r for r in data["jobs"].values() if not r.get("status") and not r.get("duplicate_of")
                     and low <= (r.get("score") or 0) < high), key=lambda r: -(r.get("score") or 0))
    for rec in picked[:limit]:
        standard_card(rec, data, cfg, prof, problems)
    return min(len(picked), limit)


def promote(jid: str, data_path: Path = store.DEFAULT_PATH, readme_path: Path = Path("README.md")) -> dict | None:
    data = store.load(data_path)
    rec = find(data, jid)
    if not rec:
        print(f"No job {jid} found.")
        return None
    if rec.get("status"):
        print(f"{rec['title']} at {rec['company']} already has a card (#{rec.get('issue_number')}).")
        return rec
    cfg = scan.load_config("config.yml")
    prof = profile.load()
    problems: list[str] = []
    standard_card(rec, data, cfg, prof, problems)
    store.save(data, data_path)
    dashboard.write(data, readme_path)
    for p in problems:
        print(p)
    print(f"Moved {rec['title']} at {rec['company']} to Active as #{rec.get('issue_number')}.")
    return rec


def promote_many(ids: str) -> list:
    """Several IDs at once (the batch page sends them comma-separated)."""
    return [promote(i) for i in dict.fromkeys(x.strip() for x in ids.split(",") if x.strip())]


if __name__ == "__main__":
    telemetry.main("promote", promote_many, sys.argv[1])
