"""Outgoing webhook: post alerts into your Discord channel.

A Discord webhook is a secret web address. Sending it a POST request with
JSON makes a message appear in the channel, no bot or login needed.
"""
from __future__ import annotations

import json
import os
import time

import requests

GREEN, YELLOW, BLUE, GREY = 0x2ECC71, 0xF1C40F, 0x3498DB, 0x95A5A6


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def send(payload: dict, attachment: str | None = None) -> bool:
    """POST to the Discord webhook. Returns False (and prints) if not configured."""
    for embed in payload.get("embeds", []):  # Discord rejects empty links
        if not embed.get("url"):
            embed.pop("url", None)
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        print("[discord] DISCORD_WEBHOOK_URL not set; would have sent:", payload.get("embeds", [{}])[0].get("title"))
        return False
    for _ in range(3):
        try:
            if attachment and os.path.exists(attachment):
                with open(attachment, "rb") as f:  # webhook file upload: JSON goes in payload_json
                    resp = requests.post(url, data={"payload_json": json.dumps(payload)}, timeout=60,
                                         files={"files[0]": (os.path.basename(attachment), f, "application/pdf")})
            else:
                resp = requests.post(url, json=payload, timeout=30)
        except requests.RequestException as e:
            print(f"[discord] couldn't send: {e}")
            return False
        if resp.status_code == 429:  # Discord rate limit: wait as told, then retry
            time.sleep(float(resp.json().get("retry_after", 2)))
            continue
        if resp.status_code >= 400:
            print(f"[discord] webhook answered {resp.status_code}: {resp.text[:200]}")
            return False
        return True
    return False


def job_alert(job: dict, issue_url: str | None) -> dict:
    """Build the message for a new strong match."""
    s = job["score"]
    lines = [job.get("summary", "")]
    if job.get("strengths"):
        lines += ["", "**Why you fit**"] + [f"• {x}" for x in job["strengths"]]
    if job.get("gaps"):
        lines += ["", "**Watch out for**"] + [f"• {x}" for x in job["gaps"]]
    fields = [
        {"name": "Location", "value": _clip(job.get("location") or "Not listed", 1024), "inline": True},
        {"name": "Posted", "value": (job.get("posted") or "Unknown")[:10], "inline": True},
        {"name": "Pay", "value": _clip(job.get("pay_text") or "Not posted", 1024), "inline": True},
    ]
    snap = job.get("company_snapshot") or {}
    said = " · ".join(x for x in [snap.get("rating"), snap.get("ceo") and f"CEO: {snap['ceo']}"] if x)
    if said:
        fields.append({"name": "Employees say", "value": _clip(said, 1024), "inline": False})
    if snap.get("red_flags"):
        fields.append({"name": "⚠️ Worth asking about", "value": _clip("\n".join(snap["red_flags"][:2]), 1024),
                       "inline": False})
    if issue_url:
        fields.append({"name": "Tracker card", "value": f"[Open card: application kit, bullets, call script]({issue_url})", "inline": False})
    return {
        "username": "Job Radar",
        "embeds": [{
            "title": _clip(f"{s}/10 · {job['title']} at {job['company']}", 256),
            "url": job.get("url") or None,
            "description": _clip("\n".join(lines), 4000),
            "color": GREEN if s >= 8 else YELLOW,
            "fields": fields,
        }],
    }


def status_update(job: dict, old: str, new: str, issue_url: str | None) -> dict:
    return {
        "username": "Job Radar",
        "embeds": [{
            "title": _clip(f"{job['title']} at {job['company']}: {old} → {new}", 256),
            "url": issue_url or job.get("url") or None,
            "color": BLUE,
        }],
    }


def run_summary(new_matches: int, scored: int, seen: int, problems: list[str], dashboard_url: str | None) -> dict:
    desc = f"Checked {seen} matching jobs, scored {scored} new ones, found {new_matches} strong matches."
    if dashboard_url:
        desc += f"\n[Open dashboard]({dashboard_url})"
    if problems:
        desc += "\n\n**Needs attention**\n" + "\n".join(f"• {p}" for p in problems[:10])
    return {"username": "Job Radar",
            "embeds": [{"title": "Daily scan finished", "description": _clip(desc, 4000), "color": GREY}]}
