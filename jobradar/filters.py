"""Cheap, rule-based checks that run before any AI is used.

Only jobs that pass these (your roles, your work arrangement and areas,
pay not below your floor, recent enough) are sent to Claude for scoring.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from . import profile as profile_mod
from .sources import Job


def _has_phrase(text: str, phrase: str) -> bool:
    # Whole-word match, so "vp" doesn't match "mvp" and "uk" doesn't match "duke".
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase.lower())}(?![a-z0-9])", text) is not None


def title_ok(job: Job, cfg: dict, prof: dict) -> bool:
    title = job.title.lower()
    if not any(_has_phrase(title, p) for p in profile_mod.title_phrases(prof)):
        return False
    excludes = cfg.get("titles_exclude", []) + profile_mod.title_excludes(prof)
    return not any(_has_phrase(title, p) for p in excludes)


def _outside_us(where: str, cfg: dict) -> bool:
    return any(_has_phrase(where, m) for m in cfg.get("non_us_markers", []))


US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC", "washington d.c.": "DC", "washington dc": "DC",
}
_US_WIDE = re.compile(r"united states|\busa\b|\bu\.s\.?|\bus\b(?!-[a-z]{2}\b)|us-remote|north america|\bamer\b|americas|"
                      r"nationwide|anywhere", re.I)
_STATE_CODE = re.compile(r"(?:\bUS-|-\s|,\s|\(|^)(" + "|".join(sorted(set(US_STATES.values()))) + r")\b")
_REMOTE = re.compile(r"\bremote\b", re.I)
# Countries and regions a "remote" job can be tied to that aren't the US (matched as whole words).
_ELSEWHERE_NAMES = (
    "afghanistan albania algeria argentina armenia australia austria azerbaijan bahrain bangladesh belarus belgium "
    "bolivia bosnia brazil bulgaria cambodia cameroon canada chile china colombia croatia cyprus czech czechia "
    "denmark ecuador egypt estonia ethiopia finland france germany ghana greece guatemala honduras hungary iceland "
    "india indonesia iran iraq ireland israel italy jamaica japan jordan kazakhstan kenya korea kosovo kuwait latvia "
    "lebanon lithuania luxembourg malaysia malta mexico moldova mongolia montenegro morocco nepal netherlands "
    "nicaragua nigeria norway oman pakistan panama paraguay peru philippines poland portugal qatar romania russia "
    "serbia singapore slovakia slovenia spain sweden switzerland taiwan tanzania thailand tunisia turkey turkiye "
    "uganda ukraine uruguay uzbekistan venezuela vietnam zambia zimbabwe uae uk england scotland wales emea apac "
    "latam europe european asia africa nordics dach benelux anz"
).split() + ["costa rica", "dominican republic", "el salvador", "hong kong", "new zealand", "saudi arabia",
             "south africa", "sri lanka", "united arab emirates", "united kingdom", "middle east"]
_ELSEWHERE = re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(n) for n in _ELSEWHERE_NAMES)
                        + r")(?![a-z0-9])", re.I)
# Country codes as ATS boards write them: "BE-Belgium-Remote", "Remote - CA" is a state, so codes only at the start.
_ELSEWHERE_CODE = re.compile(r"^\s*(?:BE|CH|DE|FR|NL|GB|IE|IN|BR|MX|AU|SG|JP|ES|IT|PL|SE)-", re.I)


def _states_in(segment: str) -> set[str]:
    low = segment.lower()
    found = {code for name, code in US_STATES.items() if _has_phrase(low, name)}
    if "DC" in found:
        found.discard("WA")
    return found | set(_STATE_CODE.findall(segment))


def home_state(prof: dict) -> str | None:
    states = _states_in(str((prof.get("contact") or {}).get("city") or ""))
    return next(iter(states)) if len(states) == 1 else None


def remote_us_ok(location: str, prof: dict, cfg: dict) -> bool:
    """A remote job you can take from home: open US-wide, or open in your state.
    "Remote - Delaware, Maryland or Virginia" doesn't count when you live in Texas, and
    "Belgium (Remote)" never counts."""
    home = home_state(prof)
    segments = [x for x in re.split(r"[;/|]| or ", location) if x.strip()] or [location]
    for seg in segments:
        states = _states_in(seg)
        if states:
            if home is None or home in states:
                return True
            continue
        foreign = _ELSEWHERE.search(seg) or _ELSEWHERE_CODE.match(seg) or _outside_us(seg.lower(), cfg)
        if _US_WIDE.search(seg) and not (foreign and not re.search(r"united states|\busa\b", seg, re.I)):
            return True
        if _REMOTE.search(seg) and not foreign:
            return True
    return False


def location_ok(job: Job, cfg: dict, prof: dict) -> bool:
    wanted = set(prof.get("work_arrangements") or ["remote", "hybrid", "onsite"])
    where = job.location.lower()
    in_area = any(a.lower() in where for a in prof.get("areas") or [])

    if job.workplace == "remote":
        if "remote" not in wanted:
            return False
        return prof.get("remote_scope") == "anywhere" or remote_us_ok(job.location, prof, cfg)

    # Hybrid, on-site, or not stated (treated as in-office unless it says otherwise)
    kinds = {job.workplace} if job.workplace in ("hybrid", "onsite") else {"hybrid", "onsite"}
    if not kinds & wanted:
        return False
    if in_area:
        return True
    return bool(prof.get("open_to_relocation")) and not _outside_us(where, cfg)


def pay_ok(job: Job, prof: dict) -> bool:
    """Skip only when the posted range tops out below your minimum base."""
    floor = prof.get("min_base_salary")
    return not (floor and job.pay_max and job.pay_max < floor)


def recent_enough(job: Job, cfg: dict, now: datetime | None = None) -> bool:
    max_days = cfg.get("max_age_days")
    if not max_days or not job.posted:
        return True
    try:
        posted = datetime.fromisoformat(job.posted.replace("Z", "+00:00"))
    except ValueError:
        return True
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return now - posted <= timedelta(days=max_days)


def matches(job: Job, cfg: dict, prof: dict) -> bool:
    return (title_ok(job, cfg, prof) and location_ok(job, cfg, prof)
            and pay_ok(job, prof) and recent_enough(job, cfg))
