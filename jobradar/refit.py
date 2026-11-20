"""Keep active cards in step with you. When your resume or preferences change, every active card
(not ones you've applied to: those are frozen) goes back to Claude for a fresh score. If the new
score earns a tailored resume (7+, or 6+ when the job asks for experience that's only in
profile.yml's extra_experience) and the card doesn't have one, Claude writes it and the card is
rebuilt in place. Either way the card's title and score are updated.
"""
from __future__ import annotations

from datetime import datetime, timezone

ACTIVE = {"new"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def eligible(rec: dict) -> bool:
    return bool(rec.get("issue_number")) and rec.get("status") in ACTIVE and not rec.get("resume_frozen")


def queue(data: dict, reason: str) -> int:
    """Send every active card back for a fresh score. The next scan adds each one's posting."""
    n = 0
    for rec in data["jobs"].values():
        if eligible(rec):
            rec["rescore_pending"] = {"reason": reason, "at": _now()}
            n += 1
    return n


def apply_rescore(rec: dict, alert_min: int, card_min: int) -> None:
    """Called after a fresh score lands on an active card."""
    rec.pop("rescore_pending", None)
    rec["card_refresh"] = True
    wants_tailored = rec["score"] >= alert_min or (rec.get("uses_extra_experience") and rec["score"] >= card_min)
    if wants_tailored and not (rec.get("tailored_resume") or {}).get("pdf"):
        rec["retailor"] = True
        rec["kit_pending"] = _now()
    else:
        rec.pop("pending_brief", None)


def retailor_card(rec: dict, data: dict, kit: dict, resume: str, ai_cfg: dict, prof: dict, problems: list,
                  base_docx, out_root) -> None:
    """Give an existing active card its tailored resume and kit, keeping its status and history."""
    from . import github_api, lastmile, scan
    status, history = rec.get("status"), list(rec.get("history", []))
    scan.make_card(lastmile.job_from_record(rec), rec, resume, ai_cfg, prof, problems, data, base_docx,
                   out_root, kit=kit, use_ai=False)
    rec["status"], rec["history"] = status, history + [{"status": status, "at": _now(), "note": "tailored resume added"}]
    rec.pop("standard_resume", None)
    for k in ("retailor", "kit_pending", "pending_brief", "questions", "force_kit"):
        rec.pop(k, None)
    rec["card_refresh"] = True


def refresh_changed(data: dict, prof: dict, problems: list) -> int:
    """Rewrite (title included) every card whose score or resume just changed."""
    from . import github_api
    n = 0
    for rec in data["jobs"].values():
        if not rec.get("card_refresh"):
            continue
        if github_api.enabled():
            try:
                github_api.refresh_card(rec["issue_number"], rec, prof, retitle=True)
            except Exception as e:  # noqa: BLE001 - try again next run
                problems.append(f"Card #{rec['issue_number']} not refreshed ({e.__class__.__name__}).")
                continue
        rec.pop("card_refresh", None)
        n += 1
    return n


def main() -> None:
    """python -m jobradar.refit: queue every active card for a fresh score (after a manual edit)."""
    from . import store
    data = store.load()
    print("queued for a fresh score:", queue(data, "resume or preferences changed"))
    store.save(data)


if __name__ == "__main__":
    main()
