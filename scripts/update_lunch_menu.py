"""Fetch LINQ Connect lunch entrees and write an ICS calendar.

Pulls a rolling window of menu data (past week through 4 weeks out) and
writes lunch-menu.ics with one all-day event per school day listing the
Main Entree options. Designed to run daily from GitHub Actions or cron;
output is deterministic so git only sees a diff when the menu changes.

Environment variables (all optional, defaults are Alpine SD):
    LINQ_IDENTIFIER   district code            default TFCNC9
    LINQ_DISTRICT_ID  district GUID            default Alpine
    LINQ_BUILDING_ID  school building GUID     default from capture
    ICS_PATH          output path              default lunch-menu.ics
    CAL_NAME          calendar display name    default "School Lunch"
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timedelta

import requests

BASE_URL = "https://api.linqconnect.com/api"

DISTRICT_ID = os.environ.get("LINQ_DISTRICT_ID", "a83d5cd9-a7a8-ed11-8e69-da0395d724bd")
BUILDING_ID = os.environ.get("LINQ_BUILDING_ID", "da12ddae-57ad-ed11-8e6a-9bfa3b2b51d1")
ICS_PATH = os.environ.get("ICS_PATH", "RME-Lunch.ics")
CAL_NAME = os.environ.get("CAL_NAME", "Rocky Mountain Lunch (RME)")

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://linqconnect.com",
    "Referer": "https://linqconnect.com/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15"
    ),
}


def fmt_date(d: date) -> str:
    return f"{d.month}-{d.day}-{d.year}"


def fetch_menu(start: date, end: date) -> dict:
    resp = requests.get(
        f"{BASE_URL}/FamilyMenu",
        params={
            "buildingId": BUILDING_ID,
            "districtId": DISTRICT_ID,
            "startDate": fmt_date(start),
            "endDate": fmt_date(end),
        },
        headers={**HEADERS, "Linq-Nutrition-Url": str(uuid.uuid4())},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def extract_lunch_entrees(menu: dict) -> dict[date, list[str]]:
    """{date: [entree names]} for the Lunch session, Main Entree category."""
    out: dict[date, list[str]] = {}
    for sess in menu.get("FamilyMenuSessions", []):
        if sess.get("ServingSession", "").strip().lower() != "lunch":
            continue
        for plan in sess.get("MenuPlans", []):
            for day in plan.get("Days", []):
                try:
                    d = datetime.strptime(day["Date"], "%m/%d/%Y").date()
                except (KeyError, ValueError):
                    continue
                names: list[str] = []
                for meal in day.get("MenuMeals", []):
                    for cat in meal.get("RecipeCategories", []):
                        if "entree" not in cat.get("CategoryName", "").lower():
                            continue
                        for r in cat.get("Recipes", []):
                            name = (r.get("RecipeName") or "").strip()
                            if name and name not in names:
                                names.append(name)
                if names:
                    out.setdefault(d, [])
                    for n in names:
                        if n not in out[d]:
                            out[d].append(n)
    return out


def ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """RFC 5545 line folding at 75 octets."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    parts = []
    while encoded:
        chunk = encoded[:75]
        # don't split mid multi-byte char
        while True:
            try:
                parts.append(chunk.decode("utf-8"))
                break
            except UnicodeDecodeError:
                chunk = chunk[:-1]
        encoded = encoded[len(chunk):]
    return "\r\n ".join(parts)


def build_ics(entrees_by_day: dict[date, list[str]]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Matt//Rocky Mountain Lunch//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(CAL_NAME)}",
        "X-WR-TIMEZONE:America/Denver",
    ]
    for d in sorted(entrees_by_day):
        names = entrees_by_day[d]
        summary = "| ".join(names)
        description = "See LINQ Connect for sides, fruit, milk, and condiments."
        dtstart = d.strftime("%Y%m%d")
        dtend = (d + timedelta(days=1)).strftime("%Y%m%d")
        lines += [
            "BEGIN:VEVENT",
            # Matches the hand-built file's UID scheme so existing
            # subscribers get in-place updates, not duplicates.
            f"UID:{dtstart}-rme-lunch@linq",
            # Fixed DTSTAMP keeps output deterministic; only menu changes diff.
            f"DTSTAMP:{dtstart}T000000Z",
            f"DTSTART;VALUE=DATE:{dtstart}",
            f"DTEND;VALUE=DATE:{dtend}",
            f"SUMMARY:{ics_escape(summary)}",
            f"DESCRIPTION:{ics_escape(description)}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def main() -> int:
    today = date.today()
    start = today - timedelta(days=7)
    end = today + timedelta(days=35)

    # Fetch in one-week chunks; matches app behavior and keeps payloads sane.
    entrees: dict[date, list[str]] = {}
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=6), end)
        try:
            menu = fetch_menu(chunk_start, chunk_end)
        except requests.HTTPError as exc:
            print(f"WARN: fetch {chunk_start}..{chunk_end} failed: {exc}", file=sys.stderr)
            chunk_start = chunk_end + timedelta(days=1)
            continue
        entrees.update(extract_lunch_entrees(menu))
        chunk_start = chunk_end + timedelta(days=1)

    if not entrees:
        print("ERROR: no lunch entrees found in window; leaving ICS untouched", file=sys.stderr)
        return 1

    ics = build_ics(entrees)
    old = ""
    if os.path.exists(ICS_PATH):
        with open(ICS_PATH, "r", encoding="utf-8", newline="") as f:
            old = f.read()
    if ics == old:
        print("No menu changes.")
        return 0
    with open(ICS_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(ics)
    print(f"Wrote {ICS_PATH}: {len(entrees)} days, {sum(len(v) for v in entrees.values())} entrees.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
